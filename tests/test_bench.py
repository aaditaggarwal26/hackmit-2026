"""orbit.bench: the workload equals the golden model, the report maths is right, samplers
degrade to "unavailable" instead of raising, and the runner writes append-only results in
which every number carries method/scope/measured."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from orbit.bench import report, runner, sampler, workload
from orbit.bench.report import measured, unavailable
from orbit.config import BenchSettings
from orbit.corpus import Corpus, load
from orbit.golden.score import Config, score_frame
from orbit.golden.vectors import awkward_frames

CFG = Config()
FAKE_MW = 5000
FAKE_MJ_STEP = 500
FAST_HZ = 50.0


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load()


def _cuda_available() -> bool:
    try:
        workload.cuda_device()
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------- workload
def test_numpy_matches_golden_on_corpus_and_awkward(corpus: Corpus) -> None:
    for _, f, r in workload.pairs(corpus):
        assert workload.score_numpy(f, r, CFG) == score_frame(f, r, CFG).score
    for _, f, r in awkward_frames():
        assert workload.score_numpy(f, r, CFG) == score_frame(f, r, CFG).score


def test_identity_pairs_span_corpus_and_include_awkward(corpus: Corpus) -> None:
    ps = workload.identity_pairs(corpus, 8)
    assert len(ps) == 8 + len(awkward_frames())
    labels = [p[0] for p in ps]
    assert labels[0] == "corpus:0" and int(labels[7].split(":")[1]) > len(corpus.ids) // 2


def test_assert_identical_raises_on_wrong_kernel(corpus: Corpus, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workload, "score_numpy", lambda f, r, cfg: score_frame(f, r, cfg).score + 1)
    with pytest.raises(workload.WorkloadMismatch):
        workload.assert_identical(corpus, CFG, 2)


@pytest.mark.skipif(not _cuda_available(), reason="no CUDA device")
def test_torch_matches_golden(corpus: Corpus) -> None:
    import torch

    dev = workload.cuda_device()
    out = workload.assert_identical(corpus, CFG, 32, device=dev)
    assert out["torch_matches_golden"] is True
    # and on every corpus pair, not just the sampled ones
    for _, f, r in workload.pairs(corpus):
        ft, rt = torch.from_numpy(f).to(dev), torch.from_numpy(r).to(dev)
        assert workload.score_torch(ft, rt, CFG, dev) == score_frame(f, r, CFG).score


# ----------------------------------------------------------------------- report maths
def test_mean_std_percentiles() -> None:
    assert report.mean([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert report.stddev([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.2909944)
    assert report.stddev([1.0]) is None
    xs = [float(i) for i in range(1, 101)]
    assert report.percentile(xs, 50) == 50.5 and report.percentile(xs, 99) == pytest.approx(99.01)
    st = report.latency_stats_ms([1_000_000, 2_000_000, 3_000_000], (50, 99))
    assert st["mean"] == 2.0 and st["median"] == 2.0 and st["p99"] == pytest.approx(2.98)
    assert report.j_per_1000(2.0, 4000) == 0.5


def test_mean_std_labels_and_unavailable() -> None:
    reps = [measured(10.0, "W", "m", "gpu_die"), measured(12.0, "W", "m", "gpu_die")]
    s = report.mean_std(reps)
    assert (
        s["mean"]["value"] == 11.0 and s["mean"]["scope"] == "gpu_die" and s["std"]["value"] == pytest.approx(1.4142135)
    )
    one = report.mean_std(reps[:1])
    assert one["mean"]["value"] == 10.0 and one["std"]["value"] == "unavailable"
    mixed = report.mean_std([reps[0], unavailable("meter unplugged", "none", "cpu_rail")])
    assert mixed["mean"]["value"] == "unavailable" and mixed["mean"]["reason"] == "meter unplugged"
    assert report.mean_std([])["mean"]["measured"] is False


def test_table_and_markdown_carry_scope(tmp_path: Path) -> None:
    res = runner.run_tier("esp32", BenchSettings(results_dir=str(tmp_path)))
    txt = report.table([res])
    assert "esp32" in txt and "n/a" in txt and "gpu_die" in txt
    md = report.markdown(res)
    assert "Methodology" in md and "measured later on hardware" in md and "excludes the CPU" in md
    assert report.fmt(measured(10.234, "W", "m", "gpu_die")) == "10.23 W [gpu_die]"
    assert report.fmt(unavailable("x", "none", "cpu_rail")) == "n/a"


# ----------------------------------------------------------------------- samplers
class FakeNVML:
    """Enough of pynvml for NvmlSampler: constant power, a counting energy counter, no util."""

    class NVMLError(Exception):
        pass

    NVML_TEMPERATURE_GPU = 0

    def __init__(self, fail_init: bool = False, fail_after: int | None = None) -> None:
        self.fail_init = fail_init
        self.fail_after = fail_after
        self.calls = 0
        self.mj = 1_000_000

    def nvmlInit(self) -> None:
        if self.fail_init:
            raise self.NVMLError("Driver Not Loaded")

    def nvmlDeviceGetHandleByIndex(self, i: int) -> str:
        return "h"

    def nvmlDeviceGetPowerUsage(self, h: str) -> int:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise OSError("nvml went away")
        return FAKE_MW

    def nvmlDeviceGetTotalEnergyConsumption(self, h: str) -> int:
        self.mj += FAKE_MJ_STEP
        return self.mj

    def nvmlDeviceGetTemperature(self, h: str, kind: int) -> int:
        return 40

    def nvmlDeviceGetUtilizationRates(self, h: str) -> Any:
        raise self.NVMLError("Not Supported")


def test_nvml_sampler_reduces_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNVML()
    monkeypatch.setattr(sampler, "pynvml", fake)
    s = sampler.NvmlSampler(FAST_HZ)
    s.start()
    time.sleep(0.1)
    out = s.stop()
    assert out["scope"] == "gpu_die" and out["n_samples"] >= 3
    assert out["mean_w"]["value"] == FAKE_MW / 1000 and out["mean_w"]["measured"] is True
    assert out["energy_j"]["value"] == pytest.approx((out["n_samples"] - 1) * FAKE_MJ_STEP / 1000)
    assert out["util_mean_pct"]["value"] == "unavailable"  # NOT_SUPPORTED per field, not a crash
    assert out["temp_max_c"]["value"] == 40.0
    assert len(s.raw()) == out["n_samples"]


@pytest.mark.parametrize("fake", [FakeNVML(fail_init=True), FakeNVML(fail_after=1), None])
def test_nvml_sampler_degrades(monkeypatch: pytest.MonkeyPatch, fake: FakeNVML | None) -> None:
    monkeypatch.setattr(sampler, "pynvml", fake)
    s = sampler.NvmlSampler(FAST_HZ)
    s.start()
    time.sleep(0.1)
    out = s.stop()
    assert out["value"] == "unavailable" and out["measured"] is False and out["reason"] and out["scope"] == "gpu_die"


def _thermal_root(tmp_path: Path, proc_state: int) -> Path:
    root = tmp_path / "thermal"
    for i, milli in enumerate((35000, 45000)):
        (root / f"thermal_zone{i}").mkdir(parents=True)
        (root / f"thermal_zone{i}" / "temp").write_text(f"{milli}\n")
        (root / f"thermal_zone{i}" / "type").write_text("acpitz\n")
    devs = (("Processor", proc_state), ("PCIe_Port_Link_Speed_0000:00:00.0", 2), ("Processor", -231))
    for i, (typ, state) in enumerate(devs):
        (root / f"cooling_device{i}").mkdir()
        (root / f"cooling_device{i}" / "type").write_text(typ + "\n")
        (root / f"cooling_device{i}" / "cur_state").write_text(f"{state}\n")
    return root


def test_thermal_sampler(tmp_path: Path) -> None:
    root = _thermal_root(tmp_path, 0)
    s = sampler.ThermalSampler(FAST_HZ, root)
    s.start()
    out = s.stop()
    assert out["max_temp_c"]["value"] == 45.0 and out["throttling_observed"]["value"] is False  # PCIe/negative ignored
    assert out["zones"] == 2 and out["cooling_devices"] == 2
    (root / "cooling_device0" / "cur_state").write_text("1\n")
    s2 = sampler.ThermalSampler(FAST_HZ, root)
    s2.start()
    assert s2.stop()["throttling_observed"]["value"] is True
    empty = sampler.ThermalSampler(FAST_HZ, tmp_path / "nowhere")
    empty.start()
    assert empty.stop()["value"] == "unavailable"


def test_cpu_util_sampler(tmp_path: Path) -> None:
    stat = tmp_path / "stat"
    stat.write_text("cpu  100 0 100 800 0 0 0 0 0 0\ncpu3 10 0 10 80 0 0 0 0 0 0\n")
    s = sampler.CpuUtilSampler(FAST_HZ, 3, stat)
    s.start()
    stat.write_text("cpu  200 0 200 800 0 0 0 0 0 0\ncpu3 20 0 10 90 0 0 0 0 0 0\n")
    out = s.stop()
    assert out["busy_pct_all_cores"]["value"] == 100.0
    assert out["busy_pct_pinned_core"]["value"] == 50.0 and out["busy_pct_pinned_core"]["scope"] == "cpu3"
    gone = sampler.CpuUtilSampler(FAST_HZ, None, tmp_path / "missing")
    gone.start()
    assert gone.stop()["value"] == "unavailable"
    assert sampler.Unavailable("cpu_rail", "no meter").stop()["reason"] == "no meter"


# ----------------------------------------------------------------------- runner
EXEMPT = {"schema", "settings", "environment", "sample_counts", "workload", "raw_file"}
TINY = ["--idle-s", "0.2", "--warmup-s", "0.1", "--duration-s", "0.2", "--reps", "2"]


def _check_schema(node: Any, path: str) -> None:
    """Every number is the `value` of a labelled measure, or the measure says "unavailable" and why."""
    if isinstance(node, dict):
        if "value" in node:
            assert {"method", "scope", "measured"} <= node.keys(), path
            if node["value"] == "unavailable":
                assert node["measured"] is False and node["reason"], path
            else:
                assert node["measured"] is True and isinstance(node["value"], int | float), path
            return
        for k, v in node.items():
            _check_schema(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _check_schema(v, f"{path}[{i}]")
    elif isinstance(node, int | float) and not isinstance(node, bool):
        raise AssertionError(f"bare number at {path}: {node}")


def test_cpu_tier_end_to_end_and_jsonl_append(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--tier", "cpu", "--results-dir", str(tmp_path), *TINY]
    assert runner.main(argv) == 0
    assert runner.main(argv) == 0
    lines = (tmp_path / "bench.jsonl").read_text().splitlines()
    assert len(lines) == 2  # append, never overwrite
    assert len(list((tmp_path / "raw").glob("*-cpu.json"))) == 2
    assert "Methodology" in (tmp_path / "bench.md").read_text()
    assert "cpu" in capsys.readouterr().out
    res = json.loads(lines[-1])
    assert res["tier"] == "cpu" and len(res["reps"]) == 2 and res["settings"]["reps"] == 2
    assert res["workload"]["identity"]["numpy_matches_golden"] is True
    for k in ("mean_w", "energy_j", "marginal_w", "j_per_1000_frames", "j_per_1000_frames_marginal"):
        assert res["reps"][0][k]["value"] == "unavailable" and res["reps"][0][k]["scope"] == "cpu_rail"
        assert res["reps"][0][k]["reason"] == runner.NO_CPU_POWER
    assert res["summary"]["mean_w"]["mean"]["value"] == "unavailable"
    assert res["summary"]["frames_per_s"]["mean"]["measured"] is True and res["reps"][0]["frames"]["value"] > 0
    assert res["reps"][0]["gpu_die_context"]["scope"] == "gpu_die"  # context, never the CPU's power
    assert {"mean", "median", "p95", "p99"} <= res["reps"][0]["ms_per_frame"].keys()
    assert "throttling_observed" in res["thermal"] and res["environment"]["pinned_core"] is not None
    for line in lines:
        _check_schema({k: v for k, v in json.loads(line).items() if k not in EXEMPT}, "result")
    raw = json.loads(next((tmp_path / "raw").glob("*-cpu.json")).read_text())
    assert len(raw["samples"]["reps"]) == 2 and len(raw["samples"]["reps"][0]["latency_ns"]) > 0


@pytest.mark.skipif(not _cuda_available(), reason="no CUDA device")
def test_gpu_tier_end_to_end(tmp_path: Path) -> None:
    res = runner.run_tier(
        "gpu", BenchSettings(results_dir=str(tmp_path), idle_s=0.2, warmup_s=0.1, duration_s=0.2, reps=2)
    )
    assert res["workload"]["identity"]["torch_matches_golden"] is True and len(res["reps"]) == 2
    assert res["idle"]["duration_s"]["method"] == runner.METHOD_IDLE_COLD
    assert res["idle_armed"]["duration_s"]["method"] == runner.METHOD_IDLE_ARMED
    rep0 = res["reps"][0]
    assert rep0["mean_w"]["scope"] == "gpu_die" and rep0["energy_j"]["scope"] == "gpu_die"
    assert {"marginal_w", "marginal_w_vs_armed_idle"} <= rep0.keys() and "marginal_w_vs_armed_idle" in res["summary"]
    _check_schema({k: v for k, v in res.items() if k not in EXEMPT | {"raw_samples"}}, "gpu")


def test_placeholder_tiers_schema(tmp_path: Path) -> None:
    for tier, reason in runner.PLACEHOLDER_REASON.items():
        res = runner.run_tier(tier, BenchSettings(results_dir=str(tmp_path)))
        assert res["unavailable_reason"] == reason and res["reps"] == []
        assert res["summary"]["j_per_1000_frames"]["mean"]["reason"] == reason
        _check_schema({k: v for k, v in res.items() if k not in EXEMPT | {"raw_samples"}}, tier)
    with pytest.raises(ValueError):
        runner.run_tier("tpu", BenchSettings())


def test_gpu_tier_records_probe_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom() -> Any:
        raise RuntimeError("no CUDA here")

    monkeypatch.setattr(runner, "cuda_device", boom)
    res = runner.run_tier("gpu", BenchSettings(results_dir=str(tmp_path), idle_s=0.0))
    assert res["unavailable_reason"] == "RuntimeError: no CUDA here" and res["reps"] == []


def test_runner_refuses_wrong_kernel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workload, "score_numpy", lambda f, r, cfg: 0)
    with pytest.raises(workload.WorkloadMismatch):
        runner.run_tier("cpu", BenchSettings(results_dir=str(tmp_path), idle_s=0.0, identity_frames=1))


def test_pick_big_core() -> None:
    assert runner.pick_big_core(BenchSettings(big_core=3)) == (3, "BenchSettings.big_core")
    core, why = runner.pick_big_core(BenchSettings())
    assert core is None or (isinstance(core, int) and "cpuinfo_max_freq" in why)


def test_split_counts_keeps_bookkeeping_out_of_figures() -> None:
    fig, counts = runner._split_counts({"x": {"n_samples": 3, "m": measured(1.0, "W", "a", "b"), "ok": True}})
    assert fig == {"x": {"m": measured(1.0, "W", "a", "b"), "ok": True}} and counts == {"x": {"n_samples": 3}}
    assert np.isfinite(fig["x"]["m"]["value"])
