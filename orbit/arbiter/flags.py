"""Flagging (Section 12): telling "aging is working" from "aging has failed".

Two thresholds on a satellite's wait:

* SOFT — the wait is long but the aging term is climbing and will win a slot. Normal.
  Informational only.
* HARD — the wait is so long that the aging term *should* have won by now and did not.
  Something else is wrong: bidding but never granted, granted but never transmitting,
  a corrupt score, a one-way link. This is the one that wants an operator.

Wait time alone cannot tell these apart, so each satellite is first classified:

* starved            — holds items, is bidding, is not winning. Soft/hard apply.
* idle               — nothing to send. Its wait is meaningless; never flagged.
* never_transmitted  — has been seen but has no transmission history. Its "wait" is
                       measured from first sight, not from boot, so a satellite that
                       has bid for HARD seconds and never won *is* flagged (that is the
                       anomaly), while one that just appeared is not.
* nominal            — has transmitted recently enough.

Memory pressure is an independent axis: a satellite evicting frames is losing data to
onboard storage, not to arbitration, and can be pressured whether or not it is starved.
Silence (no bus traffic from it) is a third independent axis.

A flag that fires on ordinary aging is noise, and noise teaches people to ignore the
panel. Everything here errs toward not flagging.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from orbit.config import Settings
from orbit.protocol.messages import BufferStats


class WaitKind(StrEnum):
    NOMINAL = "nominal"
    STARVED = "starved"
    IDLE = "idle"
    NEVER_TRANSMITTED = "never_transmitted"
    SILENT = "silent"  # not heard from within peer_stale_s; nothing else can be judged


class Level(StrEnum):
    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


@dataclass(frozen=True)
class Assessment:
    hostname: str
    kind: WaitKind
    level: Level
    wait_s: float  # what the aging term is using
    memory_pressured: bool
    evictions_per_min: float
    silent: bool
    has_items: bool
    reason: str

    @property
    def attention(self) -> bool:
        """Operator attention: a hard flag, memory pressure, or silence. Soft alone is not."""
        return self.level is Level.HARD or self.memory_pressured or self.silent


@dataclass
class SatelliteRecord:
    """What the ground remembers about one hostname. Nothing here is managed — only observed."""

    hostname: str
    first_seen_s: float
    last_seen_s: float
    last_tx_complete_s: float | None = None
    transmissions: int = 0
    grants: int = 0
    revokes: int = 0
    failed_tx: int = 0
    last_bid_round: int = -1
    queue_len: int = 0
    top_score: float = -1.0
    eviction_count: int = 0  # satellite's own since-boot counter
    buffer: BufferStats | None = None
    eviction_times: deque[float] = field(default_factory=deque)  # ground-observed, for the rate
    last_window: tuple[tuple[int, float, float], ...] = ()  # diagnostic window snapshot for the display

    @property
    def has_items(self) -> bool:
        return self.queue_len > 0

    def wait_s(self, now: float) -> float:
        """satellite_wait_seconds as the arbiter uses it: since the last confirmed transmission,
        or since first sight if there has never been one."""
        epoch = self.last_tx_complete_s if self.last_tx_complete_s is not None else self.first_seen_s
        return max(0.0, now - epoch)

    def note_eviction(self, now: float, s: Settings) -> None:
        self.eviction_times.append(now)
        self._trim(now, s)

    def evictions_per_min(self, now: float, s: Settings) -> float:
        self._trim(now, s)
        if not self.eviction_times:
            return 0.0
        return len(self.eviction_times) * 60.0 / s.memory_pressure_window_s

    def _trim(self, now: float, s: Settings) -> None:
        cutoff = now - s.memory_pressure_window_s
        while self.eviction_times and self.eviction_times[0] < cutoff:
            self.eviction_times.popleft()


def assess(rec: SatelliteRecord, now: float, s: Settings) -> Assessment:
    silent = (now - rec.last_seen_s) > s.peer_stale_s
    rate = rec.evictions_per_min(now, s)
    pressured = rate >= s.memory_pressure_evictions_per_min
    wait = rec.wait_s(now)

    if silent:
        kind, level, why = WaitKind.SILENT, Level.NONE, f"no message for {now - rec.last_seen_s:.1f}s"
    elif not rec.has_items:
        kind, level, why = WaitKind.IDLE, Level.NONE, "queue empty; wait is not meaningful"
    elif rec.last_tx_complete_s is None:
        kind = WaitKind.NEVER_TRANSMITTED
        level = _level(wait, s)
        why = f"no transmission since first seen {wait:.1f}s ago"
    else:
        level = _level(wait, s)
        kind = WaitKind.STARVED if level is not Level.NONE else WaitKind.NOMINAL
        why = f"holding {rec.queue_len} item(s), last transmitted {wait:.1f}s ago"

    if level is Level.HARD:
        why += "; aging should have won a slot by now"
    if pressured:
        why += f"; evicting at {rate:.1f}/min"
    return Assessment(
        hostname=rec.hostname,
        kind=kind,
        level=level,
        wait_s=wait,
        memory_pressured=pressured,
        evictions_per_min=rate,
        silent=silent,
        has_items=rec.has_items,
        reason=why,
    )


def _level(wait: float, s: Settings) -> Level:
    if wait > s.hard_wait_s:
        return Level.HARD
    if wait > s.soft_wait_s:
        return Level.SOFT
    return Level.NONE
