"""The contact window: one total byte budget for the whole pass.

This is the only time-based quantity in the system, and it is *capacity*, not
allocation. ``capacity_bytes = duration × rate / 8``; each completed transmission
debits one fixed frame; when the next frame no longer fits the window is CLOSED.
There are no per-satellite slices anywhere — who gets each slot is decided by the
arbiter from the value of what is offered, never from whose turn it is.
"""

from __future__ import annotations

from dataclasses import dataclass

from orbit import config
from orbit.protocol.messages import WindowStatus


@dataclass
class ContactWindow:
    duration_s: float = config.DEFAULTS.window_duration_s
    rate_bps: float = config.DEFAULTS.link_rate_bps
    frame_bytes: int = config.FRAME_BYTES
    used_bytes: int = 0
    slots_used: int = 0

    @classmethod
    def from_settings(cls, s: config.Settings) -> ContactWindow:
        return cls(duration_s=s.window_duration_s, rate_bps=s.link_rate_bps, frame_bytes=s.frame_bytes)

    @classmethod
    def for_slots(cls, slots: int, duration_s: float = 10.0, frame_bytes: int = config.FRAME_BYTES) -> ContactWindow:
        """A window that fits exactly ``slots`` frames in ``duration_s`` seconds (demo scaling)."""
        return cls(duration_s=duration_s, rate_bps=slots * frame_bytes * 8 / duration_s, frame_bytes=frame_bytes)

    @property
    def capacity_bytes(self) -> int:
        return int(self.duration_s * self.rate_bps / 8)

    @property
    def remaining_bytes(self) -> int:
        return self.capacity_bytes - self.used_bytes

    @property
    def slots_total(self) -> int:
        return self.capacity_bytes // self.frame_bytes

    @property
    def slots_remaining(self) -> int:
        return self.remaining_bytes // self.frame_bytes

    @property
    def open(self) -> bool:
        return self.remaining_bytes >= self.frame_bytes

    @property
    def scaled(self) -> bool:
        """True unless this is a real pass (600 s at 10 Mbit/s). The display labels scaled windows."""
        return (self.duration_s, self.rate_bps) != (config.REAL_WINDOW_DURATION_S, config.REAL_LINK_RATE_BPS)

    def debit(self, n: int | None = None) -> None:
        n = self.frame_bytes if n is None else n
        if not 0 <= n <= self.remaining_bytes:
            raise ValueError(f"debit {n} exceeds remaining {self.remaining_bytes}")
        self.used_bytes += n
        self.slots_used += 1

    def reset(self) -> None:
        self.used_bytes, self.slots_used = 0, 0

    def status(self) -> WindowStatus:
        return WindowStatus(
            capacity_bytes=self.capacity_bytes,
            used_bytes=self.used_bytes,
            remaining_bytes=self.remaining_bytes,
            slots_remaining=self.slots_remaining,
        )

    def snapshot(self) -> dict[str, float | int | bool]:
        return dict(
            duration_s=self.duration_s,
            rate_bps=self.rate_bps,
            capacity_bytes=self.capacity_bytes,
            used_bytes=self.used_bytes,
            remaining_bytes=self.remaining_bytes,
            slots_total=self.slots_total,
            slots_used=self.slots_used,
            slots_remaining=self.slots_remaining,
            open=self.open,
            scaled=self.scaled,
        )
