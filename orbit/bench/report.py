"""Pure functions: the labelled-number shape every figure uses, the small statistics the
runner needs, and the plain-text / markdown renderings of a result.

Why a labelled shape instead of bare floats: the tiers are measured with different
instruments over different scopes (GPU die via NVML; nothing at all for the CPU rail).
A bare 10.2 next to a bare 4.1 invites the reader to subtract them. Every value in a
result therefore travels with its `scope`, its `method` and a `measured` flag, and
the renderers print the scope next to the number rather than in a footnote."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from typing import Any

import numpy as np

UNAVAILABLE = "unavailable"
NS_PER_MS = 1e6
FRAMES_PER_KILO = 1000.0
Measure = dict[str, Any]


def measured(value: float | bool, unit: str, method: str, scope: str) -> Measure:
    return {"value": value, "unit": unit, "method": method, "scope": scope, "measured": True}


def unavailable(reason: str, method: str, scope: str) -> Measure:
    return {"value": UNAVAILABLE, "reason": reason, "method": method, "scope": scope, "measured": False}


def is_measured(m: Measure | None) -> bool:
    return bool(m) and m is not None and m.get("measured") is True and isinstance(m.get("value"), int | float)


def value_of(m: Measure) -> float:
    if not is_measured(m):
        raise ValueError(f"not a measured value: {m.get('reason', m)}")
    return float(m["value"])


# --- statistics ------------------------------------------------------------------------
def mean(xs: Sequence[float]) -> float:
    return statistics.fmean(xs)


def stddev(xs: Sequence[float]) -> float | None:
    """Sample stddev (n-1); None below two points rather than a fake 0.0."""
    return statistics.stdev(xs) if len(xs) >= 2 else None


def percentile(xs: Sequence[float], p: float) -> float:
    """Linear interpolation between order statistics (numpy's default), p in 0..100."""
    return float(np.percentile(np.asarray(xs, dtype=np.float64), p))


def latency_stats_ms(latencies_ns: Sequence[int], percentiles: Sequence[int]) -> dict[str, float]:
    ms = [ns / NS_PER_MS for ns in latencies_ns]
    out = {"mean": mean(ms)}
    for p in percentiles:
        out["median" if p == 50 else f"p{p}"] = percentile(ms, p)
    return out


def j_per_1000(energy_j: float, frames: int) -> float:
    return energy_j * FRAMES_PER_KILO / frames


def mean_std(ms: Sequence[Measure]) -> dict[str, Measure]:
    """Across-rep summary of one figure, inheriting its unit/method/scope so a summary can
    never be labelled differently from the reps it came from; unavailable if any rep was."""
    method = str(ms[0].get("method", "")) if ms else ""
    scope = str(ms[0].get("scope", "")) if ms else ""
    unit = str(ms[0].get("unit", "")) if ms else ""
    if not ms or not all(is_measured(m) for m in ms):
        reason = next((str(m["reason"]) for m in ms if not is_measured(m)), "no reps")
        return {"mean": unavailable(reason, method, scope), "std": unavailable(reason, method, scope)}
    xs = [value_of(m) for m in ms]
    sd = stddev(xs)
    return {
        "mean": measured(mean(xs), unit, f"mean over {len(xs)} reps of: {method}", scope),
        "std": measured(sd, unit, f"sample stddev over {len(xs)} reps of: {method}", scope)
        if sd is not None
        else unavailable("stddev needs >= 2 reps", method, scope),
    }


# --- rendering -------------------------------------------------------------------------
def fmt(m: Measure | None, digits: int = 2, scope: bool = True) -> str:
    """'10.21 W [gpu_die]' or 'n/a' — the scope rides with the number on purpose."""
    if m is None:
        return "-"
    if not is_measured(m):
        return "n/a"
    v = m["value"]
    if isinstance(v, bool):
        s = "yes" if v else "no"
    elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        s = str(v)
    else:
        s = f"{float(v):.{digits}f}"
    if m.get("unit") and m["unit"] not in ("bool", "s"):
        s += f" {m['unit']}"
    if scope and m.get("scope"):
        s += f" [{m['scope']}]"
    return s


def _pm(pair: dict[str, Measure], digits: int = 2) -> str:
    if not is_measured(pair["mean"]):
        return "n/a"
    s = f"{value_of(pair['mean']):.{digits}f}"
    if is_measured(pair["std"]):
        s += f" ±{value_of(pair['std']):.{digits}f}"
    unit = pair["mean"].get("unit", "")
    if unit:
        s += f" {unit}"
    return f"{s} [{pair['mean'].get('scope', '')}]"


TABLE_COLUMNS = (
    "tier",
    "frames/s",
    "ms/frame p50",
    "ms/frame p99",
    "mean W",
    "marginal W",
    "J/1000 fr (marginal)",
    "throttled",
)


def rows_for(result: dict[str, Any]) -> list[str]:
    s = result["summary"]
    reps = result["reps"]
    first = reps[0] if reps else {}
    return [
        result["tier"],
        _pm(s["frames_per_s"]),
        fmt(first.get("ms_per_frame", {}).get("median"), 3, scope=False) if reps else "n/a",
        fmt(first.get("ms_per_frame", {}).get("p99"), 3, scope=False) if reps else "n/a",
        _pm(s["mean_w"]),
        _pm(s["marginal_w"]),
        _pm(s["j_per_1000_frames_marginal"]),
        fmt(result["thermal"]["throttling_observed"], scope=False),
    ]


def table(results: Sequence[dict[str, Any]]) -> str:
    """Plain text; rich is not installed and this must survive a copy-paste into a chat."""
    rows = [list(TABLE_COLUMNS)] + [rows_for(r) for r in results]
    widths = [max(len(r[i]) for r in rows) for i in range(len(TABLE_COLUMNS))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    notes = ["", "scopes: [gpu_die] = NVML GPU-die counter only, excludes CPU, memory and board;"]
    notes.append("        marginal W = rep mean W - cold idle mean W (idle recorded before any load or CUDA context);")
    notes.append("        n/a = not measurable on this machine (see bench.md / bench.jsonl for the reason).")
    return "\n".join(lines + notes)


def _reason_lines(result: dict[str, Any]) -> list[str]:
    seen: dict[str, str] = {}
    stack: list[Any] = [result["summary"], result["thermal"], result["idle"]]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("value") == UNAVAILABLE and node.get("reason"):
                seen.setdefault(str(node.get("scope", "")) + ": " + str(node["reason"]), "")
            else:
                stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return [f"- unavailable ({k})" for k in sorted(seen)]


def markdown(result: dict[str, Any]) -> str:
    """One block per run: the methodology stated inline, every figure with its scope."""
    st = result["settings"]
    s = result["summary"]
    env = result["environment"]
    lines = [
        f"## {result['tier']} — {result['timestamp']} on {result['hostname']}",
        "",
        f"Workload: {result['workload']['description']}.",
        "",
        "Methodology: (a) idle baseline recorded for "
        f"{st['idle_s']} s with all samplers running before any load; (b) one warm-up run of {st['warmup_s']} s, "
        f"discarded; (c) {st['reps']} timed repetitions of {st['duration_s']} s, per-frame latency via "
        "time.perf_counter_ns; (d) per rep: frames, frames/s, latency percentiles, energy, mean W, marginal W "
        "(load minus idle), J per 1000 frames total and marginal; (e) mean and sample stddev across reps; "
        f"(f) thermal throttling flag from Processor cooling devices. Samplers polled at {st['sample_hz']} Hz.",
        "",
        "| figure | value | scope | method |",
        "|---|---|---|---|",
    ]
    for label, pair in (
        ("frames/s", s["frames_per_s"]),
        ("mean W", s["mean_w"]),
        ("marginal W (load - idle)", s["marginal_w"]),
        ("J per 1000 frames (total)", s["j_per_1000_frames"]),
        ("J per 1000 frames (marginal)", s["j_per_1000_frames_marginal"]),
        ("marginal W vs armed idle (CUDA context resident)", s.get("marginal_w_vs_armed_idle")),
        ("J per 1000 frames (marginal vs armed idle)", s.get("j_per_1000_frames_marginal_vs_armed_idle")),
    ):
        if pair is None:
            continue
        m = pair["mean"]
        lines.append(f"| {label} | {_pm(pair)} | {m.get('scope', '')} | {m.get('method', '') or m.get('reason', '')} |")
    th = result["thermal"]
    for label, m, digits in (
        ("throttling observed", th["throttling_observed"], 2),
        ("max thermal-zone temp", th["max_temp_c"], 1),
    ):
        how = m.get("method", "") or m.get("reason", "")
        lines.append(f"| {label} | {fmt(m, digits, scope=False)} | {m.get('scope', '')} | {how} |")
    for label, phase in (("cold idle mean W", "idle"), ("armed idle mean W", "idle_armed")):
        idle_w = result.get(phase, {}).get("gpu_die", {}).get("mean_w")
        if idle_w is not None:
            how = result[phase]["duration_s"]["method"]
            lines.append(f"| {label} | {fmt(idle_w, scope=False)} | {idle_w.get('scope', '')} | {how} |")
    lines += [
        "",
        "Scope note: every power/energy figure above is GPU-die only (NVML energy counter); it excludes the CPU, "
        "memory and the rest of the board. There is no CPU-rail or board meter on this machine, so CPU-tier power "
        "is unavailable, not zero, and the GPU-die reading recorded during the CPU run is context, not CPU power. "
        "Die figures and board figures are never to be compared as if they were the same quantity.",
        "",
        f"Environment: python {env.get('python')}, numpy {env.get('numpy')}, torch {env.get('torch')}, "
        f"driver {env.get('nvidia_driver')}, cpu {env.get('cpu_model')}, pinned core {env.get('pinned_core')}, "
        f"gpu processes present: {env.get('gpu_processes')}.",
        "",
    ]
    lines += _reason_lines(result)
    lines.append("")
    return "\n".join(lines)
