"""The display contract (docs/event_stream.md): envelope rules, the FIFO baseline, usable-from-cloud-only,
and the WebSocket fan-out with backlog."""

import asyncio
import itertools
import json

import pytest

from orbit import config
from orbit.bus.base import BusStats
from orbit.ground.stream import (
    BUS_ALERT_PER_MIN,
    BUS_HEALTH_WINDOW_S,
    EventStream,
    FifoBaseline,
    FrameMeta,
    StreamServer,
)
from orbit.sim.run import run_scenario

S = config.Settings()
TYPES = {
    "run_start",
    "node_status",
    "queue_window",
    "frame_scored",
    "grant",
    "frame_arrived",
    "baseline_arrival",
    "window_update",
    "ground_status",
    "bus_health",
    "node_event",
    "run_end",
}
WINDOW = {  # a ContactWindow.snapshot() as the periodic tick passes it in
    "open": True,
    "capacity_bytes": 983040,
    "used_bytes": 0,
    "remaining_bytes": 983040,
    "slots_used": 0,
    "slots_remaining": 60,
}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("runs")
    sim = run_scenario("memory_pressure", 25, seed=3, settings=S.with_overrides(runs_dir=str(d)), run_id="t")
    lines = (d / "t.jsonl").read_text().splitlines()
    return sim, [json.loads(x) for x in lines]


def test_envelope_and_order(run):
    sim, ev = run
    assert [e["seq"] for e in ev] == list(range(1, len(ev) + 1))  # contiguous from 1, never skips
    assert all(e["t"] >= 0 and isinstance(e["t"], float) for e in ev)
    assert all(ev[i]["t"] <= ev[i + 1]["t"] for i in range(len(ev) - 1))
    assert {e["type"] for e in ev} <= TYPES
    assert ev[0]["type"] == "run_start" and sum(e["type"] == "run_start" for e in ev) == 1
    assert ev[-1]["type"] == "run_end" and sum(e["type"] == "run_end" for e in ev) == 1
    assert list(sim.stream.lines) == [json.dumps(json.loads(x), separators=(",", ":")) for x in sim.stream.lines]


def test_run_start_contents(run):
    _, ev = run
    rs = ev[0]
    assert rs["mode"] == "live" and rs["run_id"] == "t"
    assert [n["node_id"] for n in rs["nodes"]] == [0, 1, 2] and [n["label"] for n in rs["nodes"]] == [
        "sat-a",
        "sat-b",
        "sat-c",
    ]
    assert all(n["real"] is False for n in rs["nodes"])  # simulated satellites: the display shows a badge
    assert rs["window"] == {"budget_bytes": S.window_capacity_bytes, "duration_s": S.window_duration_s}
    assert rs["usable_rule"] == {"metric": "cloud_frac", "max": S.usable_cloud_max}
    assert rs["scoring"]["cloud_thr"] == config.CLOUD_THRESHOLD and rs["queue_limit"] == S.sat_buffer_slots


def test_grant_has_every_node_and_itemised_priority(run):
    _, ev = run
    grants = [e for e in ev if e["type"] == "grant"]
    assert grants
    for g in grants:
        assert {b["node_id"] for b in g["bids"]} == {0, 1, 2}
        assert g["reason"] in ("highest_score", "starvation_forced", "only_ready")
        w = next(b for b in g["bids"] if b["node_id"] == g["node_id"])
        assert w["ready"] and w["priority"] == pytest.approx(w["top_score"] + w["item_age_term"] + w["sat_wait_term"])
        assert all(b["top_score"] == 0 for b in g["bids"] if not b["ready"])
        ready = [b for b in g["bids"] if b["ready"]]
        if g["reason"] == "only_ready":
            assert len(ready) == 1
        elif g["reason"] == "highest_score":
            assert w["top_score"] == max(b["top_score"] for b in ready)
        else:
            assert w["top_score"] < max(b["top_score"] for b in ready)  # aging decided it
    assert any(g["reason"] == "starvation_forced" for g in grants)  # memory_pressure makes sat-a/b win on wait


def test_frame_arrived_usable_from_cloud_only(run):
    _, ev = run
    arrived = [e for e in ev if e["type"] == "frame_arrived"]
    scored = {(e["node_id"], e["frame_id"]): e for e in ev if e["type"] == "frame_scored"}
    assert len(arrived) == 25
    for a in arrived:
        assert a["usable"] == (a["cloud_frac"] <= S.usable_cloud_max)
        assert a["bytes"] == config.FRAME_BYTES and a["duration_s"] > 0
        fs = scored[(a["node_id"], a["frame_id"])]
        assert fs["cloud_frac"] == a["cloud_frac"] and set(fs["parts"]) == {"clear", "sharp", "change"}
    # usable is not a function of score: there exist frames with a high score that are unusable or vice versa
    # is not guaranteed for one seed, so check the definition instead: two frames with equal usability can differ
    # arbitrarily in score.
    slot_ids = [a["slot_id"] for a in arrived]
    assert slot_ids == sorted(slot_ids) and len(set(slot_ids)) == len(slot_ids)


def test_baseline_gets_the_same_slots_and_budget(run):
    _, ev = run
    arrived = [e for e in ev if e["type"] == "frame_arrived"]
    base = [e for e in ev if e["type"] == "baseline_arrival"]
    assert len(base) == len(arrived)  # one baseline slot per Orbit slot
    assert all(b["bytes"] == config.FRAME_BYTES for b in base)
    end = ev[-1]
    assert end["orbit"]["frames_down"] == len(arrived) == end["baseline"]["frames_down"]
    assert end["orbit"]["bytes_used"] == end["baseline"]["bytes_used"] == len(arrived) * config.FRAME_BYTES
    assert end["orbit"]["usable_down"] == sum(a["usable"] for a in arrived)
    assert end["baseline"]["usable_down"] == sum(b["usable"] for b in base)
    h = end["headline"]
    assert h["metric"] == "usable frames downlinked" and h["orbit"] == end["orbit"]["usable_down"]
    if h["baseline"]:
        assert h["gain"] == pytest.approx(h["orbit"] / h["baseline"], abs=1e-3)
    # the baseline is round-robin over nodes: no node is served twice while another has frames waiting
    nodes = [b["node_id"] for b in base[:6]]
    assert len(set(nodes)) > 1


def test_window_update_and_node_status(run):
    _, ev = run
    wu = [e for e in ev if e["type"] == "window_update"]
    assert wu and all(w["budget_bytes"] == w["used_bytes"] + w["remaining_bytes"] for w in wu)
    assert all(wu[i]["used_bytes"] <= wu[i + 1]["used_bytes"] for i in range(len(wu) - 1))
    ns = [e for e in ev if e["type"] == "node_status"]
    assert ns and all(
        {"queue_depth", "top_score", "frames_scored", "frames_evicted", "frames_sent", "busy", "link_ok"} <= set(e)
        for e in ns
    )
    assert all(e["link_ok"] for e in ns)
    qw = [e for e in ev if e["type"] == "queue_window"]
    assert qw and all(1 <= len(e["top"]) <= 5 for e in qw)
    evs = [e for e in ev if e["type"] == "node_event"]
    assert any("joined" in e["message"] for e in evs) and any("onboard storage full" in e["message"] for e in evs)
    assert all(e["level"] in ("info", "warn", "error") for e in evs)


def test_fifo_baseline_drops_newest_when_full_and_round_robins():
    b = FifoBaseline(frame_bytes=10)
    for i in range(5):
        b.scored(FrameMeta(0, i, 50.0, 0.1), cap=3)
    assert b.dropped == 2 and [m.frame_id for m in b.queues[0]] == [0, 1, 2]  # newest two never fit
    b.scored(FrameMeta(1, 100, 10.0, 0.9), cap=3)
    taken = [b.take(1000) for _ in range(4)]
    assert [(m.node_id, m.frame_id) for m in taken if m] == [(0, 0), (1, 100), (0, 1), (0, 2)]
    assert b.take(1000) is None and b.take(5) is None


def test_late_joiner_gets_next_node_id_and_an_event(tmp_path):
    sim = run_scenario(
        "late_joiner", 20, seed=1, settings=S.with_overrides(runs_dir=str(tmp_path), expected_sats="sat-a,sat-b")
    )
    ev = [json.loads(x) for x in sim.stream.lines]
    assert [n["label"] for n in ev[0]["nodes"]] == ["sat-a", "sat-b"]
    joined = [e for e in ev if e["type"] == "node_event" and "sat-c joined" in e["message"]]
    assert joined and joined[0]["node_id"] == 2


def test_revoke_and_hard_flag_show_in_node_events(tmp_path):
    sim = run_scenario("revoke", 60, seed=2, settings=S.with_overrides(runs_dir=str(tmp_path)))
    ev = [json.loads(x) for x in sim.stream.lines]
    msgs = [e for e in ev if e["type"] == "node_event" and e["node_id"] == 2]
    assert any("revoked" in e["message"] and e["level"] == "warn" for e in msgs)
    assert any(e["message"].startswith("HARD flag") for e in msgs)
    assert sum(e["message"].startswith("HARD flag") for e in msgs) == 1  # edge-triggered, not every second


def test_websocket_backlog_then_live():
    async def go():
        s = S.with_overrides(stream_port=0)
        stream = EventStream(s, "ws", wall=False)
        stream.start(0.0)
        server = StreamServer(stream, "127.0.0.1", 0, queue_max=100)
        await server.start()
        port = next(iter(server._server.sockets)).getsockname()[1]
        from websockets.asyncio.client import connect

        async with connect(f"ws://127.0.0.1:{port}/") as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert first["type"] == "run_start" and first["seq"] == 1  # backlog on connect
            stream.end(5.0, reason="stopped")
            second = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert second["type"] == "run_end" and second["seq"] == 2
        await server.stop()

    asyncio.run(go())


def test_slow_client_is_dropped_not_waited_for():
    async def go():
        stream = EventStream(S, "slow", wall=False)
        server = StreamServer(stream, "127.0.0.1", 0, queue_max=3)
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=3)

        async def fake_handler():
            await asyncio.sleep(3600)

        task = asyncio.create_task(fake_handler())
        server._clients[q] = task
        for i in range(10):
            stream._emit("node_event", float(i), node_id=None, level="info", message="x")
        await asyncio.sleep(0)
        assert q not in server._clients and stream.seq == 10  # every event still burned its number
        assert task.cancelled() or task.done()  # the client is closed, not stranded
        assert server.dropped_clients == 1

    asyncio.run(go())


def test_run_end_is_the_last_event_even_if_the_bus_keeps_talking(tmp_path):
    """After the window closes satellites still heartbeat; the contract says run_end is last, so the stream goes quiet."""
    from orbit.protocol import messages as M

    sim = run_scenario(
        "nominal", 500, seed=1, settings=S.with_overrides(window_duration_s=10.0, runs_dir=str(tmp_path))
    )
    ev = [json.loads(x) for x in sim.stream.lines]
    assert ev[-1]["type"] == "run_end" and ev[-1]["reason"] == "window_closed"
    n = len(ev)
    buf = M.BufferStats(8, 8 * config.FRAME_BYTES, 1, 7, 12.5)
    sim.stream.on_bus(
        M.Heartbeat(
            "sat-a",
            999,
            0,
            buffer=buf,
            eviction_count=0,
            queue_len=1,
            top_score=50.0,
            top_item_id=1,
            uptime_s=1.0,
            frames_scored=1,
            frames_sent=0,
        ),
        sim.now + 1,
        False,
    )
    sim.stream.on_event("no_bids", {"t": sim.now + 1, "round_id": 999, "excluded": []})
    sim.stream.end(sim.now + 2, reason="stopped")
    assert len(sim.stream.lines) == n and sim.stream.seq == n


# ------------------------------------------------------------------ ground liveness


def _events(stream):
    return [json.loads(x) for x in stream.lines]


def _tick(stream, now, st, **kw):
    stream.tick(
        now, state=kw.get("state", "READY"), rounds=kw.get("rounds", 0), window=WINDOW, bus_stats=st, period_s=1.0
    )


def _bus_alerts(events):
    return [e for e in events if e["type"] == "node_event" and e["node_id"] is None and "bus " in e["message"]]


def test_ground_status_is_periodic_and_says_who_is_linked(run):
    _, ev = run
    gs = [e for e in ev if e["type"] == "ground_status"]
    assert len(gs) >= 5
    for g in gs:
        assert g["uptime_s"] == g["t"] and g["period_s"] == 1.0
        assert g["state"] in ("READY", "BUSY", "COMPLETE", "CLOSED")
        assert set(g["nodes"]) == {"expected", "seen", "linked"}
        assert g["nodes"]["expected"] == 3 and g["nodes"]["linked"] <= g["nodes"]["seen"] <= 3
        w = g["window"]
        assert w["budget_bytes"] == w["used_bytes"] + w["remaining_bytes"] and w["open"] is True
    assert [g["rounds"] for g in gs] == sorted(g["rounds"] for g in gs)  # monotonic: it is a counter
    assert gs[-1]["nodes"]["seen"] == 3  # every satellite was heard from by the end
    # one per tick, and the tick is the simulator's snapshot period
    assert all(round(b["t"] - a["t"], 3) == 1.0 for a, b in itertools.pairwise(gs))


def test_bus_health_reports_rates_and_stays_quiet_on_a_healthy_bus(run):
    _, ev = run
    bh = [e for e in ev if e["type"] == "bus_health"]
    gs = [e for e in ev if e["type"] == "ground_status"]
    assert len(bh) == len(gs)  # the two go out together, every tick
    for b in bh:
        assert b["window_s"] == BUS_HEALTH_WINDOW_S and 0 <= b["measured_s"] <= BUS_HEALTH_WINDOW_S
        assert set(b["dropped"]) == {"malformed", "dup", "oversize", "own"}
        assert set(b["rates_per_min"]) == set(BUS_ALERT_PER_MIN)
        assert b["received"] >= b["delivered"] >= 0
        assert b["alerts"] == []  # the loopback bus in this scenario drops nothing
    assert _bus_alerts(ev) == []
    assert [b["dropped"]["dup"] for b in bh] == sorted(b["dropped"]["dup"] for b in bh)  # totals, not deltas


def test_bus_alert_fires_above_the_threshold_and_only_on_the_edge():
    """One dropped datagram is a rate, not an alert; a sustained flood is one log line, not one a second."""
    stream = EventStream(S, "rates", wall=False)
    stream.start(0.0)
    st = BusStats()
    for t in range(6):
        _tick(stream, float(t), st)
    assert _bus_alerts(_events(stream)) == []
    assert all(e["alerts"] == [] for e in _events(stream) if e["type"] == "bus_health")

    st.dropped_dup = 5  # 5 over 6 s = 50/min, under the 60/min threshold: still silent
    _tick(stream, 6.0, st)
    assert _bus_alerts(_events(stream)) == []

    st.dropped_dup = 12  # 12 over 7 s = 102.9/min: crosses
    _tick(stream, 7.0, st)
    fired = _bus_alerts(_events(stream))
    assert len(fired) == 1 and fired[0]["level"] == "warn" and "dup" in fired[0]["message"]
    alert = [e for e in _events(stream) if e["type"] == "bus_health"][-1]["alerts"]
    assert alert == [{"metric": "dup", "rate_per_min": 102.857, "threshold_per_min": 60.0}]

    st.dropped_dup = 20  # still above: no second warning
    _tick(stream, 8.0, st)
    _tick(stream, 9.0, st)
    assert len(_bus_alerts(_events(stream))) == 1

    _tick(stream, 20.0, st)  # 20 over 20 s = exactly 60/min, which is not *above* the threshold
    back = _bus_alerts(_events(stream))
    assert len(back) == 2 and back[1]["level"] == "info"
    assert [e for e in _events(stream) if e["type"] == "bus_health"][-1]["alerts"] == []


def test_malformed_rate_and_by_type_travel_together():
    stream = EventStream(S, "malformed", wall=False)
    stream.start(0.0)
    st = BusStats()
    _tick(stream, 0.0, st)
    st.dropped_malformed, st.malformed_by_type = 4, {"bid": 3, "heartbeat": 1}
    _tick(stream, 10.0, st)  # 4 over 10 s = 24/min, over the 6/min threshold
    b = [e for e in _events(stream) if e["type"] == "bus_health"][-1]
    assert b["rates_per_min"]["malformed"] == 24.0
    assert b["malformed_by_type"] == {"bid": 3, "heartbeat": 1}
    assert b["alerts"] == [{"metric": "malformed", "rate_per_min": 24.0, "threshold_per_min": 6.0}]


def test_tick_is_silent_before_run_start_and_after_run_end():
    """run_start is the first event and run_end the last; a periodic tick may not break either."""
    stream = EventStream(S, "bounds", wall=False)
    _tick(stream, 0.0, BusStats())
    assert stream.seq == 0 and not stream.lines
    stream.start(0.0)
    _tick(stream, 1.0, BusStats())
    n = stream.seq
    assert n == 3
    stream.end(2.0, reason="stopped")
    _tick(stream, 3.0, BusStats())
    assert stream.seq == n + 1 and _events(stream)[-1]["type"] == "run_end"


def test_a_lossy_bus_alerts_once_in_a_whole_run(tmp_path):
    sim = run_scenario("lossy", 12, seed=42, settings=S.with_overrides(runs_dir=str(tmp_path)))
    ev = _events(sim.stream)
    fired = _bus_alerts(ev)
    assert [e["level"] for e in fired] == ["warn"] and "dup" in fired[0]["message"]
    assert any(b["alerts"] for b in ev if b["type"] == "bus_health")
