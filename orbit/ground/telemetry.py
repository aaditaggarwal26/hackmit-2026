"""Telemetry to the display laptop (Section 13). Observational, never in the control path.

Rules this module exists to enforce:

* ``emit`` never blocks and never raises. It serialises the event and appends it to a
  bounded deque. If the display cannot keep up, the *oldest* events are discarded and
  counted; arbitration is not slowed by a single microsecond.
* Sending is fire-and-forget UDP with a non-blocking socket. A dead, slow or absent
  laptop costs nothing but a counter.
* Hostname resolution (``display.local`` over mDNS) happens off the event loop and is
  retried on a timer; until it succeeds events simply drop.

Every event is one JSON object: ``{"kind": ..., "t": <ground time>, "host": <ground>, ...}``.
The kinds the ground produces are listed in ``KINDS`` so the display has a contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orbit.config import Settings
from orbit.log import log
from orbit.protocol import messages as M

lg = logging.getLogger("orbit.telemetry")

KINDS = (
    "round_open", "decision", "tx_begin", "complete", "tx_failed", "revoke", "no_bids", "late_bid", "unexpected_tx",
    "sat_seen", "eviction", "window_closed", "state", "flags", "bus", "snapshot", "bus_stats", "telemetry_stats",
)

Sink = Callable[[dict[str, Any]], None]


@dataclass
class TelemetryStats:
    emitted: int = 0
    sent: int = 0
    dropped_queue_full: int = 0
    dropped_unresolved: int = 0
    send_errors: int = 0
    resolved: str = ""

    def as_dict(self) -> dict[str, object]:
        return dict(vars(self))


class Telemetry:
    def __init__(self, settings: Settings, hostname: str, *, network: bool = True) -> None:
        self.s = settings
        self.hostname = hostname
        self.network = network
        self.stats = TelemetryStats()
        self._queue: deque[bytes] = deque(maxlen=settings.telemetry_queue_max)
        self._sinks: list[Sink] = []
        self._addr: tuple[str, int] | None = None
        self._sock: socket.socket | None = None
        self._task: asyncio.Task[None] | None = None

    # --- producers ----------------------------------------------------------------------

    def attach(self, sink: Sink) -> None:
        """In-process consumer (the simulator's table, tests). Called synchronously; must be cheap."""
        self._sinks.append(sink)

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        event = {"kind": kind, "host": self.hostname, **payload}
        self.stats.emitted += 1
        for sink in self._sinks:
            try:
                sink(event)
            except Exception:  # a broken consumer is that consumer's problem
                lg.exception("telemetry sink failed")
        if not self.network:
            return
        try:
            data = json.dumps(event, default=_json_default, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError):
            lg.exception("unserialisable telemetry event %s", kind)
            return
        if len(self._queue) == self._queue.maxlen:
            self.stats.dropped_queue_full += 1
        self._queue.append(data)

    # --- transport ----------------------------------------------------------------------

    def open(self) -> None:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def resolve(self) -> tuple[str, int] | None:
        """Blocking mDNS/DNS lookup, IPv4 only (the link-local IPv6 answers avahi gives first are
        useless from another host). Call from a thread."""
        try:
            infos = socket.getaddrinfo(self.s.telemetry_host, self.s.telemetry_port, socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return None
        for _, _, _, _, sockaddr in infos:
            ip = str(sockaddr[0])
            if not ip.startswith("169.254."):
                return (ip, self.s.telemetry_port)
        return None

    def set_address(self, addr: tuple[str, int] | None) -> None:
        self._addr = addr
        self.stats.resolved = f"{addr[0]}:{addr[1]}" if addr else ""

    def flush(self) -> int:
        """Send what is queued, without blocking. Returns datagrams sent."""
        if self._sock is None or self._addr is None:
            if self._addr is None and self._queue:
                self.stats.dropped_unresolved += len(self._queue)
                self._queue.clear()
            return 0
        n = 0
        while self._queue:
            data = self._queue[0]
            try:
                self._sock.sendto(data, self._addr)
            except BlockingIOError:
                break  # socket buffer full: leave the rest for next time
            except OSError:
                self.stats.send_errors += 1
                self._queue.popleft()  # unreachable destination etc.: drop and keep going
                continue
            self._queue.popleft()
            self.stats.sent += 1
            n += 1
        return n

    async def run(self, period_s: float = 0.05) -> None:
        """Background pump: resolve lazily, flush periodically."""
        self.open()
        next_resolve = 0.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                now = loop.time()
                if self._addr is None and now >= next_resolve:
                    next_resolve = now + self.s.telemetry_resolve_s
                    addr = await asyncio.to_thread(self.resolve)
                    if addr:
                        self.set_address(addr)
                        log(lg, logging.INFO, "telemetry_resolved", host=self.s.telemetry_host, addr=addr[0])
                self.flush()
                await asyncio.sleep(period_s)
        finally:
            self.close()

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="telemetry")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


def bus_summary(msg: M.Message) -> dict[str, Any]:
    """A compact one-line view of a message for the bus log; chunk payloads are elided."""
    d: dict[str, Any] = {"from": msg.sender, "type": str(msg.TYPE), "seq": msg.seq}
    match msg:
        case M.Bid():
            d.update(round_id=msg.round_id, item_id=msg.item_id, score=msg.score, item_age_s=msg.item_age_s,
                     queue_len=msg.queue_len, occupancy_pct=msg.buffer.occupancy_pct,
                     window=[vars(e) for e in msg.window])
        case M.Grant():
            d.update(round_id=msg.round_id, to=msg.to, item_id=msg.item_id, total=msg.breakdown.total)
        case M.Revoke() | M.TxAck():
            d.update(round_id=msg.round_id, to=msg.to, item_id=msg.item_id, reason=msg.reason)
        case M.TxChunk():
            d.update(item_id=msg.item_id, idx=msg.idx, n=msg.n, bytes=len(msg.data))
        case M.TxBegin() | M.TxDone():
            d.update(round_id=msg.round_id, item_id=msg.item_id)
        case M.State():
            d.update(state=msg.state, round_id=msg.round_id, granted_to=msg.granted_to)
        case M.OffersOpen():
            d.update(round_id=msg.round_id)
        case M.Heartbeat():
            d.update(queue_len=msg.queue_len, top_score=msg.top_score, occupancy_pct=msg.buffer.occupancy_pct)
        case M.Eviction():
            d.update(item_id=msg.item_id, score=msg.score, loss_kind=msg.kind)
    return d


def _json_default(o: Any) -> Any:
    if isinstance(o, frozenset | set | tuple):
        return list(o)
    if hasattr(o, "__dict__"):
        return vars(o)
    return str(o)
