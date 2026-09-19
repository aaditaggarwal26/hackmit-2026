"""Pair the CPU/GPU workload with an energy sampler; emit a report entry.

    python -m orbit.bench.run --platform jetson --seconds 20 [--rail VDD_IN] [--gpu]
    python -m orbit.bench.run --platform mac --seconds 10        # dev machine, needs sudo
    python -m orbit.bench.run --platform none --seconds 10       # throughput only, energy TBD
"""
from __future__ import annotations

import argparse
import shutil
import subprocess

from . import cpu_baseline
from .energy import JetsonINA3221Sampler, MacPowermetricsSampler, NoSampler
from .report import BenchReport


def _cmd(*argv) -> str:
    """Capture a tool's output for the notes; 'unavailable' if it is not installed."""
    if not shutil.which(argv[0]):
        return "unavailable"
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout.strip().replace("\n", " / ")
        return out or "unavailable (empty output; try sudo)"
    except (subprocess.SubprocessError, OSError) as e:
        return f"unavailable ({e})"


def make_sampler(platform: str, rail: str, base: str | None = None):
    if platform == "jetson":
        return JetsonINA3221Sampler(rail=rail, **({"base": base} if base else {}))
    if platform == "mac":
        return MacPowermetricsSampler()
    return NoSampler()


def bench(platform: str, seconds: float, gpu: bool = False, rail: str = "VDD_CPU_GPU_CV", base: str | None = None,
          name: str | None = None) -> tuple[BenchReport, dict]:
    sampler = make_sampler(platform, rail, base)
    work = cpu_baseline.run_gpu if gpu else cpu_baseline.run_cpu
    sampler.start()
    w = work(seconds)
    energy = sampler.stop()
    notes = dict(workload=w["workload"], runs=w.get("runs"), window=w.get("window"))
    if w.get("note"):
        notes["note"] = w["note"]
    if platform == "jetson":
        notes["nvpmodel"] = _cmd("nvpmodel", "-q")
        notes["jetson_clocks"] = _cmd("jetson_clocks", "--show")
    rep = BenchReport()
    entry = rep.add(name or f"{platform}-{'gpu-cupy' if gpu else 'cpu-numpy'}", w["frames"], w["seconds"] or 0.0,
                    energy, **notes)
    return rep, entry


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", choices=["jetson", "mac", "none"], default="none")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--rail", default="VDD_CPU_GPU_CV", help="primary Jetson rail (all rails are recorded)")
    ap.add_argument("--hwmon-base", default=None, help="override the INA3221 sysfs base (tests)")
    ap.add_argument("--name", default=None)
    ap.add_argument("--out", default="bench/results")
    a = ap.parse_args(argv)
    rep, _ = bench(a.platform, a.seconds, a.gpu, a.rail, a.hwmon_base, a.name)
    print(rep.markdown())
    print("wrote", *rep.save(a.out))


if __name__ == "__main__":
    main()
