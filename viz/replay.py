"""Replay a recorded telemetry stream into the display, as UDP, at any speed.

    uv run python -m viz.replay events.jsonl --port 50010 --speed 10 [--loop]

The file is what ``orbit sim --events`` writes: one JSON event per line with a ground
time ``t``. Each line is sent as its own datagram to 127.0.0.1:port, paced by the gaps
in ``t`` divided by ``--speed``. This is how the dashboard is exercised without a ground
or any hardware, and how the demo is rehearsed: same events, same page, no radio.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

from orbit.config import DEFAULTS


def load(path: Path) -> list[tuple[float, bytes]]:
    """(t, raw line) pairs in file order; lines that are not JSON objects with a numeric ``t`` are
    kept with the previous timestamp so a hand-edited file still replays end to end."""
    out: list[tuple[float, bytes]] = []
    last = 0.0
    for line in path.read_bytes().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line).get("t", last)
        except (ValueError, AttributeError):
            t = last
        last = float(t) if isinstance(t, int | float) else last
        out.append((last, line))
    return out


def replay(events: list[tuple[float, bytes]], addr: tuple[str, int], speed: float, sock: socket.socket) -> int:
    """Send every event; sleep real ``dt / speed`` between them. Returns datagrams sent."""
    if not events:
        return 0
    start_wall, start_t = time.monotonic(), events[0][0]
    for t, data in events:
        due = start_wall + max(0.0, t - start_t) / speed
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        sock.sendto(data, addr)
    return len(events)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="JSON-lines file from `orbit sim --events`")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULTS.telemetry_port)
    ap.add_argument("--speed", type=float, default=1.0, help="time compression; 10 = ten seconds of ground per second")
    ap.add_argument("--loop", action="store_true", help="start over at the end (the display sees a ground restart)")
    a = ap.parse_args(argv)
    events = load(a.path)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    total = 0
    try:
        while True:
            total += replay(events, (a.host, a.port), max(a.speed, 1e-6), sock)
            print(f"replay: sent {total} datagrams to {a.host}:{a.port}", file=sys.stderr)
            if not a.loop:
                break
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
