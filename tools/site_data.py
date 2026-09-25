"""Build the public site's frame data from the committed corpus, using the real kernel.

Every number the website shows about a frame comes from here: no random cloud
percentages, no invented scores. `orbit.golden.score.score_frame` is the same
function the simulated satellites call and the same kernel the ESP32 firmware
holds bit-exact (tools/check_score_parity.py).

    uv run python tools/site_data.py

Writes site/frames/<id>.png and site/data.json, and prints what it decided.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from orbit import config as params
from orbit import corpus as corpus_mod
from orbit.golden.queue import PriorityQueue
from orbit.golden.score import Config, score_frame

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site"
FRAMES_OUT = OUT / "frames"

# The pacing the corpus gate chose (corpus/gate_result.json): one pass captures
# 12 frames and the contact window carries 3 of them, over 4 passes.
FRAMES_PER_PASS = 12
SLOTS_PER_PASS = 3
PASSES = 4
NODE_ID = 1
SEED = params.Settings().seed  # 0

# The pass the interactive comparison on the site shows. Pass 0 is unrepresentative:
# corpus.sequence() emits every scene's reference frame first and a reference is
# picked for being the clearest in its scene, so pass 0 is all-clear by construction.
SHOWN_PASS = 2


def main() -> None:
    c = corpus_mod.load()
    cfg = Config()
    ids = c.sequence(node_id=NODE_ID, seed=SEED, n=FRAMES_PER_PASS * PASSES)

    frames = []
    for capture_index, fid in enumerate(ids):
        frame = c.by_id(fid)
        ref = c.by_id(c.reference_for(fid))
        s = score_frame(frame, ref, cfg)
        cloud_frac = s.cloud_px / params.FRAME_BYTES
        frames.append(
            {
                "id": int(fid),
                "capture_index": capture_index,
                "scene": c.scene_of(fid),
                "date": c.manifest["frames"][fid]["date"],
                "score": int(s.score),
                "display": round(s.score * 100.0 / 65535, 1),
                "cloud_frac": round(cloud_frac, 4),
                "cloud_px": int(s.cloud_px),
                "changed_px": int(s.changed_px),
                "sobel_sum": int(s.sobel_sum),
                "clear": int(s.clear),
                "sharp": int(s.sharp),
                "change": int(s.change),
                "usable": bool(cloud_frac <= params.Settings().usable_cloud_max),
                "is_reference": fid == c.reference_for(fid),
                "pass": capture_index // FRAMES_PER_PASS,
            }
        )

    by_id = {f["id"]: f for f in frames}
    limit = params.Settings().sat_buffer_slots

    # Two policies, the same captures, the same window. Orbit holds a priority
    # queue and spends each window best-first. The baseline holds the same depth
    # of buffer first-in-first-out and drops the newest when it is full, which is
    # what docs/event_stream.md specifies for the baseline path.
    orbit_q = PriorityQueue(limit=limit)
    fifo_buf: list[int] = []
    orbit_sent: list[int] = []
    fifo_sent: list[int] = []
    per_pass = []

    for p in range(PASSES):
        captures = [f for f in frames if f["pass"] == p]
        for f in captures:
            orbit_q.insert(f["score"], f["id"])
            if len(fifo_buf) < limit:
                fifo_buf.append(f["id"])
        o_this = [orbit_q.pop()[1] for _ in range(min(SLOTS_PER_PASS, len(orbit_q)))]
        f_this = [fifo_buf.pop(0) for _ in range(min(SLOTS_PER_PASS, len(fifo_buf)))]
        orbit_sent += o_this
        fifo_sent += f_this
        per_pass.append(
            {
                "pass": p,
                "captured": [f["id"] for f in captures],
                "orbit_sent": o_this,
                "fifo_sent": f_this,
                "orbit_usable": sum(1 for i in o_this if by_id[i]["usable"]),
                "fifo_usable": sum(1 for i in f_this if by_id[i]["usable"]),
            }
        )

    orbit_usable = sum(1 for i in orbit_sent if by_id[i]["usable"])
    fifo_usable = sum(1 for i in fifo_sent if by_id[i]["usable"])
    result = {
        "orbit": {"sent": orbit_sent, "usable": orbit_usable},
        "fifo": {"sent": fifo_sent, "usable": fifo_usable},
        "gain": round(orbit_usable / fifo_usable, 3) if fifo_usable else None,
        "frames_down": len(orbit_sent),
        "bytes_used": len(orbit_sent) * params.FRAME_BYTES,
        "slots_per_pass": SLOTS_PER_PASS,
        "frames_per_pass": FRAMES_PER_PASS,
        "passes": PASSES,
        "queue_limit": limit,
        "bytes_per_frame": params.FRAME_BYTES,
        "usable_cloud_max": params.Settings().usable_cloud_max,
        "per_pass": per_pass,
    }

    FRAMES_OUT.mkdir(parents=True, exist_ok=True)
    shown = [f for f in frames if f["pass"] == SHOWN_PASS]
    for f in shown:
        shutil.copyfile(c.png_path(f["id"]), FRAMES_OUT / f"{f['id']:04d}.png")

    payload = {
        "generated_by": "tools/site_data.py",
        "kernel": "orbit/golden/score.py",
        "corpus": {
            "frames_total": len(c.ids),
            "source": c.manifest["source"],
            "layer": c.manifest["layer"],
            "licence": c.manifest["licence"],
            "fetched_utc": c.manifest["fetched_utc"],
        },
        "weights": {"clear": params.W_CLEAR, "sharp": params.W_SHARP, "change": params.W_CHANGE},
        "thresholds": {
            "cloud": params.CLOUD_THRESHOLD,
            "change": params.CHANGE_THRESHOLD,
            "sharp_shift": params.SHARP_SHIFT,
        },
        "run": {"node_id": NODE_ID, "seed": SEED, "shown_pass": SHOWN_PASS},
        "shown_frames": shown,
        "window": result,
    }
    (OUT / "data.json").write_text(json.dumps(payload, indent=2) + "\n")

    print(f"node {NODE_ID} seed {SEED}: {PASSES} passes x {FRAMES_PER_PASS} frames, {SLOTS_PER_PASS} slots per pass")
    print(f"shown pass {SHOWN_PASS}:")
    for f in shown:
        mark = "usable" if f["usable"] else "NOT   "
        print(
            f"  #{f['capture_index']:2d} id {f['id']:3d} {f['scene']:<12} {f['display']:5.1f}"
            f"  cloud {f['cloud_frac']:.3f}  {mark}"
        )
    for p in per_pass:
        print(
            f"  pass {p['pass']}: orbit {p['orbit_usable']}/{SLOTS_PER_PASS}  fifo {p['fifo_usable']}/{SLOTS_PER_PASS}"
        )
    print(
        f"RUN: orbit {orbit_usable}/{len(orbit_sent)} usable,"
        f" fifo {fifo_usable}/{len(fifo_sent)}, gain {result['gain']}"
    )


if __name__ == "__main__":
    main()
