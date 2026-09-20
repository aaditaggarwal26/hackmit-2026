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


def frame(n_bytes, **fields):
    """One arrival, as the stream writes it: the bytes it spent, plus whatever `usable` the test wants."""
    return {"node_id": 0, "frame_id": 1, "bytes": n_bytes, **fields}


def totals(arrivals):
    """run_end's own summary of a list of arrivals, counted here rather than by the code under test."""
    return {
        "frames_down": len(arrivals),
        "usable_down": sum(1 for a in arrivals if a.get("usable") is True),
        "bytes_used": sum(a["bytes"] for a in arrivals),
    }


def write_run(tmp_path, orbit, baseline, *, run_id="sim-nominal-42", end="auto", name="run.jsonl"):
    """The smallest run file load_run accepts: a run_start, the two arrival streams, a run_end.

    `end` replaces the run_end body, so a test can tamper with the totals the curves are held
    against; `end=None` writes no run_end at all.
    """
    if end == "auto":
        end = {"reason": "window_closed", "orbit": totals(orbit), "baseline": totals(baseline)}
    events = [
        {
            "type": "run_start",
            "run_id": run_id,
            "mode": "live",
            "window": {"budget_bytes": 65536, "duration_s": 10.0},
            "usable_rule": {"metric": "cloud_frac", "max": 0.35},
        },
        *({"type": "frame_arrived", **a} for a in orbit),
        *({"type": "baseline_arrival", **a} for a in baseline),
        *([{"type": "run_end", **end}] if end is not None else []),
    ]
    path = tmp_path / name
    path.write_text(
        "\n".join(json.dumps({"seq": n, "t": float(n), **ev}) for n, ev in enumerate(events, 1)) + "\n",
        encoding="utf-8",
    )
    return path


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


def test_cumulative_steps_once_per_arrival_in_stream_order():
    """The shape of the curve, pinned against a hand-built stream: x grows by the bytes each
    arrival spent, y only by the arrivals that were usable. A regression suite against one
    recorded run cannot see this -- it only knows the endpoints agree with run_end."""
    arrivals = [frame(100, usable=True), frame(200, usable=False), frame(300, usable=True), frame(400, usable=False)]
    series = P._cumulative(arrivals, "orbit")
    assert series.xs == (0, 100, 300, 600, 1000)
    assert series.ys == (0, 1, 1, 2, 2)
    assert (series.bytes_used, series.usable) == (1000, 2)


def test_step_points_go_right_then_up():
    """Bytes are spent before the frame lands, so a step moves right first and up second.
    Up-then-right would draw the value as delivered before it was paid for."""
    series = P._cumulative([frame(100, usable=True)], "orbit")
    points = P._step_points(series, x0=0, y0=1, plot_w=100, plot_h=1, x_max=100, y_max=1)
    assert points == [(0, 1), (100, 1), (100, 0)]  # y counts down the image, so (100, 0) is the up step


@pytest.mark.parametrize("fields", [{"usable": None}, {}, {"usable": 1}, {"usable": "true"}])
def test_only_a_strictly_true_usable_raises_the_curve(tmp_path, fields):
    """orbit/ground/stream.py emits `usable = None if cloud is None else bool(...)`, so null is
    real in the stream, and run_end counts `1 if usable else 0`. A frame that is not strictly
    True is not a usable frame, and counting one would inflate the headline."""
    arrivals = [frame(100, **fields)]
    assert P._cumulative(arrivals, "orbit").ys == (0, 0)
    run = P.load_run(write_run(tmp_path, arrivals, arrivals))
    assert (run.orbit.usable, run.baseline.usable) == (0, 0)


def test_an_arrival_without_integer_bytes_is_refused():
    """Bytes missing or stringly typed would spend nothing and slide the rest of the curve left."""
    with pytest.raises(P.PlotError, match="no integer `bytes`"):
        P._cumulative([{"seq": 7, "bytes": "16384", "usable": True}], "orbit")


@pytest.mark.parametrize("field", ["usable_down", "bytes_used"])
@pytest.mark.parametrize("key", ["orbit", "baseline"])
def test_a_tampered_run_end_is_refused(tmp_path, key, field):
    """A stream that disagrees with its own summary must raise PlotError -- not assert, because
    `python -O` strips asserts and the chart's footer claims both curves were checked."""
    orbit = [frame(100, usable=True), frame(200, usable=False)]
    baseline = [frame(150, usable=True)]
    assert P.load_run(write_run(tmp_path, orbit, baseline, name="honest.jsonl")).orbit.usable == 1
    end = {"reason": "window_closed", "orbit": totals(orbit), "baseline": totals(baseline)}
    end[key][field] += 7
    with pytest.raises(P.PlotError, match=f"run_end.{key}.{field}"):
        P.load_run(write_run(tmp_path, orbit, baseline, end=end, name="tampered.jsonl"))


def test_a_run_without_run_end_is_refused(tmp_path):
    """No run_end means no totals to check the curves against, and the footer says they were
    checked. An interrupted run is exactly when a stream is most likely to be short."""
    path = write_run(tmp_path, [frame(100, usable=True)], [frame(100, usable=False)], end=None)
    with pytest.raises(P.PlotError, match="no run_end"):
        P.load_run(path)


def test_baseline_dropped_full_is_read_from_run_end_and_never_guessed():
    """Whether the baseline ever overflowed its pool is printed on the chart's face, and it is
    what separates a contention result from a reordering one. Absent must read as unknown,
    not as zero drops, or the chart states a scope the run never measured."""
    assert P._baseline_dropped_full({"baseline": {"frames_dropped_full": 140}}) == 140
    assert P._baseline_dropped_full({"baseline": {"frames_dropped_full": 0}}) == 0
    assert P._baseline_dropped_full({"baseline": {"usable_down": 7}}) is None
    assert P._baseline_dropped_full({"orbit": {"usable_down": 7}}) is None


@pytest.mark.parametrize("run_id", ["sample-2026-09-19T20-30-00", "sim-nominal", "sim-42", "simulated-42", "live", ""])
def test_run_id_is_not_guessed_for_a_non_simulator_run(run_id):
    """scenario and seed go in the caption as facts about the run. A run id that is not the
    simulator's own naming yields neither, rather than a plausible-looking guess."""
    assert P._split_run_id(run_id) == (None, None)


def test_a_non_simulator_run_id_leaves_scenario_and_seed_off_the_caption(tmp_path):
    """The same, end to end: load_run carries the Nones through instead of inventing a scenario."""
    arrivals = [frame(100, usable=True)]
    run = P.load_run(write_run(tmp_path, arrivals, arrivals, run_id="2026-09-20T13-06-43"))
    assert (run.run_id, run.scenario, run.seed) == ("2026-09-20T13-06-43", None, None)
    assert run.budget_bytes == 65536 and run.usable_rule == "usable = cloud_frac <= 0.35"
