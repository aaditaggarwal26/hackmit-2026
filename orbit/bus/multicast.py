"""UDP multicast bus: one group, every node joins, everything is broadcast.

Points that bit during bring-up on this machine and are therefore explicit here:

* The GX10 has four interfaces with routes (WiFi, docker0, tailscale0, loopback).
  The kernel's default choice of outgoing interface for multicast is whichever it
  likes, so ``IP_MULTICAST_IF`` and the membership are always bound to a specific
  address. By default that is the address on the default route, found with the
  connect-a-UDP-socket trick (no packet is sent).
* ``IP_MULTICAST_LOOP`` stays on: several processes on one box (the demo's ground and
  three simulated satellites) must hear each other. Each node drops its own echo by
  hostname, not by socket.
* ``SO_REUSEADDR`` is what lets those processes share the port; on Linux multicast is
  delivered to every such socket. ``SO_REUSEPORT`` is set too where available for
  BSD-family hosts, but nothing depends on its load-balancing semantics.
* The bind address is not the same on every platform; see ``BIND_GROUP`` below. Getting this
  wrong is silent in one direction: the node still joins the group and still receives, and only
  its sends fail.
* TTL 1. Nothing here should ever be routed.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import sys
from typing import Any

from orbit.bus.base import Bus
from orbit.config import Settings
from orbit.log import log
from orbit.protocol import auth

lg = logging.getLogger("orbit.bus.mcast")


OFFLINE_IP = "127.0.0.1"

# Bind the group itself, or the wildcard? Linux takes the group and then delivers only datagrams
# addressed to it, which is the tighter filter and is what the ground station runs on. BSD and
# macOS cannot: the socket's source address would be the group, which is not a local address, so
# the first sendto fails with EADDRNOTAVAIL and the node is deaf and mute on its own bus. There
# the wildcard is the only working bind and IP_ADD_MEMBERSHIP does the filtering instead. The
# wider bind also admits unicast sent to this port, which costs nothing: _ingest authenticates,
# decodes and drops by hostname before anything reaches the arbiter, and counts what it dropped.
BIND_GROUP = sys.platform.startswith("linux")


def default_route_ip() -> str:
    """The local IPv4 address the kernel would use to reach the network; no packet is sent.

    With no default route at all (network unplugged, demo still expected to run) this falls back
    to loopback: Linux delivers multicast between local processes on 127.0.0.1, so the ground and
    simulated satellites on one box keep working. Real satellites obviously will not be reachable
    then, and the log says so."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return str(s.getsockname()[0])
    except OSError as e:
        log(lg, logging.WARNING, "no_default_route", error=str(e), using=OFFLINE_IP)
        return OFFLINE_IP


def open_socket(group: str, port: int, iface_ip: str, ttl: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((group if BIND_GROUP else "", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.setblocking(False)
    return sock


class MulticastBus(Bus):
    def __init__(self, settings: Settings, hostname: str) -> None:
        super().__init__(
            hostname,
            settings.dedup_window,
            settings.bus_max_datagram,
            restart_slack_ms=settings.restart_slack_ms,
            policy=auth.Policy.from_settings(settings),
        )
        self.s = settings
        self.group = (settings.mcast_group, settings.mcast_port)
        self.iface_ip = settings.bus_iface_ip or default_route_ip()
        self._sock: socket.socket | None = None
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        self._sock = open_socket(self.s.mcast_group, self.s.mcast_port, self.iface_ip, self.s.mcast_ttl)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=self._sock)
        self._transport = transport
        log(
            lg,
            logging.INFO,
            "bus_joined",
            group=self.s.mcast_group,
            port=self.s.mcast_port,
            iface=self.iface_ip,
            hostname=self.hostname,
            # never the key itself: whether one is set, and what is being enforced with it
            auth=self.policy.signing,
            pinned_ground=self.policy.ground_name,
            pinned_sats=list(self.policy.sat_names),
            anti_replay=self.policy.replay_checked,
        )

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._sock = None

    def _transmit(self, data: bytes) -> None:
        if self._transport is None:
            return
        try:
            self._transport.sendto(data, self.group)
        except OSError as e:  # e.g. network went away mid-demo: log, keep arbitrating
            log(lg, logging.ERROR, "send_failed", error=str(e))

    def send_raw(self, data: bytes) -> None:
        """For the smoke test only: bytes straight to the group."""
        self._transmit(data)


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, bus: MulticastBus) -> None:
        self.bus = bus

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.bus._ingest(data)

    def error_received(self, exc: Exception) -> None:
        log(lg, logging.WARNING, "socket_error", error=str(exc))
