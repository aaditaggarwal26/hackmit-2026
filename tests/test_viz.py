"""The display is fed by unreliable UDP from a ground that may restart, so these tests feed the
app's ingest with the same bytes the ground would send and check what the page would see."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from viz.server import Store, create_app


def ev(kind: str, **body: Any) -> bytes:
    return json.dumps({"kind": kind, "host": "ground-test", "t": body.pop("t", 1.0), **body}).encode()


def decision(round_id: int, t: float, winner: str = "sat-a", item_id: int = 7, ru: str | None = "sat-b") -> bytes:
    ranked = [
        {
            "sat": winner,
            "item_id": item_id,
            "score": 80.0,
            "item_age_s": 4.0,
            "item_age_term": 2.0,
            "sat_wait_s": 10.0,
            "sat_wait_term": 3.0,
            "total": 85.0,
            "window": [],
            "queue_len": 3,
            "occupancy_pct": 37.5,
            "eviction_count": 0,
        }
    ]
    if ru:
        ranked.append({**ranked[0], "sat": ru, "item_id": 9, "score": 70.0, "total": 75.0})
    return ev(
        "decision",
        t=t,
        round_id=round_id,
        winner=winner,
        item_id=item_id,
        margin=10.0 if ru else None,
        excluded=[],
        ranked=ranked,
    )


def snapshot(round_id: int, t: float, state: str = "READY") -> bytes:
    return ev(
        "snapshot",
        t=t,
        state=state,
        round_id=round_id,
        granted_to="",
        window={"slots_total": 60, "slots_used": 1, "scaled": True},
        counters={"rounds": round_id},
        sats={
            "sat-a": {
                "queue_len": 3,
                "top_score": 80.0,
                "window": [[7, 80.0, 4.0]],
                "flags": {"kind": "starved", "level": "soft", "memory_pressured": False, "silent": False},
            }
        },
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(telemetry_port=0)) as c:  # port 0: the OS picks a free UDP port
        yield c


def ingest(client: TestClient, *datagrams: bytes) -> None:
    for d in datagrams:
        client.app.state.ingest(d)  # type: ignore[attr-defined]


def test_state_shape_and_snapshot(client: TestClient) -> None:
    ingest(client, snapshot(3, 5.0), ev("flags", t=5.0, sats={"sat-a": {"kind": "idle", "level": "none"}}))
    s = client.get("/api/state").json()
    assert set(s) >= {"snapshot", "decisions", "recent_events", "flags", "stats", "bus"}
    assert s["snapshot"]["round_id"] == 3 and s["snapshot"]["state"] == "READY"
    assert s["flags"]["sat-a"]["kind"] == "idle"
    st = s["stats"]
    assert st["datagrams"] == 2 and st["bad_datagrams"] == 0 and st["by_kind"] == {"snapshot": 1, "flags": 1}
    assert st["connected_clients"] == 0 and st["age_s"] is not None and st["age_s"] < 5
    assert len(s["recent_events"]) == 2


def test_garbage_is_counted_not_raised(client: TestClient) -> None:
    ingest(client, b"\xff\xfe not json", b"[1,2,3]", b'{"no_kind": 1}', b'{"kind": 5}', ev("sat_seen", sat="sat-z"))
    st = client.get("/api/state").json()["stats"]
    assert st["datagrams"] == 5 and st["bad_datagrams"] == 4 and st["by_kind"] == {"sat_seen": 1}


def test_missing_keys_do_not_crash(client: TestClient) -> None:
    ingest(
        client,
        b'{"kind": "decision"}',
        b'{"kind": "snapshot"}',
        b'{"kind": "complete"}',
        b'{"kind": "revoke"}',
        b'{"kind": "bus"}',
        b'{"kind": "flags", "sats": 3}',
        b'{"kind": "eviction", "t": "soon"}',
        b'{"kind": "decision", "ranked": "nope", "round_id": "x"}',
    )
    s = client.get("/api/state").json()
    assert s["stats"]["bad_datagrams"] == 0 and len(s["decisions"]) == 2
    assert s["decisions"][0]["outcome"] == "pending" and s["decisions"][0]["winner"] is None


def test_outcome_join_by_round_and_item() -> None:
    store = Store()
    for d in (decision(1, 1.0, "sat-a", 7), decision(2, 3.0, "sat-b", 9), decision(3, 5.0, "sat-c", 11)):
        store.ingest(d)
    store.ingest(ev("complete", t=2.0, sat="sat-a", item_id=7, score=80.0, bytes=16384, window={}))
    store.ingest(ev("revoke", t=4.0, sat="sat-b", item_id=9, reason="grant_timeout", round_id=2))
    store.ingest(ev("tx_failed", t=6.0, sat="sat-c", item_id=11, reason="received 3/19 chunks"))
    outcomes = {d["round_id"]: (d["outcome"], d["reason"]) for d in store.decisions}
    assert outcomes == {1: ("complete", ""), 2: ("revoked", "grant_timeout"), 3: ("failed", "received 3/19 chunks")}
    row = store.decisions[0]
    assert row["winner"] == "sat-a" and row["score"] == 80.0 and row["item_age_term"] == 2.0
    assert (
        row["sat_wait_term"] == 3.0 and row["total"] == 85.0 and row["runner_up"] == "sat-b" and row["margin"] == 10.0
    )
    # a revoke for a round we never saw the decision of, and a stray complete, are simply ignored
    store.ingest(ev("revoke", t=7.0, sat="sat-a", item_id=1, reason="tx_timeout", round_id=99))
    store.ingest(ev("complete", t=7.0, sat="sat-q", item_id=1))
    assert len(store.decisions) == 3


def test_rearbitration_same_round_and_duplicates() -> None:
    """After a revoke the ground re-arbitrates in the *same* round: two decisions, one round_id.
    An exact duplicate datagram must not add a third."""
    store = Store()
    store.ingest(decision(5, 10.0, "sat-a", 7))
    store.ingest(ev("revoke", t=11.0, sat="sat-a", item_id=7, reason="grant_timeout", round_id=5))
    store.ingest(decision(5, 11.0, "sat-b", 9))
    store.ingest(decision(5, 11.0, "sat-b", 9))
    assert [(d["winner"], d["outcome"]) for d in store.decisions] == [("sat-a", "revoked"), ("sat-b", "pending")]
    store.ingest(ev("complete", t=12.0, sat="sat-b", item_id=9))
    assert store.decisions[1]["outcome"] == "complete"


def test_ground_restart_resets_decisions_and_tolerates_reorder() -> None:
    store = Store()
    for r in range(1, 6):
        store.ingest(decision(r, float(r)))
    store.ingest(snapshot(5, 5.5))
    store.ingest(decision(4, 4.1))  # one step out of order: not a restart, and not a new row (dedupe is by (round, t))
    assert store.restarts == 0 and len(store.decisions) == 6
    store.ingest(snapshot(1, 0.2))  # round_id went backwards: the ground rebooted
    assert store.restarts == 1 and list(store.decisions) == [] and store.snapshot is not None
    assert store.snapshot["round_id"] == 1
    store.ingest(decision(1, 0.3))
    assert len(store.decisions) == 1
    # a stale snapshot from the *old* incarnation is not a second restart either (its round is higher)
    store.ingest(snapshot(6, 6.0))
    assert store.restarts == 1


def test_restart_detected_by_ground_time_going_back() -> None:
    """`orbit ground` restarted quickly: round_id is 1 again but so was the last one seen; ground time
    (seconds since the process started) is the tell."""
    store = Store()
    store.ingest(snapshot(2, 40.0))
    store.ingest(decision(2, 40.5))
    store.ingest(snapshot(1, 0.5))
    assert store.restarts == 1 and not store.decisions


def test_old_snapshot_does_not_roll_back() -> None:
    store = Store()
    store.ingest(snapshot(3, 5.0, "BUSY"))
    store.ingest(snapshot(3, 4.0, "READY"))  # late duplicate from a second earlier
    assert store.snapshot is not None and store.snapshot["state"] == "BUSY"


def test_bus_log_aggregates_chunks_and_excludes_them_from_recent() -> None:
    store = Store()
    store.ingest(ev("bus", t=1.0, dir="in", **{"from": "sat-a"}, type="tx_begin", seq=1, item_id=7, round_id=1))
    for i in range(19):
        store.ingest(
            ev(
                "bus",
                t=1.0 + i / 100,
                dir="in",
                **{"from": "sat-a"},
                type="tx_chunk",
                seq=2 + i,
                item_id=7,
                idx=i,
                n=19,
                bytes=900,
            )
        )
    store.ingest(ev("bus", t=1.5, dir="out", **{"from": "ground-test"}, type="tx_ack", seq=30, item_id=7, ok=True))
    types = [(b["type"], b.get("count")) for b in store.bus]
    assert types == [("tx_begin", None), ("tx_chunk", 19), ("tx_ack", None)]
    assert [r["type"] for r in store.recent] == ["tx_begin", "tx_ack"]
    assert store.by_kind["bus"] == 21


def test_rates_picked_up_when_present() -> None:
    store = Store()
    assert store.state()["rates"] is None
    push = store.apply({"kind": "settings", "t": 0.0, "item_aging_rate": 0.5, "sat_aging_rate": 0.3})
    assert push["rates"] == {"item_aging_rate": 0.5, "sat_aging_rate": 0.3}
    assert store.state()["rates"] == {"item_aging_rate": 0.5, "sat_aging_rate": 0.3}


def test_backlog_is_bounded() -> None:
    store = Store(backlog=10)
    for i in range(50):
        store.ingest(ev("sat_seen", t=float(i), sat=f"s{i}"))
    assert len(store.events) == 10 and store.datagrams == 50


def test_websocket_receives_state_then_pushes(client: TestClient) -> None:
    ingest(client, snapshot(7, 30.0))
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["snapshot"]["round_id"] == 7
        assert first["stats"]["connected_clients"] == 1
        ingest(client, decision(7, 30.5))
        push = ws.receive_json()
        assert push["type"] == "event" and push["event"]["kind"] == "decision"
        assert push["decisions"][0]["outcome"] == "pending" and "reset" not in push
        ingest(client, ev("complete", t=31.0, sat="sat-a", item_id=7))
        push = ws.receive_json()
        assert push["event"]["kind"] == "complete" and push["decisions"][0]["outcome"] == "complete"
        ingest(client, snapshot(1, 0.1))  # restart: the page is told to clear its table
        push = ws.receive_json()
        assert push.get("reset") is True and push["event"]["kind"] == "snapshot"
    assert client.get("/api/state").json()["stats"]["connected_clients"] == 0


def test_index_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "Orbit" in r.text and "/ws" in r.text
    assert "GROUND UNREACHABLE" in r.text and 'id="watchdog"' in r.text  # the laptop's own liveness check
    # observe-only: the page opens one socket and one GET, and has no other way to reach the network
    assert r.text.count("new WebSocket(") == 1 and r.text.count("fetch(") == 1
    assert "sendto" not in r.text and "ws.send" not in r.text


def test_real_udp_path(client: TestClient) -> None:
    """The listener really is bound: a datagram to the chosen port lands in the store."""
    import socket
    import time

    port = client.app.state.udp_port  # type: ignore[attr-defined]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(ev("sat_seen", sat="sat-udp"), ("127.0.0.1", port))
    for _ in range(100):
        if client.get("/api/state").json()["stats"]["by_kind"].get("sat_seen"):
            break
        time.sleep(0.02)
    else:
        pytest.fail("datagram never arrived")


def test_replay_carries_a_recorded_run_including_the_liveness_events(tmp_path) -> None:
    """A recorded run is the rehearsal for the laptop's watchdog, so the periodic ground_status /
    bus_health lines have to come back out of the file in order, with their pacing intact."""
    from orbit import config
    from orbit.sim.run import run_scenario
    from viz.replay import load, replay

    run_scenario("nominal", 8, seed=42, settings=config.Settings(runs_dir=str(tmp_path)), run_id="r")
    events = load(tmp_path / "r.jsonl")
    assert events and [t for t, _ in events] == sorted(t for t, _ in events)
    by_type: dict[str, list[float]] = {}
    for t, raw in events:
        e = json.loads(raw)
        assert e["t"] == t  # paced by the event's own ground time, not by file position
        by_type.setdefault(str(e["type"]), []).append(t)
    assert len(by_type["ground_status"]) == len(by_type["bus_health"]) >= 5
    assert by_type["ground_status"] == sorted(by_type["ground_status"])
    assert by_type["ground_status"][:3] == [0.0, 1.0, 2.0]  # one per ground tick, as the contract promises

    sent: list[bytes] = []

    class FakeSocket:  # replay only ever sends; a real socket would just add a kernel round trip
        def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
            sent.append(data)

    assert replay(events, ("127.0.0.1", 0), 1e6, FakeSocket()) == len(events)  # type: ignore[arg-type]
    assert sent == [raw for _, raw in events]  # byte for byte, nothing interpreted on the way out


# --- the event stream (`type`) arriving where telemetry (`kind`) is expected --------------------


@pytest.fixture
def run_file(tmp_path: Any) -> Any:
    """A real recorded run, in the event-stream shape `runs/<run_id>.jsonl` holds."""
    from orbit import config
    from orbit.sim.run import run_scenario

    run_scenario("nominal", 12, seed=42, settings=config.Settings(runs_dir=str(tmp_path)), run_id="r")
    return tmp_path / "r.jsonl"


def test_a_recorded_run_replays_into_the_dashboard(run_file: Any) -> None:
    """The whole point: a run file at the telemetry port reaches the panels instead of the floor.
    Every number below comes out of the file; nothing here is a shape the ground never emitted."""
    from viz.replay import load

    store = Store()
    for _t, raw in load(run_file):
        store.ingest(raw)

    st = store.state()["stats"]
    assert st["datagrams"] > 200 and st["bad_datagrams"] == 0  # a run file is not garbage
    assert st["by_type"]["grant"] == st["by_type"]["frame_arrived"] == 12
    assert st["by_kind"]["decision"] == st["by_kind"]["complete"] == 12  # grant -> decision, arrival -> complete
    assert st["by_kind"]["snapshot"] == st["by_type"]["ground_status"] >= 5  # the ground heartbeat, 1:1

    snap = store.snapshot
    assert snap is not None
    assert sorted(snap["sats"]) == ["sat-a", "sat-b", "sat-c"]  # the roster, by label, from run_start
    assert snap["state"] in ("READY", "BUSY", "COMPLETE", "CLOSED") and snap["round_id"] >= 1
    assert snap["counters"]["rounds"] == snap["round_id"]
    a = snap["sats"]["sat-a"]
    assert a["queue_len"] >= 0 and a["transmissions"] >= 0 and len(a["window"][0]) == 3
    w = snap["window"]
    assert w["slots_used"] + w["slots_remaining"] == w["slots_total"] and w["capacity_bytes"] == 983040
    assert w["used_bytes"] + w["remaining_bytes"] == w["capacity_bytes"]

    assert len(store.decisions) == 12 and {d["outcome"] for d in store.decisions} == {"complete"}
    d = store.decisions[0]
    assert d["winner"] in snap["sats"] and d["ranked"][0]["sat"] == d["winner"]  # winner first, as telemetry ranks
    assert (d["score"], d["total"]) == (d["ranked"][0]["score"], d["ranked"][0]["total"])  # the bid's own terms
    assert d["total"] == pytest.approx(d["score"] + d["item_age_term"] + d["sat_wait_term"])
    assert store.state()["rates"] == {"item_aging_rate": 0.5, "sat_aging_rate": 0.3}  # from run_start.priority
    bs = store.bus_stats
    assert bs is not None and bs["received"] > 0 and bs["dropped_malformed"] == 0  # from bus_health


def test_the_stream_types_with_no_telemetry_home_are_counted_not_rendered(run_file: Any) -> None:
    """`node_event` is prose, and the rest have no panel. They are accepted and counted so an
    operator can see they arrived, and nothing is guessed back out of them."""
    from viz.replay import load

    store = Store()
    for _t, raw in load(run_file):
        store.ingest(raw)
    assert {"node_event", "frame_scored", "baseline_arrival", "run_end"} <= set(store.by_type)
    assert set(store.by_kind) == {"settings", "snapshot", "bus_stats", "decision", "complete"}
    assert not store.bus  # the stream carries no bus mirror, so the bus log stays honestly empty
    assert not store.flags  # nor any flag assessment


def test_an_accepted_line_feeds_the_watchdog_even_when_it_renders_nowhere() -> None:
    """The page calls the ground unreachable after a few seconds of silence. A `node_event` draws
    no panel, but it is still the ground speaking, so it must not look like silence."""
    store = Store()
    store.ingest(b'{"seq": 1, "t": 1.0, "type": "node_event", "node_id": 0, "level": "info", "message": "hi"}')
    assert store.bad_datagrams == 0 and store.last_rx_wall is not None
    assert store.state()["stats"]["age_s"] is not None


def test_a_telemetry_bus_event_is_never_mistaken_for_a_stream_line() -> None:
    """Telemetry `bus` events carry a bus-message `type` of their own; `kind` decides the envelope."""
    store = Store()
    store.ingest(ev("bus", t=1.0, dir="in", **{"from": "sat-a"}, type="tx_chunk", seq=1, item_id=7, idx=0, n=2))
    store.ingest(ev("bus", t=1.1, dir="in", **{"from": "sat-a"}, type="tx_begin", seq=2, item_id=7))
    assert [b["type"] for b in store.bus] == ["tx_chunk", "tx_begin"] and store.by_kind["bus"] == 2
    assert not store.by_type and store.bad_datagrams == 0


def test_bad_datagrams_still_counts_real_corruption() -> None:
    """The second envelope must not blind the counter that exists to catch a bad byte. Corruption
    is: not JSON, not an object, or neither tag a string — a run-file line is none of those."""
    store = Store()
    corrupt = (
        b"\xff\xfe not json",
        b'{"seq": 1, "t": 0.0, "type": "grant"',  # truncated mid-datagram
        b"[1, 2, 3]",
        b"{}",
        b'{"kind": 5}',
        b'{"type": 5}',  # a number where the tag should be
        b'{"seq": 1, "t": 0.0}',  # an envelope with neither tag
        b'{"type": null, "kind": null}',
    )
    for d in corrupt:
        store.ingest(d)
    assert store.datagrams == store.bad_datagrams == len(corrupt)
    assert not store.by_kind and not store.by_type
    # and a good line on either side of the garbage still lands
    store.ingest(b'{"seq": 2, "t": 0.0, "type": "run_start", "priority": {"item_aging_rate": 0.5}}')
    store.ingest(ev("sat_seen", sat="sat-a"))
    assert store.bad_datagrams == len(corrupt) and store.by_kind["sat_seen"] == 1


def test_unknown_and_half_written_stream_events_do_not_crash() -> None:
    """The stream gains event types (`ground_status` and `bus_health` are new). An unknown one is
    counted and shown nowhere, exactly as an unknown `kind` already was, and a field of the wrong
    type is survived — the display is the last thing that may fall over mid-demo."""
    store = Store()
    for raw in (
        b'{"type": "something_new", "t": 1.0, "payload": {"a": 1}}',
        b'{"type": "run_start"}',
        b'{"type": "run_start", "nodes": "not a list", "window": 7, "priority": null}',
        b'{"type": "grant"}',
        b'{"type": "grant", "bids": "nope", "slot_id": "x", "node_id": "y"}',
        b'{"type": "node_status", "node_id": 0, "label": "sat-a", "queue_depth": "deep"}',
        b'{"type": "queue_window", "node_id": 0, "top": [1, 2, {"frame_id": 3}]}',
        b'{"type": "ground_status", "t": 2.0, "window": null, "rounds": null}',
        b'{"type": "bus_health", "t": 2.0, "dropped": 4}',
        b'{"type": "frame_arrived", "t": 3.0}',
        b'{"type": "window_update", "t": 4.0, "budget_bytes": null}',
    ):
        store.ingest(raw)
    assert store.bad_datagrams == 0 and store.by_type["something_new"] == 1
    assert store.snapshot is not None and store.snapshot["sats"]["sat-a"]["window"] == [[3, None, None]]
    assert len(store.decisions) == 2 and store.decisions[0]["ranked"] == []


def test_a_run_recorded_before_the_ground_heartbeat_still_draws_the_panels() -> None:
    """`ground_status` was added late; the runs recorded before it have none. The node state still
    has to reach the page, so it is flushed on the cadence the ground uses for `snapshot`."""
    store = Store()
    lines = [b'{"seq": 1, "t": 0.0, "type": "run_start", "nodes": [{"node_id": 0, "label": "sat-a"}]}']
    for i in range(6):
        lines.append(
            b'{"seq": 2, "t": %.1f, "type": "node_status", "node_id": 0, "queue_depth": %d, "top_score": 50.0}'
            % (float(i), i)
        )
    for raw in lines:
        store.ingest(raw)
    assert "ground_status" not in store.by_type and store.by_kind["snapshot"] == 6
    assert store.snapshot is not None and store.snapshot["sats"]["sat-a"]["queue_len"] == 5
    assert store.snapshot["state"] is None and store.snapshot["round_id"] is None  # never claimed, never invented


def test_replayed_run_reaches_a_browser_over_the_real_sockets(client: TestClient, run_file: Any) -> None:
    """End to end through the app the browser talks to: UDP in, `/api/state` and `/ws` out."""
    import socket
    import time

    from viz.replay import load

    lines = load(run_file)
    port = client.app.state.udp_port  # type: ignore[attr-defined]
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "state"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _t, raw in lines[:40]:
                s.sendto(raw, ("127.0.0.1", port))
        for _ in range(200):  # loopback UDP may still drop; wait for the first decision to arrive
            if client.get("/api/state").json()["decisions"]:
                break
            time.sleep(0.02)
        else:
            pytest.fail("nothing from the run file reached the store")
    st = client.get("/api/state").json()
    assert st["stats"]["bad_datagrams"] == 0 and st["snapshot"]["sats"]
    assert st["decisions"][0]["winner"].startswith("sat-")


def test_a_looped_replay_reads_as_one_ground_restart(run_file: Any) -> None:
    """`viz.replay --loop` starts the file again; the page must be told to clear its table rather
    than grow a second run's worth of rows underneath the first."""
    from viz.replay import load

    lines = load(run_file)
    store = Store()
    for _t, raw in lines + lines:
        store.ingest(raw)
    assert store.restarts == 1 and len(store.decisions) == 12  # not 24
    assert store.snapshot is not None and sorted(store.snapshot["sats"]) == ["sat-a", "sat-b", "sat-c"]
