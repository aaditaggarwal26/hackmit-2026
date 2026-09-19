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
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from typing import Any, ClassVar, Self, TypeVar, get_args, get_origin, get_type_hints

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
        doc: dict[str, Any] = {"v": config.PROTOCOL_VERSION, "type": str(self.TYPE), "from": self.sender,
                               "seq": self.seq, "t_ms": self.t_ms}
        for f in fields(self):
            if f.name in ("sender", "seq", "t_ms"):
                continue
            doc[f.name] = _to_json(getattr(self, f.name))
        return json.dumps(doc, separators=(",", ":"), allow_nan=False).encode()

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Self:
        kw: dict[str, Any] = {"sender": _need(doc, "from", str), "seq": _need(doc, "seq", int),
                              "t_ms": _need(doc, "t_ms", int)}
        hints = get_type_hints(cls)
        for f in fields(cls):
            if f.name in kw:
                continue
            if f.name not in doc:
                raise DecodeError(f"missing field {f.name!r}", doc.get("type"))
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
    TYPE = MessageType.TX_BEGIN
    round_id: int
    item_id: int
    total_bytes: int
    chunks: int


@dataclass(frozen=True)
class TxChunk(Message):
    TYPE = MessageType.TX_CHUNK
    round_id: int
    item_id: int
    idx: int
    n: int
    data: bytes  # base64 on the wire


@dataclass(frozen=True)
class TxDone(Message):
    TYPE = MessageType.TX_DONE
    round_id: int
    item_id: int
    total_bytes: int
    score: float


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
    """Satellite → all, every SAT_HEARTBEAT_MS. Lets the ground tell idle from starved between rounds."""

    TYPE = MessageType.HEARTBEAT
    buffer: BufferStats
    eviction_count: int
    queue_len: int
    top_score: float  # -1 when the queue is empty
    top_item_id: int  # -1 when the queue is empty
    uptime_s: float


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


MESSAGE_TYPES: dict[MessageType, type[Message]] = {
    m.TYPE: m for m in (OffersOpen, Bid, Grant, Revoke, TxBegin, TxChunk, TxDone, TxAck, State, Heartbeat, Eviction)
}

GROUND_TYPES = frozenset({MessageType.OFFERS_OPEN, MessageType.GRANT, MessageType.REVOKE, MessageType.TX_ACK,
                          MessageType.STATE})
SATELLITE_TYPES = frozenset(set(MessageType) - GROUND_TYPES)


def decode(data: bytes, max_len: int = 65535) -> Message | DecodeError:
    """Bytes → Message, or a DecodeError. Never raises."""
    if len(data) > max_len:
        return DecodeError(f"datagram too long ({len(data)} bytes)")
    try:
        doc = json.loads(data.decode("utf-8"))
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
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise DecodeError(f"{name}: expected number")
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
        names = ", ".join(f"`{f.name}`" for f in fields(cls) if f.name not in ("sender", "seq", "t_ms"))
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
        ("bid", Bid(s, 7, 5000, round_id=1, item_id=14, score=91.0, item_age_s=6.0, window=win, buffer=buf,
                    eviction_count=2, queue_len=3)),
        ("grant", Grant(g, 2, 1200, round_id=1, to=s, item_id=14, pace_bps=65536.0, breakdown=bd)),
        ("revoke", Revoke(g, 3, 2200, round_id=1, to=s, item_id=14, reason="grant_timeout")),
        ("tx_begin", TxBegin(s, 8, 5200, round_id=1, item_id=14, total_bytes=config.FRAME_BYTES, chunks=16)),
        ("tx_chunk", TxChunk(s, 9, 5210, round_id=1, item_id=14, idx=0, n=16, data=bytes(range(8)))),
        ("tx_done", TxDone(s, 10, 7200, round_id=1, item_id=14, total_bytes=config.FRAME_BYTES, score=91.0)),
        ("tx_ack", TxAck(g, 4, 3210, round_id=1, to=s, item_id=14, ok=True, bytes_received=config.FRAME_BYTES,
                         reason="")),
        ("state", State(g, 5, 3211, state=config.STATE_READY, round_id=2, granted_to="", window=ws)),
        ("heartbeat", Heartbeat(s, 11, 8000, buffer=buf, eviction_count=2, queue_len=2, top_score=88.0,
                                top_item_id=12, uptime_s=8.0)),
        ("eviction", Eviction(s, 12, 8100, item_id=3, score=41.0, kind="evicted", displaced_by=15,
                              displaced_by_score=77.5)),
    ]


def vectors() -> list[dict[str, Any]]:
    return [{"name": n, "wire": m.encode().decode()} for n, m in examples()]


__all__ = [
    "GROUND_TYPES", "MESSAGE_TYPES", "SATELLITE_TYPES", "Bid", "Breakdown", "BufferStats", "DecodeError",
    "Eviction", "Grant", "Heartbeat", "Message", "MessageType", "OffersOpen", "QueueEntry", "Revoke", "State",
    "TxAck", "TxBegin", "TxChunk", "TxDone", "WindowStatus", "decode", "examples", "spec_markdown", "vectors",
]

