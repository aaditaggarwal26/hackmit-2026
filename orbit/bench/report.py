"""Joules per thousand frames scored, with provenance. Missing measurements
print TBD; there are no placeholder numbers anywhere in this module."""
from __future__ import annotations

import json
import os
import time

from .energy import TBD, tbd

COLUMNS = ["platform", "provenance", "frames/s", "mean W", "J per 1000 frames"]
FOOTNOTE = ("Provenance: `device-level` = Jetson VDD_CPU_GPU_CV rail or Vivado on-chip estimate; "
            "`module-level` = Jetson VDD_IN (module input, excludes the dev-kit carrier board); "
            "`whole-board` = Arty A7 via INA219 on its supply input (includes FTDI and regulators); "
            "`estimate` = Vivado report_power, not a measurement. "
            "Rows are like-for-like only where the labels match.")


def joules_per_thousand(joules: float | None, frames: int) -> float | None:
    if joules is None or not frames:
        return None
    return joules / frames * 1e3


def first_available(*results: dict) -> dict:
    """Fallback chain (Arty: INA219 -> Vivado estimate -> TBD): the first result
    carrying a measurement, else TBD."""
    for r in results:
        if r and r.get("joules") is not None:
            return r
    return tbd("none", tried=[r.get("source") for r in results if r])


def fmt(v, digits=2) -> str:
    return TBD if v is None else f"{v:,.{digits}f}"


class BenchReport:
    def __init__(self):
        self.entries: list[dict] = []

    def add(self, name: str, frames: int, seconds: float, sampler: dict, **notes) -> dict:
        """J/1000 = sampler mean W x the workload's own window seconds: the sampler's
        integrated span never lines up exactly with the window (a POWER frame
        every 250 ms), and this way the mismatch cannot bias the figure. The
        raw integrated joules stay in the JSON as `sampler.joules` for audit."""
        w = sampler.get("mean_watts")
        joules = w * seconds if w is not None and seconds else None
        e = dict(platform=name, frames=frames, seconds=seconds,
                 frames_per_s=frames / seconds if seconds else None,
                 provenance=sampler.get("label", TBD), mean_watts=w,
                 joules=joules, j_per_thousand=joules_per_thousand(joules, frames),
                 sampler=sampler, notes=notes)
        self.entries.append(e)
        return e

    def markdown(self) -> str:
        lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
        for e in self.entries:
            lines.append(f"| {e['platform']} | {e['provenance']} | {fmt(e['frames_per_s'], 0)} | "
                         f"{fmt(e['mean_watts'], 3)} | {fmt(e['j_per_thousand'], 4)} |")
        notes = [f"- {e['platform']}: " + "; ".join(f"{k}={v}" for k, v in e["notes"].items())
                 for e in self.entries if e["notes"]]
        return "\n".join(lines + [""] + notes + ["", FOOTNOTE, ""])

    def save(self, directory: str = "bench/results", stamp: str | None = None) -> tuple[str, str]:
        os.makedirs(directory, exist_ok=True)
        stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
        md, js = os.path.join(directory, f"{stamp}.md"), os.path.join(directory, f"{stamp}.json")
        with open(md, "w") as f:
            f.write(f"# Orbit bench {stamp}\n\n" + self.markdown())
        with open(js, "w") as f:
            json.dump(self.entries, f, indent=1, default=str)
        return md, js
