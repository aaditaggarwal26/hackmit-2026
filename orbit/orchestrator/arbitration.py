"""Per-slot arbitration (protocol.md §6): highest top score wins one slot; ties
to the lowest node id; after STARVATION_N consecutive wins the best other
candidate is granted instead. Pure, deterministic, unit-tested; the
orchestrator feeds it STATUS/TX_DONE facts and sends the GRANT."""
from __future__ import annotations

from dataclasses import dataclass, field

from orbit import params


@dataclass(frozen=True)
class Candidate:
    node: int
    has_data: bool
    top_score: int
    top_frame_id: int


@dataclass
class Decision:
    winner: int | None
    reason: str                      # "highest" | "starvation" | "none"
    candidates: list[Candidate]
    runner_up: int | None = None
    streak: int = 0                  # winner's consecutive grants including this one

    def as_dict(self) -> dict:
        return dict(winner=self.winner, reason=self.reason, runner_up=self.runner_up, streak=self.streak,
                    candidates=[c.__dict__ for c in self.candidates])


@dataclass
class Arbiter:
    starvation_n: int = params.STARVATION_N
    last_winner: int | None = None
    streak: int = 0
    starvation_switches: int = 0
    history: list[Decision] = field(default_factory=list)

    def decide(self, cands: list[Candidate]) -> Decision:
        ready = sorted((c for c in cands if c.has_data), key=lambda c: (-c.top_score, c.node))
        if not ready:
            d = Decision(None, "none", list(cands))
        else:
            best = ready[0]
            others = [c for c in ready if c.node != best.node]
            if best.node == self.last_winner and self.streak >= self.starvation_n and others:
                d = Decision(others[0].node, "starvation", list(cands), runner_up=best.node)
                self.starvation_switches += 1
            else:
                d = Decision(best.node, "highest", list(cands), runner_up=others[0].node if others else None)
            self.streak = self.streak + 1 if d.winner == self.last_winner else 1
            self.last_winner = d.winner
            d.streak = self.streak
        self.history.append(d)
        return d

    def reset(self) -> None:
        self.last_winner, self.streak = None, 0


if __name__ == "__main__":   # ponytail: the guard in four lines
    a = Arbiter(starvation_n=2)
    C = lambda n, s: Candidate(n, True, s, n)
    assert [a.decide([C(0, 90), C(1, 89)]).winner for _ in range(3)] == [0, 0, 1]   # third slot forced to node 1
    assert a.decide([C(0, 90), C(1, 89)]).winner == 0 and a.starvation_switches == 1
    assert a.decide([Candidate(0, False, 0, 0xFFFF), C(1, 5)]).winner == 1
    assert a.decide([Candidate(0, False, 0, 0xFFFF)]).winner is None
    print("ok")
