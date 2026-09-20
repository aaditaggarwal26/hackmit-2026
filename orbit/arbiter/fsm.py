"""The ground station's state machine (Section 10), with no I/O of its own.

    READY    → broadcast "offers open", collect bids, compute, grant one
    BUSY     → the granted satellite transmits; all bids ignored
    COMPLETE → transmission confirmed, window debited, back to READY
    CLOSED   → window capacity exhausted

Arbitration is per slot and event driven. One slot is one frame. The winner transmits
to completion — there is no preemption, no quantum, no round-robin — and when it
finishes arbitration runs again from current state. A satellite winning several slots
in a row is correct: it holds the most valuable frames right now. Fairness is the
aging terms' job (see ``priority``), not this machine's.

The class is deliberately a pure event machine: ``on_message(msg, now)`` and
``on_tick(now)`` return the messages to broadcast, and ``now`` is supplied by the
caller. The simulator drives it with a virtual clock for a bit-identical replay from
a seed; the live station drives it from asyncio with the monotonic clock. The two
never diverge because neither owns a timer.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from orbit import config
from orbit.arbiter.flags import Assessment, SatelliteRecord, assess
from orbit.arbiter.priority import Decision, decide
from orbit.arbiter.window import ContactWindow
from orbit.config import Settings
from orbit.log import log
from orbit.protocol import messages as M

lg = logging.getLogger("orbit.ground")
TMsg = TypeVar("TMsg", bound=M.Message)

EventSink = Callable[[str, dict[str, Any]], None]


@dataclass
class GrantState:
    """The one outstanding grant while BUSY."""

    round_id: int
    to: str
    item_id: int
    deadline: float  # grant_timeout until tx_begin, then tx_timeout until tx_done
    granted_at: float = 0.0
    began: bool = False
    chunks_expected: int = 0
    chunks_seen: set[int] = field(default_factory=set)
    chunks: dict[int, bytes] = field(default_factory=dict)  # kept until tx_done so the digest can be checked
    bytes_seen: int = 0
    done_pending: M.TxDone | None = None  # tx_done arrived before the last chunk: wait a little for stragglers
    done_deadline: float = 0.0

    def digest(self) -> str:
        h = hashlib.sha256()
        for i in sorted(self.chunks):
            h.update(self.chunks[i])
        return h.hexdigest()


@dataclass
class Counters:
    rounds: int = 0
    grants: int = 0
    completed: int = 0
    revoked: int = 0
    failed_tx: int = 0
    corrupt_tx: int = 0  # all chunks arrived, digest did not match: counted inside failed_tx too
    duplicate_offers: int = 0  # a satellite offered an item the ground already holds (its ack was lost): re-acked
    no_bid_rounds: int = 0
    late_bids: int = 0
    ignored_while_busy: int = 0
    unexpected: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))


class GroundStation:
    def __init__(self, settings: Settings, hostname: str, sink: EventSink | None = None) -> None:
        self.s = settings
        self.hostname = hostname
        self.window = ContactWindow.from_settings(settings)
        self.state = config.STATE_READY
        self.sats: dict[str, SatelliteRecord] = {}
        self.round_id = 0
        self.bids: dict[str, M.Bid] = {}  # latest bid per hostname for the current round
        self.excluded: set[str] = set()  # revoked in this round; their bids are set aside for the re-run
        self.collect_deadline: float | None = None
        self.reopen_at: float | None = None
        self.grant: GrantState | None = None
        self.decisions: list[Decision] = []
        self.delivered: set[tuple[str, int]] = set()  # (hostname, item_id) confirmed this pass: never counted twice
        self.counters = Counters()
        self._seq = 0
        self._epoch = 0.0  # t_ms on the wire is uptime: seconds since start(), not the caller's absolute clock
        self._last_state_at = -1e9
        self._sink = sink or (lambda kind, payload: None)

    # ------------------------------------------------------------------ driver API

    def start(self, now: float) -> list[M.Message]:
        """Open the first round."""
        self._epoch = now
        return self._open_round(now)

    def on_message(self, msg: M.Message, now: float) -> list[M.Message]:
        if msg.sender == self.hostname or msg.TYPE in M.GROUND_TYPES:
            return []  # our own echo, or another ground (not supported; ignored rather than fought)
        rec = self._touch(msg.sender, now)
        match msg:
            case M.Bid():
                return self._on_bid(msg, rec, now)
            case M.Heartbeat():
                self._update_from_heartbeat(msg, rec)
                return []
            case M.Eviction():
                rec.note_eviction(now, self.s)
                self._emit(
                    "eviction",
                    now,
                    sat=msg.sender,
                    item_id=msg.item_id,
                    score=msg.score,
                    loss_kind=msg.kind,
                    displaced_by=msg.displaced_by,
                    displaced_by_score=msg.displaced_by_score,
                )
                return []
            case M.TxBegin():
                return self._on_tx_begin(msg, now)
            case M.TxChunk():
                return self._on_tx_chunk(msg, now)
            case M.TxDone():
                return self._on_tx_done(msg, rec, now)
        return []

    def on_tick(self, now: float) -> list[M.Message]:
        out: list[M.Message] = []
        if self.state == config.STATE_READY:
            if self.collect_deadline is not None and now >= self.collect_deadline:
                out += self._arbitrate(now)
            elif self.reopen_at is not None and now >= self.reopen_at:
                out += self._open_round(now)
        elif self.state == config.STATE_BUSY and self.grant is not None:
            g = self.grant
            if g.done_pending is not None and (self._all_chunks(g) or now >= g.done_deadline):
                out += self._finish(g, self.sats[g.to], g.done_pending, now)
            elif now >= g.deadline:
                out += self._revoke(now, "grant_timeout" if not g.began else "tx_timeout")
        elif self.state == config.STATE_COMPLETE:  # transient; only reachable if a sink raised mid-transition
            out += self._open_round(now)
        if now - self._last_state_at >= self.s.state_period_ms / 1000.0:
            out.append(self._state_msg(now))
        return out

    def assessments(self, now: float) -> dict[str, Assessment]:
        return {h: assess(r, now, self.s) for h, r in sorted(self.sats.items())}

    def waits(self, now: float) -> dict[str, float]:
        return {h: r.wait_s(now) for h, r in self.sats.items()}

    # ------------------------------------------------------------------ rounds

    def _open_round(self, now: float) -> list[M.Message]:
        if not self.window.open:
            return self._close(now)
        self.round_id += 1
        self.counters.rounds += 1
        self.bids.clear()
        self.excluded.clear()
        self.grant = None
        self.reopen_at = None
        self.collect_deadline = now + self.s.bid_collect_ms / 1000.0
        self.state = config.STATE_READY
        self._emit("round_open", now, round_id=self.round_id, window=self.window.snapshot())
        return [
            self._mk(
                M.OffersOpen,
                now,
                round_id=self.round_id,
                window_remaining_bytes=self.window.remaining_bytes,
                collect_ms=self.s.bid_collect_ms,
            ),
            self._state_msg(now),
        ]

    def _on_bid(self, bid: M.Bid, rec: SatelliteRecord, now: float) -> list[M.Message]:
        rec.queue_len = bid.queue_len
        rec.top_score = bid.score
        rec.buffer = bid.buffer
        rec.eviction_count = bid.eviction_count
        rec.last_window = tuple((e.item_id, e.score, e.item_age_s) for e in bid.window)
        if (bid.sender, bid.item_id) in self.delivered:
            # We already have this frame; the satellite never heard the ack. Re-ack instead of spending a slot on
            # it: the satellite pops it and offers its next item in the next round.
            self.counters.duplicate_offers += 1
            self._emit("duplicate_offer", now, sat=bid.sender, item_id=bid.item_id, round_id=bid.round_id)
            return [
                self._mk(
                    M.TxAck,
                    now,
                    round_id=bid.round_id,
                    to=bid.sender,
                    item_id=bid.item_id,
                    ok=True,
                    bytes_received=0,
                    reason="already delivered",
                )
            ]
        if self.state != config.STATE_READY:
            self.counters.ignored_while_busy += 1
            return []
        if bid.round_id != self.round_id or self.collect_deadline is None:
            self.counters.late_bids += 1
            self._emit("late_bid", now, sat=bid.sender, bid_round=bid.round_id, round_id=self.round_id)
            return []
        rec.last_bid_round = bid.round_id
        self.bids[bid.sender] = bid
        return []

    def _arbitrate(self, now: float) -> list[M.Message]:
        self.collect_deadline = None
        decision = decide(self.round_id, self.bids.values(), self.waits(now), self.s, frozenset(self.excluded))
        if decision is None:
            self.counters.no_bid_rounds += 1
            self.reopen_at = now + self.s.idle_reopen_ms / 1000.0
            self._emit("no_bids", now, round_id=self.round_id, excluded=sorted(self.excluded))
            return []
        self.decisions.append(decision)
        w = decision.winner
        rec = self.sats[w.hostname]
        rec.grants += 1
        self.counters.grants += 1
        self.grant = GrantState(
            round_id=self.round_id,
            to=w.hostname,
            item_id=w.item_id,
            deadline=now + self.s.grant_timeout_ms / 1000.0,
            granted_at=now,
        )
        self.state = config.STATE_BUSY
        self._emit(
            "decision",
            now,
            round_id=self.round_id,
            winner=w.hostname,
            item_id=w.item_id,
            margin=decision.margin,
            excluded=sorted(decision.excluded),
            ranked=[
                dict(
                    sat=c.hostname,
                    item_id=c.item_id,
                    **vars(c.breakdown),
                    window=[vars(e) for e in c.bid.window],
                    queue_len=c.bid.queue_len,
                    occupancy_pct=c.bid.buffer.occupancy_pct,
                    eviction_count=c.bid.eviction_count,
                )
                for c in decision.ranked
            ],
        )
        log(
            lg,
            logging.INFO,
            "grant",
            round_id=self.round_id,
            to=w.hostname,
            item_id=w.item_id,
            total=round(w.total, 2),
            score=w.breakdown.score,
            item_age_term=round(w.breakdown.item_age_term, 2),
            sat_wait_term=round(w.breakdown.sat_wait_term, 2),
        )
        return [
            self._mk(
                M.Grant,
                now,
                round_id=self.round_id,
                to=w.hostname,
                item_id=w.item_id,
                pace_bps=self.s.link_rate_bps,
                breakdown=w.breakdown,
            ),
            self._state_msg(now),
        ]

    # ------------------------------------------------------------------ transmission

    def _holder(self, msg: M.TxBegin | M.TxChunk | M.TxDone, now: float) -> GrantState | None:
        g = self.grant
        if (
            self.state != config.STATE_BUSY
            or g is None
            or msg.sender != g.to
            or msg.item_id != g.item_id
            or msg.round_id != g.round_id
        ):
            self.counters.unexpected += 1
            self._emit(
                "unexpected_tx", now, sat=msg.sender, type=str(msg.TYPE), item_id=msg.item_id, round_id=msg.round_id
            )
            return None
        return g

    def _on_tx_begin(self, msg: M.TxBegin, now: float) -> list[M.Message]:
        g = self._holder(msg, now)
        if g is None:
            return []
        g.began = True
        g.chunks_expected = msg.chunks
        g.deadline = now + self.s.tx_timeout_ms / 1000.0
        self._emit(
            "tx_begin",
            now,
            round_id=g.round_id,
            sat=msg.sender,
            item_id=msg.item_id,
            chunks=msg.chunks,
            bytes=msg.total_bytes,
        )
        return []

    def _on_tx_chunk(self, msg: M.TxChunk, now: float) -> list[M.Message]:
        g = self._holder(msg, now)
        if g is None:
            return []
        if not g.began:  # tx_begin was lost; every chunk says how many there are
            g.began = True
            g.chunks_expected = msg.n
            g.deadline = now + self.s.tx_timeout_ms / 1000.0
        if not 0 <= msg.idx < g.chunks_expected:
            self.counters.unexpected += 1
            return []
        if msg.idx not in g.chunks_seen:
            g.chunks_seen.add(msg.idx)
            g.chunks[msg.idx] = msg.data
            g.bytes_seen += len(msg.data)
        if g.done_pending is not None and self._all_chunks(g):  # the straggler arrived
            return self._finish(g, self.sats[g.to], g.done_pending, now)
        return []

    @staticmethod
    def _all_chunks(g: GrantState) -> bool:
        return g.began and g.chunks_expected > 0 and len(g.chunks_seen) == g.chunks_expected

    def _on_tx_done(self, msg: M.TxDone, rec: SatelliteRecord, now: float) -> list[M.Message]:
        g = self._holder(msg, now)
        if g is None:
            return []
        if not self._all_chunks(g) and g.began and g.done_pending is None:
            # tx_done overtook a chunk (reordering): give stragglers a moment before failing 2 s of airtime
            g.done_pending = msg
            g.done_deadline = now + self.s.tx_straggler_ms / 1000.0
            return []
        return self._finish(g, rec, msg, now)

    def _finish(self, g: GrantState, rec: SatelliteRecord, msg: M.TxDone, now: float) -> list[M.Message]:
        g.done_pending = None
        complete = self._all_chunks(g)
        reason = f"received {len(g.chunks_seen)}/{g.chunks_expected} chunks"
        if complete and g.digest() != msg.sha256:
            complete = False  # every chunk arrived but the bytes are not the frame the satellite scored
            reason = "digest mismatch"
            self.counters.corrupt_tx += 1
        if not complete:
            rec.failed_tx += 1
            self.counters.failed_tx += 1
            self._emit("tx_failed", now, round_id=g.round_id, sat=g.to, item_id=g.item_id, reason=reason)
            log(lg, logging.WARNING, "tx_failed", sat=g.to, item_id=g.item_id, reason=reason)
            ack = self._mk(
                M.TxAck,
                now,
                round_id=g.round_id,
                to=g.to,
                item_id=g.item_id,
                ok=False,
                bytes_received=g.bytes_seen,
                reason=reason,
            )
            return [ack, *self._rearbitrate_without(g.to, now)]
        # COMPLETE: debit the window, record the transmission, confirm to the satellite
        self.state = config.STATE_COMPLETE
        self.window.debit()
        self.delivered.add((g.to, g.item_id))
        rec.last_tx_complete_s = now
        rec.transmissions += 1
        rec.queue_len = max(0, rec.queue_len - 1)
        self.counters.completed += 1
        self._emit(
            "complete",
            now,
            round_id=g.round_id,
            sat=g.to,
            item_id=g.item_id,
            score=msg.score,
            cloud_frac=msg.cloud_frac,
            bytes=g.bytes_seen,
            granted_at=g.granted_at,
            sha256=msg.sha256,
            window=self.window.snapshot(),
        )
        log(lg, logging.INFO, "complete", sat=g.to, item_id=g.item_id, slots_remaining=self.window.slots_remaining)
        out: list[M.Message] = [
            self._mk(
                M.TxAck,
                now,
                round_id=g.round_id,
                to=g.to,
                item_id=g.item_id,
                ok=True,
                bytes_received=g.bytes_seen,
                reason="",
            ),
            self._state_msg(now),
        ]
        self.grant = None
        return out + self._open_round(now)

    def _revoke(self, now: float, reason: str) -> list[M.Message]:
        g = self.grant
        assert g is not None
        self.sats[g.to].revokes += 1
        self.counters.revoked += 1
        self._emit("revoke", now, sat=g.to, item_id=g.item_id, reason=reason, round_id=g.round_id)
        log(lg, logging.WARNING, "revoke", sat=g.to, item_id=g.item_id, reason=reason)
        msg = self._mk(M.Revoke, now, round_id=g.round_id, to=g.to, item_id=g.item_id, reason=reason)
        return [msg, *self._rearbitrate_without(g.to, now)]

    def _rearbitrate_without(self, hostname: str, now: float) -> list[M.Message]:
        """Same round, same bids, minus the satellite that failed to deliver."""
        self.excluded.add(hostname)
        self.grant = None
        self.state = config.STATE_READY
        out = self._arbitrate(now)
        if self.grant is None:  # nobody else bid this round: open a fresh one rather than idle-wait
            self.reopen_at = None
            out += self._open_round(now)
        return out

    def _close(self, now: float) -> list[M.Message]:
        if self.state != config.STATE_CLOSED:
            self.state = config.STATE_CLOSED
            self.collect_deadline = self.reopen_at = None
            self._emit("window_closed", now, window=self.window.snapshot(), counters=self.counters.as_dict())
            log(lg, logging.INFO, "window_closed", slots_used=self.window.slots_used)
        return [self._state_msg(now)]

    # ------------------------------------------------------------------ bookkeeping

    def _touch(self, hostname: str, now: float) -> SatelliteRecord:
        rec = self.sats.get(hostname)
        if rec is None:
            rec = self.sats[hostname] = SatelliteRecord(hostname=hostname, first_seen_s=now, last_seen_s=now)
            self._emit("sat_seen", now, sat=hostname)
            log(lg, logging.INFO, "sat_seen", sat=hostname)
        rec.last_seen_s = now
        return rec

    def _update_from_heartbeat(self, hb: M.Heartbeat, rec: SatelliteRecord) -> None:
        rec.queue_len = hb.queue_len
        rec.top_score = hb.top_score
        rec.buffer = hb.buffer
        rec.eviction_count = hb.eviction_count

    def _state_msg(self, now: float) -> M.State:
        self._last_state_at = now
        return self._mk(
            M.State,
            now,
            state=self.state,
            round_id=self.round_id,
            granted_to=self.grant.to if self.grant else "",
            window=self.window.status(),
        )

    def _mk(self, cls: type[TMsg], now: float, **body: Any) -> TMsg:
        self._seq += 1
        return cls(sender=self.hostname, seq=self._seq, t_ms=int((now - self._epoch) * 1000), **body)

    def _emit(self, kind: str, now: float, **payload: Any) -> None:
        try:
            self._sink(kind, {"t": now, **payload})
        except Exception:  # telemetry/stream are observers; a bug there must not cost a slot
            lg.exception("event sink failed on %s", kind)

    def snapshot(self, now: float) -> dict[str, Any]:
        return dict(
            state=self.state,
            round_id=self.round_id,
            granted_to=self.grant.to if self.grant else "",
            window=self.window.snapshot(),
            counters=self.counters.as_dict(),
            sats={
                h: dict(
                    queue_len=r.queue_len,
                    top_score=r.top_score,
                    transmissions=r.transmissions,
                    grants=r.grants,
                    revokes=r.revokes,
                    failed_tx=r.failed_tx,
                    eviction_count=r.eviction_count,
                    wait_s=r.wait_s(now),
                    occupancy_pct=r.buffer.occupancy_pct if r.buffer else None,
                    window=[list(e) for e in r.last_window],
                    flags=vars(a) | {"kind": str(a.kind), "level": str(a.level)},
                )
                for h, r in sorted(self.sats.items())
                for a in [assess(r, now, self.s)]
            },
        )
