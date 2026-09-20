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
