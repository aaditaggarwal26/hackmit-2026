"""Is UDP multicast actually working between two machines on this network?

On the display laptop (or any second device on the same SSID):

    uv run orbit bus-smoke --listen

On the GX10:

    uv run orbit bus-smoke --send

The listener prints every datagram it receives with its source address. If the sender
sees its own packets (loopback) but the listener sees nothing after 10 s, the access
point is filtering client-to-client multicast: switch to the phone hotspot / travel
router. ``--unicast <ip>`` sends the same test datagrams point-to-point as a control,
so "unicast works, multicast doesn't" is a one-command diagnosis.

Test datagrams are valid ``heartbeat`` messages, so a running ground station will list
the sender as a satellite (idle, empty queue) — a second way to confirm the path.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

from orbit.bus.multicast import default_route_ip, open_socket
from orbit.config import Settings
from orbit.protocol import messages as M


def listen(s: Settings, iface: str, seconds: float) -> int:
    sock = open_socket(s.mcast_group, s.mcast_port, iface, s.mcast_ttl)
    sock.setblocking(True)
    sock.settimeout(0.5)
    print(f"listening on {s.mcast_group}:{s.mcast_port} via {iface} for {seconds:.0f}s", file=sys.stderr)
    t0, n, senders = time.monotonic(), 0, set()
    while time.monotonic() - t0 < seconds:
        try:
            data, addr = sock.recvfrom(65535)
        except TimeoutError:
            continue
        n += 1
        senders.add(addr[0])
        m = M.decode(data)
        if isinstance(m, M.Message):
            desc = f"{type(m).__name__} from {m.sender} seq={m.seq}"
        else:
            desc = f"{len(data)}B (not orbit: {m.reason})"
        print(f"{time.monotonic() - t0:6.2f}s  {addr[0]}:{addr[1]}  {desc}")
    print(f"received {n} datagrams from {sorted(senders) or 'nobody'}", file=sys.stderr)
    return 0 if n else 1


def send(s: Settings, iface: str, count: int, interval: float, hostname: str, unicast: str | None) -> int:
    sock = open_socket(s.mcast_group, s.mcast_port, iface, s.mcast_ttl)
    dest = (unicast, s.mcast_port) if unicast else (s.mcast_group, s.mcast_port)
    buf = M.BufferStats(slots=0, capacity_bytes=0, used=0, free=0, occupancy_pct=0.0)
    print(f"sending {count} heartbeats to {dest[0]}:{dest[1]} via {iface} as {hostname}", file=sys.stderr)
    for i in range(count):
        m = M.Heartbeat(
            hostname,
            i + 1,
            int(time.monotonic() * 1000),
            buffer=buf,
            eviction_count=0,
            queue_len=0,
            top_score=-1.0,
            top_item_id=-1,
            uptime_s=float(i),
            frames_scored=0,
            frames_sent=0,
        )
        sock.sendto(m.encode(), dest)
        print(f"sent {i + 1}/{count}")
        time.sleep(interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--listen", action="store_true")
    g.add_argument("--send", action="store_true")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument(
        "--iface-ip", default=None, help="local IPv4 to bind the group on (default: default-route interface)"
    )
    ap.add_argument("--unicast", default=None, help="send point-to-point to this IP instead (control test)")
    ap.add_argument("--hostname", default=None)
    a = ap.parse_args(argv)
    s = Settings.from_env()
    iface = a.iface_ip or s.bus_iface_ip or default_route_ip()
    host = a.hostname or f"smoke-{socket.gethostname().split('.')[0]}"
    return listen(s, iface, a.seconds) if a.listen else send(s, iface, a.count, a.interval, host, a.unicast)


if __name__ == "__main__":
    raise SystemExit(main())
