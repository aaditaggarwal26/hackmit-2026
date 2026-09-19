"""Orchestrator over sim:// nodes with a virtual clock: arbitration, starvation
guard, window accounting, mirror/golden agreement, FIFO baseline, scenarios."""
import asyncio
from collections import Counter

import pytest

from orbit import params
from orbit.orchestrator import scenarios
from orbit.orchestrator.arbitration import Arbiter, Candidate
from orbit.orchestrator.main import Orchestrator, main
from orbit.orchestrator.window import ContactWindow
from orbit.protocol import messages as M


def run(name, passes=2, nodes=("sim://0", "sim://1"), **kw):
    o = scenarios.build(name, nodes=list(nodes), passes=passes, virtual=True, **kw)
    asyncio.run(o.run())
    o.close()
    return o


# ------------------------------------------------------------------ pure parts
def test_arbiter_rules():
    a = Arbiter(starvation_n=2)
    C = lambda n, s: Candidate(n, True, s, n)
    d = a.decide([C(0, 50), C(1, 50)])
    assert d.winner == 0 and d.reason == "highest" and d.runner_up == 1       # tie -> lowest id
    assert a.decide([C(0, 90), C(1, 89)]).winner == 0
    d = a.decide([C(0, 90), C(1, 89)])
    assert d.winner == 1 and d.reason == "starvation" and d.runner_up == 0 and a.starvation_switches == 1
    assert a.decide([C(0, 90), C(1, 89)]).winner == 0 and a.streak == 1
    assert a.decide([Candidate(0, False, 0, 0xFFFF), C(1, 1)]).winner == 1
    assert a.decide([Candidate(0, False, 0, 0xFFFF)]).reason == "none"
    a.reset()
    assert a.streak == 0 and a.last_winner is None


def test_window_accounting():
    w = ContactWindow.for_slots(3, 10.0)
    assert w.slots_total == 3 and w.capacity_bytes == 3 * params.FRAME_BYTES and w.snapshot()["scaled"]
    w.debit(params.FRAME_BYTES); w.debit(params.FRAME_BYTES)
    assert w.open and w.slots_remaining == 1
    w.debit(params.FRAME_BYTES)
    assert not w.open and w.slots_used == 3
    with pytest.raises(AssertionError):
        w.debit(1)
    assert not ContactWindow().snapshot()["scaled"]           # the un-scaled defaults are labelled as such


# ------------------------------------------------------------------ runs
def test_nominal_two_nodes():
    o = run("nominal", passes=2)
    assert o.done and len(o.history) == 2
    per_pass = o.window.slots_total
    assert all(len(h["slots"]) == per_pass for h in o.history)
    for n in o.nodes:
        assert n.mismatches == 0 and n.timeouts == 0 and n.captures == 2 * o.frames_per_pass
        assert len(n.scored) == n.captures and n.snapshot()["config_valid"] and n.snapshot()["ref_loaded"]
        assert len(n.mirror) == n.captures - len(n.sent) - n.mirror.evicted   # STATUS vs mirror is checked live: mismatches == 0
    assert sum(len(n.sent) for n in o.nodes) == 2 * per_pass
    assert o.value_filtered >= o.value_fifo > 0
    assert all(s["reason"] == "highest" for s in o.slots) or o.arbiter.starvation_switches > 0
    # every granted slot debited exactly one frame and the window closed spent
    assert all(s["bytes"] == params.FRAME_BYTES for s in o.slots)
    assert o.history[-1]["window"]["slots_remaining"] == 0
    snap = o.snapshot()
    assert snap["provenance"]["synthetic"] is False and snap["window"]["scaled"] and snap["wire"]["baud"] == params.BAUD


def test_starvation_guard_fires():
    o = run("starvation", passes=2, starvation_n=2, slots_per_pass=6)   # window longer than the streak
    winners = [s["node"] for s in o.slots]
    assert 0 in winners and 1 in winners
    assert o.arbiter.starvation_switches > 0
    forced = [s for s in o.slots if s["reason"] == "starvation"]
    assert forced and all(s["node"] == 0 and s["runner_up"] == 1 for s in forced)
    for h in o.history:                                          # streaks reset per window
        per_pass = [s["node"] for s in h["slots"]]
        assert max(len(list(g)) for _, g in __import__("itertools").groupby(per_pass)) <= 2


def test_lead_change():
    o = run("lead_change", passes=3, slots_per_pass=6)
    firsts = [s["node"] for s in o.history[0]["slots"]]
    assert firsts[0] == 0                                       # node 0 opens with its best frames
    later = Counter(s["node"] for h in o.history[1:] for s in h["slots"])
    assert later[1] > 0                                         # the lead changes hands


def test_scaling_many_simulated_nodes():
    o = run("scaling", passes=1, nodes=[f"sim://{k}" for k in range(6)], slots_per_pass=5)
    assert len(o.slots) == 5 and all(n.mismatches == 0 for n in o.nodes)
    assert all(n.snapshot()["simulated"] for n in o.nodes)


def test_eviction_reported_when_queue_is_full():
    o = run("nominal", passes=1, frames_per_pass=8, slots_per_pass=1, config=__import__("orbit.golden.score", fromlist=["Config"]).Config(queue_limit=4))
    ev = [s for n in o.nodes for s in n.scored if s["evicted_id"] != 0xFFFF]
    assert ev and all(n.mirror.evicted == n.status["frames_evicted"] for n in o.nodes)
    assert o.fifo_dropped > 0                                    # the baseline also loses frames to its queue


def test_pause_step_and_speed_controls():
    o = scenarios.build("nominal", nodes=["sim://0", "sim://1"], passes=1, virtual=True, frames_per_pass=2, slots_per_pass=1)
    o.pause()

    async def drive():
        task = asyncio.create_task(o.run())
        for _ in range(50):
            await asyncio.sleep(0)
        assert not o.done and o.nodes[0].captures == 0
        o.set_speed(100)
        assert o.speed == 16.0
        o.play()
        await task

    asyncio.run(drive())
    assert o.done


def test_cli_prints_summary(capsys):
    assert main(["--scenario", "nominal", "--passes", "1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("scenario=nominal") and "mismatches=0" in out
