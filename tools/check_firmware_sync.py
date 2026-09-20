"""Fail if the firmware's constants have drifted from orbit/config.py.

orbit_config.h restates facts that live in config.py -- frame geometry, kernel weights
and thresholds, protocol version, bus address, satellite tunables. A silent divergence
here produces scores the ground cannot reproduce, which is exactly the failure that is
hardest to read off a dashboard. So it is checked, not trusted.

  uv run python tools/check_firmware_sync.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "firmware/satellite_esp32/orbit_config.h"
GOLDEN_REF = "origin/ground-station"

# firmware name -> (python name, where it lives: "const" = module level, "setting" = Settings default)
CHECKS = {
    "ORBIT_PROTOCOL_VERSION": ("PROTOCOL_VERSION", "const"),
    "FRAME_W": ("FRAME_W", "const"),
    "FRAME_H": ("FRAME_H", "const"),
    "CLOUD_THRESHOLD": ("CLOUD_THRESHOLD", "const"),
    "CHANGE_THRESHOLD": ("CHANGE_THRESHOLD", "const"),
    "SHARP_SHIFT": ("SHARP_SHIFT", "const"),
    "W_CLEAR": ("W_CLEAR", "const"),
    "W_SHARP": ("W_SHARP", "const"),
    "W_CHANGE": ("W_CHANGE", "const"),
    "MCAST_PORT": ("mcast_port", "setting"),
    "MCAST_TTL": ("mcast_ttl", "setting"),
    "BUS_MAX_DATAGRAM": ("bus_max_datagram", "setting"),
    "CHUNK_BYTES": ("chunk_bytes", "setting"),
    "BID_WINDOW_N": ("bid_window_n", "setting"),
    "SAT_HEARTBEAT_MS": ("sat_heartbeat_ms", "setting"),
    "SAT_ACK_TIMEOUT_MS": ("sat_ack_timeout_ms", "setting"),
    "SAT_BUFFER_SLOTS": ("sat_buffer_slots", "setting"),
}


def firmware_values(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"#define\s+(\w+)\s+(\S+)", text):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(r"static const \w+\s+(\w+)\s*=\s*([^;]+);", text):
        out[m.group(1)] = m.group(2).strip()
    return out


def main() -> int:
    py = subprocess.run(["git", "-C", str(ROOT), "show", f"{GOLDEN_REF}:orbit/config.py"],
                        capture_output=True, text=True, check=True).stdout
    fw = firmware_values(HEADER.read_text())

    consts = dict(re.findall(r"^([A-Z_][A-Z0-9_]*)\s*=\s*([^#\n]+)", py, re.M))
    settings = dict(re.findall(r"^\s{4}([a-z_]+):\s*[\w\[\], |]+\s*=\s*([^#\n]+)", py, re.M))

    bad = []
    for fname, (pname, kind) in CHECKS.items():
        src = consts if kind == "const" else settings
        if pname not in src:
            bad.append(f"{fname}: {pname} not found in config.py ({kind})")
            continue
        want = src[pname].strip().rstrip(",").strip().strip('"')
        got = fw.get(fname, "<missing>").strip().rstrip("uf").strip('"')
        if fname == "MCAST_GROUP":
            want = want.strip('"')
        try:
            same = float(got) == float(want.replace("_", ""))
        except ValueError:
            same = got == want
        if not same:
            bad.append(f"{fname}: firmware={got!r} config.py {pname}={want!r}")

    # the multicast group is a string, checked separately
    grp = re.search(r'#define MCAST_GROUP\s+"([^"]+)"', HEADER.read_text())
    want_grp = re.search(r'mcast_group:\s*str\s*=\s*"([^"]+)"', py)
    if grp and want_grp and grp.group(1) != want_grp.group(1):
        bad.append(f"MCAST_GROUP: firmware={grp.group(1)!r} config.py={want_grp.group(1)!r}")

    if bad:
        print("firmware constants have drifted from orbit/config.py:\n")
        for b in bad:
            print("  " + b)
        return 1
    print(f"OK: {len(CHECKS)} constants + MCAST_GROUP match orbit/config.py ({GOLDEN_REF})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
