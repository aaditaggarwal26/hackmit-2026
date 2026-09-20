"""The simulated satellite behaves like the firmware will: fixed pool, local eviction, bid with a diagnostic
window, transmit on grant, pop only on a positive ack."""

import pytest

from orbit import config, corpus
from orbit.protocol import codec
from orbit.protocol import messages as M
from orbit.sim.satellite import FakeSatellite, FrameBuffer, SatelliteProfile

S = config.Settings(chunk_bytes=4096, bid_window_n=3, sat_heartbeat_ms=100000)


@pytest.fixture(scope="module")
def corp():
    return corpus.load()


def sat(corp, **kw):
    s = FakeSatellite(
        SatelliteProfile("sat-t", capture_period_s=1.0, capture_jitter=0.0, buffer_slots=4, **kw), S, corp
    )
    s.start(0.0)
    return s


def test_frame_buffer_is_a_fixed_pool():
    fb = FrameBuffer(2, 4)
    fb.store(1, b"aaaa")
    fb.store(2, b"bbbb")
    with pytest.raises(MemoryError):
        fb.store(3, b"cccc")
    with pytest.raises(ValueError):
        fb.release(1) or fb.store(3, b"toolong")
    assert bytes(fb.read(2)) == b"bbbb" and fb.free == 1 and fb.stats().occupancy_pct == 50.0
    fb.store(3, b"cccc")
    assert bytes(fb.read(3)) == b"cccc"


def test_capture_fills_then_evicts_or_rejects(corp):
    s = sat(corp)
    out = []
    for t in range(1, 13):
        out += s.on_tick(float(t))
    assert s.counters.captured == 12 and len(s.queue) == 4 and s.buffer.free == 0
    lost = [m for m in out if isinstance(m, M.Eviction)]
    assert len(lost) == 8 == s.counters.evicted + s.counters.rejected
    assert {m.kind for m in lost} <= {"evicted", "rejected"} and all(e["item_id"] for e in s.eviction_log)
    # queue is score-descending and every queued item has a buffer slot
    keys = [k for k, _ in s.queue.cells]
    assert keys == sorted(keys, reverse=True)
    assert set(s.items) == {i for _, i in s.queue.cells}
    # every evicted item was worse than what displaced it; every rejected item was no better than the worst held
    for m in lost:
        if m.kind == "evicted":
            assert m.displaced_by_score > m.score
        else:
            assert m.displaced_by == -1


def test_bid_offers_top_and_diagnostic_window(corp):
    s = sat(corp)
    for t in range(1, 6):
        s.on_tick(float(t))
    [b] = s.on_message(M.OffersOpen("g", 1, 0, round_id=7, window_remaining_bytes=1, collect_ms=1), 5.0)
    assert isinstance(b, M.Bid) and b.round_id == 7 and b.queue_len == 4 and len(b.window) == 3
    assert b.score >= max(e.score for e in b.window)
    assert b.buffer.used == 4 and b.buffer.occupancy_pct == 100.0 and b.eviction_count == 1
    assert b.item_age_s == pytest.approx(5.0 - s.items[b.item_id].captured_at)


def test_idle_satellite_does_not_bid(corp):
    s = sat(corp)
    assert s.on_message(M.OffersOpen("g", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 0.5) == []


def test_grant_transmit_ack_pops_only_on_ok(corp):
    s = sat(corp)
    s.on_tick(1.0)
    [b] = s.on_message(M.OffersOpen("g", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 1.0)
    bd = M.Breakdown(b.score, 0, 0, 0, 0, b.score)
    [begin] = s.on_message(
        M.Grant("g", 2, 0, round_id=1, to="sat-t", item_id=b.item_id, pace_bps=1e9, breakdown=bd), 1.1
    )
    # total_bytes is the RAW frame; the chunks carry the compressed blob and are counted over it
    assert isinstance(begin, M.TxBegin) and begin.total_bytes == config.FRAME_BYTES
    assert begin.enc == codec.ENC_ZLIB and begin.enc_bytes is not None
    assert begin.chunks == codec.chunk_count(begin.enc_bytes, S.chunk_bytes)
    out = s.on_tick(1.2)
    chunks = [m for m in out if isinstance(m, M.TxChunk)]
    assert len(chunks) == begin.chunks and all(c.enc == begin.enc for c in chunks)
    payload = b"".join(c.data for c in chunks)
    assert len(payload) == begin.enc_bytes
    assert codec.decompress(begin.enc, payload, begin.total_bytes) == bytes(s.buffer.read(b.item_id))
    assert isinstance(out[-1], M.TxDone) and s.awaiting_ack == b.item_id
    import hashlib

    assert out[-1].sha256 == hashlib.sha256(bytes(s.buffer.read(b.item_id))).hexdigest()  # what the ground checks
    # while awaiting the ack the satellite neither bids nor pops (a re-sent offer for the same round proves nothing)
    assert s.on_message(M.OffersOpen("g", 3, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 1.3) == []
    s.on_message(
        M.TxAck("g", 4, 0, round_id=1, to="sat-t", item_id=b.item_id, ok=False, bytes_received=0, reason="x"), 1.4
    )
    assert b.item_id in s.items and s.counters.failed == 1 and s.awaiting_ack is None  # frame kept
    [b2] = s.on_message(M.OffersOpen("g", 5, 0, round_id=3, window_remaining_bytes=1, collect_ms=1), 1.5)
    assert b2.item_id == b.item_id  # offered again
    s.on_message(M.Grant("g", 6, 0, round_id=3, to="sat-t", item_id=b.item_id, pace_bps=1e9, breakdown=bd), 1.6)
    s.on_tick(1.7)
    s.on_message(
        M.TxAck("g", 7, 0, round_id=3, to="sat-t", item_id=b.item_id, ok=True, bytes_received=1, reason=""), 1.8
    )
    assert b.item_id not in s.items and len(s.queue) == 0 and s.buffer.free == 4 and s.counters.transmitted == 1


def test_chunks_are_paced_at_the_quoted_rate(corp):
    s = sat(corp)
    s.on_tick(1.0)
    [b] = s.on_message(M.OffersOpen("g", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 1.0)
    bd = M.Breakdown(b.score, 0, 0, 0, 0, b.score)
    s.on_message(M.Grant("g", 2, 0, round_id=1, to="sat-t", item_id=b.item_id, pace_bps=4096 * 8.0, breakdown=bd), 1.0)
    assert len([m for m in s.on_tick(1.0) if isinstance(m, M.TxChunk)]) == 1  # one chunk per second at that rate
    assert len([m for m in s.on_tick(1.5) if isinstance(m, M.TxChunk)]) == 0
    assert len([m for m in s.on_tick(3.0) if isinstance(m, M.TxChunk)]) == 2


def test_revoke_aborts_and_keeps_frame(corp):
    s = sat(corp)
    s.on_tick(1.0)
    [b] = s.on_message(M.OffersOpen("g", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 1.0)
    bd = M.Breakdown(b.score, 0, 0, 0, 0, b.score)
    s.on_message(M.Grant("g", 2, 0, round_id=1, to="sat-t", item_id=b.item_id, pace_bps=1.0, breakdown=bd), 1.0)
    s.on_message(M.Revoke("g", 3, 0, round_id=1, to="sat-t", item_id=b.item_id, reason="tx_timeout"), 2.0)
    assert s.tx is None and b.item_id in s.items and s.counters.revoked == 1


def test_never_transmit_profile_stays_silent(corp):
    s = sat(corp, never_transmit=True)
    s.on_tick(1.0)
    [b] = s.on_message(M.OffersOpen("g", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 1.0)
    bd = M.Breakdown(b.score, 0, 0, 0, 0, b.score)
    assert (
        s.on_message(M.Grant("g", 2, 0, round_id=1, to="sat-t", item_id=b.item_id, pace_bps=1e9, breakdown=bd), 1.0)
        == []
    )
    assert s.counters.grants == 1 and s.tx is None


def test_in_flight_item_is_never_evicted(corp):
    s = sat(corp)
    for t in range(1, 5):
        s.on_tick(float(t))
    worst_id = s.queue.cells[-1][1]
    # pretend the worst item is being transmitted; a much better newcomer must be rejected, not admitted over it
    s.awaiting_ack = worst_id
    from orbit.sim.satellite import Item

    out = s._admit(Item(999, 0, 65535, 4.5), bytes(config.FRAME_BYTES), 4.5)
    assert out and out[0].kind == "rejected" and worst_id in s.items


def test_score_bias_is_clipped(corp):
    s = sat(corp, score_bias=-200.0)
    s.on_tick(1.0)
    [b] = s.on_message(M.OffersOpen("g", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 1.0)
    assert b.score == 0.0


def test_lost_ack_does_not_wedge_the_satellite(corp):
    """The tx_ack datagram can be lost. The satellite must keep the frame (it cannot know) and bid again."""
    s = sat(corp)
    s.on_tick(1.0)
    [b] = s.on_message(M.OffersOpen("g", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 1.0)
    bd = M.Breakdown(b.score, 0, 0, 0, 0, b.score)
    s.on_message(M.Grant("g", 2, 0, round_id=1, to="sat-t", item_id=b.item_id, pace_bps=1e9, breakdown=bd), 1.0)
    s.on_tick(1.1)
    assert s.awaiting_ack == b.item_id
    # the ground opened a later round: it has moved on, the ack is gone
    [b2] = s.on_message(M.OffersOpen("g", 3, 0, round_id=2, window_remaining_bytes=1, collect_ms=1), 1.5)
    assert b2.item_id == b.item_id and s.awaiting_ack is None and s.counters.ack_lost == 1 and b.item_id in s.items
    # timeout path
    s.on_message(M.Grant("g", 4, 0, round_id=2, to="sat-t", item_id=b.item_id, pace_bps=1e9, breakdown=bd), 1.6)
    s.on_tick(1.7)
    assert s.awaiting_ack == b.item_id
    s.on_tick(1.7 + S.sat_ack_timeout_ms / 1000.0 + 0.01)
    assert s.awaiting_ack is None and s.counters.ack_lost == 2 and b.item_id in s.items


def test_late_positive_ack_pops_without_resending(corp):
    """We gave up waiting (ground moved on), then the ok-ack arrives after all: the ground has the frame."""
    s = sat(corp)
    s.on_tick(1.0)
    [b] = s.on_message(M.OffersOpen("g", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 1.0)
    bd = M.Breakdown(b.score, 0, 0, 0, 0, b.score)
    s.on_message(M.Grant("g", 2, 0, round_id=1, to="sat-t", item_id=b.item_id, pace_bps=1e9, breakdown=bd), 1.0)
    s.on_tick(1.1)
    s.on_message(M.OffersOpen("g", 3, 0, round_id=2, window_remaining_bytes=1, collect_ms=1), 1.2)  # gives up, re-bids
    assert s.awaiting_ack is None and b.item_id in s.items
    s.on_message(
        M.TxAck("g", 4, 0, round_id=1, to="sat-t", item_id=b.item_id, ok=True, bytes_received=1, reason=""), 1.21
    )
    assert b.item_id not in s.items and s.counters.late_acks == 1 and s.counters.transmitted == 1 and s.buffer.free == 4
