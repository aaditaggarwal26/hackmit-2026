"""Bench harness: the CPU baseline is the same integers as the golden model, the
report labels provenance and prints TBD for missing measurements, the Arty runner's
control flow works against sim://0 (energy and cycles TBD there)."""
import numpy as np

from orbit import corpus as C, params
from orbit.bench import arty_run, cpu_baseline, run
from orbit.bench.ina219 import convert
from orbit.bench.report import BenchReport, first_available, joules_per_thousand
from orbit.golden.score import Config, score_frame
from orbit.protocol import messages as M
from orbit.protocol.transport import open_transport


def test_cpu_baseline_matches_golden_and_counts_frames():
    r = cpu_baseline.run_cpu(0.2)
    assert r["frames"] > 5 and r["seconds"] >= 0.2 and r["frames_per_s"] > 0
    c = C.load()
    for i in (0, 17, 100):
        f, ref = c.by_id(i), c.by_id(c.reference_for(i))
        assert cpu_baseline.score_xp(f, ref, Config(), np) == score_frame(f, ref)


def test_gpu_baseline_unavailable_is_honest():
    r = cpu_baseline.run_gpu(0.01)
    if not r.get("available"):
        assert r["frames"] == 0 and r["frames_per_s"] is None and "cupy" in r["note"]


def test_report_labels_and_tbd(tmp_path):
    rep = BenchReport()
    e = rep.add("x-cpu", frames=1000, seconds=2.0, sampler=dict(label="device-level", mean_watts=4.0, joules=8.0, source="t"))
    assert e["j_per_thousand"] == 8.0 and e["frames_per_s"] == 500.0
    e2 = rep.add("arty", frames=1000, seconds=2.0, sampler=dict(label="TBD", mean_watts=None, source="none"))
    assert e2["j_per_thousand"] is None
    md = rep.markdown()
    assert "J per 1000 frames" in md and "TBD" in md and "device-level" in md
    assert joules_per_thousand(None, 10) is None and joules_per_thousand(1.0, 0) is None
    assert first_available({"joules": None}, {"joules": 2.0, "source": "b"})["source"] == "b"
    assert first_available({"joules": None, "source": "a"})["label"].startswith("TBD")
    md_path, js_path = rep.save(str(tmp_path), stamp="20260914-000000")
    assert md_path.endswith("20260914-000000.md")


def test_ina219_conversion():
    v = convert(0x2648, 4210)       # bus raw: (raw >> 3) * 4 mV ; shunt raw: 10 uV/LSB over 0.1 ohm
    assert abs(v["volts"] - ((0x2648 >> 3) * 0.004)) < 1e-9 and abs(v["amps"] - 4210 * 10e-6 / 0.1) < 1e-9


def test_arty_run_against_sim_reports_tbd():
    tr = open_transport("sim://0", params.BAUD, 0)
    work, ina, est = arty_run.bench_arty(tr, 0.3, iterations=10, run_timeout=5.0)
    assert est is None
    tr.close()
    assert work["board_matches_golden"] and work["runs"] >= 1 and work["frames"] == 10 * work["runs"]
    assert work["cycles_per_frame"] is None and work["kernel_frames_per_s"] is None   # the model reports 0 cycles
    assert ina.get("joules") is None                                                    # no INA219 on a sim node


def test_arty_run_cli_sim(tmp_path, capsys):
    arty_run.main(["--port", "sim://0", "--seconds", "0.1", "--iterations", "5", "--out", str(tmp_path),
                   "--vivado", str(tmp_path / "none.txt")])
    out = capsys.readouterr().out
    assert "arty-a7-100t" in out and "TBD" in out and "wrote" in out


def test_run_cli_platform_none(tmp_path, capsys):
    run.main(["--platform", "none", "--seconds", "0.1", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert "none-cpu-numpy" in out and "J per 1000 frames" in out
