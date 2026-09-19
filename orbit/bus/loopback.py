"""An in-process bus for the simulator and the tests.

Every endpoint attached to a ``LoopbackHub`` hears every datagram, exactly as on the
multicast group, and the bytes go through the real encoder and decoder so a codec bug
cannot hide behind the simulator. The hub can also misbehave on purpose — duplicate,
drop or reorder datagrams from a seeded RNG — because the arbiter has to survive a
WiFi that does all three, and the simulation is where that gets proven.

Delivery is explicit (``hub.deliver()``) so a virtual-clock driver controls exactly
when in-flight datagrams land.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from orbit.bus.base import Bus


@dataclass
class Faults:
    """Probabilities per datagram. Defaults are a perfect network."""

    duplicate: float = 0.0
    drop: float = 0.0
    reorder: float = 0.0  # chance a datagram is held back one delivery cycle


class LoopbackHub:
    def __init__(self, seed: int = 0, faults: Faults | None = None) -> None:
        self.endpoints: list[LoopbackBus] = []
        self.faults = faults or Faults()
        self.rng = random.Random(seed)
        self._in_flight: list[tuple[LoopbackBus, bytes]] = []
        self._held: list[tuple[LoopbackBus, bytes]] = []
        self.datagrams = 0

    def attach(self, ep: LoopbackBus) -> None:
        self.endpoints.append(ep)

    def _post(self, src: LoopbackBus, data: bytes) -> None:
        self.datagrams += 1
        self._in_flight.append((src, data))

    def deliver(self) -> int:
        """Land everything in flight on every endpoint (including the sender, like multicast
        with loopback on; the endpoint drops its own). Returns datagrams landed."""
        batch, self._in_flight = self._held + self._in_flight, []
        self._held = []
        landed = 0
        f = self.faults
        for src, data in batch:
            if f.drop and self.rng.random() < f.drop:
                continue
            if f.reorder and self.rng.random() < f.reorder:
                self._held.append((src, data))
                continue
            copies = 2 if f.duplicate and self.rng.random() < f.duplicate else 1
            for _ in range(copies):
                for ep in self.endpoints:
                    ep._ingest(data)
                landed += 1
        return landed


class LoopbackBus(Bus):
    def __init__(self, hub: LoopbackHub, hostname: str, dedup_window: int = 4096, max_datagram: int = 1400) -> None:
        super().__init__(hostname, dedup_window, max_datagram)
        self.hub = hub
        hub.attach(self)

    def _transmit(self, data: bytes) -> None:
        self.hub._post(self, data)
