"""Hold the ESP32 firmware's scoring kernel to the Python golden model, bit for bit.

The firmware header is compiled by g++ into firmware/test/score_host and fed the same
corpus frame/ref pairs the satellite would score. Every intermediate is compared, not
just the final score, so a failure names the term that drifted.

The golden side is imported from the ground-station branch verbatim -- this compares the
firmware against the real spec, never against a second transcription of it.

  uv run --with numpy python tools/check_score_parity.py [--n 40]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
# Which revision the ground's own modules are read from. Integration work lives on the branch
# this file sits on, so HEAD is the authoritative source; override to compare against another
# branch: ORBIT_GOLDEN_REF=origin/ground-station.
GOLDEN_REF = os.environ.get("ORBIT_GOLDEN_REF", "HEAD")


def load_golden(tmp: Path) -> tuple[ModuleType, ModuleType]:
    """Materialise orbit.config and orbit.golden.score from the ground-station branch."""
    pkg = tmp / "orbit"
    (pkg / "golden").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "golden" / "__init__.py").write_text("")
    for path in ("orbit/config.py", "orbit/golden/score.py"):
        blob = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{GOLDEN_REF}:{path}"], capture_output=True, text=True, check=True
        ).stdout
        (tmp / path).write_text(blob)
    sys.path.insert(0, str(tmp))
    from orbit import config
    from orbit.golden import score

    return config, score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40, help="frame/ref pairs to compare")
    a = ap.parse_args()

    exe = ROOT / "firmware/test/score_host"
    if not exe.exists():
        print("build it first:  g++ -O2 -I../satellite_esp32 score_host.cpp -o score_host", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _config, score = load_golden(tmp)

        npz = np.load(ROOT / "corpus/frames.npz")
        frames = npz[npz.files[0]]
        manifest = json.loads((ROOT / "corpus/manifest.json").read_text())

        # pair each sampled frame with its scene's reference, exactly as the satellite does
        pairs, expect = [], []
        n = min(a.n, len(frames))
        for i in range(n):
            scene = manifest["frames"][i]["scene"]
            ref_id = int(manifest["scenes"][scene]["reference_id"])
            f, r = np.asarray(frames[i]), np.asarray(frames[ref_id])
            pairs.append((f, r))
            expect.append(score.score_frame(f, r))

        blob = tmp / "pairs.bin"
        with open(blob, "wb") as fh:
            for f, r in pairs:
                fh.write(f.astype(np.uint8).tobytes())
                fh.write(r.astype(np.uint8).tobytes())

        out = subprocess.run([str(exe), str(blob)], capture_output=True, text=True, check=True).stdout
        got = [tuple(int(x) for x in line.split()) for line in out.strip().splitlines()]

    if len(got) != len(expect):
        print(f"FAIL: firmware returned {len(got)} rows, expected {len(expect)}")
        return 1

    fields = ("cloud_px", "changed_px", "sobel_sum", "clear", "sharp", "change", "score")
    bad = 0
    for i, (g, e) in enumerate(zip(got, expect, strict=True)):
        want = (e.cloud_px, e.changed_px, e.sobel_sum, e.clear, e.sharp, e.change, e.score)
        if g != want:
            bad += 1
            diff = [f"{name}: firmware={a_} golden={b}" for name, a_, b in zip(fields, g, want, strict=True) if a_ != b]
            print(f"FAIL frame {i}: " + "; ".join(diff))

    if bad:
        print(f"\n{bad}/{len(expect)} frames differ")
        return 1
    print(
        f"OK: {len(expect)} frames, all 7 intermediates bit-exact "
        f"(cloud_px, changed_px, sobel_sum, clear, sharp, change, score)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
