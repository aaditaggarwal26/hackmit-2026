"""End to end on the loopback bus: the arbiter grants, satellites transmit, the window drains, and the run is
reproducible from its seed. Each scenario checks the claim it exists to make visible."""

import pytest

from orbit import config
from orbit.sim.run import run_scenario

S = config.Settings()


def test_nominal_delivers_frames_and_is_deterministic():
    a = run_scenario("nominal", 12, seed=7, settings=S)
    b = run_scenario("nominal", 12, seed=7, settings=S)
    assert a.digest() == b.digest()
    assert a.ground.counters.completed >= 12 and a.ground.counters.failed_tx == 0 and a.ground.counters.revoked == 0
    assert a.ground.window.slots_used == a.ground.counters.completed
    assert sum(s.counters.transmitted for s in a.sats) == a.ground.counters.completed
    assert a.ground_bus.stats.dropped_malformed == 0
    assert {r.winner for r in a.rows} == {"sat-a", "sat-b", "sat-c"}
    d = next(e for e in a.events if e["kind"] == "decision")
    assert {"score", "item_age_term", "sat_wait_term", "total"} <= set(d["ranked"][0])


def test_different_seed_different_run():
    assert run_scenario("nominal", 12, seed=1, settings=S).digest() != run_scenario("nominal", 12, seed=2, settings=S).digest()


def test_memory_pressure_evicts_yet_still_wins_slots():
    sim = run_scenario("memory_pressure", 30, seed=3, settings=S)
    c = next(s for s in sim.sats if s.hostname == "sat-c")
    assert c.counters.evicted + c.counters.rejected > 20
    assert c.counters.transmitted > 0
    assert any(r.sats["sat-c"]["flag"].endswith("+MEM") for r in sim.rows)
    others = [s for s in sim.sats if s.hostname != "sat-c"]
    assert all(s.counters.transmitted > 0 for s in others)  # nobody is locked out


def test_low_scorer_gets_slots_through_satellite_aging():
    sim = run_scenario("low_scorer", 40, seed=5, settings=S)
    b = next(s for s in sim.sats if s.hostname == "sat-b")
    assert b.counters.transmitted > 0
    wins = [r for r in sim.rows if r.winner == "sat-b" and r.outcome == "complete"]
    assert wins and any(r.sat_wait_term + r.item_age_term > r.score * 0.1 for r in wins)


def test_revoke_scenario_times_out_and_flags_hard():
    sim = run_scenario("revoke", 60, seed=2, settings=S)
    assert sim.ground.counters.revoked > 0
    c = sim.ground.sats["sat-c"]
    assert c.revokes == sim.ground.counters.revoked and c.transmissions == 0
    assert any(r.outcome.startswith("revoked") for r in sim.rows)
    a = sim.ground.assessments(sim.now)["sat-c"]
    assert str(a.level) == "hard" and str(a.kind) == "never_transmitted"
    assert all(sim.ground.assessments(sim.now)[h].level != "hard" for h in ("sat-a", "sat-b"))
    assert sim.ground.counters.completed > 25  # the other two kept the link busy


def test_lossy_bus_never_loses_a_frame():
    sim = run_scenario("lossy", 30, seed=4, settings=S)
    g = sim.ground
    assert g.counters.completed >= 15  # ~23 datagrams per slot at 2% loss: roughly a third of slots fail
    assert g.counters.completed + g.counters.failed_tx + g.counters.revoked >= len(sim.rows)
    assert sim.ground_bus.stats.dropped_dup > 0
    # No frame is ever lost to the bus. A satellite pops only on tx_ack{ok}; if that ack was dropped it keeps the
    # frame (ack_lost) even though the ground counted the slot, so the gap is bounded by lost acks. A dropped
    # tx_ack{ok:false} likewise shows up as ack_lost on the satellite rather than as a nack.
    tx, lost, failed = (sum(getattr(s.counters, k) for s in sim.sats) for k in ("transmitted", "ack_lost", "failed"))
    assert tx <= g.counters.completed <= tx + lost
    assert failed <= g.counters.failed_tx <= failed + lost
    assert lost > 0  # the scenario really did drop acks
    for sat in sim.sats:  # everything not confirmed is still on board
        assert sat.buffer.used == len(sat.queue) == len(sat.items)


def test_late_joiner_needs_no_configuration():
    sim = run_scenario("late_joiner", 40, seed=6, settings=S)
    c = next(s for s in sim.sats if s.hostname == "sat-c")
    assert "sat-c" in sim.ground.sats and sim.ground.sats["sat-c"].first_seen_s >= 40.0
    assert c.counters.transmitted > 0


def test_window_closes_when_exhausted():
    sim = run_scenario("nominal", 500, seed=1, settings=S.with_overrides(window_duration_s=10.0))
    assert sim.ground.state == config.STATE_CLOSED
    assert sim.ground.window.slots_used == sim.ground.window.slots_total == S.with_overrides(window_duration_s=10.0).window_slots
    assert any(e["kind"] == "window_closed" for e in sim.events)


@pytest.mark.parametrize("item_rate,sat_rate", [(0.0, 0.0), (5.0, 0.0), (0.0, 5.0)])
def test_aging_rates_are_tunable_and_change_outcomes(item_rate, sat_rate):
    base = run_scenario("low_scorer", 25, seed=9, settings=S).digest()
    tuned = run_scenario("low_scorer", 25, seed=9, settings=S.with_overrides(item_aging_rate=item_rate, sat_aging_rate=sat_rate))
    assert tuned.digest() != base or (item_rate, sat_rate) == (S.item_aging_rate, S.sat_aging_rate)
    if sat_rate == 0.0 and item_rate == 0.0:
        assert all(r.item_age_term == 0.0 and r.sat_wait_term == 0.0 for r in tuned.rows)
