"""The benchmark workload: the §5.2 scoring kernel, once through NumPy and once through
torch, both held to the golden model before any timing starts.

Why two implementations of one kernel: the CPU and GPU tiers must do *identical*
integer work or the J/frame comparison is meaningless. `score_numpy` is the golden
`score_frame` itself; `score_torch` re-derives the same per-pixel counts with int64
tensor ops and then hands the three integers to the golden `composite`, so the
thresholds, shift and saturation are literally the same code on both tiers. One
device→host sync per frame (`.tolist()`) is deliberate: a real node makes a decision
per frame, so a batched, sync-free GPU number would flatter the GPU."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

from orbit.corpus import Corpus
from orbit.golden.score import Config, composite, score_frame
from orbit.golden.vectors import awkward_frames
from orbit.log import log

if TYPE_CHECKING:
    import torch
else:  # torch is an optional extra; the GPU tier reports *why* it is unavailable if it is missing
    try:
        import torch
    except ImportError:
        torch = None

logger = logging.getLogger("orbit.bench.workload")

Frame = npt.NDArray[np.uint8]
Pair = tuple[str, Frame, Frame]  # (label, frame, reference)

DESCRIPTION = (
    "protocol.md §5.2 scoring kernel per 128x128 uint8 frame: cloud count (px > CLOUD_THRESHOLD), "
    "change count (|frame-ref| > CHANGE_THRESHOLD), interior 3x3 Sobel sum |Gx|+|Gy|, saturating composite; "
    "one frame scored per call, one host sync per frame"
)


class WorkloadMismatch(AssertionError):
    """A tier's kernel disagreed with the golden model; its numbers must not be reported."""


def score_numpy(frame: Frame, ref: Frame, cfg: Config) -> int:
    return score_frame(frame, ref, cfg).score


def score_torch(frame_t: torch.Tensor, ref_t: torch.Tensor, cfg: Config, device: torch.device) -> int:
    """Same taps and thresholds as `orbit.golden.score`, in int64 so nothing wraps; `device`
    is unused for math and kept in the signature so a caller cannot forget where the tensors live."""
    p = frame_t.to(torch.int64)
    r = ref_t.to(torch.int64)
    gx = (p[:-2, 2:] - p[:-2, :-2]) + ((p[1:-1, 2:] - p[1:-1, :-2]) << 1) + (p[2:, 2:] - p[2:, :-2])
    gy = (p[2:, :-2] - p[:-2, :-2]) + ((p[2:, 1:-1] - p[:-2, 1:-1]) << 1) + (p[2:, 2:] - p[:-2, 2:])
    counts = torch.stack(
        ((p > cfg.cloud_thr).sum(), ((p - r).abs() > cfg.change_thr).sum(), (gx.abs() + gy.abs()).sum())
    )
    cloud_px, changed_px, sobel = (int(v) for v in counts.tolist())  # the one sync per frame
    return composite(cloud_px, changed_px, sobel, cfg).score


def pairs(corpus: Corpus) -> list[Pair]:
    """Every corpus frame with its scene's reference, id order; the same list on every tier."""
    return [(f"corpus:{i}", corpus.by_id(i), corpus.by_id(corpus.reference_for(i))) for i in range(len(corpus.ids))]


def identity_pairs(corpus: Corpus, n: int) -> list[Pair]:
    """`n` corpus pairs spread over the whole corpus (not the first n: scenes are contiguous)
    plus the awkward frames, which hit the saturation and threshold edges the corpus does not."""
    allp = pairs(corpus)
    step = max(1, len(allp) // max(1, n))
    return allp[::step][:n] + [(f"awkward:{name}", f, r) for name, f, r in awkward_frames()]


def cuda_device() -> torch.device:
    """Import torch and run one trial int64 kernel; any failure propagates with its text so the
    runner can record *why* the GPU tier is unavailable rather than a bare 'no'."""
    if torch is None:
        raise RuntimeError("torch is not installed (uv sync --extra gpu)")
    if not torch.cuda.is_available():
        raise RuntimeError("torch.cuda.is_available() is False")
    dev = torch.device("cuda", 0)
    trial = torch.arange(16, dtype=torch.int64, device=dev)
    if int((trial * 2).sum().item()) != 240:
        raise RuntimeError("trial int64 kernel returned the wrong sum")
    return dev


def assert_identical(corpus: Corpus, cfg: Config, n: int = 32, device: torch.device | None = None) -> dict[str, Any]:
    """numpy == golden on every checked pair, and torch == golden when a device is given.
    Raises WorkloadMismatch on the first disagreement: a fast wrong kernel is worse than none."""
    checked = identity_pairs(corpus, n)
    for label, f, r in checked:
        golden = score_frame(f, r, cfg).score
        got = score_numpy(f, r, cfg)
        if got != golden:
            raise WorkloadMismatch(f"numpy {label}: {got} != golden {golden}")
    if device is not None:
        for label, f, r in checked:
            golden = score_frame(f, r, cfg).score
            ft = torch.from_numpy(np.ascontiguousarray(f)).to(device)
            rt = torch.from_numpy(np.ascontiguousarray(r)).to(device)
            got = score_torch(ft, rt, cfg, device)
            if got != golden:
                raise WorkloadMismatch(f"torch {label}: {got} != golden {golden}")
    log(logger, logging.INFO, "workload identity ok", frames=len(checked), torch=device is not None)
    # None = torch was not checked (CPU tier); a mismatch never returns, it raises WorkloadMismatch above.
    return {
        "frames_checked": len(checked),
        "numpy_matches_golden": True,
        "torch_matches_golden": True if device is not None else None,
    }
