"""A simulated satellite that behaves the way the ESP32-S3 firmware will (Section 5).

It is fully autonomous: it captures, scores, buffers, evicts, queues and bids on its
own, and the only thing the ground ever tells it is "you may transmit this item now".
The ground never sees an unscored frame and never manages this node's internals.

Two data structures, kept apart on purpose:

* ``FrameBuffer`` — a fixed pool allocated once at boot, holding raw frame bytes. It is
  large and never moves. There is no growth path, because on an ESP32-S3 there is
  none: a few hundred KB of usable RAM, allocated up front, full is full.
* ``PriorityQueue`` (from the golden model) — small entries of (score, item_id) that
  reorder constantly. The queue points *into* the buffer; it does not contain it.

Eviction is a local decision made when a new frame arrives and no slot is free: if it
outscores the worst item held, the worst is evicted; otherwise the newcomer is rejected.
Either way a frame is permanently lost to *onboard storage*, which is a different
failure from losing arbitration, and every such loss is announced on the bus.

The class is a pure event machine (``on_message``/``on_tick`` with a caller-supplied
clock), so the simulator can replay it bit-for-bit from a seed and the live demo can
drive the same code from asyncio.
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass
from typing import Any, TypeVar

import numpy as np

from orbit import config, corpus
from orbit.config import Settings
from orbit.golden.queue import NO_FRAME, PriorityQueue
from orbit.golden.score import Config as ScoreConfig
from orbit.golden.score import display, score_frame
from orbit.log import log
from orbit.protocol import messages as M

lg = logging.getLogger("orbit.sat")
TMsg = TypeVar("TMsg", bound=M.Message)


@dataclass(frozen=True)
class SatelliteProfile:
    """Scenario knobs for one simulated satellite. Everything the firmware would have as constants or
    as its physical situation (orbit, optics, cloud climatology)."""

    hostname: str
    capture_period_s: float = 3.0
    capture_jitter: float = 0.2  # ± fraction of the period, seeded
    score_bias: float = 0.0  # added to every displayed score (0..100), clipped: "consistently scores lower"
    buffer_slots: int = 8
    seq_seed: int = 0  # which corpus capture order this satellite follows
    boot_delay_s: float = 0.0  # comes online later than the others
    never_transmit: bool = False  # accepts grants, never sends: exercises revoke
    chunk_drop_prob: float = 0.0  # silently loses chunks: exercises tx_ack ok=false


@dataclass
class Item:
    item_id: int
    corpus_id: int
    raw_score: int  # 0..65535 as the kernel produces it
    captured_at: float
    cloud_frac: float = 0.0
    parts: tuple[float, float, float] = (0.0, 0.0, 0.0)  # clear, sharp, change on 0..100

    def score(self, bias: float) -> float:
        return min(100.0, max(0.0, display(self.raw_score) + bias))


class FrameBuffer:
    """Fixed pool of ``slots`` frames. ``store`` requires a free slot — admission is decided by the caller."""

    def __init__(self, slots: int, frame_bytes: int = config.FRAME_BYTES) -> None:
        self.slots = slots
        self.frame_bytes = frame_bytes
        self._pool = bytearray(slots * frame_bytes)  # the one allocation
        self._free: list[int] = list(range(slots))
        self._slot_of: dict[int, int] = {}

    @property
    def used(self) -> int:
        return len(self._slot_of)

    @property
    def free(self) -> int:
        return len(self._free)

    def store(self, item_id: int, data: bytes | memoryview) -> None:
        if not self._free:
            raise MemoryError("frame buffer full")
        if len(data) != self.frame_bytes:
            raise ValueError(f"frame is {len(data)} bytes, pool slot is {self.frame_bytes}")
        slot = self._free.pop()
        off = slot * self.frame_bytes
        self._pool[off:off + self.frame_bytes] = data
        self._slot_of[item_id] = slot

    def release(self, item_id: int) -> None:
        slot = self._slot_of.pop(item_id)
        self._free.append(slot)

    def read(self, item_id: int) -> memoryview:
        off = self._slot_of[item_id] * self.frame_bytes
        return memoryview(self._pool)[off:off + self.frame_bytes]

    def stats(self) -> M.BufferStats:
        pct = round(100.0 * self.used / self.slots, 1) if self.slots else 0.0
        return M.BufferStats(slots=self.slots, capacity_bytes=self.slots * self.frame_bytes, used=self.used,
                             free=self.free, occupancy_pct=pct)


@dataclass
class Transmission:
    round_id: int
    item_id: int
    pace_bps: float
    chunks: list[bytes]
    sha256: str = ""
    next_idx: int = 0
    next_at: float = 0.0
    done_sent: bool = False


@dataclass
class SatCounters:
    captured: int = 0
    evicted: int = 0
    rejected: int = 0
    bids: int = 0
    grants: int = 0
    transmitted: int = 0
    failed: int = 0
    revoked: int = 0
    ack_lost: int = 0  # tx_done sent, no tx_ack heard: frame kept, bidding resumed
    late_acks: int = 0  # a positive ack arrived after we had given up (or on a re-offer): popped, not resent
    peer_grants_seen: int = 0  # what a relay would build on: we hear who else is granted


class FakeSatellite:
    def __init__(self, profile: SatelliteProfile, settings: Settings, corp: corpus.Corpus, seed: int = 0) -> None:
        self.p = profile
        self.s = settings
        self.hostname = profile.hostname
        self.corpus = corp
        self.rng = random.Random(seed * 7919 + profile.seq_seed)
        # A scene's reference frame is a stored prior the satellite already carries, not something it captures
        # again: it is excluded from the capture order. (References are the clearest frames in the corpus; feeding
        # them as captures handed the no-scoring FIFO baseline the cleanest frames first, for free.)
        self.sequence = [i for i in corp.sequence(profile.seq_seed, seed) if i != corp.reference_for(i)]
        self.seq_pos = 0
        self.buffer = FrameBuffer(profile.buffer_slots, settings.frame_bytes)
        self.queue = PriorityQueue(profile.buffer_slots)  # one queue cell per buffer slot
        self.items: dict[int, Item] = {}
        self.references: dict[int, np.ndarray] = {}  # scene reference frames the satellite carries
        self.cfg = ScoreConfig()
        self.counters = SatCounters()
        self.tx: Transmission | None = None
        self.awaiting_ack: int | None = None
        self.awaiting_since = 0.0
        self.awaiting_round = -1
        self.boot_at: float | None = None
        self.next_capture_at = 0.0
        self.next_heartbeat_at = 0.0
        self._next_item_id = 1
        self._seq = 0
        self.eviction_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ driver API

    def start(self, now: float) -> list[M.Message]:
        self.boot_at = now + self.p.boot_delay_s
        self.next_capture_at = self.boot_at + self._period()
        self.next_heartbeat_at = self.boot_at
        return []

    @property
    def online(self) -> bool:
        return self.boot_at is not None

    def uptime_ms(self, now: float) -> int:
        return int((now - (self.boot_at or now)) * 1000)

    def on_tick(self, now: float) -> list[M.Message]:
        if self.boot_at is None or now < self.boot_at:
            return []
        out: list[M.Message] = []
        while now >= self.next_capture_at:
            out += self.capture(self.next_capture_at)
            self.next_capture_at += self._period()
        if self.tx is not None:
            out += self._pump_tx(now)
        if self.awaiting_ack is not None and now - self.awaiting_since > self.s.sat_ack_timeout_ms / 1000.0:
            self._give_up_ack("ack_timeout")
        if now >= self.next_heartbeat_at:
            self.next_heartbeat_at = now + self.s.sat_heartbeat_ms / 1000.0
            out.append(self._heartbeat(now))
        return out

    def on_message(self, msg: M.Message, now: float) -> list[M.Message]:
        if self.boot_at is None or now < self.boot_at or msg.sender == self.hostname:
            return []
        match msg:
            case M.OffersOpen():
                if self.awaiting_ack is not None and msg.round_id > self.awaiting_round:
                    self._give_up_ack("ground_moved_on")  # the ack was lost; the ground is already past us
                return self._bid(msg, now)
            case M.Grant():
                if msg.to == self.hostname:
                    return self._granted(msg, now)
                self.counters.peer_grants_seen += 1
                return []
            case M.Revoke():
                if msg.to == self.hostname:
                    self.counters.revoked += 1
                    self.tx = None
                    self.awaiting_ack = None
                    log(lg, logging.WARNING, "revoked", sat=self.hostname, item_id=msg.item_id, reason=msg.reason)
                return []
            case M.TxAck():
                if msg.to == self.hostname:
                    return self._acked(msg, now)
                return []
        return []

    # ------------------------------------------------------------------ capture, score, admit

    def capture(self, now: float) -> list[M.Message]:
        """Acquire the next frame, score it onboard, and decide locally whether it earns a slot."""
        cid = self.sequence[self.seq_pos % len(self.sequence)]
        self.seq_pos += 1
        frame = self.corpus.by_id(cid)
        ref_id = self.corpus.reference_for(cid)
        ref = self.references.setdefault(ref_id, self.corpus.by_id(ref_id))
        s = score_frame(frame, ref, self.cfg)
        item = Item(item_id=self._next_item_id, corpus_id=cid, raw_score=s.score, captured_at=now,
                    cloud_frac=round(s.cloud_px / config.FRAME_BYTES, 4),
                    parts=(round(display(s.clear), 2), round(display(s.sharp), 2), round(display(s.change), 2)))
        self._next_item_id += 1
        self.counters.captured += 1
        out = self._admit(item, frame.tobytes(), now)
        lost = out[0] if out else None
        queued = not (isinstance(lost, M.Eviction) and lost.item_id == item.item_id)
        evicted = lost.item_id if (isinstance(lost, M.Eviction) and queued) else -1
        out.append(self._mk(M.Scored, now, item_id=item.item_id, score=item.score(self.p.score_bias),
                            parts=M.ScoreParts(*item.parts), cloud_frac=item.cloud_frac, queued=queued,
                            evicted_item_id=evicted, queue_depth=len(self.queue)))
        return out

    def _admit(self, item: Item, data: bytes, now: float) -> list[M.Message]:
        key = self._queue_key(item)
        if self.buffer.free > 0:
            self.buffer.store(item.item_id, data)
            self.items[item.item_id] = item
            self.queue.insert(key, item.item_id)
            return []
        # Full. The queue's tail is the worst item held; the in-flight item is never a candidate.
        worst_key, worst_id = self.queue.cells[-1]
        in_flight = self.tx.item_id if self.tx else self.awaiting_ack
        if key <= worst_key or worst_id == in_flight:
            self.counters.rejected += 1
            return [self._lost(item, "rejected", displaced_by=None, now=now)]
        lost_id = self.queue.insert(key, item.item_id)
        assert lost_id == worst_id and lost_id != NO_FRAME
        lost = self.items.pop(lost_id)
        self.buffer.release(lost_id)
        self.buffer.store(item.item_id, data)
        self.items[item.item_id] = item
        self.counters.evicted += 1
        return [self._lost(lost, "evicted", displaced_by=item, now=now)]

    def _lost(self, item: Item, kind: str, displaced_by: Item | None, now: float) -> M.Eviction:
        entry = dict(t=now, item_id=item.item_id, score=item.score(self.p.score_bias), kind=kind,
                     displaced_by=displaced_by.item_id if displaced_by else -1,
                     displaced_by_score=displaced_by.score(self.p.score_bias) if displaced_by else -1.0)
        self.eviction_log.append(entry)
        log(lg, logging.INFO, "frame_lost", sat=self.hostname, **entry)
        return self._mk(M.Eviction, now, item_id=item.item_id, score=entry["score"], kind=kind,
                        displaced_by=entry["displaced_by"], displaced_by_score=entry["displaced_by_score"])

    def _queue_key(self, item: Item) -> int:
        """Queue order follows the biased score the satellite would report; ties keep insert order."""
        return round(item.score(self.p.score_bias) * 100)

    # ------------------------------------------------------------------ bidding

    def _top_items(self) -> list[Item]:
        return [self.items[i] for _, i in self.queue.cells]

    def _bid(self, offer: M.OffersOpen, now: float) -> list[M.Message]:
        if self.tx is not None or self.awaiting_ack is not None or not self.queue.has_data:
            return []
        items = self._top_items()
        top, rest = items[0], items[1:1 + self.s.bid_window_n]
        self.counters.bids += 1
        return [self._mk(M.Bid, now, round_id=offer.round_id, item_id=top.item_id, score=top.score(self.p.score_bias),
                         item_age_s=round(now - top.captured_at, 3),
                         window=tuple(M.QueueEntry(i.item_id, i.score(self.p.score_bias), round(now - i.captured_at, 3))
                                      for i in rest),
                         buffer=self.buffer.stats(), eviction_count=self.counters.evicted + self.counters.rejected,
                         queue_len=len(self.queue))]

    # ------------------------------------------------------------------ transmitting

    def _granted(self, g: M.Grant, now: float) -> list[M.Message]:
        if g.item_id not in self.items:
            log(lg, logging.WARNING, "grant_for_unknown_item", sat=self.hostname, item_id=g.item_id)
            return []
        self.counters.grants += 1
        if self.p.never_transmit:
            return []  # the fault we model: granted, silent
        data = bytes(self.buffer.read(g.item_id))
        n = self.s.chunk_bytes
        chunks = [data[i:i + n] for i in range(0, len(data), n)]
        self.tx = Transmission(round_id=g.round_id, item_id=g.item_id, pace_bps=g.pace_bps, chunks=chunks,
                               sha256=hashlib.sha256(data).hexdigest(), next_at=now)
        return [self._mk(M.TxBegin, now, round_id=g.round_id, item_id=g.item_id, total_bytes=len(data),
                         chunks=len(chunks))]

    def _pump_tx(self, now: float) -> list[M.Message]:
        tx = self.tx
        assert tx is not None
        out: list[M.Message] = []
        while tx.next_idx < len(tx.chunks) and now >= tx.next_at:
            idx = tx.next_idx
            tx.next_idx += 1
            tx.next_at += len(tx.chunks[idx]) * 8 / tx.pace_bps  # paced at the link rate the ground quoted
            if self.p.chunk_drop_prob and self.rng.random() < self.p.chunk_drop_prob:
                continue
            out.append(self._mk(M.TxChunk, now, round_id=tx.round_id, item_id=tx.item_id, idx=idx, n=len(tx.chunks),
                                data=tx.chunks[idx]))
        if tx.next_idx >= len(tx.chunks) and not tx.done_sent:
            tx.done_sent = True
            item = self.items[tx.item_id]
            out.append(self._mk(M.TxDone, now, round_id=tx.round_id, item_id=tx.item_id,
                                total_bytes=sum(map(len, tx.chunks)), score=item.score(self.p.score_bias),
                                cloud_frac=item.cloud_frac, sha256=tx.sha256))
            self.awaiting_ack = tx.item_id
            self.awaiting_since = now
            self.awaiting_round = tx.round_id
            self.tx = None
        return out

    def _acked(self, ack: M.TxAck, now: float) -> list[M.Message]:
        if ack.item_id != self.awaiting_ack:
            # Not the ack we are waiting for. If it is a positive ack for an item we still hold, the ground has
            # the frame (our earlier ack was lost or reordered, or we re-offered it): pop it now, never resend.
            if ack.ok and ack.item_id in self.items and not (self.tx and self.tx.item_id == ack.item_id):
                self.counters.late_acks += 1
                self._pop(ack.item_id)
                log(lg, logging.INFO, "late_ack_pop", sat=self.hostname, item_id=ack.item_id, reason=ack.reason)
            return []
        self.awaiting_ack = None
        if not ack.ok:
            self.counters.failed += 1  # keep the frame: a failed transmission must not lose data
            log(lg, logging.WARNING, "tx_nack", sat=self.hostname, item_id=ack.item_id, reason=ack.reason)
            return []
        self._pop(ack.item_id)  # confirmed: only now does the item leave the queue and the buffer
        return []

    def _pop(self, item_id: int) -> None:
        self.queue.cells = [c for c in self.queue.cells if c[1] != item_id]
        self.items.pop(item_id)
        self.buffer.release(item_id)
        self.counters.transmitted += 1

    def _give_up_ack(self, reason: str) -> None:
        """A lost tx_ack must not wedge the satellite. We cannot know whether the ground counted the frame, so
        the frame stays (never lose data on uncertainty) and we go back to bidding; at worst the ground receives
        it twice. The firmware needs this same rule."""
        self.counters.ack_lost += 1
        log(lg, logging.WARNING, "ack_lost", sat=self.hostname, item_id=self.awaiting_ack, reason=reason)
        self.awaiting_ack = None

    # ------------------------------------------------------------------ misc

    def _heartbeat(self, now: float) -> M.Heartbeat:
        top = self._top_items()[0] if self.queue.has_data else None
        return self._mk(M.Heartbeat, now, buffer=self.buffer.stats(),
                        eviction_count=self.counters.evicted + self.counters.rejected, queue_len=len(self.queue),
                        top_score=top.score(self.p.score_bias) if top else -1.0,
                        top_item_id=top.item_id if top else -1, uptime_s=round(self.uptime_ms(now) / 1000.0, 3),
                        frames_scored=self.counters.captured, frames_sent=self.counters.transmitted)

    def _period(self) -> float:
        j = self.p.capture_jitter
        return self.p.capture_period_s * (1.0 + self.rng.uniform(-j, j)) if j else self.p.capture_period_s

    def _mk(self, cls: type[TMsg], now: float, **body: Any) -> TMsg:
        self._seq += 1
        return cls(sender=self.hostname, seq=self._seq, t_ms=self.uptime_ms(now), **body)

    def snapshot(self, now: float) -> dict[str, Any]:
        items = self._top_items()
        return dict(hostname=self.hostname, online=self.online and now >= (self.boot_at or 0.0),
                    queue=[(i.item_id, round(i.score(self.p.score_bias), 1), round(now - i.captured_at, 1))
                           for i in items],
                    buffer=vars(self.buffer.stats()), counters=vars(self.counters).copy(),
                    transmitting=self.tx.item_id if self.tx else None)

