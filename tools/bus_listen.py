"""Listen on the Orbit multicast bus and decode with the GROUND's own protocol module.

This is the end-to-end protocol check: the firmware's JSON is parsed by exactly the code
the ground station runs (`orbit/protocol/messages.py` from the ground-station branch), so
a field the firmware gets wrong shows up as a DecodeError here rather than as a silent
mismatch during the demo.

  uv run --with numpy python tools/bus_listen.py --seconds 12
"""
from __future__ import annotations

import argparse
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_REF = "origin/ground-station"


def load_ground_protocol(tmp: Path):
    pkg = tmp / "orbit"
    (pkg / "protocol").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "protocol" / "__init__.py").write_text("")
    for path in ("orbit/config.py", "orbit/protocol/messages.py"):
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f"{GOLDEN_REF}:{path}"],
                              capture_output=True, text=True, check=True).stdout
        (tmp / path).write_text(blob)
    sys.path.insert(0, str(tmp))
    from orbit import config
    from orbit.protocol import messages
    return config, messages


def open_socket(group: str, port: int, iface_ip: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind((group, port))
    mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface_ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.settimeout(0.5)
    return s


def default_route_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return str(s.getsockname()[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--iface-ip", default="")
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        config, M = load_ground_protocol(Path(td))
        s_cfg = config.Settings()
        iface = a.iface_ip or default_route_ip()
        print(f"listening on {s_cfg.mcast_group}:{s_cfg.mcast_port} via {iface} for {a.seconds:.0f}s\n")
        sock = open_socket(s_cfg.mcast_group, s_cfg.mcast_port, iface)

        kinds, senders, errors = Counter(), Counter(), []
        first_of_kind: dict[str, object] = {}
        end = time.time() + a.seconds
        n = 0
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            n += 1
            msg = M.decode(data)
            if isinstance(msg, M.DecodeError):
                errors.append((addr[0], msg.reason, data[:180]))
                continue
            kinds[str(msg.TYPE)] += 1
            senders[msg.sender] += 1
            first_of_kind.setdefault(str(msg.TYPE), msg)
        sock.close()

        print(f"{n} datagrams, {sum(kinds.values())} decoded by the ground's own decoder, {len(errors)} rejected\n")
        if senders:
            print("senders:")
            for k, v in senders.items():
                print(f"  {k}: {v}")
        if kinds:
            print("\nmessage types:")
            for k, v in kinds.most_common():
                print(f"  {k}: {v}")
        if errors:
            print("\nDECODE ERRORS (firmware/ground protocol mismatch):")
            for ip, reason, raw in errors[:6]:
                print(f"  from {ip}: {reason}\n    {raw!r}")
            return 1
        print("\nsample of each type, as the ground parsed it:")
        for k, m in first_of_kind.items():
            print(f"\n  {k}: {m}")
        return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
