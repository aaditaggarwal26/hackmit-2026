"""The bus interface every node speaks, plus the duplicate filter every node needs.

A bus is pub/sub: ``send`` broadcasts one message to everybody (including, on real
multicast, ourselves), ``poll`` drains whatever arrived. There is no addressing —
messages that concern one node name it in a field (``to``) and everyone else hears
it anyway. That is deliberate: a satellite watching a peer being granted and never
transmitting is the seed of relay.

``poll`` is synchronous so the simulator can step it under a virtual clock; asyncio
drivers ``await wait()`` between polls. Neither path ever blocks the caller on I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field

from orbit.log import log
from orbit.protocol import messages as M

lg = logging.getLogger("orbit.bus")


@dataclass
class BusStats:
    sent: int = 0
    received: int = 0
    delivered: int = 0
    dropped_own: int = 0
    dropped_dup: int = 0
    dropped_malformed: int = 0
    dropped_oversize: int = 0
    last_malformed: str = ""
    malformed_by_type: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return dict(vars(self))


class Deduper:
    """Remembers the last ``window`` (sender, seq) pairs. UDP may duplicate; multicast on WiFi
    frequently does. Sequence numbers are per sender, so reordering is tolerated too: a
    message is new if we have not seen exactly that pair, regardless of arrival order."""

    def __init__(self, window: int) -> None:
        self.window = window
        self._seen: set[tuple[str, int]] = set()
        self._order: deque[tuple[str, int]] = deque()

    def is_new(self, sender: str, seq: int) -> bool:
        key = (sender, seq)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._order.append(key)
        if len(self._order) > self.window:
            self._seen.discard(self._order.popleft())
        return True


class Bus(ABC):
    def __init__(self, hostname: str, dedup_window: int, max_datagram: int, drop_own: bool = True) -> None:
        self.hostname = hostname
        self.max_datagram = max_datagram
        self.drop_own = drop_own
        self.stats = BusStats()
        self._dedup = Deduper(dedup_window)
        self._inbox: deque[M.Message] = deque()
        self._wakeup: asyncio.Event | None = None
        self.taps: list[M.Message] = []  # every accepted message incl. our own sends, for the bus log

    # --- to implement -------------------------------------------------------------------

    @abstractmethod
    def _transmit(self, data: bytes) -> None: ...

    async def start(self) -> None:  # noqa: B027 - a no-op is the right default (loopback)
        """Open sockets / join groups. The loopback bus has nothing to do."""

    async def stop(self) -> None:  # noqa: B027
        """Release whatever start() acquired."""

    # --- public -------------------------------------------------------------------------

    def send(self, msg: M.Message) -> None:
        data = msg.encode()
        if len(data) > self.max_datagram:
            self.stats.dropped_oversize += 1
            log(lg, logging.ERROR, "oversize_message", type=str(msg.TYPE), bytes=len(data), limit=self.max_datagram)
            return
        self.stats.sent += 1
        self._transmit(data)

    def poll(self) -> list[M.Message]:
        out = list(self._inbox)
        self._inbox.clear()
        return out

    async def wait(self, timeout: float) -> None:
        """Return when something is in the inbox or ``timeout`` elapses."""
        if self._inbox:
            return
        if self._wakeup is None:
            self._wakeup = asyncio.Event()
        self._wakeup.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wakeup.wait(), timeout)

    # --- for implementations ------------------------------------------------------------

    def _ingest(self, data: bytes) -> None:
        """Bytes off the wire → inbox, or one of the drop counters. Never raises."""
        self.stats.received += 1
        decoded = M.decode(data, self.max_datagram)
        if isinstance(decoded, M.DecodeError):
            self.stats.dropped_malformed += 1
            self.stats.last_malformed = decoded.reason
            key = decoded.raw_type or "?"
            self.stats.malformed_by_type[key] = self.stats.malformed_by_type.get(key, 0) + 1
            log(lg, logging.WARNING, "malformed_datagram", reason=decoded.reason, bytes=len(data))
            return
        if self.drop_own and decoded.sender == self.hostname:
            self.stats.dropped_own += 1
            return
        if not self._dedup.is_new(decoded.sender, decoded.seq):
            self.stats.dropped_dup += 1
            return
        self.stats.delivered += 1
        self._inbox.append(decoded)
        if self._wakeup is not None:
            self._wakeup.set()
