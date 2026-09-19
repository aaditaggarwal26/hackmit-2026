"""The ground state machine driven by hand-built messages under a hand-held clock."""

import pytest

from orbit import config
from orbit.arbiter.fsm import GroundStation
from orbit.protocol import messages as M

S = config.Settings(bid_collect_ms=100, grant_timeout_ms=500, tx_timeout_ms=1000, idle_reopen_ms=200,
                    state_period_ms=100000, window_duration_s=10.0, link_rate_bps=config.FRAME_BYTES * 8 * 3 / 10.0,
                    chunk_bytes=4096)  # a 3-slot window; 4 chunks per frame
G = "ground"
BUF = M.BufferStats(slots=8, capacity_bytes=8 * config.FRAME_BYTES, used=1, free=7, occupancy_pct=12.5)


def bid(host, score, round_id, age=0.0, item_id=1, seq=1):
    return M.Bid(host, seq, 0, round_id=round_id, item_id=item_id, score=score, item_age_s=age, window=(),
                 buffer=BUF, eviction_count=0, queue_len=2)


def types(msgs):
    return [type(m).__name__ for m in msgs]


def open_and_collect(g, now, bids):
    out = g.start(now) if g.round_id == 0 else []
    rid = g.round_id
    for b in bids:
        out += g.on_message(bid(b[0], b[1], rid, *b[2:]), now)
    return rid, out


def transmit(g, host, item_id, rid, now, chunks=4, skip=()):
    out = g.on_message(M.TxBegin(host, 10, 0, round_id=rid, item_id=item_id, total_bytes=config.FRAME_BYTES,
                                 chunks=chunks), now)
    for i in range(chunks):
        if i in skip:
            continue
        out += g.on_message(M.TxChunk(host, 11 + i, 0, round_id=rid, item_id=item_id, idx=i, n=chunks,
                                      data=b"x" * (config.FRAME_BYTES // chunks)), now)
    out += g.on_message(M.TxDone(host, 20, 0, round_id=rid, item_id=item_id, total_bytes=config.FRAME_BYTES,
                                 score=50.0), now)
    return out


def test_ready_collect_grant_busy_complete_ready():
    events = []
    g = GroundStation(S, G, sink=lambda k, p: events.append((k, p)))
    rid, out = open_and_collect(g, 0.0, [("sat-a", 60.0), ("sat-b", 70.0, 0.0, 5)])
    assert types(out) == ["OffersOpen", "State"] and g.state == config.STATE_READY and rid == 1
    assert g.on_tick(0.05) == []  # still collecting
    out = g.on_tick(0.1)
    assert types(out) == ["Grant", "State"]
    grant = out[0]
    assert isinstance(grant, M.Grant) and grant.to == "sat-b" and grant.item_id == 5 and g.state == config.STATE_BUSY
    assert grant.breakdown.total == pytest.approx(70.0 + 0.1 * S.sat_aging_rate)  # 0.1 s of wait since first seen
    # bids while BUSY are ignored
    assert g.on_message(bid("sat-c", 99.0, rid), 0.2) == [] and g.counters.ignored_while_busy == 1
    out = transmit(g, "sat-b", 5, rid, 0.5)
    assert types(out) == ["TxAck", "State", "OffersOpen", "State"]
    ack = out[0]
    assert isinstance(ack, M.TxAck) and ack.ok and ack.bytes_received == config.FRAME_BYTES
    assert g.window.slots_used == 1 and g.state == config.STATE_READY and g.round_id == 2
    assert g.sats["sat-b"].last_tx_complete_s == 0.5 and g.sats["sat-b"].transmissions == 1
    kinds = [k for k, _ in events]
    assert kinds.count("decision") == 1 and "complete" in kinds
    d = next(p for k, p in events if k == "decision")
    assert d["winner"] == "sat-b" and {c["sat"] for c in d["ranked"]} == {"sat-a", "sat-b"}
    assert {"score", "item_age_term", "sat_wait_term", "total"} <= set(d["ranked"][0])


def test_satellite_wait_is_tracked_by_the_ground():
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0), ("sat-b", 60.0)])
    assert g.on_tick(0.1)[0].to == "sat-a"  # tie → alphabetical
    transmit(g, "sat-a", 1, rid, 1.0)
    rid = g.round_id
    for h in ("sat-a", "sat-b"):
        g.on_message(bid(h, 60.0, rid), 1.0)
    grant = g.on_tick(1.1)[0]
    assert isinstance(grant, M.Grant) and grant.to == "sat-b"  # sat-b has waited since 0.0, sat-a since 1.0
    assert grant.breakdown.sat_wait_term == pytest.approx(1.1 * S.sat_aging_rate)
    assert grant.breakdown.sat_wait_term > 0.1 * S.sat_aging_rate


def test_missing_chunks_nack_keeps_frame_and_rearbitrates():
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0), ("sat-b", 70.0)])
    g.on_tick(0.1)
    out = transmit(g, "sat-b", 1, rid, 0.5, skip=(2,))
    assert isinstance(out[0], M.TxAck) and not out[0].ok and "3/4" in out[0].reason
    assert g.window.slots_used == 0 and g.counters.failed_tx == 1
    assert isinstance(out[1], M.Grant) and out[1].to == "sat-a" and out[1].round_id == rid  # same round, sat-b excluded
    assert g.excluded == {"sat-b"}


def test_grant_timeout_revokes_and_rearbitrates():
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0), ("sat-b", 70.0)])
    g.on_tick(0.1)
    assert g.on_tick(0.59) == []
    out = g.on_tick(0.6)
    assert types(out)[:2] == ["Revoke", "Grant"]
    assert out[0].to == "sat-b" and out[0].reason == "grant_timeout" and out[1].to == "sat-a"
    assert g.sats["sat-b"].revokes == 1 and g.counters.revoked == 1
    # sat-a also never transmits: revoke, nobody left, fresh round opens
    out = g.on_tick(1.2)
    assert types(out)[:2] == ["Revoke", "OffersOpen"] and g.round_id == rid + 1 and g.excluded == set()


def test_tx_timeout_after_begin():
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0)])
    g.on_tick(0.1)
    g.on_message(M.TxBegin("sat-a", 1, 0, round_id=rid, item_id=1, total_bytes=config.FRAME_BYTES, chunks=4), 0.2)
    assert g.on_tick(1.1) == []  # tx_timeout is 1.0 s from tx_begin
    out = g.on_tick(1.2)
    assert isinstance(out[0], M.Revoke) and out[0].reason == "tx_timeout"


def test_unexpected_transmission_is_ignored():
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0), ("sat-b", 70.0)])
    g.on_tick(0.1)
    # wrong sender, wrong item, wrong round: none of it counts
    assert transmit(g, "sat-a", 1, rid, 0.5) == []
    assert transmit(g, "sat-b", 9, rid, 0.5) == []
    assert transmit(g, "sat-b", 1, rid + 1, 0.5) == []
    assert g.state == config.STATE_BUSY and g.counters.unexpected == 18 and g.window.slots_used == 0


def test_late_bid_and_no_bids_reopen():
    g = GroundStation(S, G)
    g.start(0.0)
    assert g.on_tick(0.1) == [] and g.counters.no_bid_rounds == 1 and g.state == config.STATE_READY
    assert g.on_message(bid("sat-a", 50.0, 1), 0.15) == [] and g.counters.late_bids == 1
    out = g.on_tick(0.35)
    assert types(out) == ["OffersOpen", "State"] and g.round_id == 2


def test_window_exhaustion_closes():
    g = GroundStation(S, G)
    for n in range(3):
        rid, _ = open_and_collect(g, float(n), [("sat-a", 60.0)])
        g.on_tick(n + 0.1)
        out = transmit(g, "sat-a", 1, rid, n + 0.5)
    assert g.window.slots_used == 3 and not g.window.open
    assert g.state == config.STATE_CLOSED and out[-1].state == config.STATE_CLOSED
    assert g.on_tick(10.0) == [] or all(isinstance(m, M.State) for m in g.on_tick(10.0))


def test_ignores_own_and_other_ground_messages():
    g = GroundStation(S, G)
    g.start(0.0)
    assert g.on_message(M.OffersOpen(G, 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 0.0) == []
    assert g.on_message(M.OffersOpen("other-ground", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 0.0) == []
    assert g.sats == {}


def test_eviction_and_heartbeat_update_records():
    g = GroundStation(S, G)
    g.start(0.0)
    g.on_message(M.Heartbeat("sat-a", 1, 0, buffer=BUF, eviction_count=3, queue_len=0, top_score=-1.0, top_item_id=-1,
                             uptime_s=1.0), 1.0)
    assert g.sats["sat-a"].queue_len == 0 and g.sats["sat-a"].eviction_count == 3
    for i in range(7):
        g.on_message(M.Eviction("sat-a", 2 + i, 0, item_id=i, score=10.0, kind="rejected", displaced_by=-1,
                                displaced_by_score=-1.0), 2.0)
    a = g.assessments(2.0)["sat-a"]
    assert a.memory_pressured and str(a.kind) == "idle"
