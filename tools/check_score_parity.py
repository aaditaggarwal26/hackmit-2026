"""Hold the ESP32 firmware's scoring kernel to the Python golden model, bit for bit.

The firmware header is compiled by g++ into firmware/test/score_host -- by this script, on
demand -- and fed the same corpus frame/ref pairs the satellite would score. Every
intermediate is compared, not just the final score, so a failure names the term that drifted.

The golden side is copied into a throwaway package and imported from there, never from the
`orbit` already on sys.path, so what the firmware is held to is orbit/config.py and
orbit/golden/score.py themselves and never a second transcription of them. By default those
two files are read from the working tree, which is the only source that catches the drift this
check exists for: a kernel constant edited and not yet reflected in the firmware header. Set
ORBIT_GOLDEN_REF to read them from a git ref instead (ORBIT_GOLDEN_REF=origin/ground-station).

  uv run --with numpy python tools/check_score_parity.py [--n 40]

ORBIT_SCORE_HOST moves the compiled harness off its default path in the tree, which is how the
tests build one without disturbing anyone else's.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/test/score_host.cpp"
INC = ROOT / "firmware/satellite_esp32"
# Where the compiled harness lands. In the tree by default -- .gitignore covers firmware/test/*_host,
# so it is never committed -- and anywhere ORBIT_SCORE_HOST points otherwise.
EXE = Path(os.environ["ORBIT_SCORE_HOST"]) if os.environ.get("ORBIT_SCORE_HOST") else ROOT / "firmware/test/score_host"
# Same flags the pytest harnesses build their host binaries with (tests/test_firmware_codec.py,
# test_firmware_sat.py, test_firmware_fsimage.py, test_crypto_parity.py). score_host.cpp is clean
# under -Werror, so nothing is relaxed here: a new warning in the firmware header is a failure.
CXXFLAGS = ["-O2", "-std=c++17", "-Wall", "-Wextra", "-Werror"]
# Which revision the ground's own modules are read from. Unset means the working tree: the
# firmware header on the other side of this comparison is read from the working tree too, and
# reading only the golden half out of git would hold the firmware to a config.py from a
# different point in history -- so an edited CLOUD_THRESHOLD that the firmware has not caught up
# with would be reported bit-exact. ORBIT_GOLDEN_REF=<ref> opts into the cross-branch compare.
GOLDEN_REF = os.environ.get("ORBIT_GOLDEN_REF") or ""


class CheckError(RuntimeError):
    """The comparison could not be made at all, as opposed to being made and failing.

    No g++, a harness that will not compile, a golden ref this clone does not have: all of them
    mean "unknown", which is exit 2, and none of them may be reported as parity.
    """


def _sources() -> list[Path]:
    """Everything the binary is built from: the harness and every header it can include."""
    return [SRC, *sorted(INC.glob("*.h"))]


def build_host(exe: Path = EXE) -> Path:
    """Compile score_host.cpp when ``exe`` is missing or older than any of its sources.

    The same header the ESP32 builds, built here by g++ -- so the thing being compared with the
    golden model is the firmware itself and not a copy of it that drifted since someone last
    remembered to rebuild by hand.
    """
    sources = _sources()
    fresh = exe.exists() and exe.stat().st_mtime >= max(p.stat().st_mtime for p in sources)
    if fresh:
        return exe
    if shutil.which("g++") is None:
        raise CheckError(
            f"no g++ on PATH to build {SRC.relative_to(ROOT)} with"
            + (f", and {exe.relative_to(ROOT)} is older than its sources" if exe.exists() else "")
        )
    exe.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["g++", *CXXFLAGS, f"-I{INC}", str(SRC), "-o", str(exe)], capture_output=True, text=True, cwd=SRC.parent
    )
    if r.returncode != 0:
        raise CheckError(f"g++ failed to build {SRC.relative_to(ROOT)}:\n{r.stdout}{r.stderr}".rstrip())
    return exe


def golden_source(path: str) -> str:
    """One golden module's text: the working tree, or GOLDEN_REF when one is set."""
    if not GOLDEN_REF:
        return (ROOT / path).read_text()
    show = subprocess.run(["git", "-C", str(ROOT), "show", f"{GOLDEN_REF}:{path}"], capture_output=True, text=True)
    if show.returncode != 0:
        raise CheckError(
            f"cannot read {path} from ref {GOLDEN_REF!r}: {show.stderr.strip() or 'git show failed'}\n"
            "set ORBIT_GOLDEN_REF to a ref this clone has, or unset it to use the working tree"
        )
    return show.stdout


def load_golden(tmp: Path) -> tuple[ModuleType, ModuleType]:
    """Import orbit.config and orbit.golden.score out of a throwaway package.

    Copied rather than imported in place because ``orbit`` is usually already on sys.path (and
    already in sys.modules under pytest), which would quietly compare the firmware against
    whichever copy got imported first.
    """
    pkg = tmp / "orbit"
    (pkg / "golden").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "golden" / "__init__.py").write_text("")
    for path in ("orbit/config.py", "orbit/golden/score.py"):
        (tmp / path).write_text(golden_source(path))
    sys.path.insert(0, str(tmp))
    from orbit import config
    from orbit.golden import score

    return config, score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40, help="frame/ref pairs to compare")
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            exe = build_host()
            _config, score = load_golden(tmp)
        except CheckError as err:  # no compiler, no such ref: unknown, which is not parity
            print(err, file=sys.stderr)
            return 2

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
