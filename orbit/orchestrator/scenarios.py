"""Named, seeded scenarios: each is an ordering of corpus frames per satellite
plus window/pacing parameters, built into a configured Orchestrator.

    nominal          each satellite captures its own shuffled sequence
    lead_change      satellite 0 starts with its best frames, exhausts them, 1 takes over
    starvation       satellite 1 always holds better frames; the guard forces 0 through
    filtered_vs_fifo nominal with more passes: the headline chart
    scaling          N satellites (however many --nodes), nominal ordering each

Window/pacing defaults come from corpus/gate_result.json when the gate has
run (scaled so a pass fits a handful of frames and contention is visible),
else from the fallbacks below. Every snapshot says which applied.
"""
from __future__ import annotations

import json
from pathlib import Path

from orbit import corpus as C, params
from orbit.golden.score import Config, score_frame
from orbit.orchestrator.main import Orchestrator
from orbit.orchestrator.source import CorpusSource
from orbit.orchestrator.window import ContactWindow

ROOT = Path(__file__).resolve().parents[2]
GATE_RESULT = ROOT / "corpus" / "gate_result.json"
FALLBACK = dict(frames_per_pass=12, slots_per_pass=4, window_s=10.0, passes=4, source="fallback (gate not run)")


def gate_params() -> dict:
    if GATE_RESULT.exists():
        g = json.loads(GATE_RESULT.read_text())
        return dict(frames_per_pass=g["frames_per_pass"], slots_per_pass=g["slots_per_pass"], window_s=g["window_s"],
                    passes=g.get("passes", FALLBACK["passes"]), source=f"corpus/gate_result.json ({g.get('date')})")
    return dict(FALLBACK)


def scored_ids(corpus: C.Corpus, cfg: Config = Config()) -> dict[int, int]:
    """Golden score of every corpus frame against its scene reference (ground-side plan only)."""
    return {int(i): score_frame(corpus.by_id(int(i)), corpus.by_id(corpus.reference_for(int(i))), cfg).score
            for i in corpus.ids}


def _by_scene_then_score(ids: list[int], corpus: C.Corpus, scores: dict[int, int], reverse: bool) -> list[int]:
    """Keep frames grouped by scene (a reference precedes the frames it serves) but order
    scenes and frames within a scene by score."""
    scenes: dict[str, list[int]] = {}
    for i in ids:
        scenes.setdefault(corpus.scene_of(i), []).append(i)
    ordered = []
    for sc in sorted(scenes, key=lambda s: -max(scores[i] for i in scenes[s]) if reverse else min(scores[i] for i in scenes[s])):
        ordered += sorted(scenes[sc], key=lambda i: -scores[i] if reverse else scores[i])
    return ordered


def orders_for(name: str, n_nodes: int, seed: int, corpus: C.Corpus) -> list[list[int]]:
    seqs = [corpus.sequence(k, seed) for k in range(n_nodes)]
    if name in ("nominal", "filtered_vs_fifo", "scaling"):
        return seqs
    scores = scored_ids(corpus)
    if name == "lead_change":
        best_first = _by_scene_then_score(seqs[0], corpus, scores, reverse=True)
        return [best_first] + seqs[1:]
    if name == "starvation":
        ranked = sorted(corpus.ids.tolist(), key=lambda i: scores[i])
        half = len(ranked) // 2
        low, high = ranked[:half], ranked[half:]
        node0 = [i for i in seqs[0] if i in set(low)]
        node1 = [i for i in seqs[1] if i in set(high)]
        return [node0, node1] + seqs[2:]
    raise KeyError(name)


SCENARIOS = {"nominal": "each satellite captures its own shuffled sequence",
             "lead_change": "satellite 0 starts with its best frames, exhausts them, 1 takes over",
             "starvation": "satellite 1 always holds better frames; the guard forces 0 through",
             "filtered_vs_fifo": "nominal with more passes: the headline chart",
             "scaling": "N satellites, nominal ordering each"}


def build(name: str, nodes: list[str] = params.NODES, seed: int = 0, passes: int | None = None,
          frames_per_pass: int | None = None, slots_per_pass: int | None = None, window_s: float | None = None,
          starvation_n: int = params.STARVATION_N, **orch_kw) -> Orchestrator:
    if name not in SCENARIOS:
        raise KeyError(name)
    g = gate_params()
    corpus = C.load()
    orders = orders_for(name, len(nodes), seed, corpus)
    if name == "filtered_vs_fifo" and passes is None:
        passes = g["passes"] * 2
    window = ContactWindow.for_slots(slots_per_pass or g["slots_per_pass"], window_s or g["window_s"])
    return Orchestrator(nodes=nodes, source=CorpusSource(orders, corpus), scenario=name, window=window,
                        frames_per_pass=frames_per_pass or g["frames_per_pass"], passes=passes or g["passes"],
                        starvation_n=starvation_n, pacing_source=g["source"], **orch_kw)
