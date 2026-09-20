"""The ground state machine driven by hand-built messages under a hand-held clock."""

import hashlib

import pytest

from orbit import config
from orbit.arbiter.fsm import GroundStation
from orbit.protocol import messages as M

S = config.Settings(
    bid_collect_ms=100,
    grant_timeout_ms=500,
    tx_timeout_ms=1000,
    idle_reopen_ms=200,
    state_period_ms=100000,
    window_duration_s=10.0,
    link_rate_bps=config.FRAME_BYTES * 8 * 3 / 10.0,
    chunk_bytes=4096,
)  # a 3-slot window; 4 chunks per frame
G = "ground"
BUF = M.BufferStats(slots=8, capacity_bytes=8 * config.FRAME_BYTES, used=1, free=7, occupancy_pct=12.5)


def bid(host, score, round_id, age=0.0, item_id=1, seq=1):
    return M.Bid(
        host,
        seq,
        0,
        round_id=round_id,
        item_id=item_id,
        score=score,
        item_age_s=age,
        window=(),
        buffer=BUF,
        eviction_count=0,
        queue_len=2,
    )


def types(msgs):
    return [type(m).__name__ for m in msgs]


def open_and_collect(g, now, bids):
    out = g.start(now) if g.round_id == 0 else []
    rid = g.round_id
    for b in bids:
        out += g.on_message(bid(b[0], b[1], rid, *b[2:]), now)
    return rid, out


def transmit(g, host, item_id, rid, now, chunks=4, skip=(), corrupt=False):
    out = g.on_message(
        M.TxBegin(host, 10, 0, round_id=rid, item_id=item_id, total_bytes=config.FRAME_BYTES, chunks=chunks), now
    )
    parts = [bytes([i]) * (config.FRAME_BYTES // chunks) for i in range(chunks)]
    digest = hashlib.sha256(b"".join(parts)).hexdigest()
    for i in range(chunks):
        if i in skip:
            continue
        data = parts[i] if not (corrupt and i == 1) else b"\xff" * len(parts[i])
        out += g.on_message(M.TxChunk(host, 11 + i, 0, round_id=rid, item_id=item_id, idx=i, n=chunks, data=data), now)
    out += g.on_message(
        M.TxDone(
            host,
            20,
            0,
            round_id=rid,
            item_id=item_id,
            total_bytes=config.FRAME_BYTES,
            score=50.0,
            cloud_frac=0.1,
            sha256=digest,
        ),
        now,
    )
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
    assert transmit(g, "sat-b", 1, rid, 0.5, skip=(2,)) == []  # tx_done before the last chunk: stragglers get a grace
    out = g.on_tick(0.5 + S.tx_straggler_ms / 1000.0 + 0.01)
    assert isinstance(out[0], M.TxAck) and not out[0].ok and "3/4" in out[0].reason
    assert g.window.slots_used == 0 and g.counters.failed_tx == 1
    assert isinstance(out[1], M.Grant) and out[1].to == "sat-a" and out[1].round_id == rid  # same round, sat-b excluded
    assert g.excluded == {"sat-b"}


def test_corrupted_frame_is_nacked_and_frame_kept():
    """Every chunk arrives but one is not what the satellite scored: the digest catches it, nothing is debited."""
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0), ("sat-b", 70.0)])
    g.on_tick(0.1)
    out = transmit(g, "sat-b", 1, rid, 0.5, corrupt=True)
    assert isinstance(out[0], M.TxAck) and not out[0].ok and out[0].reason == "digest mismatch"
    assert g.window.slots_used == 0 and g.counters.failed_tx == 1 and g.counters.corrupt_tx == 1
    assert isinstance(out[1], M.Grant) and out[1].to == "sat-a"


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
        rid, _ = open_and_collect(g, float(n), [("sat-a", 60.0, 0.0, n + 1)])
        g.on_tick(n + 0.1)
        out = transmit(g, "sat-a", n + 1, rid, n + 0.5)
    assert g.window.slots_used == 3 and not g.window.open
    assert g.state == config.STATE_CLOSED and out[-1].state == config.STATE_CLOSED
    assert g.on_tick(10.0) == [] or all(isinstance(m, M.State) for m in g.on_tick(10.0))


def test_ignores_own_and_other_ground_messages():
    g = GroundStation(S, G)
    g.start(0.0)
    assert g.on_message(M.OffersOpen(G, 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 0.0) == []
    assert (
        g.on_message(M.OffersOpen("other-ground", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 0.0) == []
    )
    assert g.sats == {}


def test_eviction_and_heartbeat_update_records():
    g = GroundStation(S, G)
    g.start(0.0)
    g.on_message(
        M.Heartbeat(
            "sat-a",
            1,
            0,
            buffer=BUF,
            eviction_count=3,
            queue_len=0,
            top_score=-1.0,
            top_item_id=-1,
            uptime_s=1.0,
            frames_scored=3,
            frames_sent=0,
        ),
        1.0,
    )
    assert g.sats["sat-a"].queue_len == 0 and g.sats["sat-a"].eviction_count == 3
    for i in range(7):
        g.on_message(
            M.Eviction(
                "sat-a", 2 + i, 0, item_id=i, score=10.0, kind="rejected", displaced_by=-1, displaced_by_score=-1.0
            ),
            2.0,
        )
    a = g.assessments(2.0)["sat-a"]
    assert a.memory_pressured and str(a.kind) == "idle"


def test_duplicate_offer_is_reacked_without_a_slot():
    """The satellite never heard tx_ack{ok}; it offers the same frame again. Re-ack it, spend no airtime."""
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0)])
    g.on_tick(0.1)
    transmit(g, "sat-a", 1, rid, 0.5)
    assert g.window.slots_used == 1 and ("sat-a", 1) in g.delivered
    rid2 = g.round_id
    out = g.on_message(bid("sat-a", 60.0, rid2, item_id=1, seq=9), 1.0)
    assert len(out) == 1 and isinstance(out[0], M.TxAck) and out[0].ok and out[0].reason == "already delivered"
    assert g.bids == {} and g.counters.duplicate_offers == 1
    g.on_message(bid("sat-a", 55.0, rid2, item_id=2, seq=10), 1.0)
    assert g.on_tick(1.1)[0].item_id == 2 and g.window.slots_used == 1  # nothing counted twice


def test_lost_tx_begin_is_recovered_from_the_chunks():
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0)])
    g.on_tick(0.1)
    parts = [bytes([i]) * (config.FRAME_BYTES // 4) for i in range(4)]
    for i in range(4):  # no tx_begin at all
        g.on_message(M.TxChunk("sat-a", 11 + i, 0, round_id=rid, item_id=1, idx=i, n=4, data=parts[i]), 0.5)
    out = g.on_message(
        M.TxDone(
            "sat-a",
            20,
            0,
            round_id=rid,
            item_id=1,
            total_bytes=config.FRAME_BYTES,
            score=1.0,
            cloud_frac=0.1,
            sha256=hashlib.sha256(b"".join(parts)).hexdigest(),
        ),
        0.6,
    )
    assert isinstance(out[0], M.TxAck) and out[0].ok and g.window.slots_used == 1


def test_out_of_range_chunk_index_is_ignored():
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0)])
    g.on_tick(0.1)
    g.on_message(M.TxBegin("sat-a", 10, 0, round_id=rid, item_id=1, total_bytes=config.FRAME_BYTES, chunks=4), 0.2)
    for idx in (0, 1, 7, -3):
        g.on_message(M.TxChunk("sat-a", 20 + idx, 0, round_id=rid, item_id=1, idx=idx, n=4, data=b"x" * 4096), 0.3)
    assert g.grant is not None and g.grant.chunks_seen == {0, 1} and g.counters.unexpected == 2


def test_tx_done_overtaking_last_chunk_waits_for_the_straggler():
    g = GroundStation(S, G)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0)])
    g.on_tick(0.1)
    parts = [bytes([i]) * (config.FRAME_BYTES // 4) for i in range(4)]
    digest = hashlib.sha256(b"".join(parts)).hexdigest()
    g.on_message(M.TxBegin("sat-a", 10, 0, round_id=rid, item_id=1, total_bytes=config.FRAME_BYTES, chunks=4), 0.2)
    for i in (0, 1, 2):
        g.on_message(M.TxChunk("sat-a", 11 + i, 0, round_id=rid, item_id=1, idx=i, n=4, data=parts[i]), 0.3)
    done = M.TxDone(
        "sat-a",
        20,
        0,
        round_id=rid,
        item_id=1,
        total_bytes=config.FRAME_BYTES,
        score=1.0,
        cloud_frac=0.1,
        sha256=digest,
    )
    assert g.on_message(done, 0.4) == [] and g.grant is not None and g.grant.done_pending is not None
    assert g.on_tick(0.5) == []  # still within the grace
    out = g.on_message(M.TxChunk("sat-a", 14, 0, round_id=rid, item_id=1, idx=3, n=4, data=parts[3]), 0.6)
    assert isinstance(out[0], M.TxAck) and out[0].ok and g.window.slots_used == 1
    # and when the straggler never comes, the grace expires into a nack
    g2 = GroundStation(S, G)
    rid, _ = open_and_collect(g2, 0.0, [("sat-a", 60.0)])
    g2.on_tick(0.1)
    g2.on_message(M.TxBegin("sat-a", 10, 0, round_id=rid, item_id=1, total_bytes=config.FRAME_BYTES, chunks=4), 0.2)
    g2.on_message(M.TxChunk("sat-a", 11, 0, round_id=rid, item_id=1, idx=0, n=4, data=parts[0]), 0.3)
    g2.on_message(done, 0.4)
    assert g2.on_tick(0.6) == []
    out = g2.on_tick(0.4 + S.tx_straggler_ms / 1000.0 + 0.01)
    assert isinstance(out[0], M.TxAck) and not out[0].ok and g2.window.slots_used == 0


def test_a_raising_sink_cannot_wedge_the_arbiter():
    """Telemetry and the stream are observers. A bug there must cost a log line, never a slot."""

    def bad_sink(kind, payload):
        if kind == "complete":
            raise KeyError("renamed field")

    g = GroundStation(S, G, sink=bad_sink)
    rid, _ = open_and_collect(g, 0.0, [("sat-a", 60.0)])
    g.on_tick(0.1)
    out = transmit(g, "sat-a", 1, rid, 0.5)
    assert isinstance(out[0], M.TxAck) and out[0].ok and g.state == config.STATE_READY and g.round_id == rid + 1
