"""Run one tier of the efficiency benchmark and persist a fully-labelled result.

    python -m orbit.bench.runner --tier cpu,gpu [--idle-s 30 --duration-s 10 --reps 3]

Methodology, in the order it happens, because it will be challenged: the corpus is
loaded and the tier's kernel is checked against the golden model (a wrong kernel raises,
it is never timed); then (a) an idle baseline of `idle_s` is recorded with every sampler
running and no load; (b) one warm-up run of `warmup_s` is discarded; (c) `reps` timed
runs of `duration_s` follow, each with fresh samplers so its energy is a clean counter
delta and each frame's latency from perf_counter_ns; (d) per-rep and (e) across-rep
figures are derived from those windows only; (f) any Processor cooling device above
state 0 during the session flags throttling. Rule one: no number that was not measured
here appears anywhere; unmeasurable fields say "unavailable" and why."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from orbit.bench import report
from orbit.bench.report import Measure, is_measured, j_per_1000, latency_stats_ms, measured, unavailable, value_of
from orbit.bench.sampler import (
    SCOPE_GPU_DIE,
    CpuUtilSampler,
    NvmlSampler,
    Sampler,
    ThermalSampler,
    Unavailable,
)
from orbit.bench.workload import DESCRIPTION, Pair, assert_identical, cuda_device, pairs, score_numpy, score_torch
from orbit.config import BenchSettings
from orbit.corpus import load
from orbit.golden.score import Config
from orbit.log import log, setup

logger = logging.getLogger("orbit.bench.runner")

SCHEMA = 1
TIERS = ("cpu", "gpu", "esp32", "ugen300")
PLACEHOLDER_REASON = {"esp32": "measured later on hardware", "ugen300": "HailoRT unavailable"}
NO_CPU_POWER = "no CPU or board power instrumentation on GX10; needs inline USB-C PD meter"
SCOPE_CPU_RAIL = "cpu_rail"
SCOPE_WORKLOAD = "workload"
METHOD_IDLE_COLD = "sleep with samplers running before any load: no corpus loaded, no CUDA context"
METHOD_IDLE_ARMED = "sleep with samplers running after CUDA context creation and tensor upload, no work"
MARGINAL_KEYS = ("marginal_w", "j_per_1000_frames_marginal")
ARMED_KEYS = ("marginal_w_vs_armed_idle", "j_per_1000_frames_marginal_vs_armed_idle")
METHOD_NONE = "none"
METHOD_LATENCY = "time.perf_counter_ns around each single-frame score call"
METHOD_FPS = "frames completed / wall time of the rep (perf_counter_ns)"
METHOD_COUNT = "count of single-frame score calls completed in the rep"
CPU_SYS = Path("/sys/devices/system/cpu")
NS_PER_S = 1e9
NVIDIA_SMI = ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv"]
SUBPROCESS_TIMEOUT_S = 10.0
RUN_ID_FORMAT = "%Y%m%dT%H%M%S.%fZ"  # microseconds: raw files from back-to-back runs must never collide
MAX_SAME_STAMP = 1000
ARM_PARTS = {"0xd85": "Cortex-X925", "0xd87": "Cortex-A725"}  # decode of /proc/cpuinfo "CPU part" on this SoC

ScoreFn = Callable[[int], int]  # pair index -> score
Windows = dict[str, dict[str, Any]]


# --- machine facts ------------------------------------------------------------------------
def pick_big_core(settings: BenchSettings) -> tuple[int | None, str]:
    """The core the CPU tier pins to and how it was chosen; None if cpufreq is unreadable."""
    if settings.big_core is not None:
        return settings.big_core, "BenchSettings.big_core"
    best: tuple[int, int] | None = None
    try:
        cores = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None, "sched_getaffinity unavailable"
    for c in cores:
        try:
            khz = int((CPU_SYS / f"cpu{c}/cpufreq/cpuinfo_max_freq").read_text())
        except (OSError, ValueError):
            continue
        if best is None or khz > best[1]:
            best = (c, khz)
    return (best[0], f"highest cpuinfo_max_freq = {best[1]} kHz") if best else (None, "cpufreq not readable")


def _cpu_model(core: int | None) -> str:
    try:
        blocks = Path("/proc/cpuinfo").read_text().split("\n\n")
    except OSError as e:
        return f"unavailable: {e}"
    block = blocks[core] if core is not None and core < len(blocks) else blocks[0]
    for line in block.splitlines():
        k, _, v = line.partition(":")
        if k.strip() == "model name":
            return v.strip()
        if k.strip() == "CPU part":
            return ARM_PARTS.get(v.strip(), f"ARM part {v.strip()}")
    return platform.processor() or platform.machine()


def _gpu_processes() -> str:
    try:
        out = subprocess.run(NVIDIA_SMI, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S, check=False)
        rows = out.stdout.strip().splitlines()[1:]  # drop the CSV header; an empty list means no GPU clients
        if out.returncode != 0:
            return f"unavailable: {out.stderr.strip() or out.stdout.strip()}"
        return " / ".join(r.strip() for r in rows) or "none"
    except (OSError, subprocess.SubprocessError) as e:
        return f"unavailable: {e}"


def _nvml_facts() -> dict[str, str]:
    try:
        import pynvml

        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return {"nvidia_driver": str(pynvml.nvmlSystemGetDriverVersion()), "gpu_name": str(pynvml.nvmlDeviceGetName(h))}
    except Exception as e:
        return {"nvidia_driver": f"unavailable: {e}", "gpu_name": f"unavailable: {e}"}


def _torch_version() -> str:
    try:
        import torch

        return str(torch.__version__)
    except ImportError as e:
        return f"unavailable: {e}"


def environment(core: int | None, core_reason: str) -> dict[str, Any]:
    gov = CPU_SYS / f"cpu{core}/cpufreq/scaling_governor"
    return {
        "python": platform.python_version(),
        "numpy": str(np.__version__),
        "torch": _torch_version(),
        **_nvml_facts(),
        "cpu_model": _cpu_model(core),
        "pinned_core": core,
        "pinned_core_reason": core_reason,
        "governor": gov.read_text().strip() if gov.exists() else "unavailable",
        "gpu_processes": _gpu_processes(),
        "kernel": platform.release(),
        "machine": platform.machine(),
    }


# --- phases ----------------------------------------------------------------------------------
def _samplers(settings: BenchSettings, core: int | None) -> dict[str, Sampler]:
    return {
        SCOPE_GPU_DIE: NvmlSampler(settings.sample_hz),
        "thermal": ThermalSampler(settings.sample_hz),
        "cpu_util": CpuUtilSampler(settings.sample_hz, core),
        SCOPE_CPU_RAIL: Unavailable(SCOPE_CPU_RAIL, NO_CPU_POWER),
    }


def _windowed(
    settings: BenchSettings, core: int | None, work: Callable[[], Any]
) -> tuple[Any, Windows, dict[str, Any]]:
    """Start every sampler, do `work`, stop every sampler: one clean window per phase."""
    ss = _samplers(settings, core)
    for s in ss.values():
        s.start()
    out = work()
    windows = {k: s.stop() for k, s in ss.items()}
    raw: dict[str, Any] = {k: getattr(s, "raw", list)() for k, s in ss.items()}
    return out, windows, raw


def _split_counts(windows: Windows) -> tuple[Windows, dict[str, dict[str, int]]]:
    """Bare ints in a sampler window are bookkeeping (sample counts), not figures; keep them apart."""
    figures: Windows = {}
    counts: dict[str, dict[str, int]] = {}
    for k, w in windows.items():
        figures[k] = {kk: v for kk, v in w.items() if not (isinstance(v, int) and not isinstance(v, bool))}
        counts[k] = {kk: v for kk, v in w.items() if isinstance(v, int) and not isinstance(v, bool)}
    return figures, counts


def _sleep(seconds: float, tick: float) -> None:
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(tick, left))


def _loop(score: ScoreFn, n: int, seconds: float, core: int | None) -> tuple[list[int], float]:
    """Score frames round-robin until `seconds` elapse; returns per-frame ns and the wall time.
    Affinity is set here, after the samplers started, so only the workload thread is pinned."""
    before: set[int] | None = None
    if core is not None:
        try:
            before = os.sched_getaffinity(0)
            os.sched_setaffinity(0, {core})
        except (AttributeError, OSError) as e:
            log(logger, logging.WARNING, "could not pin", core=core, err=str(e))
    lat: list[int] = []
    i = 0
    t_begin = time.perf_counter_ns()
    deadline = t_begin + int(seconds * NS_PER_S)
    while True:
        t0 = time.perf_counter_ns()
        score(i % n)
        t1 = time.perf_counter_ns()
        lat.append(t1 - t0)
        i += 1
        if t1 >= deadline:
            break
    if before is not None:
        os.sched_setaffinity(0, before)
    return lat, (t1 - t_begin) / NS_PER_S


def _power_of(tier: str, windows: Windows) -> tuple[Measure, Measure]:
    """(energy_j, mean_w) for the tier's *own* power scope; the GPU die's window is only the
    GPU tier's power. For the CPU tier it is context and is kept under its own key."""
    if tier == "gpu":
        w = windows[SCOPE_GPU_DIE]
        return w.get("energy_j", w), w.get("mean_w", w)
    return unavailable(NO_CPU_POWER, METHOD_NONE, SCOPE_CPU_RAIL), unavailable(
        NO_CPU_POWER, METHOD_NONE, SCOPE_CPU_RAIL
    )


def _marginal(mean_w: Measure, idle_w: Measure, wall_s: float, frames: int, baseline: str) -> tuple[Measure, Measure]:
    """Load minus a named idle baseline, and the J/1000 frames that difference implies."""
    scope = str(mean_w["scope"])
    if not (is_measured(mean_w) and is_measured(idle_w)):
        why = str(mean_w.get("reason") or idle_w.get("reason") or "idle power missing")
        return unavailable(why, METHOD_NONE, scope), unavailable(why, METHOD_NONE, scope)
    marginal = value_of(mean_w) - value_of(idle_w)
    # A negative marginal is a real measurement of noise, not a saving: say so where the number is quoted.
    note = "" if marginal >= 0 else " [NOTE: load measured below the idle baseline; difference is within noise]"
    return (
        measured(marginal, "W", f"rep mean W - {baseline} mean W; {mean_w['method']}{note}", scope),
        measured(
            j_per_1000(marginal * wall_s, frames),
            "J",
            f"(rep mean W - {baseline} mean W) * wall s * 1000 / frames{note}",
            scope,
        ),
    )


def _rep(
    tier: str,
    lat: list[int],
    wall_s: float,
    windows: Windows,
    idle_w: Measure,
    armed_idle_w: Measure | None,
    settings: BenchSettings,
) -> dict[str, Any]:
    frames = len(lat)
    energy, mean_w = _power_of(tier, windows)
    d: dict[str, Any] = {
        "frames": measured(frames, "frames", METHOD_COUNT, SCOPE_WORKLOAD),
        "wall_s": measured(wall_s, "s", "perf_counter_ns, first call start to last call end", SCOPE_WORKLOAD),
        "frames_per_s": measured(frames / wall_s, "frames/s", METHOD_FPS, SCOPE_WORKLOAD),
        "ms_per_frame": {
            k: measured(v, "ms", METHOD_LATENCY, SCOPE_WORKLOAD)
            for k, v in latency_stats_ms(lat, settings.percentiles).items()
        },
        "energy_j": energy,
        "mean_w": mean_w,
    }
    scope = str(mean_w["scope"])
    d["marginal_w"], d["j_per_1000_frames_marginal"] = _marginal(mean_w, idle_w, wall_s, frames, "cold idle")
    if armed_idle_w is not None:
        d["marginal_w_vs_armed_idle"], d["j_per_1000_frames_marginal_vs_armed_idle"] = _marginal(
            mean_w, armed_idle_w, wall_s, frames, "armed idle (CUDA context resident)"
        )
    d["j_per_1000_frames"] = (
        measured(j_per_1000(value_of(energy), frames), "J", f"energy J * 1000 / frames; {energy['method']}", scope)
        if is_measured(energy)
        else unavailable(str(energy.get("reason", "")), METHOD_NONE, scope)
    )
    d["gpu_die_context" if tier != "gpu" else "gpu_die"] = windows[SCOPE_GPU_DIE]
    d["thermal"] = windows["thermal"]
    d["cpu_util"] = windows["cpu_util"]
    return d


def _thermal(all_windows: Sequence[Windows]) -> dict[str, Measure]:
    ths = [w["thermal"] for w in all_windows]
    temps = [t.get("max_temp_c") for t in ths]
    flags = [t.get("throttling_observed") for t in ths]
    gpu_t = [w[SCOPE_GPU_DIE].get("temp_max_c") for w in all_windows]
    out: dict[str, Measure] = {}
    if ths and all(is_measured(t) for t in temps) and all(f is not None and f.get("measured") for f in flags):
        t0 = temps[0] or {}
        f0 = flags[0] or {}
        out["max_temp_c"] = measured(max(value_of(t) for t in temps if t), "degC", str(t0["method"]), str(t0["scope"]))
        out["throttling_observed"] = measured(
            any(bool(f["value"]) for f in flags if f), "bool", str(f0["method"]), str(f0["scope"])
        )
    else:
        why = str((ths[0].get("reason") if ths else None) or "thermal sampler unavailable")
        out["max_temp_c"] = unavailable(why, METHOD_NONE, "board_thermal_zones")
        out["throttling_observed"] = unavailable(why, METHOD_NONE, "board_thermal_zones")
    if gpu_t and all(is_measured(g) for g in gpu_t):
        g0 = gpu_t[0] or {}
        out["gpu_temp_max_c"] = measured(max(value_of(g) for g in gpu_t if g), "degC", str(g0["method"]), SCOPE_GPU_DIE)
    else:
        out["gpu_temp_max_c"] = unavailable("NVML temperature unavailable", METHOD_NONE, SCOPE_GPU_DIE)
    return out


def _summary(reps: Sequence[dict[str, Any]], tier: str) -> dict[str, dict[str, Measure]]:
    keys: tuple[str, ...] = ("frames_per_s", "mean_w", *MARGINAL_KEYS, "j_per_1000_frames")
    if reps and all(k in r for r in reps for k in ARMED_KEYS):
        keys += ARMED_KEYS
    if not reps:
        why = PLACEHOLDER_REASON.get(tier, "tier did not run")
        return {
            k: {"mean": unavailable(why, METHOD_NONE, tier), "std": unavailable(why, METHOD_NONE, tier)} for k in keys
        }
    return {k: report.mean_std([r[k] for r in reps]) for k in keys}


def _skeleton(tier: str, settings: BenchSettings, env: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "tier": tier,
        "timestamp": now.isoformat(timespec="seconds"),
        "run_id": now.strftime(RUN_ID_FORMAT),
        "hostname": socket.gethostname(),
        "workload": {"description": DESCRIPTION, "identity": None},
        "settings": settings.as_dict(),
        "environment": env,
        "idle": {},
        "reps": [],
        "summary": _summary([], tier),
        "thermal": _thermal([]),
        "sample_counts": {},
        "raw_samples": {},
    }


def _unavailable_tier(result: dict[str, Any], reason: str) -> dict[str, Any]:
    result["unavailable_reason"] = reason
    tier = result["tier"]
    result["summary"] = {
        k: {"mean": unavailable(reason, METHOD_NONE, tier), "std": unavailable(reason, METHOD_NONE, tier)}
        for k in result["summary"]
    }
    result["thermal"] = {k: unavailable(reason, METHOD_NONE, tier) for k in result["thermal"]}
    return result


# --- the tier -------------------------------------------------------------------------------
def _prepare(
    tier: str, ps: list[Pair], cfg: Config, settings: BenchSettings, corpus: Any
) -> tuple[ScoreFn, dict[str, Any]]:
    """Build the per-index scorer and prove it equals the golden model; raises on GPU probe failure
    (caller records the reason) and on any kernel mismatch (nobody records anything)."""
    if tier == "cpu":
        identity = assert_identical(corpus, cfg, settings.identity_frames)
        return (lambda i: score_numpy(ps[i][1], ps[i][2], cfg)), identity
    import torch

    dev = cuda_device()
    identity = assert_identical(corpus, cfg, settings.identity_frames, device=dev)
    ft = [torch.from_numpy(np.ascontiguousarray(p[1])).to(dev) for p in ps]
    rt = [torch.from_numpy(np.ascontiguousarray(p[2])).to(dev) for p in ps]
    torch.cuda.synchronize(dev)
    return (lambda i: score_torch(ft[i], rt[i], cfg, dev)), identity | {
        "device": str(dev),
        "gpu": torch.cuda.get_device_name(dev),
    }


def _idle_phase(
    settings: BenchSettings, core: int | None, tier: str, method: str
) -> tuple[Windows, dict[str, Any], dict[str, Any], Measure]:
    """One idle window; returns (figures, counts, raw, the tier's idle power measure)."""
    _, all_w, raw = _windowed(settings, core, lambda: _sleep(settings.idle_s, settings.tick_s))
    fig, counts = _split_counts(all_w)
    power = fig[SCOPE_GPU_DIE].get("mean_w", fig[SCOPE_GPU_DIE]) if tier == "gpu" else _power_of(tier, fig)[1]
    fig = {"duration_s": measured(settings.idle_s, "s", method, SCOPE_WORKLOAD), **fig}
    log(logger, logging.INFO, "idle done", tier=tier, method=method, idle_w=power.get("value"))
    return fig, counts, raw, power


def run_tier(tier: str, settings: BenchSettings) -> dict[str, Any]:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; one of {TIERS}")
    now = dt.datetime.now(dt.UTC)
    core, core_reason = pick_big_core(settings) if tier == "cpu" else (None, "not pinned (not the cpu tier)")
    result = _skeleton(tier, settings, environment(core, core_reason), now)
    if tier in PLACEHOLDER_REASON:
        log(logger, logging.INFO, "tier placeholder", tier=tier, reason=PLACEHOLDER_REASON[tier])
        return _unavailable_tier(result, PLACEHOLDER_REASON[tier])

    # (a) cold idle first: nothing loaded, no CUDA context, so the baseline is the machine at rest.
    result["idle"], result["sample_counts"]["idle"], result["raw_samples"]["idle"], idle_power = _idle_phase(
        settings, core, tier, METHOD_IDLE_COLD
    )

    corpus = load()
    ps = pairs(corpus)
    cfg = Config()
    try:
        score, identity = _prepare(tier, ps, cfg, settings, corpus)
    except AssertionError:
        raise  # WorkloadMismatch: a kernel that disagrees with golden is not benchmarked
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        log(logger, logging.WARNING, "tier unavailable", tier=tier, reason=reason)
        return _unavailable_tier(result, reason)
    result["workload"]["identity"] = identity
    log(logger, logging.INFO, "tier start", tier=tier, core=core, frames=len(ps))

    # On GB10 the die sits ~6 W higher with a CUDA context resident and no work; record that
    # second baseline too, so marginal W can be quoted against either and nobody has to guess.
    armed_power: Measure | None = None
    all_windows: list[Windows] = [result["idle"]]
    if tier == "gpu":
        (
            result["idle_armed"],
            result["sample_counts"]["idle_armed"],
            result["raw_samples"]["idle_armed"],
            armed_power,
        ) = _idle_phase(settings, core, tier, METHOD_IDLE_ARMED)
        all_windows.append(result["idle_armed"])

    _loop(score, len(ps), settings.warmup_s, core)  # warm-up, discarded

    result["sample_counts"]["reps"] = []
    result["raw_samples"]["reps"] = []
    for r in range(settings.reps):
        (lat, wall), win, raw = _windowed(settings, core, lambda: _loop(score, len(ps), settings.duration_s, core))
        fig, counts = _split_counts(win)
        rep = _rep(tier, lat, wall, fig, idle_power, armed_power, settings)
        result["reps"].append(rep)
        result["sample_counts"]["reps"].append(counts)
        result["raw_samples"]["reps"].append(raw | {"latency_ns": lat})
        all_windows.append(fig)
        log(
            logger,
            logging.INFO,
            "rep done",
            tier=tier,
            rep=r,
            frames=len(lat),
            fps=rep["frames_per_s"]["value"],
            mean_w=rep["mean_w"]["value"],
        )

    result["summary"] = _summary(result["reps"], tier)
    result["thermal"] = _thermal(all_windows)
    return result


# --- persistence ----------------------------------------------------------------------------
def _fresh_path(folder: Path, stem: str, suffix: str) -> Path:
    """A path that does not exist yet: two runs in the same second must not share a raw file."""
    for n in range(MAX_SAME_STAMP):
        candidate = folder / f"{stem}{'' if n == 0 else f'-{n}'}{suffix}"
        try:
            candidate.touch(exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"{MAX_SAME_STAMP} raw files already share the stamp {stem}")


def persist(result: dict[str, Any], settings: BenchSettings) -> Path:
    """Append-only: one JSON line per tier per run, raw samples beside it, a markdown block below."""
    root = Path(settings.results_dir)
    (root / "raw").mkdir(parents=True, exist_ok=True)
    raw = result.pop("raw_samples", {})
    raw_path = _fresh_path(root / "raw", f"{result['run_id']}-{result['tier']}", ".json")
    raw_path.write_text(
        json.dumps({"tier": result["tier"], "timestamp": result["timestamp"], "samples": raw}, default=str)
    )
    result["raw_file"] = str(raw_path)
    with (root / "bench.jsonl").open("a") as f:
        f.write(json.dumps(result, default=str) + "\n")
    with (root / "bench.md").open("a") as f:
        f.write(report.markdown(result))
    log(logger, logging.INFO, "persisted", tier=result["tier"], jsonl=str(root / "bench.jsonl"), raw=str(raw_path))
    return root / "bench.jsonl"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", default="cpu,gpu", help=f"comma-separated subset of {','.join(TIERS)}")
    ap.add_argument("--idle-s", type=float)
    ap.add_argument("--warmup-s", type=float)
    ap.add_argument("--duration-s", type=float)
    ap.add_argument("--reps", type=int)
    ap.add_argument("--sample-hz", type=float)
    ap.add_argument("--results-dir")
    ap.add_argument("--big-core", type=int)
    ap.add_argument("--identity-frames", type=int)
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args(argv)
    setup(a.log_level)
    tiers = [t.strip() for t in a.tier.split(",") if t.strip()]
    bad = [t for t in tiers if t not in TIERS]
    if bad:
        ap.error(f"unknown tier(s) {bad}; choose from {TIERS}")
    overrides = {k: v for k, v in vars(a).items() if k not in ("tier", "log_level") and v is not None}
    settings = replace(BenchSettings(), **overrides)
    results = [run_tier(t, settings) for t in tiers]
    for r in results:
        persist(r, settings)
    print(report.table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
