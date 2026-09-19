"""Offline loader for the Earth-observation frame corpus in corpus/ (built by orbit.corpus.fetch).

NumPy only; never imports the fetcher, so the demo has no path to the network.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SYNTHETIC = False                 # real MODIS imagery, not generated
ROOT = Path(__file__).resolve().parents[2] / "corpus"


@dataclass
class Corpus:
    frames: np.ndarray            # uint8, N x 128 x 128
    ids: np.ndarray               # uint16, N; id == row index
    manifest: dict

    def by_id(self, id: int) -> np.ndarray:
        return self.frames[id]

    def scene_of(self, id: int) -> str:
        return self.manifest["frames"][id]["scene"]

    def reference_for(self, id: int) -> int:
        return self.manifest["scenes"][self.scene_of(id)]["reference_id"]

    def png_path(self, id: int) -> Path:
        return ROOT / "png" / f"{id:04d}.png"

    def sequence(self, node_id: int, seed: int = 0, n: int | None = None) -> list[int]:
        """Deterministic capture order for one satellite: scenes round-robin in a shuffled order,
        each scene's reference frame first so a change score always has its reference already seen."""
        rng = random.Random(seed * 1000 + node_id)
        queues = []
        for name in sorted(self.manifest["scenes"]):
            ref = self.manifest["scenes"][name]["reference_id"]
            rest = [f["id"] for f in self.manifest["frames"] if f["scene"] == name and f["id"] != ref]
            rng.shuffle(rest)
            queues.append([ref] + rest)
        rng.shuffle(queues)
        order = []
        while queues:
            for q in queues:
                order.append(q.pop(0))
            queues = [q for q in queues if q]
        if n is None:
            return order
        return [order[i % len(order)] for i in range(n)]


def load(root: Path = ROOT) -> Corpus:
    z = np.load(root / "frames.npz")
    return Corpus(z["frames"], z["ids"], json.loads((root / "manifest.json").read_text()))


def provenance(root: Path = ROOT) -> dict:
    m = json.loads((root / "manifest.json").read_text())
    return {k: m[k] for k in ("source", "layer", "licence", "fetched_utc")} | {"frames": len(m["frames"]), "synthetic": SYNTHETIC}
