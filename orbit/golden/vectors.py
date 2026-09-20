"""Awkward frames: the inputs most likely to expose a width, border or saturation
bug in the kernel or the queue. Seeded, so the benchmark and the golden tests see
the same bytes."""

from __future__ import annotations

import numpy as np

from orbit import config as params

H, W = params.FRAME_H, params.FRAME_W


def awkward_frames(seed: int = 1) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """(name, frame, ref). Each targets one edge of §5.2 / §5.3."""
    rng = np.random.default_rng(seed)
    z = np.zeros((H, W), np.uint8)
    full = np.full((H, W), 255, np.uint8)
    ramp = np.tile(np.arange(W, dtype=np.uint8) * 2, (H, 1))
    checker = np.indices((H, W)).sum(0) % 2 * 255
    stripes = np.zeros((H, W), np.uint8)
    stripes[:, (np.arange(W) // 2) % 2 == 1] = 255  # 0 0 255 255 ...: |Gx| = 4·255 at every interior pixel
    # (period-2 stripes would give Gx = 0: the taps at x-1 and x+1 share parity)
    border = z.copy()
    border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = 255  # edges only: interior sees them once
    noise = rng.integers(0, 256, (H, W), dtype=np.uint8)
    noise2 = rng.integers(0, 256, (H, W), dtype=np.uint8)
    thr = np.full((H, W), params.CLOUD_THRESHOLD, np.uint8)  # exactly at the threshold: not cloud
    thr1 = thr + 1  # one above: all cloud
    delta = (noise.astype(np.int16) + params.CHANGE_THRESHOLD).clip(0, 255).astype(np.uint8)  # |diff| == thr: unchanged
    delta1 = (noise.astype(np.int16) + params.CHANGE_THRESHOLD + 1).clip(0, 255).astype(np.uint8)
    return [
        ("all_black", z, z),
        ("all_white", full, z),
        ("white_on_white_ref", full, full),
        ("ramp", ramp, z),
        ("checkerboard", checker.astype(np.uint8), z),
        ("stripes_max_sobel", stripes, z),
        ("border_only", border, z),
        ("noise", noise, z),
        ("noise_vs_noise", noise, noise2),
        ("identical_to_ref", noise, noise),
        ("cloud_thr_exact", thr, z),
        ("cloud_thr_plus1", thr1, z),
        ("change_thr_exact", delta, noise),
        ("change_thr_plus1", delta1, noise),
    ]


def random_frame(rng: np.random.Generator, kind: str = "smooth") -> np.ndarray:
    """Corpus-like frames without the corpus: low-frequency terrain with optional cloud."""
    if kind == "noise":
        return rng.integers(0, 256, (H, W), dtype=np.uint8)
    base = rng.normal(128, 40, (H // 8, W // 8))
    img = np.kron(base, np.ones((8, 8)))
    img += rng.normal(0, 6, (H, W))
    if kind == "cloudy":
        mask = np.kron(rng.random((H // 16, W // 16)) > 0.5, np.ones((16, 16)))
        img = np.where(mask, 240 + rng.normal(0, 5, (H, W)), img)
    return img.clip(0, 255).astype(np.uint8)
