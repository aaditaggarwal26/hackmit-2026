"""Golden model: NumPy kernel == spec transcription, queue rules, node behaviour
and emission order, SimulatedNode over the transport."""
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from orbit import params
from orbit.golden import worked_example
from orbit.golden.node import ScoringNode, SimulatedNode, rows
from orbit.golden.queue import NO_FRAME, PriorityQueue
from orbit.golden.score import Config, sat16, score_frame, score_frame_ref
from orbit.golden.vectors import awkward_frames, random_frame
from orbit.protocol import messages as M
from orbit.protocol.messages import FrameDecoder, encode
from orbit.protocol.transport import open_transport

ROOT = Path(__file__).resolve().parent.parent
H, W = params.FRAME_H, params.FRAME_W


# ----------------------------------------------------------------------- kernel
@pytest.mark.parametrize("name,frame,ref", awkward_frames(), ids=[n for n, _, _ in awkward_frames()])
def test_numpy_matches_spec_transcription(name, frame, ref):
    assert score_frame(frame, ref) == score_frame_ref(frame, ref)


def test_awkward_edges():
    d = {n: score_frame(f, r) for n, f, r in awkward_frames()}
    assert d["all_black"].cloud_px == 0 and d["all_black"].sobel_sum == 0 and d["all_black"].clear == 65535
    assert d["all_white"].cloud_px == 16384 and d["all_white"].clear == 0 and d["all_white"].changed_px == 16384
    assert d["white_on_white_ref"].changed_px == 0 and d["white_on_white_ref"].change == 0
    assert d["stripes_max_sobel"].sobel_sum == 126 * 126 * 1020          # |Gx| = 4·255 on every interior pixel
    assert d["border_only"].sobel_sum > 0                                # border pixels feed interior windows
    assert d["cloud_thr_exact"].cloud_px == 0 and d["cloud_thr_plus1"].cloud_px == 16384
    assert d["change_thr_exact"].changed_px == 0                         # |diff| == thr is not a change (strict >)
    f1, r1 = dict((n, (f, r)) for n, f, r in awkward_frames())["change_thr_plus1"]
    assert d["change_thr_plus1"].changed_px == int((r1.astype(int) <= 255 - params.CHANGE_THRESHOLD - 1).sum())  # unclipped pixels
    assert d["identical_to_ref"].changed_px == 0


def test_saturation_and_widths():
    f = dict((n, f) for n, f, _ in awkward_frames())["stripes_max_sobel"]
    s = score_frame(f, np.zeros((H, W), np.uint8), Config(sharp_shift=0))
    assert s.sobel_sum < (1 << 25) and s.sharp == 65535                  # saturates, does not wrap
    assert sat16(65535) == 65535 and sat16(65536) == 65535
    s2 = score_frame(np.zeros((H, W), np.uint8), np.zeros((H, W), np.uint8), Config(w_clear=65535, w_sharp=65535, w_change=65535))
    assert s2.score == 65534                                             # (65535·65535) >> 16, no overflow into wrap


@settings(max_examples=25, deadline=None)
@given(st.integers(0, 2**32 - 1), st.sampled_from(["smooth", "cloudy", "noise"]), st.integers(0, 255), st.integers(0, 255),
       st.integers(0, params.SHARP_SHIFT_MAX))
def test_random_frames_match_spec(seed, kind, cloud_thr, change_thr, shift):
    rng = np.random.default_rng(seed)
    f, r = random_frame(rng, kind), random_frame(rng, kind)
    cfg = Config(cloud_thr=cloud_thr, change_thr=change_thr, sharp_shift=shift)
    assert score_frame(f, r, cfg) == score_frame_ref(f, r, cfg)


# ----------------------------------------------------------------------- queue
def test_queue_rules():
    q = PriorityQueue(3)
    assert q.top == (0, NO_FRAME) and not q.has_data
    assert [q.insert(5, 1), q.insert(9, 2), q.insert(5, 3)] == [NO_FRAME] * 3
    assert q.cells == [(9, 2), (5, 1), (5, 3)]
    assert q.insert(4, 4) == 4 and q.evicted == 1 and len(q) == 3        # loser is the newcomer
    assert q.insert(5, 5) == 5                                           # equal to tail: newcomer lost
    assert q.insert(7, 6) == 3 and q.cells == [(9, 2), (7, 6), (5, 1)]   # tail evicted
    assert q.pop() == (9, 2) and q.top == (7, 6)
    assert q.set_limit(1) == 1 and q.cells == [(7, 6)] and q.evicted == 4


@settings(max_examples=50, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 65535), st.integers(0, 65534)), max_size=80), st.integers(1, params.QUEUE_DEPTH))
def test_queue_is_a_stable_bounded_sort(items, limit):
    q = PriorityQueue(limit)
    for s, i in items:
        q.insert(s, i)
    assert len(q) == min(limit, len(items))
    scores = [s for s, _ in q.cells]
    assert scores == sorted(scores, reverse=True)
    # the kept set is the top-`limit` by score; equal scores keep earlier inserts
    kept = sorted(range(len(items)), key=lambda k: (-items[k][0], k))[:limit]
    assert q.cells == [items[k] for k in kept]


# ----------------------------------------------------------------------- node
def _ingest(node, frame, fid, now=0, cls=M.FrameIngest):
    out = []
    for m in rows(frame, fid, cls):
        out += node.handle(m, now)
    return out


def test_node_ingest_score_grant_sequence():
    n = ScoringNode(0)
    f = random_frame(np.random.default_rng(3))
    assert _ingest(n, f, 0, cls=M.RefFrameSet) == [] and n.ref_loaded and n.rows_rx == 128
    out = _ingest(n, f, 7)
    assert len(out) == 1 and isinstance(out[0], M.FrameScored) and out[0].frame_id == 7
    exp = score_frame(f, f)
    assert (out[0].clear, out[0].sharp, out[0].change, out[0].score) == (exp.clear, exp.sharp, exp.change, exp.score)
    assert out[0].evicted_id == NO_FRAME and out[0].queue_depth == 1 and n.frames_scored == 1
    st_ = n.handle(M.StatusQuery(), 0)[0]
    assert st_.flags & M.StatusReply.F_HAS_DATA and st_.top_frame_id == 7 and st_.top_score == exp.score
    # grant with too small a budget: TX_DONE alone, nothing consumed
    [d] = n.handle(M.Grant(slot_id=1, budget_bytes=params.FRAME_BYTES - 1), 0)
    assert isinstance(d, M.TxDone) and d.bytes_consumed == 0 and d.flags & M.TxDone.F_HAS_DATA and d.new_top_score == exp.score
    # grant with budget: TX_FRAME then TX_DONE, queue empty after
    tx, d = n.handle(M.Grant(slot_id=2, budget_bytes=1 << 20), 0)
    assert tx == M.TxFrame(node_id=0, slot_id=2, frame_id=7, score=exp.score, byte_count=params.FRAME_BYTES)
    assert d == M.TxDone(node_id=0, slot_id=2, bytes_consumed=params.FRAME_BYTES, new_top_score=0, flags=0)
    assert n.frames_sent == 1
    [d] = n.handle(M.Grant(slot_id=3, budget_bytes=1 << 20), 0)
    assert d.bytes_consumed == 0 and d.new_top_score == 0


def test_node_partial_rows_and_bad_row():
    n = ScoringNode(1)
    f = random_frame(np.random.default_rng(4))
    for m in rows(f, 3)[:127]:
        assert n.handle(m, 0) == []
    assert n.frames_scored == 0 and n.rows_rx == 127
    assert n.handle(M.FrameIngest(frame_id=3, row=128, pixels=bytes(128)), 0) == [] and n.busy_drops == 1
    [sc] = n.handle(rows(f, 3)[127], 0)
    assert sc.frame_id == 3 and n.frames_scored == 1


def test_node_config_validation_and_queue_shrink():
    n = ScoringNode(0)
    bad = M.ConfigSet(w_clear=1, w_sharp=1, w_change=1, cloud_thr=1, change_thr=1, sharp_shift=params.SHARP_SHIFT_MAX + 1, queue_limit=4)
    [s] = n.handle(bad, 0)
    assert not (s.flags & M.StatusReply.F_CONFIG_VALID) and n.cfg == Config() and n.busy_drops == 1
    bad2 = M.ConfigSet(w_clear=1, w_sharp=1, w_change=1, cloud_thr=1, change_thr=1, sharp_shift=0, queue_limit=0)
    assert not (n.handle(bad2, 0)[0].flags & M.StatusReply.F_CONFIG_VALID)
    for i in range(5):
        _ingest(n, random_frame(np.random.default_rng(i)), i)
    assert len(n.queue) == 5
    good = M.ConfigSet(w_clear=65535, w_sharp=0, w_change=0, cloud_thr=100, change_thr=5, sharp_shift=3, queue_limit=2)
    [s] = n.handle(good, 0)
    assert s.flags & M.StatusReply.F_CONFIG_VALID and n.cfg.w_clear == 65535 and len(n.queue) == 2 and s.frames_evicted == 3
    # eviction on a full queue reports the lost id
    q_ids = [fid for _, fid in n.queue.cells]
    black = np.zeros((H, W), np.uint8)
    [sc] = _ingest(n, black, 99)                          # w_clear only, black frame -> clear=65535 -> max score
    assert sc.evicted_id in q_ids and sc.queue_depth == 2 and n.queue.top[1] == 99


def test_node_bench_and_timers():
    n = ScoringNode(0)
    assert n.handle(M.BenchRun(iterations=0), 0) == [M.BenchDone(node_id=0, iterations=0, cycles=0)]
    assert n.handle(M.BenchRun(iterations=1000), 0) == [M.BenchDone(node_id=0, iterations=1000, cycles=0)]
    assert n.tick(0) == []
    out = n.tick(params.HEARTBEAT_MS)
    assert isinstance(out[0], M.Heartbeat) and isinstance(out[1], M.StatusReply) and isinstance(out[2], M.Power)
    assert out[2].flags == 0                                              # no INA219 on a simulated node
    n.handle(M.Heartbeat(sender=params.ORCH_ID, seq=0, uptime_ms=0), 1000)
    assert n.link_ok
    n.tick(1000 + params.LINK_TIMEOUT_MS + 1)
    assert not n.link_ok


def test_simulated_node_over_transport_with_virtual_clock():
    t = {"ms": 0.0}
    tr = open_transport("sim://5", params.BAUD, None)
    tr.clock = lambda: t["ms"]; tr.t0 = 0.0; tr.node.__init__(5)          # rewire the clock deterministically
    dec = FrameDecoder()
    assert tr.recv(0) == b"\x00"
    f = random_frame(np.random.default_rng(8))
    for m in rows(f, 11):
        tr.send(encode(m))
    got = []
    while (d := tr.recv(0)) is not None:
        got += dec.feed(d)
    assert [type(m) for m in got] == [M.FrameScored] and got[0].frame_id == 11
    t["ms"] = params.HEARTBEAT_MS
    got = []
    while (d := tr.recv(0)) is not None:
        got += dec.feed(d)
    assert [type(m) for m in got] == [M.Heartbeat, M.StatusReply, M.Power]
    assert got[1].node_id == 5 and got[1].top_frame_id == 11
    # a corrupted frame is counted and mirrored into STATUS
    bad = bytearray(encode(M.StatusQuery())); bad[1] ^= 1
    tr.send(bytes(bad)); tr.send(encode(M.StatusQuery()))
    got = []
    while (d := tr.recv(0)) is not None:
        got += dec.feed(d)
    assert len(got) == 1 and got[0].crc_errors + got[0].len_errors + got[0].unknown_type == 1


def test_worked_example_current():
    assert (ROOT / "docs" / "worked_example.md").read_text() == worked_example.render(), \
        "run: uv run python -m orbit.golden.worked_example --md docs/worked_example.md"
