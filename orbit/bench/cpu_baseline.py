"""The identical scoring workload on a CPU (NumPy) or GPU (CuPy), for the
like-for-like energy claim. Same integers as protocol.md §5.2 and the RTL:
cloud count, change count, interior Sobel |Gx|+|Gy|, saturating composite.
Vectorised sensibly (whole-frame slicing, int16/int64), not a strawman: this is
how one would write it in NumPy on purpose. Frames are the committed corpus,
round-robin, against each frame's scene reference.

    uv run python -m orbit.bench.cpu_baseline --seconds 5 [--gpu]
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from orbit import corpus as C, params
from orbit.golden.score import Config, Scored, composite, score_frame


def score_xp(frame, ref, cfg: Config, xp) -> Scored:
    """score_frame with the array module as a parameter (NumPy or CuPy)."""
    p = frame.astype(xp.int64)
    gx = (p[:-2, 2:] - p[:-2, :-2]) + ((p[1:-1, 2:] - p[1:-1, :-2]) << 1) + (p[2:, 2:] - p[2:, :-2])
    gy = (p[2:, :-2] - p[:-2, :-2]) + ((p[2:, 1:-1] - p[:-2, 1:-1]) << 1) + (p[2:, 2:] - p[:-2, 2:])
    sob = int((xp.abs(gx) + xp.abs(gy)).sum())
    cloud = int((frame > cfg.cloud_thr).sum())
    changed = int((xp.abs(frame.astype(xp.int16) - ref.astype(xp.int16)) > cfg.change_thr).sum())
    return composite(cloud, changed, sob, cfg)


def _pairs(corpus: C.Corpus):
    return [(corpus.by_id(i), corpus.by_id(corpus.reference_for(i))) for i in corpus.ids.tolist()]


def run_cpu(seconds: float, cfg: Config = Config()) -> dict:
    """Score corpus frames round-robin for `seconds`; counts only completed frames."""
    corpus = C.load()
    pairs = _pairs(corpus)
    assert all(score_xp(f, r, cfg, np) == score_frame(f, r, cfg) for f, r in pairs[:8])   # same ints as the golden
    t0 = time.perf_counter()
    n = 0
    checksum = 0
    while True:
        f, r = pairs[n % len(pairs)]
        checksum ^= score_xp(f, r, cfg, np).score
        n += 1
        t = time.perf_counter() - t0
        if t >= seconds:
            break
    return dict(workload="score_frame NumPy int64: cloud + change + interior Sobel + composite, 128x128 u8 corpus frames",
                frames=n, seconds=t, frames_per_s=n / t if t else None, window="whole loop; corpus load excluded",
                checksum=checksum, threads=1, corpus_frames=len(pairs))


def run_gpu(seconds: float, cfg: Config = Config()) -> dict:
    try:
        import cupy as xp
    except ImportError:
        return dict(workload="score_frame CuPy", frames=0, seconds=0.0, frames_per_s=None, available=False,
                    note="cupy not available")
    corpus = C.load()
    pairs = [(xp.asarray(f), xp.asarray(r)) for f, r in _pairs(corpus)]
    # warm-up so the kernels are compiled before the timed window
    for f, r in pairs[:4]:
        score_xp(f, r, cfg, xp)
    xp.cuda.Device().synchronize()
    t0 = time.perf_counter()
    n = 0
    while True:
        f, r = pairs[n % len(pairs)]
        score_xp(f, r, cfg, xp)          # the int() reductions synchronise every frame, like a real per-frame decision
        n += 1
        t = time.perf_counter() - t0
        if t >= seconds:
            break
    return dict(workload="score_frame CuPy (same ops, one frame per launch set)", frames=n, seconds=t,
                frames_per_s=n / t if t else None, available=True, window="whole loop; upload + warm-up excluded")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--gpu", action="store_true")
    a = ap.parse_args(argv)
    r = run_gpu(a.seconds) if a.gpu else run_cpu(a.seconds)
    for k, v in r.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
