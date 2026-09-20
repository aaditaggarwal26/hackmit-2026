"""Orbit wire protocol v1: JSON objects, one per UDP datagram, on one multicast group.

Every node — ground, each satellite, and any listener — hears every datagram and
filters locally. That is why every message carries its sender's hostname and a
per-sender sequence number: peers can watch each other (the basis for relay later),
and duplicated or reordered datagrams are recognised without any connection state.

Envelope (every message)::

    {"v": 1, "type": "<name>", "from": "<hostname>", "seq": <int>, "t_ms": <int>, ...body}

``t_ms`` is the *sender's* uptime in milliseconds. Satellites have no wall clock, and
nothing here needs clocks to agree: ages are computed by whoever owns the timer and
sent as durations.

Why JSON: the ESP32 has ArduinoJson, a human can read the bus log during a demo, and
the field set is small. Frame data is the only bulk payload and rides in ``tx_chunk``
as base64, one chunk per datagram.

Decoding never raises on hostile input. ``decode()`` returns either a ``Message`` or
a ``DecodeError`` describing why the bytes were rejected; the bus counts the latter
and moves on. A malformed datagram must not be able to stop the arbiter.

Required vs optional fields: a field declared *without* a default is REQUIRED and its
absence rejects the whole datagram. A field declared *with* a default (in practice
``X | None = None``) is OPTIONAL: an absent value means "this sender cannot report it",
not "malformed". Optional fields are also omitted from ``encode()`` when they are
``None``, so "unknown" never goes on the wire dressed up as data. Health telemetry is
the reason: RSSI, free heap and PSRAM presence exist only on real hardware, the
simulator cannot produce them, and that set will keep growing as the firmware learns to
report more about itself. Making each such field a new *message* type would put a
schema change (and a ground-station release) behind every new gauge; making them
optional heartbeat fields does not. Every field that existed before this rule stays
strict — an old sender that omits ``frames_scored`` is still rejected — because those
fields are inputs the ground reasons with, not gauges it displays.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import types
from dataclasses import MISSING, dataclass, fields, is_dataclass
from enum import StrEnum
from typing import Any, ClassVar, Self, TypeVar, Union, get_args, get_origin, get_type_hints

from orbit import config

T = TypeVar("T", bound="Message")


class MessageType(StrEnum):
    # ground → all
    OFFERS_OPEN = "offers_open"  # READY: bids for this round are being collected
    GRANT = "grant"  # exactly one satellite may transmit exactly one item
    REVOKE = "revoke"  # the grant holder went quiet; slot taken back
    TX_ACK = "tx_ack"  # transmission confirmed (or not) — the satellite pops only on ok=true
    STATE = "state"  # FSM state + window budget, on every transition and periodically
    # satellite → all
    BID = "bid"  # top item + diagnostic window + buffer telemetry
    TX_BEGIN = "tx_begin"
    TX_CHUNK = "tx_chunk"
    TX_DONE = "tx_done"
    HEARTBEAT = "heartbeat"  # buffer/queue state while no round is open
    EVICTION = "eviction"  # a frame was lost to onboard storage limits (never to arbitration)
    SCORED = "scored"  # a frame was scored onboard: score parts + cloud fraction, whether it was kept
    FAULT = "fault"  # an edge-triggered onboard fault, identified by a registry code


class DecodeError(Exception):
    """Why a datagram was rejected. Carries no stack: these are counted, not raised."""

    def __init__(self, reason: str, raw_type: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw_type = raw_type


# --- nested records -------------------------------------------------------------------


@dataclass(frozen=True)
class QueueEntry:
    """One entry of a satellite's diagnostic window. Never read by arbitration."""

    item_id: int
    score: float
    item_age_s: float


@dataclass(frozen=True)
class BufferStats:
    """A satellite's own account of its fixed frame pool."""

    slots: int  # capacity, slots
    capacity_bytes: int  # capacity, bytes
    used: int  # occupied slots
    free: int
    occupancy_pct: float


@dataclass(frozen=True)
class Breakdown:
    """The priority calculation, itemised. ``total`` is what the winner was chosen on.

    Only ``score`` (the bid's top item) and the two aging terms enter ``total``. The
    bid's window is not here because it is not an input.
    """

    score: float
    item_age_s: float
    item_age_term: float  # item_age_s × ITEM_AGING_RATE
    sat_wait_s: float
    sat_wait_term: float  # sat_wait_s × SAT_AGING_RATE
    total: float


@dataclass(frozen=True)
class WindowStatus:
    capacity_bytes: int
    used_bytes: int
    remaining_bytes: int
    slots_remaining: int


# --- messages -------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """Envelope fields. Subclasses add their body and set ``TYPE``."""

    TYPE: ClassVar[MessageType]

    sender: str
    seq: int
    t_ms: int

    def encode(self) -> bytes:
        doc: dict[str, Any] = {
            "v": config.PROTOCOL_VERSION,
            "type": str(self.TYPE),
            "from": self.sender,
            "seq": self.seq,
            "t_ms": self.t_ms,
        }
        for f in fields(self):
            if f.name in ("sender", "seq", "t_ms"):
                continue
            v = getattr(self, f.name)
            if v is None:
                continue  # an optional field this sender cannot report: absent, not null
            doc[f.name] = _to_json(v)
        return json.dumps(doc, separators=(",", ":"), allow_nan=False).encode()

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Self:
        """Body fields out of a decoded JSON object.

        A field with no dataclass default is REQUIRED: missing it rejects the datagram, which
        is the rule every field written before health telemetry existed still lives under. A
        field with a default is OPTIONAL and simply falls back to it. Unknown *extra* keys are
        ignored in both cases, so a newer sender never breaks an older receiver.
        """
        kw: dict[str, Any] = {
            "sender": _need(doc, "from", str),
            "seq": _need(doc, "seq", int),
            "t_ms": _need(doc, "t_ms", int),
        }
        hints = get_type_hints(cls)
        for f in fields(cls):
            if f.name in kw:
                continue
            if f.name not in doc:
                if f.default is MISSING and f.default_factory is MISSING:
                    raise DecodeError(f"missing field {f.name!r}", doc.get("type"))
                continue  # optional: the dataclass default stands for "not reported"
            kw[f.name] = _from_json(doc[f.name], hints[f.name], f.name)
        return cls(**kw)


@dataclass(frozen=True)
class OffersOpen(Message):
    """Ground → all. A new round: satellites answer with one ``bid`` each within ``collect_ms``."""

    TYPE = MessageType.OFFERS_OPEN
    round_id: int
    window_remaining_bytes: int
    collect_ms: int


@dataclass(frozen=True)
class Bid(Message):
    """Satellite → all. Offers exactly one item: the top of its local queue.

    ``window`` is the next ``BID_WINDOW_N`` entries *below* the top. It is DIAGNOSTIC:
    the arbiter reads ``item_id``/``score``/``item_age_s`` and nothing else. The window
    exists so the ground and the display can see queue *shape* — dense with good frames,
    or one good item over junk — which single top scores cannot show. Four of the five
    scores in this message are telemetry.
    """

    TYPE = MessageType.BID
    round_id: int
    item_id: int
    score: float  # 0..100, the satellite's own onboard score
    item_age_s: float  # how long the top item has waited on the satellite
    window: tuple[QueueEntry, ...]
    buffer: BufferStats
    eviction_count: int  # since boot; storage loss, distinct from losing arbitration
    queue_len: int


@dataclass(frozen=True)
class Grant(Message):
    """Ground → all. ``to`` may transmit ``item_id`` now, paced at ``pace_bps``. Nobody else transmits."""

    TYPE = MessageType.GRANT
    round_id: int
    to: str
    item_id: int
    pace_bps: float
    breakdown: Breakdown


@dataclass(frozen=True)
class Revoke(Message):
    """Ground → all. The grant holder did not begin/finish in time. Its bid is excluded from the re-run."""

    TYPE = MessageType.REVOKE
    round_id: int
    to: str
    item_id: int
    reason: str


@dataclass(frozen=True)
class TxBegin(Message):
    """Satellite → all. ``chunks`` chunks of the ENCODED payload are about to follow.

    ``total_bytes`` is and stays the RAW frame size: what the ground must end up holding, and
    what the ``tx_done`` digest is taken over. ``enc``/``enc_bytes`` describe the blob the
    chunks actually carry (see ``orbit/protocol/codec.py``). Both are optional so that a
    satellite which does not compress — an older build, or one that could not allocate the
    encoder's workspace — is not a malformed sender: an absent ``enc`` means ``"raw"``, and an
    absent ``enc_bytes`` means the payload is the frame itself.
    """

    TYPE = MessageType.TX_BEGIN
    round_id: int
    item_id: int
    total_bytes: int  # RAW frame bytes, before compression
    chunks: int  # chunks of the ENCODED payload
    enc: str | None = None  # None/absent = "raw"; "zlib" = an RFC 1950 stream over the frame
    enc_bytes: int | None = None  # encoded payload size; absent = the same as total_bytes


@dataclass(frozen=True)
class TxChunk(Message):
    """Satellite → all. One slice of the encoded payload, base64 inside the JSON.

    ``enc`` is repeated here rather than left to ``tx_begin`` alone for the same reason ``n``
    is: ``tx_begin`` is a single datagram on a lossy multicast bus, and the ground already
    reconstructs the chunk count from the chunks when it is lost. Without ``enc`` on the chunk
    that recovery path would hand compressed bytes to the scorer as though they were a frame —
    a caught, loud failure, but a whole frame of airtime thrown away to save 14 bytes a chunk.
    """

    TYPE = MessageType.TX_CHUNK
    round_id: int
    item_id: int
    idx: int
    n: int
    data: bytes  # base64 on the wire
    enc: str | None = None  # None/absent = "raw"; must agree with tx_begin


@dataclass(frozen=True)
class TxDone(Message):
    TYPE = MessageType.TX_DONE
    round_id: int
    item_id: int
    total_bytes: int  # RAW frame bytes: the digest below is over exactly this many
    score: float
    cloud_frac: float  # repeated here so "usable" can be judged even if the earlier `scored` was lost
    sha256: str  # hex digest of the RAW frame; the ground reassembles, decodes, and must match it


@dataclass(frozen=True)
class TxAck(Message):
    """Ground → all. ``ok`` means every chunk arrived; the satellite pops the item only then.

    A failed transmission (``ok=false``) leaves the item on the satellite and debits nothing:
    the frame is not lost, the slot is simply re-arbitrated.
    """

    TYPE = MessageType.TX_ACK
    round_id: int
    to: str
    item_id: int
    ok: bool
    bytes_received: int
    reason: str


@dataclass(frozen=True)
class State(Message):
    TYPE = MessageType.STATE
    state: str  # config.STATE_*
    round_id: int
    granted_to: str  # "" when nobody holds the slot
    window: WindowStatus


@dataclass(frozen=True)
class Heartbeat(Message):
    """Satellite → all, every SAT_HEARTBEAT_MS. Lets the ground tell idle from starved between rounds.

    The three health fields below are LEVEL-triggered and self-clearing: each heartbeat states the
    node's condition *now*, so a bad value needs no "it got better" message and a missed heartbeat
    costs at most one period of staleness. They are optional because they exist only on hardware —
    the simulator has no radio and no heap — and absent means "not reported", never zero. A node
    that stops being able to measure one simply drops it again.
    """

    TYPE = MessageType.HEARTBEAT
    buffer: BufferStats
    eviction_count: int
    queue_len: int
    top_score: float  # -1 when the queue is empty
    top_item_id: int  # -1 when the queue is empty
    uptime_s: float
    frames_scored: int  # since boot
    frames_sent: int  # since boot, confirmed by tx_ack
    rssi_dbm: int | None = None  # WiFi signal strength, negative dBm; None on a node with no radio
    free_heap_bytes: int | None = None  # smallest number that matters for "will it still allocate"
    psram_ok: bool | None = None  # False = the frame pool fell back to internal SRAM


@dataclass(frozen=True)
class Eviction(Message):
    """Satellite → all. A frame was permanently lost to onboard storage limits.

    ``kind`` = "evicted": a held frame was displaced by a better new one.
    ``kind`` = "rejected": the new frame did not beat the worst held one and was dropped.
    Either way the loss is the satellite's capture rate outrunning its downlink share —
    the problem this system exists to make visible — and it is *not* the same as losing
    a round of arbitration.
    """

    TYPE = MessageType.EVICTION
    item_id: int
    score: float
    kind: str
    displaced_by: int  # -1 for "rejected"
    displaced_by_score: float


@dataclass(frozen=True)
class ScoreParts:
    """The three normalised components, 0..100 each, as the display shows them."""

    clear: float
    sharp: float
    change: float


@dataclass(frozen=True)
class Scored(Message):
    """Satellite → all. One frame went through the onboard kernel.

    The ground does not need this to arbitrate — it prices bids — but it is what lets the
    ground run the no-scoring FIFO baseline over the same byte budget and judge frames as
    usable from cloud fraction alone, which is the pitch's headline comparison.
    """

    TYPE = MessageType.SCORED
    item_id: int
    score: float
    parts: ScoreParts
    cloud_frac: float  # fraction of pixels above CLOUD_THRESHOLD
    queued: bool  # False = rejected on arrival (pool full, did not beat the worst held)
    evicted_item_id: int  # -1 when nothing was displaced
    queue_depth: int


@dataclass(frozen=True)
class Fault(Message):
    """Satellite → all. Something went wrong onboard that the node itself named.

    This message is only the envelope for a fault: ``code_id`` indexes the fault-code registry,
    which lives outside this module precisely so that adding, renaming or reclassifying a code
    never touches the wire schema. Nothing here validates the id, and no code list is defined
    here — a receiver that does not know an id still gets ``severity`` and ``detail``.

    Unlike the heartbeat's health gauges this is EDGE-triggered: it reports an event, not a level,
    so it is sent once when the condition occurs rather than repeated.
    """

    TYPE = MessageType.FAULT
    code_id: int  # index into the fault-code registry (owned elsewhere)
    severity: str  # the registry's severity name for this code
    detail: str  # free text for a human reading the bus log; "" when there is nothing to add


MESSAGE_TYPES: dict[MessageType, type[Message]] = {
    m.TYPE: m
    for m in (
        OffersOpen,
        Bid,
        Grant,
        Revoke,
        TxBegin,
        TxChunk,
        TxDone,
        TxAck,
        State,
        Heartbeat,
        Eviction,
        Scored,
        Fault,
    )
}

GROUND_TYPES = frozenset(
    {MessageType.OFFERS_OPEN, MessageType.GRANT, MessageType.REVOKE, MessageType.TX_ACK, MessageType.STATE}
)
SATELLITE_TYPES = frozenset(set(MessageType) - GROUND_TYPES)


def decode(data: bytes, max_len: int = 65535) -> Message | DecodeError:
    """Bytes → Message, or a DecodeError. Never raises."""
    if len(data) > max_len:
        return DecodeError(f"datagram too long ({len(data)} bytes)")
    try:
        doc = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as e:
        return DecodeError(f"not JSON: {e}")
    if not isinstance(doc, dict):
        return DecodeError("top level is not an object")
    try:
        if doc.get("v") != config.PROTOCOL_VERSION:
            raise DecodeError(f"protocol version {doc.get('v')!r} != {config.PROTOCOL_VERSION}", doc.get("type"))
        raw_type = doc.get("type")
        try:
            mtype = MessageType(str(raw_type))
        except ValueError:
            raise DecodeError(f"unknown type {raw_type!r}", str(raw_type)) from None
        return MESSAGE_TYPES[mtype].from_doc(doc)
    except DecodeError as e:
        return e
    except (TypeError, ValueError, KeyError) as e:  # anything the field coercion did not expect
        return DecodeError(f"malformed: {e}", str(doc.get("type")))


# --- (de)serialisation helpers --------------------------------------------------------


def _reject_constant(name: str) -> Any:
    """json.loads would happily turn NaN/Infinity/-Infinity into floats; on this bus they are hostile."""
    raise ValueError(f"non-finite constant {name}")


def _to_json(v: Any) -> Any:
    if isinstance(v, bytes):
        return base64.b64encode(v).decode("ascii")
    if isinstance(v, StrEnum):
        return str(v)
    if is_dataclass(v) and not isinstance(v, type):
        return {f.name: _to_json(getattr(v, f.name)) for f in fields(v)}
    if isinstance(v, tuple | list):
        return [_to_json(x) for x in v]
    return v


def _need(doc: dict[str, Any], key: str, typ: type) -> Any:
    if key not in doc:
        raise DecodeError(f"missing field {key!r}", doc.get("type"))
    return _from_json(doc[key], typ, key)


def _from_json(v: Any, typ: Any, name: str) -> Any:
    origin = get_origin(typ)
    if origin in (types.UnionType, Union):
        # Only `X | None` is supported: an explicit null is the same as "not reported".
        inner = [a for a in get_args(typ) if a is not type(None)]
        if len(inner) != 1 or len(inner) == len(get_args(typ)):
            raise DecodeError(f"{name}: unsupported field type {typ!r}")
        return None if v is None else _from_json(v, inner[0], name)
    if origin is tuple:
        (inner, *_) = get_args(typ)
        if not isinstance(v, list):
            raise DecodeError(f"{name}: expected list")
        return tuple(_from_json(x, inner, name) for x in v)
    if typ is bytes:
        if not isinstance(v, str):
            raise DecodeError(f"{name}: expected base64 string")
        try:
            return base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError) as e:
            raise DecodeError(f"{name}: bad base64: {e}") from None
    if typ is bool:
        if not isinstance(v, bool):
            raise DecodeError(f"{name}: expected bool")
        return v
    if typ is int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise DecodeError(f"{name}: expected int")
        return v
    if typ is float:
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            raise DecodeError(f"{name}: expected finite number")
        return float(v)
    if typ is str:
        if not isinstance(v, str):
            raise DecodeError(f"{name}: expected string")
        return v
    if isinstance(typ, type) and is_dataclass(typ):
        if not isinstance(v, dict):
            raise DecodeError(f"{name}: expected object")
        hints = get_type_hints(typ)
        kw = {}
        for f in fields(typ):
            if f.name not in v:
                raise DecodeError(f"{name}.{f.name}: missing")
            kw[f.name] = _from_json(v[f.name], hints[f.name], f"{name}.{f.name}")
        return typ(**kw)
    raise DecodeError(f"{name}: unsupported field type {typ!r}")


# --- documentation ---------------------------------------------------------------------


def spec_markdown() -> str:
    """The message table for docs/protocol.md, generated so it cannot drift from the code."""
    out = ["| type | direction | fields |", "|---|---|---|"]
    for mtype, cls in MESSAGE_TYPES.items():
        direction = "ground → all" if mtype in GROUND_TYPES else "satellite → all"
        names = ", ".join(
            # `?` marks an optional field: one the sender may leave out entirely
            f"`{f.name}`" + ("?" if f.default is not MISSING or f.default_factory is not MISSING else "")
            for f in fields(cls)
            if f.name not in ("sender", "seq", "t_ms")
        )
        out.append(f"| `{mtype}` | {direction} | {names} |")
    return "\n".join(out)


def examples() -> list[tuple[str, Message]]:
    """One instance per type; the encode/decode test vectors and the spec's worked examples."""
    buf = BufferStats(slots=8, capacity_bytes=8 * config.FRAME_BYTES, used=5, free=3, occupancy_pct=62.5)
    win = (QueueEntry(12, 88.0, 4.5), QueueEntry(9, 71.25, 9.0))
    bd = Breakdown(score=91.0, item_age_s=6.0, item_age_term=3.0, sat_wait_s=10.0, sat_wait_term=3.0, total=97.0)
    ws = WindowStatus(capacity_bytes=983040, used_bytes=16384, remaining_bytes=966656, slots_remaining=59)
    g, s = "gx10-f548", "sat-a"
    return [
        ("offers_open", OffersOpen(g, 1, 1000, round_id=1, window_remaining_bytes=983040, collect_ms=200)),
        (
            "bid",
            Bid(
                s,
                7,
                5000,
                round_id=1,
                item_id=14,
                score=91.0,
                item_age_s=6.0,
                window=win,
                buffer=buf,
                eviction_count=2,
                queue_len=3,
            ),
        ),
        ("grant", Grant(g, 2, 1200, round_id=1, to=s, item_id=14, pace_bps=65536.0, breakdown=bd)),
        ("revoke", Revoke(g, 3, 2200, round_id=1, to=s, item_id=14, reason="grant_timeout")),
        ("tx_begin", TxBegin(s, 8, 5200, round_id=1, item_id=14, total_bytes=config.FRAME_BYTES, chunks=16)),
        ("tx_chunk", TxChunk(s, 9, 5210, round_id=1, item_id=14, idx=0, n=16, data=bytes(range(8)))),
        (
            "tx_done",
            TxDone(
                s,
                10,
                7200,
                round_id=1,
                item_id=14,
                total_bytes=config.FRAME_BYTES,
                score=91.0,
                cloud_frac=0.08,
                sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            ),
        ),
        (
            "tx_ack",
            TxAck(g, 4, 3210, round_id=1, to=s, item_id=14, ok=True, bytes_received=config.FRAME_BYTES, reason=""),
        ),
        ("state", State(g, 5, 3211, state=config.STATE_READY, round_id=2, granted_to="", window=ws)),
        (
            "heartbeat",
            Heartbeat(
                s,
                11,
                8000,
                buffer=buf,
                eviction_count=2,
                queue_len=2,
                top_score=88.0,
                top_item_id=12,
                uptime_s=8.0,
                frames_scored=9,
                frames_sent=4,
                rssi_dbm=-57,
                free_heap_bytes=214512,
                psram_ok=True,
            ),
        ),
        (
            "eviction",
            Eviction(s, 12, 8100, item_id=3, score=41.0, kind="evicted", displaced_by=15, displaced_by_score=77.5),
        ),
        (
            "scored",
            Scored(
                s,
                13,
                8100,
                item_id=15,
                score=77.5,
                parts=ScoreParts(clear=92.0, sharp=61.5, change=79.0),
                cloud_frac=0.08,
                queued=True,
                evicted_item_id=3,
                queue_depth=5,
            ),
        ),
        # code_id is a registry index, not a constant of this module: 1 is a placeholder here.
        ("fault", Fault(s, 14, 8200, code_id=1, severity="warn", detail="frame pool in internal SRAM")),
    ]


def vectors() -> list[dict[str, Any]]:
    return [{"name": n, "wire": m.encode().decode()} for n, m in examples()]


__all__ = [
    "GROUND_TYPES",
    "MESSAGE_TYPES",
    "SATELLITE_TYPES",
    "Bid",
    "Breakdown",
    "BufferStats",
    "DecodeError",
    "Eviction",
    "Fault",
    "Grant",
    "Heartbeat",
    "Message",
    "MessageType",
    "OffersOpen",
    "QueueEntry",
    "Revoke",
    "ScoreParts",
    "Scored",
    "State",
    "TxAck",
    "TxBegin",
    "TxChunk",
    "TxDone",
    "WindowStatus",
    "decode",
    "examples",
    "spec_markdown",
    "vectors",
]
