"""The self-check behind tools/plot_value.py: the curves must equal what run_end recorded."""

import json
from pathlib import Path

import pytest

from tools import plot_value as P

REPO = Path(__file__).resolve().parent.parent
SAMPLE = REPO / "runs" / "sample.jsonl"


def run_end(path):
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ev = json.loads(line)
            if ev.get("type") == "run_end":
                last = ev
    return last


def test_cumulative_series_match_run_end():
    """The last point of each curve is run_end's own total, for frames and for bytes."""
    run = P.load_run(SAMPLE)
    end = run_end(SAMPLE)
    assert end is not None, "runs/sample.jsonl has no run_end"
    assert run.orbit.usable == end["orbit"]["usable_down"]
    assert run.baseline.usable == end["baseline"]["usable_down"]
    assert run.orbit.bytes_used == end["orbit"]["bytes_used"]
    assert run.baseline.bytes_used == end["baseline"]["bytes_used"]


def test_series_are_monotone_and_start_at_the_origin():
    run = P.load_run(SAMPLE)
    for series in (run.orbit, run.baseline):
        assert (series.xs[0], series.ys[0]) == (0, 0)
        assert all(b >= a for a, b in zip(series.xs, series.xs[1:], strict=False))
        assert all(b - a in (0, 1) for a, b in zip(series.ys, series.ys[1:], strict=False))


def test_missing_baseline_is_a_message_not_a_traceback(tmp_path, capsys):
    bad = tmp_path / "no-baseline.jsonl"
    bad.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "seq": 1,
                        "t": 0.0,
                        "type": "run_start",
                        "run_id": "sim-nominal-42",
                        "mode": "live",
                        "window": {"budget_bytes": 65536, "duration_s": 10.0},
                        "usable_rule": {"metric": "cloud_frac", "max": 0.35},
                    }
                ),
                json.dumps(
                    {
                        "seq": 2,
                        "t": 1.0,
                        "type": "frame_arrived",
                        "node_id": 0,
                        "frame_id": 1,
                        "bytes": 16384,
                        "cloud_frac": 0.1,
                        "usable": True,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(P.PlotError, match="no baseline_arrival"):
        P.load_run(bad)
    assert P.main([str(bad), "-o", str(tmp_path / "out.png")]) == 2
    assert "baseline_arrival" in capsys.readouterr().err


def test_renders_a_png(tmp_path):
    out = tmp_path / "chart.png"
    assert P.main([str(SAMPLE), "-o", str(out)]) == 0
    assert out.exists() and out.stat().st_size > 0


def test_font_fallback_still_renders(tmp_path, monkeypatch):
    """No system TTF: load_default must carry the chart rather than crash it."""

    monkeypatch.setattr(P, "FONT_CANDIDATES", ("/nonexistent/no-such-face.ttf",))
    assert P.load_fonts().truetype is False
    out = tmp_path / "fallback.png"
    assert P.main([str(SAMPLE), "-o", str(out)]) == 0
    assert out.exists() and out.stat().st_size > 0


def test_run_id_split():
    assert P._split_run_id("sim-nominal-42") == ("nominal", "42")
    assert P._split_run_id("sim-memory_pressure-42-r200") == ("memory_pressure", "42")
    assert P._split_run_id("2026-09-20T13-06-43") == (None, None)
