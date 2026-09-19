"""The scoring kernel, protocol.md §5.2, as integers. `score_frame` is the fast NumPy
form used everywhere; `score_frame_ref` is the per-pixel pure-Python transcription of
the spec that the tests hold NumPy to. Both are pure functions of (frame, ref, config)
and must match the RTL bit for bit."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from orbit import config as params

U16 = 0xFFFF


def sat16(x: int) -> int:
    return U16 if x > U16 else int(x)


@dataclass(frozen=True)
class Config:
    w_clear: int = params.W_CLEAR
    w_sharp: int = params.W_SHARP
    w_change: int = params.W_CHANGE
    cloud_thr: int = params.CLOUD_THRESHOLD
    change_thr: int = params.CHANGE_THRESHOLD
    sharp_shift: int = params.SHARP_SHIFT
    queue_limit: int = params.QUEUE_DEPTH

    def valid(self) -> bool:
        return 1 <= self.queue_limit <= params.QUEUE_DEPTH and 0 <= self.sharp_shift <= params.SHARP_SHIFT_MAX


@dataclass(frozen=True)
class Scored:
    cloud_px: int
    changed_px: int
    sobel_sum: int
    clear: int
    sharp: int
    change: int
    score: int


def composite(cloud_px: int, changed_px: int, sobel_sum: int, cfg: Config) -> Scored:
    """Per-frame tail of the kernel: three saturating normalisations, three multiplies, one shift."""
    clear = sat16((params.FRAME_BYTES - cloud_px) << 2)
    sharp = sat16(sobel_sum >> cfg.sharp_shift)
    change = sat16(changed_px << 2)
    score = sat16((cfg.w_clear * clear + cfg.w_sharp * sharp + cfg.w_change * change) >> 16)
    return Scored(cloud_px, changed_px, sobel_sum, clear, sharp, change, score)


def sobel_sum(frame: np.ndarray) -> int:
    """Σ |Gx|+|Gy| over the interior; ±1/±2 taps as adds and a shift, no multiply."""
    p = frame.astype(np.int64)
    gx = (p[:-2, 2:] - p[:-2, :-2]) + ((p[1:-1, 2:] - p[1:-1, :-2]) << 1) + (p[2:, 2:] - p[2:, :-2])
    gy = (p[2:, :-2] - p[:-2, :-2]) + ((p[2:, 1:-1] - p[:-2, 1:-1]) << 1) + (p[2:, 2:] - p[:-2, 2:])
    return int((np.abs(gx) + np.abs(gy)).sum())


DEFAULT_CONFIG = Config()


def score_frame(frame: np.ndarray, ref: np.ndarray, cfg: Config = DEFAULT_CONFIG) -> Scored:
    assert frame.shape == ref.shape == (params.FRAME_H, params.FRAME_W) and frame.dtype == ref.dtype == np.uint8
    cloud_px = int((frame > cfg.cloud_thr).sum())
    changed_px = int((np.abs(frame.astype(np.int16) - ref.astype(np.int16)) > cfg.change_thr).sum())
    return composite(cloud_px, changed_px, sobel_sum(frame), cfg)


def score_frame_ref(frame: np.ndarray, ref: np.ndarray, cfg: Config = DEFAULT_CONFIG) -> Scored:
    """protocol.md §5.2 line by line, Python ints only. Slow (~0.1 s); tests only."""
    H, W = params.FRAME_H, params.FRAME_W
    p = [[int(frame[y][x]) for x in range(W)] for y in range(H)]
    r = [[int(ref[y][x]) for x in range(W)] for y in range(H)]
    cloud_px = sum(1 for y in range(H) for x in range(W) if p[y][x] > cfg.cloud_thr)
    changed_px = sum(1 for y in range(H) for x in range(W) if abs(p[y][x] - r[y][x]) > cfg.change_thr)
    s = 0
    for y in range(1, H - 1):
        for x in range(1, W - 1):
            gx = ((p[y - 1][x + 1] - p[y - 1][x - 1]) + 2 * (p[y][x + 1] - p[y][x - 1])
                  + (p[y + 1][x + 1] - p[y + 1][x - 1]))
            gy = ((p[y + 1][x - 1] - p[y - 1][x - 1]) + 2 * (p[y + 1][x] - p[y - 1][x])
                  + (p[y + 1][x + 1] - p[y - 1][x + 1]))
            s += abs(gx) + abs(gy)
    return composite(cloud_px, changed_px, s, cfg)


def display(score: int) -> float:
    """0..65535 -> 0..100 for humans; never on the wire."""
    return score * 100.0 / U16
