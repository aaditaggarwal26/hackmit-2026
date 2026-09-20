"""The arbitration formula: itemised terms, alphabetical tiebreak, exclusion, latest-bid-wins, and the
one property that matters most — the window field is never an input."""

from hypothesis import given, settings
from hypothesis import strategies as st

from orbit import config
from orbit.arbiter.priority import breakdown, decide, rank
from orbit.protocol import messages as M

S = config.Settings(item_aging_rate=0.5, sat_aging_rate=0.3)
BUF = M.BufferStats(slots=8, capacity_bytes=8 * config.FRAME_BYTES, used=1, free=7, occupancy_pct=12.5)


def bid(host, score, age=0.0, window=(), item_id=1, round_id=1, seq=1):
    return M.Bid(
        host,
        seq,
        0,
        round_id=round_id,
        item_id=item_id,
        score=score,
        item_age_s=age,
        window=tuple(M.QueueEntry(i, s, a) for i, s, a in window),
        buffer=BUF,
        eviction_count=0,
        queue_len=1,
    )


def test_breakdown_is_the_formula():
    b = breakdown(score=80.0, item_age_s=10.0, sat_wait_s=20.0, s=S)
    assert (b.item_age_term, b.sat_wait_term, b.total) == (5.0, 6.0, 91.0)


def test_highest_priority_wins_and_terms_are_itemised():
    d = decide(1, [bid("sat-a", 70.0, age=0.0), bid("sat-b", 60.0, age=30.0)], {"sat-a": 0.0, "sat-b": 0.0}, S)
    assert d is not None and d.winner.hostname == "sat-b"  # 60 + 15 beats 70
    assert d.winner.breakdown.item_age_term == 15.0 and d.winner.breakdown.sat_wait_term == 0.0
    assert d.runner_up is not None and d.runner_up.hostname == "sat-a" and d.margin == 5.0


def test_satellite_aging_lifts_a_consistent_low_scorer():
    """Item aging alone cannot rescue a satellite that never wins; satellite wait can."""
    d = decide(1, [bid("sat-a", 80.0), bid("sat-b", 65.0)], {"sat-a": 0.0, "sat-b": 60.0}, S)
    assert d is not None and d.winner.hostname == "sat-b" and d.winner.breakdown.sat_wait_term == 18.0


def test_tiebreak_is_lowest_hostname():
    d = decide(1, [bid("sat-c", 50.0), bid("sat-a", 50.0), bid("sat-b", 50.0)], {}, S)
    assert d is not None and [c.hostname for c in d.ranked] == ["sat-a", "sat-b", "sat-c"]


def test_exclusion_sets_a_bid_aside():
    d = decide(1, [bid("sat-a", 90.0), bid("sat-b", 50.0)], {}, S, exclude=frozenset({"sat-a"}))
    assert d is not None and d.winner.hostname == "sat-b" and d.excluded == {"sat-a"}
    assert decide(1, [bid("sat-a", 90.0)], {}, S, exclude=frozenset({"sat-a"})) is None


def test_no_bids_no_decision():
    assert decide(1, [], {}, S) is None


def test_latest_bid_per_host_wins():
    ranked = rank([bid("sat-a", 10.0, seq=1), bid("sat-a", 90.0, seq=2)], {}, S)
    assert len(ranked) == 1 and ranked[0].total == 90.0


def test_window_is_never_an_input():
    """Four of the five scores in a bid are telemetry. Give sat-b an absurd window; sat-a must still win."""
    plain = decide(1, [bid("sat-a", 70.0), bid("sat-b", 60.0)], {}, S)
    loaded = decide(1, [bid("sat-a", 70.0), bid("sat-b", 60.0, window=[(2, 100.0, 9999.0)] * 4)], {}, S)
    assert plain is not None and loaded is not None
    assert plain.winner.hostname == loaded.winner.hostname == "sat-a"
    assert [c.total for c in plain.ranked] == [c.total for c in loaded.ranked]


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["sat-a", "sat-b", "sat-c", "sat-d"]),
            st.floats(0, 100),
            st.floats(0, 1000),
            st.floats(0, 1000),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_winner_has_maximal_total(entries):
    bids = [bid(h, sc, age=a, seq=i) for i, (h, sc, a, _) in enumerate(entries)]
    waits = {h: w for h, _, _, w in entries}
    d = decide(1, bids, waits, S)
    assert d is not None
    assert all(d.winner.total >= c.total for c in d.ranked)
    tied = [c for c in d.ranked if c.total == d.winner.total]
    assert d.winner.hostname == min(c.hostname for c in tied)
