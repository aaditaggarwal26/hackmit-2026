"""The live ground station process: arbiter on the multicast bus, telemetry to the display.

    uv run orbit ground [--telemetry-host display.local]

Everything decision-related lives in ``GroundStation``; this module only wires it to a
real bus, a real clock and the telemetry pump, and mirrors every bus message into the
telemetry stream so the display can show the bus log. If the display is unreachable
the telemetry queue overflows and drops; the arbiter never notices.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket

from orbit.arbiter.fsm import GroundStation
from orbit.bus.multicast import MulticastBus
from orbit.config import Settings
from orbit.ground.runtime import Clock, drive
from orbit.ground.telemetry import Telemetry, bus_summary
from orbit.log import log
from orbit.protocol import messages as M

lg = logging.getLogger("orbit.ground.station")


def local_hostname(settings: Settings) -> str:
    return settings.hostname or socket.gethostname().split(".")[0]


async def run_ground(settings: Settings, *, telemetry_network: bool = True, stop: asyncio.Event | None = None) -> None:
    hostname = local_hostname(settings)
    telemetry = Telemetry(settings, hostname, network=telemetry_network)
    ground = GroundStation(settings, hostname, sink=telemetry.emit)
    bus = MulticastBus(settings, hostname)
    clock = Clock()
    stop = stop or asyncio.Event()

    def observer(msg: M.Message, outbound: bool, now: float) -> None:
        telemetry.emit("bus", {"t": now, "dir": "out" if outbound else "in", **bus_summary(msg)})

    def periodic(now: float) -> None:
        snap = ground.snapshot(now)
        telemetry.emit("snapshot", {"t": now, **snap})
        telemetry.emit("flags", {"t": now, "sats": {h: s["flags"] for h, s in snap["sats"].items()}})
        telemetry.emit("bus_stats", {"t": now, **bus.stats.as_dict()})
        telemetry.emit("telemetry_stats", {"t": now, **telemetry.stats.as_dict()})

    log(lg, logging.INFO, "ground_start", hostname=hostname, settings=settings.as_dict())
    await bus.start()
    if telemetry_network:
        telemetry.start()
    try:
        await drive(ground, bus, clock, stop=stop, observer=observer, periodic=periodic)
    finally:
        await telemetry.stop()
        await bus.stop()
        log(lg, logging.INFO, "ground_stop", counters=ground.counters.as_dict(), window=ground.window.snapshot())


def install_signal_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def main_async(settings: Settings, telemetry_network: bool) -> None:
    stop = asyncio.Event()
    install_signal_stop(stop)
    await run_ground(settings, telemetry_network=telemetry_network, stop=stop)
