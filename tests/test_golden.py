"""Golden model: NumPy kernel == spec transcription, queue rules, worked example pinned."""

from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orbit import config as params
from orbit.golden import worked_example
from orbit.golden.queue import NO_FRAME, PriorityQueue
from orbit.golden.score import Config, sat16, score_frame, score_frame_ref
from orbit.golden.vectors import awkward_frames, random_frame

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
    assert d["stripes_max_sobel"].sobel_sum == 126 * 126 * 1020  # |Gx| = 4·255 on every interior pixel
    assert d["border_only"].sobel_sum > 0  # border pixels feed interior windows
    assert d["cloud_thr_exact"].cloud_px == 0 and d["cloud_thr_plus1"].cloud_px == 16384
    assert d["change_thr_exact"].changed_px == 0  # |diff| == thr is not a change (strict >)
    _f1, r1 = dict((n, (f, r)) for n, f, r in awkward_frames())["change_thr_plus1"]
    assert d["change_thr_plus1"].changed_px == int(
        (r1.astype(int) <= 255 - params.CHANGE_THRESHOLD - 1).sum()
    )  # unclipped pixels
    assert d["identical_to_ref"].changed_px == 0


def test_saturation_and_widths():
    f = dict((n, f) for n, f, _ in awkward_frames())["stripes_max_sobel"]
    s = score_frame(f, np.zeros((H, W), np.uint8), Config(sharp_shift=0))
    assert s.sobel_sum < (1 << 25) and s.sharp == 65535  # saturates, does not wrap
    assert sat16(65535) == 65535 and sat16(65536) == 65535
    s2 = score_frame(
        np.zeros((H, W), np.uint8), np.zeros((H, W), np.uint8), Config(w_clear=65535, w_sharp=65535, w_change=65535)
    )
    assert s2.score == 65534  # (65535·65535) >> 16, no overflow into wrap


@settings(max_examples=25, deadline=None)
@given(
    st.integers(0, 2**32 - 1),
    st.sampled_from(["smooth", "cloudy", "noise"]),
    st.integers(0, 255),
    st.integers(0, 255),
    st.integers(0, params.SHARP_SHIFT_MAX),
)
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
    assert q.insert(4, 4) == 4 and q.evicted == 1 and len(q) == 3  # loser is the newcomer
    assert q.insert(5, 5) == 5  # equal to tail: newcomer lost
    assert q.insert(7, 6) == 3 and q.cells == [(9, 2), (7, 6), (5, 1)]  # tail evicted
    assert q.pop() == (9, 2) and q.top == (7, 6)
    assert q.set_limit(1) == 1 and q.cells == [(7, 6)] and q.evicted == 4


@settings(max_examples=50, deadline=None)
@given(
    st.lists(st.tuples(st.integers(0, 65535), st.integers(0, 65534)), max_size=80), st.integers(1, params.QUEUE_DEPTH)
)
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


def test_worked_example_current():
    assert (ROOT / "docs" / "worked_example.md").read_text() == worked_example.render(), (
        "run: uv run python -m orbit.golden.worked_example --md docs/worked_example.md"
    )
