"""The arbitration formula (Section 9), as pure functions with no I/O.

    priority = score
             + item_age_seconds       × ITEM_AGING_RATE
             + satellite_wait_seconds × SAT_AGING_RATE

Both aging terms exist because neither alone is enough:

* Item aging lifts a frame that keeps being out-scored by newer arrivals on *other*
  satellites, so no individual frame is stranded forever.
* Satellite aging lifts a whole satellite that consistently scores a little lower than
  its peers. Item aging cannot fix that case: a satellite that never wins never drains
  its queue, so its items age but never reach the top of the *constellation's* order.

Fairness comes entirely from these two terms. There is no timer, quantum or round-robin
behind them, and there must not be: a slot handed out because it is "someone's turn"
is a slot spent on whatever that satellite happens to hold, good or not.

The arbiter reads exactly three numbers from each bid: ``score``, ``item_age_s`` and
the ground's own ``satellite_wait_seconds`` for that hostname. The bid's ``window`` is
diagnostic and is not an input here — see ``Bid``.

Ties break on the lowest hostname, so a replay from the same seed reproduces the same
table on every run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from orbit.config import Settings
from orbit.protocol.messages import Bid, Breakdown


@dataclass(frozen=True)
class Candidate:
    """One bid, priced. Sorted by (-total, hostname): highest priority first, ties alphabetical."""

    hostname: str
    item_id: int
    breakdown: Breakdown
    bid: Bid

    @property
    def total(self) -> float:
        return self.breakdown.total


@dataclass(frozen=True)
class Decision:
    """Everything the display needs to explain a grant: who won, by how much, and the itemised alternatives."""

    round_id: int
    winner: Candidate
    ranked: tuple[Candidate, ...]  # winner first
    excluded: frozenset[str]  # hostnames whose bids were set aside (revoked this round)

    @property
    def runner_up(self) -> Candidate | None:
        return self.ranked[1] if len(self.ranked) > 1 else None

    @property
    def margin(self) -> float | None:
        ru = self.runner_up
        return None if ru is None else self.winner.total - ru.total


def breakdown(score: float, item_age_s: float, sat_wait_s: float, s: Settings) -> Breakdown:
    """The formula, itemised so the display can show each contribution separately."""
    item_term = item_age_s * s.item_aging_rate
    sat_term = sat_wait_s * s.sat_aging_rate
    return Breakdown(
        score=score,
        item_age_s=item_age_s,
        item_age_term=item_term,
        sat_wait_s=sat_wait_s,
        sat_wait_term=sat_term,
        total=score + item_term + sat_term,
    )


def price(bid: Bid, sat_wait_s: float, s: Settings) -> Candidate:
    """Only the top item is priced. ``bid.window`` is deliberately not read."""
    return Candidate(
        hostname=bid.sender, item_id=bid.item_id, breakdown=breakdown(bid.score, bid.item_age_s, sat_wait_s, s), bid=bid
    )


def rank(
    bids: Iterable[Bid], waits: Mapping[str, float], s: Settings, exclude: frozenset[str] = frozenset()
) -> tuple[Candidate, ...]:
    """Price every non-excluded bid and order them. One bid per hostname: a later duplicate replaces an earlier one
    (a satellite that re-bids has fresher numbers), so the caller can hand over raw bus traffic."""
    latest: dict[str, Bid] = {}
    for b in bids:
        if b.sender not in exclude:
            latest[b.sender] = b
    cands = [price(b, waits.get(h, 0.0), s) for h, b in latest.items()]
    cands.sort(key=lambda c: (-c.total, c.hostname))
    return tuple(cands)


def decide(
    round_id: int, bids: Iterable[Bid], waits: Mapping[str, float], s: Settings, exclude: frozenset[str] = frozenset()
) -> Decision | None:
    """Grant exactly one slot to exactly one satellite, or None if nobody (eligible) bid."""
    ranked = rank(bids, waits, s, exclude)
    if not ranked:
        return None
    return Decision(round_id=round_id, winner=ranked[0], ranked=ranked, excluded=exclude)
