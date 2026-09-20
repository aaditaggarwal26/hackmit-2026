"""Build the LittleFS image set for one ESP32 satellite.

Lays out, for satellite <id>:
  /frames/NNN.bin   capture sequence, raw 128x128 8-bit gray, FRAME_BYTES each
  /refs/RRR.bin     each scene's reference frame, stored once
  /manifest.json    {"sat": "b", "frames": [{"frame": N, "ref": R}, ...]}

The capture order is the corpus's own deterministic per-node sequence, so satellite B and
satellite C see different imagery from the same corpus -- the only difference between the
two boards besides the hostname.

  uv run --with numpy python tools/build_fs_image.py --sat b --frames 40
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
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
# Which revision the ground's own modules are read from. Integration work lives on the branch
# this file sits on, so HEAD is the authoritative source; override to compare against another
# branch (ORBIT_GOLDEN_REF=origin/ground-station, or --golden-ref).
GOLDEN_REF = os.environ.get("ORBIT_GOLDEN_REF", "HEAD")
SAT_NODE_ID = {"b": 1, "c": 2}          # which corpus sequence each board follows (sim seq_seed)


def load_corpus_module(tmp: Path, ref: str = GOLDEN_REF) -> ModuleType:
    pkg = tmp / "orbit"
    (pkg / "corpus").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for path in ("orbit/config.py", "orbit/corpus/__init__.py"):
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{path}"],
                              capture_output=True, text=True, check=True).stdout
        (tmp / path).write_text(blob)
    # the module resolves the corpus relative to its own location, so point it at the real one
    (tmp / "corpus").symlink_to(ROOT / "corpus")
    sys.path.insert(0, str(tmp))
    from orbit import corpus  # noqa: E402
    return corpus


def capture_sequence(corp: Any, node_id: int, seed: int, n: int) -> list[int]:
    """The frames a satellite actually captures, in order.

    A scene's reference frame is a stored prior the satellite already carries (it ships in
    /refs), not something it captures again, so it is dropped from the capture order -- exactly
    as orbit/sim/satellite.py does when it builds ``FakeSatellite.sequence``. Leaving the
    references in handed the no-scoring FIFO baseline the corpus's cleanest frames for free,
    which is the margin corpus/gate_result.json measures.
    """
    return [i for i in corp.sequence(node_id, seed) if i != corp.reference_for(i)][:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sat", choices=sorted(SAT_NODE_ID), required=True)
    ap.add_argument("--golden-ref", default=GOLDEN_REF,
                    help=f"git revision the ground's modules are read from (default {GOLDEN_REF})")
    ap.add_argument("--frames", type=int, default=40, help="frames in the capture sequence")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None, help="output .bin (default firmware/build/littlefs_<sat>.bin)")
    ap.add_argument("--size", default="0x180000", help="SPIFFS partition size (default_8MB.csv: spiffs is 0x180000)")
    a = ap.parse_args()

    out = Path(a.out) if a.out else ROOT / f"firmware/build/littlefs_{a.sat}.bin"
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        corpus_mod = load_corpus_module(tmp, a.golden_ref)
        corp = corpus_mod.load()
        seq = capture_sequence(corp, SAT_NODE_ID[a.sat], a.seed, a.frames)

        root = tmp / "fs"
        (root / "frames").mkdir(parents=True)
        (root / "refs").mkdir(parents=True)

        entries, refs_written = [], set()
        for i, cid in enumerate(seq):
            (root / "frames" / f"{i:03d}.bin").write_bytes(np.asarray(corp.by_id(cid), dtype=np.uint8).tobytes())
            ref_id = corp.reference_for(cid)
            if ref_id not in refs_written:
                (root / "refs" / f"{ref_id:03d}.bin").write_bytes(
                    np.asarray(corp.by_id(ref_id), dtype=np.uint8).tobytes())
                refs_written.add(ref_id)
            entries.append({"frame": i, "ref": int(ref_id)})

        (root / "manifest.json").write_text(json.dumps({"sat": a.sat, "frames": entries}, separators=(",", ":")))

        payload = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
        print(f"{len(seq)} frames, {len(refs_written)} references, {payload/1024:.0f} KiB of payload")
        if payload > int(a.size, 16):
            print(f"ERROR: payload exceeds the {int(a.size,16)/1024:.0f} KiB partition", file=sys.stderr)
            return 1

        mklittlefs = next(Path.home().glob(".arduino15/packages/esp32/tools/mklittlefs/*/mklittlefs"), None)
        if mklittlefs is None:
            print("ERROR: mklittlefs not found; install the esp32 core first", file=sys.stderr)
            return 1

        cmd = [str(mklittlefs), "-c", str(root), "-b", "4096", "-p", "256", "-s", str(int(a.size, 16)), str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout + r.stderr, file=sys.stderr)
            return 1

    print(f"wrote {out}  ({out.stat().st_size/1024:.0f} KiB image)")
    print(f"flash with:  esptool --chip esp32s3 --port <port> write-flash 0x670000 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
