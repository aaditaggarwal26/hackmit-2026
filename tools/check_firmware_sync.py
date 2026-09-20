"""Fail if the firmware's constants or fault codes have drifted from the ground station.

Two headers restate facts owned elsewhere, and both are checked rather than trusted:

* ``orbit_config.h`` restates facts from ``orbit/config.py`` -- frame geometry, kernel weights
  and thresholds, protocol version, bus address, satellite tunables. A silent divergence there
  produces scores the ground cannot reproduce, which is exactly the failure that is hardest to
  read off a dashboard.
* ``orbit_faults.h`` is GENERATED from ``orbit/protocol/registry.py`` and carries the integer
  ``code_id`` of every fault. Those numbers travel on the wire and get flashed into boards, so a
  header regenerated from an older registry -- or hand-edited, which its banner forbids and
  nothing else prevents -- means a board saying 7 while the ground reads 7 as something else.
  The ids and severity names are parsed back out of the header the firmware actually compiles
  against, so a stale header is caught whatever the generator would produce today.

  uv run python tools/check_firmware_sync.py

The config side is read out of git rather than off disk, so the firmware is held to a committed
revision of ``orbit/config.py`` and not to whatever the working tree happens to hold. That revision
is ``HEAD`` by default; set ``ORBIT_GOLDEN_REF`` to compare against another branch
(``ORBIT_GOLDEN_REF=origin/ground-station``), exactly as the other two checkers do. The registry is
owned here rather than on any other ref, so it is imported.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from orbit.protocol import registry as R

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "orbit/config.py"
HEADER = ROOT / "firmware/satellite_esp32/orbit_config.h"
FAULTS_HEADER = ROOT / "firmware/satellite_esp32/orbit_faults.h"
# Which revision orbit/config.py is read from. Unset means the working tree, which is the only
# answer that catches the drift this check exists for: the edit in progress. Both the firmware
# headers and the registry are read from the working tree too, so reading config.py out of git by
# default would compare three files across two different points in history and pass a config.py
# whose new constant had simply not been committed yet. ORBIT_GOLDEN_REF=<ref> opts into the
# cross-branch comparison this once needed (ORBIT_GOLDEN_REF=origin/ground-station).
GOLDEN_REF = os.environ.get("ORBIT_GOLDEN_REF") or ""

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


_ENUM = re.compile(r"ORBIT_FAULT_(\w+)\s*=\s*(\d+)\s*,")
_SEVERITY = re.compile(r"case ORBIT_FAULT_(\w+):\s*return\s*\"(\w*)\";")


def fault_header_codes(text: str) -> tuple[dict[str, int], dict[str, str]]:
    """(slug -> id, slug -> severity) as orbit_faults.h actually defines them.

    Parsed, not imported: the point is to read the file the compiler reads. The severity map
    comes from the first switch only -- orbit_fault_slug's cases return the slug, not a severity,
    and would otherwise overwrite every entry with its own name.
    """
    ids = {m.group(1).lower(): int(m.group(2)) for m in _ENUM.finditer(text)}
    sev: dict[str, str] = {}
    body = text.split("orbit_fault_slug")[0]
    for m in _SEVERITY.finditer(body):
        sev.setdefault(m.group(1).lower(), m.group(2))
    return ids, sev


def fault_drift() -> list[str]:
    """Every way orbit_faults.h and the registry can disagree about a code."""
    if not FAULTS_HEADER.exists():
        return [f"{FAULTS_HEADER.relative_to(ROOT)} is missing entirely"]
    ids, sev = fault_header_codes(FAULTS_HEADER.read_text())
    want_id = {R.slug(c): int(c) for c in R.FAULTS}
    want_sev = {R.slug(c): str(s.severity) for c, s in R.FAULTS.items()}

    bad = [
        f"{s}: in the registry (id {want_id[s]}), absent from orbit_faults.h" for s in sorted(set(want_id) - set(ids))
    ]
    bad += [f"{s}: in orbit_faults.h (id {ids[s]}), absent from the registry" for s in sorted(set(ids) - set(want_id))]
    for s in sorted(set(ids) & set(want_id)):
        if ids[s] != want_id[s]:
            # The one that matters most: the same name, a different number. Every flashed board
            # carrying the old number now means something else to the ground.
            bad.append(f"{s}: orbit_faults.h id={ids[s]} registry id={want_id[s]}")
        if sev.get(s, "") != want_sev[s]:
            bad.append(f"{s}: orbit_faults.h severity={sev.get(s, '<missing>')!r} registry={want_sev[s]!r}")
    return bad


def config_source() -> str | None:
    """orbit/config.py as the check should read it, or None if the named ref cannot be read."""
    if not GOLDEN_REF:
        return CONFIG.read_text()
    show = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{GOLDEN_REF}:orbit/config.py"], capture_output=True, text=True
    )
    if show.returncode != 0:
        # A clone without that ref: say which one, instead of raising CalledProcessError.
        print(f"cannot read orbit/config.py from ref {GOLDEN_REF!r}:", file=sys.stderr)
        print("  " + (show.stderr.strip() or "git show failed"), file=sys.stderr)
        print("  set ORBIT_GOLDEN_REF to a ref this clone has, or unset it to use the working tree", file=sys.stderr)
        return None
    return show.stdout


def main() -> int:
    py = config_source()
    if py is None:
        return 2
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

    if drift := fault_drift():
        print("orbit_faults.h has drifted from the fault registry:\n")
        for d in drift:
            print("  " + d)
        print(f"\nregenerate: {R.REGEN}")
        return 1

    print(f"OK: {len(CHECKS)} constants + MCAST_GROUP match orbit/config.py ({GOLDEN_REF or 'working tree'})")
    print(f"OK: {len(R.FAULTS)} fault code ids and severities match orbit/protocol/registry.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
