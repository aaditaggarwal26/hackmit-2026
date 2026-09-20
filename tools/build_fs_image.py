"""Build the LittleFS image set for one ESP32 satellite.

Lays out, for satellite <id>:
  /frames/NNN.bin   capture sequence, raw 128x128 8-bit gray, FRAME_BYTES each
  /refs/RRR.bin     each scene's reference frame, stored once
  /manifest.json    {"fmt": 2, "sat": "b",
                     "frames": [{"frame": N, "ref": R, "crc32": C, "ref_crc32": D}, ...]}

The capture order is the corpus's own deterministic per-node sequence, so satellite B and
satellite C see different imagery from the same corpus -- the only difference between the
two boards besides the hostname.

Every blob carries a CRC-32 of its exact bytes, which the firmware checks at boot before it
captures anything (firmware/satellite_esp32/orbit_fsimage.h). This catches a flash write that
came back "OK" but left the image short or corrupted; it is not a defence against tampering,
which is not the threat -- a bad USB cable is.

  uv run --with numpy python tools/build_fs_image.py --sat b --frames 40
  uv run --with numpy python tools/build_fs_image.py --sat b --fs-dir /tmp/img   # stage only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
# Which revision the ground's own modules are read from. Integration work lives on the branch
# this file sits on, so HEAD is the authoritative source; override to compare against another
# branch (ORBIT_GOLDEN_REF=origin/ground-station, or --golden-ref).
GOLDEN_REF = os.environ.get("ORBIT_GOLDEN_REF", "HEAD")
SAT_NODE_ID = {"b": 1, "c": 2}  # which corpus sequence each board follows (sim seq_seed)

# Manifest layout version. Bump when the firmware starts REQUIRING a new field, so a board still
# carrying an older image reports "manifest predates ..." once instead of failing every blob.
# Must equal ORBIT_MANIFEST_FMT in firmware/satellite_esp32/orbit_fsimage.h; the two are held
# together by tests/test_firmware_fsimage.py.
MANIFEST_FMT = 2


def load_corpus_module(tmp: Path, ref: str = GOLDEN_REF) -> ModuleType:
    pkg = tmp / "orbit"
    (pkg / "corpus").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for path in ("orbit/config.py", "orbit/corpus/__init__.py"):
        blob = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{ref}:{path}"], capture_output=True, text=True, check=True
        ).stdout
        (tmp / path).write_text(blob)
    # the module resolves the corpus relative to its own location, so point it at the real one
    (tmp / "corpus").symlink_to(ROOT / "corpus")
    sys.path.insert(0, str(tmp))
    from orbit import corpus

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


def _blob(corp: Any, cid: int) -> bytes:
    return bytes(np.asarray(corp.by_id(cid), dtype=np.uint8).tobytes())


def build_image(corp: Any, seq: list[int], sat: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    """The manifest, and the exact bytes of every blob it describes.

    The checksum is taken over the same ``bytes`` object that gets written, not over a second
    rendering of the frame -- a CRC of something other than what landed on flash would verify
    nothing. CRC-32 is zlib's (CRC-32/ISO-HDLC); orbit_crc32() in orbit_fsimage.h is the same
    function, and tests/test_firmware_fsimage.py proves it over this corpus.

    ``ref_crc32`` is repeated on every frame of a scene rather than given its own array. It
    costs a few hundred bytes of manifest and saves the firmware a second parse pass; the
    firmware reads each reference once regardless.
    """
    entries: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {}
    for i, cid in enumerate(seq):
        data = _blob(corp, cid)
        blobs[f"frames/{i:03d}.bin"] = data
        ref_id = int(corp.reference_for(cid))
        ref_path = f"refs/{ref_id:03d}.bin"
        if ref_path not in blobs:
            blobs[ref_path] = _blob(corp, ref_id)
        entries.append(
            {
                "frame": i,
                "ref": ref_id,
                "crc32": zlib.crc32(data),
                "ref_crc32": zlib.crc32(blobs[ref_path]),
            }
        )
    return {"fmt": MANIFEST_FMT, "sat": sat, "frames": entries}, blobs


def stage(root: Path, manifest: dict[str, Any], blobs: dict[str, bytes]) -> None:
    (root / "frames").mkdir(parents=True, exist_ok=True)
    (root / "refs").mkdir(parents=True, exist_ok=True)
    for name, data in blobs.items():
        (root / name).write_bytes(data)
    (root / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sat", choices=sorted(SAT_NODE_ID), required=True)
    ap.add_argument(
        "--golden-ref",
        default=GOLDEN_REF,
        help=f"git revision the ground's modules are read from (default {GOLDEN_REF})",
    )
    ap.add_argument("--frames", type=int, default=40, help="frames in the capture sequence")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None, help="output .bin (default firmware/build/littlefs_<sat>.bin)")
    ap.add_argument("--size", default="0x180000", help="SPIFFS partition size (default_8MB.csv: spiffs is 0x180000)")
    ap.add_argument(
        "--fs-dir",
        default=None,
        help="stage the image tree here and stop, instead of packing a .bin (needs no mklittlefs)",
    )
    a = ap.parse_args()

    out = Path(a.out) if a.out else ROOT / f"firmware/build/littlefs_{a.sat}.bin"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        corpus_mod = load_corpus_module(tmp, a.golden_ref)
        corp = corpus_mod.load()
        seq = capture_sequence(corp, SAT_NODE_ID[a.sat], a.seed, a.frames)

        manifest, blobs = build_image(corp, seq, a.sat)
        n_refs = sum(1 for k in blobs if k.startswith("refs/"))

        root = Path(a.fs_dir) if a.fs_dir else tmp / "fs"
        # Only ever clear a directory this tool itself staged. --fs-dir takes a path from the
        # command line, and rmtree on a mistyped one ("." , "~") is not a mistake that can be
        # undone; a manifest.json in it is the proof that it is a previous staging.
        if a.fs_dir and (root / "manifest.json").exists():
            shutil.rmtree(root)
        stage(root, manifest, blobs)

        payload = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
        print(f"{len(seq)} frames, {n_refs} references, {payload / 1024:.0f} KiB of payload")
        print(f"manifest fmt {MANIFEST_FMT}: {len(manifest['frames'])} frames, each with crc32 + ref_crc32")
        if payload > int(a.size, 16):
            print(f"ERROR: payload exceeds the {int(a.size, 16) / 1024:.0f} KiB partition", file=sys.stderr)
            return 1

        if a.fs_dir:
            print(f"staged {root}  (pack it with mklittlefs, or re-run without --fs-dir)")
            return 0

        mklittlefs = next(Path.home().glob(".arduino15/packages/esp32/tools/mklittlefs/*/mklittlefs"), None)
        if mklittlefs is None:
            print("ERROR: mklittlefs not found; install the esp32 core first", file=sys.stderr)
            print("       (--fs-dir DIR stages the same tree without it)", file=sys.stderr)
            return 1

        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [str(mklittlefs), "-c", str(root), "-b", "4096", "-p", "256", "-s", str(int(a.size, 16)), str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout + r.stderr, file=sys.stderr)
            return 1

    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KiB image)")
    print(f"flash with:  esptool --chip esp32s3 --port <port> write-flash 0x670000 {out}")
    # The manifest format changed: a board flashed from an older image cannot be verified and
    # will refuse to capture. BOTH boards have to be reflashed with this image set.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
