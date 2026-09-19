"""Drive any event-machine node (ground or satellite) from asyncio over a real bus.

The nodes own no timers and no sockets; this loop owns both. It polls the bus, hands
each message to the node with the current time, ticks the node, and sends whatever the
node returns. The clock starts at zero when the loop starts so log timestamps read like
the simulator's.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Protocol

from orbit.bus.base import Bus
from orbit.log import log
from orbit.protocol import messages as M

lg = logging.getLogger("orbit.runtime")

DEFAULT_TICK_S = 0.02


class Node(Protocol):
    def start(self, now: float) -> list[M.Message]: ...
    def on_message(self, msg: M.Message, now: float) -> list[M.Message]: ...
    def on_tick(self, now: float) -> list[M.Message]: ...


class Clock:
    """Monotonic seconds since construction."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def __call__(self) -> float:
        return time.monotonic() - self.t0


async def drive(node: Node, bus: Bus, clock: Callable[[], float], *, tick_s: float = DEFAULT_TICK_S,
                stop: asyncio.Event | None = None, observer: Callable[[M.Message, bool, float], None] | None = None,
                periodic: Callable[[float], None] | None = None, period_s: float = 1.0) -> None:
    """Run until ``stop`` is set. ``observer(msg, outbound, now)`` sees every message in and out;
    ``periodic(now)`` runs every ``period_s`` (snapshots, flag evaluation)."""
    stop = stop or asyncio.Event()

    def send(msgs: list[M.Message], now: float) -> None:
        for m in msgs:
            bus.send(m)
            if observer:
                observer(m, True, now)

    send(node.start(clock()), clock())
    next_periodic = clock() + period_s
    while not stop.is_set():
        await bus.wait(tick_s)
        now = clock()
        for msg in bus.poll():
            if observer:
                observer(msg, False, now)
            try:
                send(node.on_message(msg, now), now)
            except Exception:  # one bad message must never take the node down
                lg.exception("node.on_message failed")
                log(lg, logging.ERROR, "node_error", type=str(msg.TYPE), sender=msg.sender)
        try:
            send(node.on_tick(now), now)
        except Exception:
            lg.exception("node.on_tick failed")
        if periodic and now >= next_periodic:
            next_periodic = now + period_s
            try:
                periodic(now)
            except Exception:
                lg.exception("periodic failed")
