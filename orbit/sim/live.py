"""A simulated satellite as a real process on the real multicast bus.

    uv run orbit sat --profile sat-a [--scenario nominal]

This is what the ESP32 firmware replaces: same messages, same behaviour, different
box. Running three of these next to the ground station on one machine exercises the
multicast path, the dedup, the timeouts and the display exactly as the hardware will.
"""

from __future__ import annotations

import asyncio
import logging

from orbit import corpus
from orbit.bus.multicast import MulticastBus
from orbit.config import Settings
from orbit.ground.runtime import Clock, drive
from orbit.ground.station import install_signal_stop
from orbit.log import log
from orbit.sim.satellite import FakeSatellite, SatelliteProfile
from orbit.sim.scenarios import SCENARIOS

lg = logging.getLogger("orbit.sat.live")


def profile_for(scenario: str, hostname: str) -> SatelliteProfile:
    for p in SCENARIOS[scenario].profiles:
        if p.hostname == hostname:
            return p
    return SatelliteProfile(hostname=hostname, seq_seed=sum(map(ord, hostname)))  # a fourth satellite needs no config


async def run_satellite(
    profile: SatelliteProfile, settings: Settings, seed: int, stop: asyncio.Event | None = None
) -> None:
    sat = FakeSatellite(profile, settings, corpus.load(), seed=seed)
    bus = MulticastBus(settings, profile.hostname)
    clock = Clock()
    stop = stop or asyncio.Event()
    log(lg, logging.INFO, "sat_start", hostname=profile.hostname, profile=vars(profile))

    def periodic(now: float) -> None:
        log(lg, logging.INFO, "sat_status", **sat.snapshot(now))

    await bus.start()
    try:
        await drive(sat, bus, clock, stop=stop, periodic=periodic, period_s=5.0)
    finally:
        await bus.stop()
        log(lg, logging.INFO, "sat_stop", hostname=profile.hostname, counters=vars(sat.counters))


async def main_async(profile: SatelliteProfile, settings: Settings, seed: int) -> None:
    stop = asyncio.Event()
    install_signal_stop(stop)
    await run_satellite(profile, settings, seed, stop)
