"""Where frames come from: a per-satellite ordered list of corpus frame ids.
The source hands the orchestrator one capture at a time and says which
reference frame the node must hold for it (a scene change means a new
REF_FRAME_SET before the next FRAME_INGEST). Scenarios are just different
orderings; the pixels are always the committed corpus."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from orbit import corpus as C


@dataclass
class Capture:
    frame_id: int
    frame: np.ndarray
    ref_id: int
    ref: np.ndarray
    scene: str
    index: int              # position in this satellite's sequence


class CorpusSource:
    def __init__(self, orders: list[list[int]], corpus: C.Corpus | None = None):
        self.corpus = corpus or C.load()
        self.orders = orders
        self.pos = [0] * len(orders)

    @property
    def n_nodes(self) -> int:
        return len(self.orders)

    def remaining(self, node: int) -> int:
        return len(self.orders[node]) - self.pos[node]

    def next_capture(self, node: int) -> Capture | None:
        if self.pos[node] >= len(self.orders[node]):
            return None
        fid = self.orders[node][self.pos[node]]
        self.pos[node] += 1
        ref_id = self.corpus.reference_for(fid)
        return Capture(fid, self.corpus.by_id(fid), ref_id, self.corpus.by_id(ref_id), self.corpus.scene_of(fid),
                       self.pos[node] - 1)

    def peek_all(self, node: int) -> list[int]:
        return self.orders[node][self.pos[node]:]

    def meta(self) -> dict:
        p = C.provenance()
        p.update(synthetic=C.SYNTHETIC, per_node=[len(o) for o in self.orders])
        return p
