"""Wire messages (protocol v3): one dataclass per type, generic pack/unpack
driven by a field table, so the layout lives in exactly one place.
docs/protocol.md §4 is GENERATED from this module (`--emit-md`) and
docs/protocol_vectors.json from `--emit-vectors`; tests/test_protocol.py
fails if either is stale.

    python -m orbit.protocol.messages --emit-vectors docs/protocol_vectors.json
    python -m orbit.protocol.messages --emit-md docs/protocol.md
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, fields, asdict
from typing import ClassVar

from .crc16 import crc16
from . import cobs

# field kind -> (size in bytes, signed, count)
KINDS = {
    "u8": (1, False, 1), "u16": (2, False, 1), "u32": (4, False, 1),
    "i16": (2, True, 1), "i32": (4, True, 1),
    "u8x128": (1, False, 128),
}

ORCH, NODE, BOTH = "O→N", "N→O", "both"


class ProtocolError(ValueError):
    pass


@dataclass
class Message:
    TYPE: ClassVar[int]
    DIR: ClassVar[str]
    FIELDS: ClassVar[list[tuple[str, str]]]
    NOTES: ClassVar[dict[str, str]] = {}

    @classmethod
    def size(cls) -> int:
        return sum(KINDS[k][0] * KINDS[k][2] for _, k in cls.FIELDS)

    def pack(self) -> bytes:
        out = bytearray()
        for name, kind in self.FIELDS:
            size, signed, count = KINDS[kind]
            val = getattr(self, name)
            vals = val if count > 1 else (val,)
            if len(vals) != count:
                raise ProtocolError(f"{type(self).__name__}.{name}: need {count} values")
            for v in vals:
                try:
                    out += int(v).to_bytes(size, "little", signed=signed)
                except OverflowError as e:
                    raise ProtocolError(f"{type(self).__name__}.{name}={v} does not fit {kind}") from e
        return bytes(out)

    @classmethod
    def unpack(cls, payload: bytes):
        if len(payload) != cls.size():
            raise ProtocolError(f"{cls.__name__}: payload {len(payload)} != {cls.size()}")
        kw, off = {}, 0
        for name, kind in cls.FIELDS:
            size, signed, count = KINDS[kind]
            vals = []
            for _ in range(count):
                vals.append(int.from_bytes(payload[off:off + size], "little", signed=signed))
                off += size
            kw[name] = tuple(vals) if count > 1 else vals[0]
        return cls(**kw)


class _RowMessage(Message):
    """frame_id, row index, one 128-pixel row. 128 messages carry a frame; row 127 triggers
    the action (score, or mark the reference loaded). Rows may arrive in any order; a
    missing row leaves the previous contents of that buffer row in place."""
    FIELDS = [("frame_id", "u16"), ("row", "u8"), ("pixels", "u8x128")]
    NOTES = {"frame_id": "ground-assigned id; latched from every row, used when row 127 arrives",
             "row": "0..FRAME_H−1; ≥ FRAME_H is rejected (`busy_drops++`)",
             "pixels": "8-bit grayscale, x = 0..127 left to right"}

    def __post_init__(self):
        self.pixels = tuple(self.pixels)


@dataclass
class FrameIngest(_RowMessage):
    """One row of a captured frame into the frame buffer. Stands in for the camera."""
    TYPE = 0x01
    DIR = ORCH
    frame_id: int
    row: int
    pixels: tuple


@dataclass
class ConfigSet(Message):
    """Scoring weights, thresholds and queue limit. Rejected as a whole if any field is out
    of range (STATUS `CONFIG_VALID` cleared, old config kept). Does not clear the queue:
    send it before ingesting. Node answers with STATUS_REPLY."""
    TYPE = 0x02
    DIR = ORCH
    FIELDS = [("w_clear", "u16"), ("w_sharp", "u16"), ("w_change", "u16"), ("cloud_thr", "u8"),
              ("change_thr", "u8"), ("sharp_shift", "u8"), ("queue_limit", "u8")]
    NOTES = {"w_clear": "score = sat16((w_clear·clear + w_sharp·sharp + w_change·change) >> 16)",
             "w_sharp": "", "w_change": "",
             "cloud_thr": "pixel > cloud_thr counts as cloud",
             "change_thr": "abs(pixel − ref) > change_thr counts as changed",
             "sharp_shift": "0..SHARP_SHIFT_MAX; sharp = sat16(sobel_sum >> sharp_shift)",
             "queue_limit": "1..QUEUE_DEPTH cells in use"}
    w_clear: int
    w_sharp: int
    w_change: int
    cloud_thr: int
    change_thr: int
    sharp_shift: int
    queue_limit: int


@dataclass
class RefFrameSet(_RowMessage):
    """One row of the change-detection reference into the reference buffer. Row 127 sets
    STATUS `REF_LOADED`. The buffer is zero at power-on, so scores before the first
    reference count nearly every pixel as changed; the ground always sends one first."""
    TYPE = 0x03
    DIR = ORCH
    frame_id: int
    row: int
    pixels: tuple


@dataclass
class BenchRun(Message):
    """Score the resident frame buffer `iterations` times without touching the queue or
    emitting FRAME_SCORED, then BENCH_DONE. This is how energy per frame is measured with
    the UART idle: the kernel is fixed-function, so work per frame does not depend on
    content. Node is BUSY meanwhile (rows and grants → `busy_drops++`)."""
    TYPE = 0x04
    DIR = ORCH
    FIELDS = [("iterations", "u32")]
    NOTES = {"iterations": "0 → immediate BENCH_DONE(0, 0)"}
    iterations: int


@dataclass
class StatusQuery(Message):
    """Empty payload. Node answers with STATUS_REPLY."""
    TYPE = 0x10
    DIR = ORCH
    FIELDS = []


@dataclass
class StatusReply(Message):
    """Sent on STATUS_QUERY, after CONFIG_SET, and with every node HEARTBEAT."""
    TYPE = 0x11
    DIR = NODE
    FIELDS = [("node_id", "u8"), ("state", "u8"), ("flags", "u8"), ("queue_depth", "u8"),
              ("top_score", "u16"), ("top_frame_id", "u16"), ("frames_scored", "u16"),
              ("frames_evicted", "u16"), ("frames_sent", "u16"), ("rows_rx", "u16"), ("busy_drops", "u16"),
              ("crc_errors", "u16"), ("len_errors", "u16"), ("unknown_type", "u16"), ("rx_overflow", "u16"),
              ("cycles_last_frame", "u32")]
    NOTES = {"node_id": "", "state": "0 IDLE, 1 SCORING, 2 BENCH",
             "flags": "bit0 HAS_DATA, bit1 LINK_OK, bit2 CONFIG_VALID, bit3 REF_LOADED, bit4 BUSY, "
                      "bit5 TX_STALLED, bit6 INA219_PRESENT",
             "queue_depth": "entries in the queue", "top_score": "head of queue; 0 when empty",
             "top_frame_id": "0xFFFF when empty", "frames_scored": "wraps", "frames_evicted": "wraps",
             "frames_sent": "wraps", "rows_rx": "FRAME_INGEST + REF_FRAME_SET rows accepted; wraps",
             "busy_drops": "rows/grants/bench dropped while BUSY, bad rows, CONFIG_SET rejects",
             "crc_errors": "§3", "len_errors": "§3", "unknown_type": "§3", "rx_overflow": "§3",
             "cycles_last_frame": "kernel clocks for the last scoring pass; golden model reports 0"}
    node_id: int
    state: int
    flags: int
    queue_depth: int
    top_score: int
    top_frame_id: int
    frames_scored: int
    frames_evicted: int
    frames_sent: int
    rows_rx: int
    busy_drops: int
    crc_errors: int
    len_errors: int
    unknown_type: int
    rx_overflow: int
    cycles_last_frame: int
    IDLE: ClassVar[int] = 0
    SCORING: ClassVar[int] = 1
    BENCH: ClassVar[int] = 2
    F_HAS_DATA: ClassVar[int] = 1 << 0
    F_LINK_OK: ClassVar[int] = 1 << 1
    F_CONFIG_VALID: ClassVar[int] = 1 << 2
    F_REF_LOADED: ClassVar[int] = 1 << 3
    F_BUSY: ClassVar[int] = 1 << 4
    F_TX_STALLED: ClassVar[int] = 1 << 5
    F_INA219_PRESENT: ClassVar[int] = 1 << 6
    NO_FRAME: ClassVar[int] = 0xFFFF


@dataclass
class FrameScored(Message):
    """Emitted once per scored frame (row 127 of FRAME_INGEST), after the queue insert.
    Carries the three component metrics so the ground can show them and check the
    board against the golden model frame by frame."""
    TYPE = 0x12
    DIR = NODE
    FIELDS = [("node_id", "u8"), ("frame_id", "u16"), ("clear", "u16"), ("sharp", "u16"), ("change", "u16"),
              ("score", "u16"), ("evicted_id", "u16"), ("queue_depth", "u8")]
    NOTES = {"node_id": "", "frame_id": "", "clear": "sat16((FRAME_BYTES − cloud_pixels) << 2)",
             "sharp": "sat16(sobel_sum >> sharp_shift)", "change": "sat16(changed_pixels << 2)",
             "score": "composite, §5.2", "evicted_id": "frame lost by this insert (tail, or this frame), else 0xFFFF",
             "queue_depth": "after the insert"}
    node_id: int
    frame_id: int
    clear: int
    sharp: int
    change: int
    score: int
    evicted_id: int
    queue_depth: int


@dataclass
class BenchDone(Message):
    """Answer to BENCH_RUN."""
    TYPE = 0x13
    DIR = NODE
    FIELDS = [("node_id", "u8"), ("iterations", "u32"), ("cycles", "u32")]
    NOTES = {"node_id": "", "iterations": "as requested", "cycles": "total kernel clocks; golden model reports 0"}
    node_id: int
    iterations: int
    cycles: int


@dataclass
class Grant(Message):
    """One downlink slot. If the node has data and FRAME_BYTES ≤ budget_bytes it pops its
    head and answers TX_FRAME then TX_DONE; otherwise TX_DONE alone with bytes_consumed 0."""
    TYPE = 0x20
    DIR = ORCH
    FIELDS = [("slot_id", "u16"), ("budget_bytes", "u32")]
    NOTES = {"slot_id": "echoed in TX_FRAME / TX_DONE", "budget_bytes": "remaining window capacity"}
    slot_id: int
    budget_bytes: int


@dataclass
class TxFrame(Message):
    """Accounting record of a transmitted frame. No pixels cross this link: the ground holds
    the corpus and debits `byte_count` from the modelled window (§6)."""
    TYPE = 0x21
    DIR = NODE
    FIELDS = [("node_id", "u8"), ("slot_id", "u16"), ("frame_id", "u16"), ("score", "u16"), ("byte_count", "u32")]
    NOTES = {"node_id": "", "slot_id": "", "frame_id": "popped head", "score": "its score",
             "byte_count": "FRAME_BYTES"}
    node_id: int
    slot_id: int
    frame_id: int
    score: int
    byte_count: int


@dataclass
class TxDone(Message):
    """Ends every GRANT."""
    TYPE = 0x22
    DIR = NODE
    FIELDS = [("node_id", "u8"), ("slot_id", "u16"), ("bytes_consumed", "u32"), ("new_top_score", "u16"),
              ("flags", "u8")]
    NOTES = {"node_id": "", "slot_id": "", "bytes_consumed": "FRAME_BYTES or 0",
             "new_top_score": "head after the pop; 0 when empty", "flags": "bit0 HAS_DATA"}
    node_id: int
    slot_id: int
    bytes_consumed: int
    new_top_score: int
    flags: int
    F_HAS_DATA: ClassVar[int] = 1 << 0


@dataclass
class Heartbeat(Message):
    """Every HEARTBEAT_MS in both directions. The node follows its own with STATUS_REPLY and
    clears LINK_OK after LINK_TIMEOUT_MS without one from the ground."""
    TYPE = 0x40
    DIR = BOTH
    FIELDS = [("sender", "u8"), ("seq", "u16"), ("uptime_ms", "u32")]
    NOTES = {"sender": "node id, or ORCH_ID", "seq": "wraps", "uptime_ms": "wraps"}
    sender: int
    seq: int
    uptime_ms: int


@dataclass
class Power(Message):
    """INA219 telemetry every POWER_PERIOD_MS; raw registers, converted on the ground
    (orbit/bench/ina219.py). `flags` bit0 VALID is 0 when no INA219 answered."""
    TYPE = 0x41
    DIR = NODE
    FIELDS = [("node_id", "u8"), ("flags", "u8"), ("bus_raw", "u16"), ("shunt_raw", "i16"), ("uptime_ms", "u32")]
    NOTES = {"node_id": "", "flags": "bit0 VALID", "bus_raw": "INA219 bus register",
             "shunt_raw": "INA219 shunt register", "uptime_ms": ""}
    node_id: int
    flags: int
    bus_raw: int
    shunt_raw: int
    uptime_ms: int
    VALID: ClassVar[int] = 0x01


# 0x30 RELAY_CMD / 0x31 RELAY_DATA are reserved for the relay stretch goal and not implemented:
# they decode as `unknown_type` on both ends until they exist.
MESSAGE_TYPES: dict[int, type[Message]] = {
    c.TYPE: c for c in (FrameIngest, ConfigSet, RefFrameSet, BenchRun, StatusQuery, StatusReply, FrameScored,
                        BenchDone, Grant, TxFrame, TxDone, Heartbeat, Power)
}
MAX_PAYLOAD = max(c.size() for c in MESSAGE_TYPES.values())
MAX_PAYLOAD_TO_ORCH = max(c.size() for c in MESSAGE_TYPES.values() if c.DIR != ORCH)
assert MAX_PAYLOAD + 3 <= 253, "COBS single-overhead-byte invariant broken"
assert MAX_PAYLOAD_TO_ORCH + 3 <= 63, "framer_tx index width (IW=6) caps N->O frames at 63 bytes pre-COBS"


# --- frame codec ---------------------------------------------------------------

def frame_body(msg: Message) -> bytes:
    """type || payload || crc16 LE  (pre-COBS)."""
    body = bytes([msg.TYPE]) + msg.pack()
    return body + crc16(body).to_bytes(2, "little")


def encode(msg: Message) -> bytes:
    """Bytes as they appear on the wire, delimiter included."""
    return cobs.encode(frame_body(msg)) + b"\x00"


class FrameDecoder:
    """Byte-stream -> messages, implementing the error table in protocol.md §3.
    Counters are u16 and wrap, same as the node's STATUS fields."""

    COUNTERS = ("crc_errors", "len_errors", "unknown_type", "rx_overflow")

    def __init__(self, max_frame: int = 253):
        self.buf = bytearray()
        self.max_frame = max_frame
        self.overflow = False
        for c in self.COUNTERS:
            setattr(self, c, 0)

    def _bump(self, name):
        setattr(self, name, (getattr(self, name) + 1) & 0xFFFF)

    def feed(self, data: bytes) -> list[Message]:
        out = []
        for b in data:
            if b == 0:
                m = self._finish()
                if m is not None:
                    out.append(m)
            elif self.overflow:
                continue
            elif len(self.buf) >= self.max_frame + 1:
                self.overflow = True
                self._bump("rx_overflow")
            else:
                self.buf.append(b)
        return out

    def _finish(self) -> Message | None:
        raw, self.buf = bytes(self.buf), bytearray()
        if self.overflow:
            self.overflow = False
            return None
        try:
            frame = cobs.decode(raw)
        except cobs.CobsError:
            self._bump("len_errors")
            return None
        if len(frame) < 3:
            return None  # empty/partial frame after link open or desync: silent
        body, crc = frame[:-2], int.from_bytes(frame[-2:], "little")
        if crc16(body) != crc:
            self._bump("crc_errors")
            return None
        cls = MESSAGE_TYPES.get(body[0])
        if cls is None:
            self._bump("unknown_type")
            return None
        if len(body) - 1 != cls.size():
            self._bump("len_errors")
            return None
        return cls.unpack(body[1:])


# --- worked examples = conformance vectors ------------------------------------
_ROW = tuple((2 * i) & 0xFF for i in range(128))       # starts with 0x00: exercises COBS inside a row
EXAMPLES: list[tuple[str, Message]] = [
    ("FRAME_INGEST", FrameIngest(frame_id=7, row=127, pixels=_ROW)),
    ("CONFIG_SET", ConfigSet(w_clear=21845, w_sharp=21845, w_change=21845, cloud_thr=200, change_thr=16,
                             sharp_shift=6, queue_limit=32)),
    ("REF_FRAME_SET", RefFrameSet(frame_id=0, row=0, pixels=tuple(range(128))[::-1])),
    ("BENCH_RUN", BenchRun(iterations=1000)),
    ("STATUS_QUERY", StatusQuery()),
    ("STATUS_REPLY", StatusReply(node_id=0, state=0, flags=0b0001111, queue_depth=3, top_score=51234,
                                 top_frame_id=7, frames_scored=12, frames_evicted=0, frames_sent=9, rows_rx=1664,
                                 busy_drops=0, crc_errors=0, len_errors=0, unknown_type=0, rx_overflow=0,
                                 cycles_last_frame=2051)),
    ("FRAME_SCORED", FrameScored(node_id=0, frame_id=7, clear=61440, sharp=30000, change=8192, score=33210,
                                 evicted_id=0xFFFF, queue_depth=3)),
    ("BENCH_DONE", BenchDone(node_id=0, iterations=1000, cycles=2051000)),
    ("GRANT", Grant(slot_id=42, budget_bytes=4_194_304)),
    ("TX_FRAME", TxFrame(node_id=0, slot_id=42, frame_id=7, score=51234, byte_count=16384)),
    ("TX_DONE", TxDone(node_id=0, slot_id=42, bytes_consumed=16384, new_top_score=48001, flags=1)),
    ("HEARTBEAT", Heartbeat(sender=0xFF, seq=42, uptime_ms=123456)),
    ("POWER", Power(node_id=0, flags=1, bus_raw=0x2648, shunt_raw=4210, uptime_ms=5000)),
]


def vectors() -> list[dict]:
    out = []
    for name, m in EXAMPLES:
        body = bytes([m.TYPE]) + m.pack()
        pre = frame_body(m)
        out.append({
            "name": name, "type": m.TYPE, "fields": asdict(m),
            "payload_hex": m.pack().hex(" "), "crc": crc16(body),
            "precobs_hex": pre.hex(" "), "wire_hex": encode(m).hex(" "),
        })
    return out


def spec_markdown() -> str:
    """docs/protocol.md §4: size table, then one subsection per message with the field
    layout and the worked hex example. Kept in sync by tests/test_protocol.py."""
    by_type = {m.TYPE: (name, m) for name, m in EXAMPLES}
    out = ["| ID | Name | Dir | Payload | Pre-COBS | Wire |", "|---|---|---|---|---|---|"]
    for t, cls in sorted(MESSAGE_TYPES.items()):
        n = cls.size()
        out.append(f"| 0x{t:02X} | {by_type[t][0]} | {cls.DIR} | {n} | {n + 3} | {n + 5} |")
    out.append("")
    out.append("Types: `u8/u16/u32`, `i16`, `u8[128]` (128 bytes). Reserved, not implemented: 0x30 RELAY_CMD, "
               "0x31 RELAY_DATA (stretch goal; decode as `unknown_type`).")
    out.append("")
    for i, (t, cls) in enumerate(sorted(MESSAGE_TYPES.items()), 1):
        name, m = by_type[t]
        out.append(f"### 4.{i} 0x{t:02X} {name}")
        doc = " ".join((cls.__doc__ or "").split())
        if doc:
            out.append(doc)
        if cls.FIELDS:
            out += ["", "| off | size | type | field | notes |", "|---|---|---|---|---|"]
            off = 0
            for fname, kind in cls.FIELDS:
                size, _, count = KINDS[kind]
                typ = f"u8[{count}]" if count > 1 else kind
                out.append(f"| {off} | {size * count} | {typ} | {fname} | {cls.NOTES.get(fname, '')} |")
                off += size * count
        else:
            out += ["", "No payload."]
        ex = ", ".join(f"{k}={v if not isinstance(v, tuple) else '…'}" for k, v in asdict(m).items())
        out += ["", f"Example — {ex}:" if ex else "Example:", "```",
                "payload : " + (m.pack().hex(" ") if m.size() else "(empty)"),
                f"crc     : 0x{crc16(bytes([m.TYPE]) + m.pack()):04X} → " + crc16(bytes([m.TYPE]) + m.pack()).to_bytes(2, 'little').hex(' '),
                "wire    : " + encode(m).hex(" "), "```", ""]
    return "\n".join(out)


BEGIN, END = "<!-- generated: messages begin -->", "<!-- generated: messages end -->"


def update_spec(path) -> str:
    text = open(path).read()
    new = f"{BEGIN}\n{spec_markdown()}\n{END}"
    if BEGIN not in text or END not in text:
        raise SystemExit(f"{path}: markers {BEGIN!r} / {END!r} not found")
    return re.sub(re.escape(BEGIN) + ".*?" + re.escape(END), lambda _: new, text, flags=re.S)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--emit-vectors":
        with open(sys.argv[2], "w") as f:
            json.dump(vectors(), f, indent=1)
        print(f"wrote {len(EXAMPLES)} vectors to {sys.argv[2]}")
    elif len(sys.argv) == 3 and sys.argv[1] == "--emit-md":
        text = update_spec(sys.argv[2])          # read fully before truncating
        with open(sys.argv[2], "w") as f:
            f.write(text)
        print(f"updated §4 in {sys.argv[2]}")
    else:
        for v in vectors():
            print(f"== {v['name']} 0x{v['type']:02X} payload={len(v['payload_hex'].split())}B")
            print("  payload :", v["payload_hex"])
            print(f"  crc     : 0x{v['crc']:04X}")
            print("  wire    :", v["wire_hex"])
