"""The corpus gate (pivot brief §8 item 7): does scoring the committed corpus give a
distribution wide enough that prioritisation visibly beats FIFO through a window that
fits only some of the frames? Runs the golden model over every frame, then the real
orchestrator on sim:// nodes for a sweep of pass sizes, and writes
corpus/gate_result.json, the pacing the scenarios then use.

Rule (decided 2026-09-14, rule-continue): PASS when filtered/FIFO delivered value
>= 1.2 at a window holding half of each pass's captures. Otherwise the gate prints
FAIL and the caller stops.

    uv run python -m orbit.corpus.gate            # -> corpus/gate_result.json, prints the FINDINGS section
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

from orbit import corpus as C, params
from orbit.golden.score import Config, display, score_frame
from orbit.orchestrator.main import Orchestrator
from orbit.orchestrator.source import CorpusSource
from orbit.orchestrator.window import ContactWindow

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "corpus" / "gate_result.json"
RULE_RATIO = 1.2
WINDOW_S = 10.0


def score_all(corpus: C.Corpus, cfg: Config = Config()) -> list[dict]:
    out = []
    for i in corpus.ids.tolist():
        ref = corpus.reference_for(i)
        s = score_frame(corpus.by_id(i), corpus.by_id(ref), cfg)
        out.append(dict(id=i, scene=corpus.scene_of(i), ref=ref, is_ref=(i == ref), clear=s.clear, sharp=s.sharp,
                        change=s.change, score=s.score, display=display(s.score), cloud_px=s.cloud_px,
                        changed_px=s.changed_px, sobel_sum=s.sobel_sum))
    return out


def histogram(scores: list[int], bins: int = 10) -> list[dict]:
    counts, edges = np.histogram(scores, bins=bins, range=(0, 65535))
    return [dict(lo=display(int(edges[k])), hi=display(int(edges[k + 1])), n=int(counts[k])) for k in range(bins)]


def run_pair(corpus: C.Corpus, frames_per_pass: int, slots_per_pass: int, passes: int, seed: int,
             cfg: Config) -> dict:
    """Two sim nodes, real protocol, real orchestrator; returns delivered value both ways."""
    orders = [corpus.sequence(k, seed) for k in range(2)]
    o = Orchestrator(nodes=["sim://0", "sim://1"], source=CorpusSource(orders, corpus), scenario="gate",
                     window=ContactWindow.for_slots(slots_per_pass, WINDOW_S), frames_per_pass=frames_per_pass,
                     passes=passes, config=cfg, virtual=True)
    asyncio.run(o.run())
    o.close()
    assert all(n.mismatches == 0 for n in o.nodes)
    return dict(frames_per_pass=frames_per_pass, slots_per_pass=slots_per_pass, passes=passes, seed=seed,
                filtered=o.value_filtered, fifo=o.value_fifo,
                ratio=o.value_filtered / o.value_fifo if o.value_fifo else None,
                evicted=sum(n.mirror.evicted for n in o.nodes), fifo_dropped=o.fifo_dropped,
                slots=len(o.slots))


def choose_sharp_shift(corpus: C.Corpus) -> int:
    """Shift so that the median frame's sharp lands mid-range instead of saturating or vanishing."""
    sums = [score_frame(corpus.by_id(i), corpus.by_id(i)).sobel_sum for i in corpus.ids.tolist()]
    med = int(np.median(sums))
    for sh in range(params.SHARP_SHIFT_MAX + 1):
        if (med >> sh) <= 40000:
            return sh
    return params.SHARP_SHIFT_MAX


def main(argv=None) -> int:
    corpus = C.load()
    n = len(corpus.ids)
    shift = choose_sharp_shift(corpus)
    cfg = Config(sharp_shift=shift)
    rows = score_all(corpus, cfg)
    scores = [r["score"] for r in rows]
    comp = {k: [r[k] for r in rows] for k in ("clear", "sharp", "change")}
    stats = dict(n=n, min=display(min(scores)), p10=display(float(np.percentile(scores, 10))),
                 median=display(float(np.median(scores))), p90=display(float(np.percentile(scores, 90))),
                 max=display(max(scores)), spread=display(max(scores) - min(scores)),
                 components={k: dict(min=display(min(v)), median=display(float(np.median(v))), max=display(max(v)))
                             for k, v in comp.items()},
                 cloudy_frames=sum(1 for r in rows if r["clear"] < 32768),
                 changed_frames=sum(1 for r in rows if not r["is_ref"] and r["change"] > 16384))
    per_scene = {}
    for r in rows:
        per_scene.setdefault(r["scene"], []).append(r["display"])
    # sweep: a pass captures F frames per satellite; the window fits S of the 2F captured
    sweep = []
    for fpp in (6, 8, 12, 16):
        for spp in (2, 3, 4, 6, 8):
            if spp >= 2 * fpp:
                continue
            sweep.append(run_pair(corpus, fpp, spp, passes=3, seed=0, cfg=cfg))
    half = [s for s in sweep if s["slots_per_pass"] == s["frames_per_pass"]]     # window = half of the 2F captures
    half_ratio = min(s["ratio"] for s in half) if half else 0.0
    passed = half_ratio >= RULE_RATIO
    # demo pacing: the biggest gap among passes short enough for a live demo (<= 12 captures per satellite,
    # ~18 s of UART at 115200 baud) with visible contention (window <= a third of the captures)
    demo = max((s for s in sweep if s["ratio"] and s["frames_per_pass"] <= 12 and s["slots_per_pass"] * 3 <= 2 * s["frames_per_pass"]),
               key=lambda s: s["ratio"], default=half[0] if half else sweep[0])
    result = dict(date=time.strftime("%Y-%m-%d"), rule=f"filtered/FIFO >= {RULE_RATIO} at window = half of each pass's captures",
                  passed=passed, half_window_min_ratio=half_ratio, sharp_shift=shift, stats=stats,
                  histogram=histogram(scores), per_scene={k: dict(n=len(v), min=min(v), max=max(v)) for k, v in per_scene.items()},
                  sweep=sweep, frames_per_pass=demo["frames_per_pass"], slots_per_pass=demo["slots_per_pass"],
                  window_s=WINDOW_S, passes=4, demo_ratio=demo["ratio"],
                  link_bps_scaled=demo["slots_per_pass"] * params.FRAME_BYTES * 8 / WINDOW_S,
                  unscaled=dict(window_s=params.WINDOW_DURATION_S, link_bps=params.LINK_RATE_BPS,
                                slots=ContactWindow().slots_total))
    OUT.write_text(json.dumps(result, indent=1))
    print(markdown(result))
    print(f"\ngate: {'PASS' if passed else 'FAIL'} (min filtered/FIFO at half-window = {half_ratio:.2f}, rule {RULE_RATIO})",
          file=sys.stderr)
    return 0 if passed else 1


def markdown(r: dict) -> str:
    s = r["stats"]
    o = [f"## {r['date']} — corpus gate: score distribution and filtered-vs-FIFO gain", "",
         f"Corpus: {s['n']} frames (NASA GIBS MODIS Terra true colour, 128×128 grayscale; `corpus/manifest.json`). "
         f"Golden model, weights {Config().w_clear}/{Config().w_sharp}/{Config().w_change}, `sharp_shift={r['sharp_shift']}` "
         f"(chosen so the median frame's sharpness sits mid-range; `params.SHARP_SHIFT` must equal it).", "",
         "Scores displayed 0–100 (u16/655.35):", "",
         "| min | p10 | median | p90 | max | spread |", "|---|---|---|---|---|---|",
         f"| {s['min']:.1f} | {s['p10']:.1f} | {s['median']:.1f} | {s['p90']:.1f} | {s['max']:.1f} | {s['spread']:.1f} |", "",
         "Histogram (10 bins):", "", "| range | frames |", "|---|---|"]
    o += [f"| {h['lo']:.0f}–{h['hi']:.0f} | {h['n']} |" for h in r["histogram"]]
    o += ["", "Components (0–100): " + ", ".join(f"{k} min/median/max {v['min']:.0f}/{v['median']:.0f}/{v['max']:.0f}"
                                                 for k, v in s["components"].items()),
          f"Frames more than half cloud: {s['cloudy_frames']}; non-reference frames with > 25 % changed pixels: {s['changed_frames']}.", "",
          "Sweep — two sim satellites, real protocol, 3 passes; a pass captures F frames per satellite and the window fits S of them:", "",
          "| F | S | slots | filtered | FIFO | ratio | evicted | FIFO dropped |", "|---|---|---|---|---|---|---|---|"]
    o += [f"| {w['frames_per_pass']} | {w['slots_per_pass']} | {w['slots']} | {w['filtered']} | {w['fifo']} | "
          f"{w['ratio']:.2f} | {w['evicted']} | {w['fifo_dropped']} |" for w in r["sweep"]]
    o += ["", f"**Rule:** {r['rule']}. Minimum ratio at half-window: **{r['half_window_min_ratio']:.2f}** → "
          f"**{'PASS' if r['passed'] else 'FAIL'}**.", "",
          f"**Demo pacing chosen:** F={r['frames_per_pass']} frames per pass per satellite, S={r['slots_per_pass']} slots "
          f"per {r['window_s']:.0f} s window (ratio {r['demo_ratio']:.2f}). This is a *scaled* window: "
          f"{r['link_bps_scaled']:.0f} bit/s for {r['window_s']:.0f} s, versus the unscaled default of "
          f"{r['unscaled']['link_bps']:,} bit/s × {r['unscaled']['window_s']} s = {r['unscaled']['slots']:,} frames, "
          f"which would fit the whole corpus many times over and show no contention. The dashboard labels it scaled.", ""]
    return "\n".join(o)


if __name__ == "__main__":
    sys.exit(main())
