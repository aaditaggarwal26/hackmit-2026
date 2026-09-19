"""The modelled contact window: a byte budget, nothing else. Real passes are
minutes at Mbit/s; the demo scales both down so a pass holds a handful of
frames and contention is visible. Every figure derived from this is labelled
'modelled' on the dashboard; the UART never carries these bytes."""
from __future__ import annotations

from dataclasses import dataclass, field

from orbit import params


@dataclass
class ContactWindow:
    duration_s: float = params.WINDOW_DURATION_S
    rate_bps: float = params.LINK_RATE_BPS
    used_bytes: int = 0
    slots_used: int = 0
    grants: list[dict] = field(default_factory=list)

    @property
    def capacity_bytes(self) -> int:
        return int(self.duration_s * self.rate_bps / 8)

    @property
    def remaining_bytes(self) -> int:
        return self.capacity_bytes - self.used_bytes

    @property
    def slots_total(self) -> int:
        return self.capacity_bytes // params.FRAME_BYTES

    @property
    def slots_remaining(self) -> int:
        return self.remaining_bytes // params.FRAME_BYTES

    @property
    def open(self) -> bool:
        return self.remaining_bytes >= params.FRAME_BYTES

    def debit(self, n: int) -> None:
        assert 0 <= n <= self.remaining_bytes, (n, self.remaining_bytes)
        self.used_bytes += n
        self.slots_used += 1

    def reset(self) -> None:
        self.used_bytes, self.slots_used, self.grants = 0, 0, []

    @classmethod
    def for_slots(cls, slots: int, duration_s: float = 10.0) -> "ContactWindow":
        """A window that fits exactly `slots` frames in `duration_s` seconds (demo scaling)."""
        return cls(duration_s=duration_s, rate_bps=slots * params.FRAME_BYTES * 8 / duration_s)

    def snapshot(self) -> dict:
        return dict(duration_s=self.duration_s, rate_bps=self.rate_bps, capacity_bytes=self.capacity_bytes,
                    used_bytes=self.used_bytes, remaining_bytes=self.remaining_bytes, slots_total=self.slots_total,
                    slots_used=self.slots_used, slots_remaining=self.slots_remaining, open=self.open,
                    scaled=(self.duration_s, self.rate_bps) != (params.WINDOW_DURATION_S, params.LINK_RATE_BPS))

