#!/usr/bin/env python3
"""Plot the cumulative value-delivered curve out of a runs/<run_id>.jsonl.

    python3 tools/plot_value.py runs/sim-memory_pressure-42-r200.jsonl
    python3 tools/plot_value.py runs/a.jsonl runs/b.jsonl -o results/two_runs.png

x is bytes of the contact window spent, y is cumulative *usable* frames delivered.
One line for Orbit -- the scored priority queue -- and one for the unfiltered FIFO
baseline the ground computes for the same window and the same byte budget
(`baseline_arrival` in docs/event_stream.md, `FifoBaseline` in orbit/ground/stream.py).

"usable" is whatever `run_start.usable_rule` says -- cloud fraction alone -- and never
the score that did the ranking. Ranking by a number and then measuring that same number
is circular, and the contract says so.

Every figure drawn here is read out of the run file. Nothing is illustrative: the two
series are asserted against `run_end`'s own recorded totals before anything is drawn,
and a run whose stream and summary disagree raises instead of plotting a pretty lie.

Drawn with PIL.ImageDraw at 3x and downsampled with LANCZOS. Two step curves, a pair of
axes and a legend do not need a plotting library, and this repo has no matplotlib.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.check_run import Event  # noqa: E402

# DESIGN.md, the palette at the top of the sheet.
STOCK = (0xED, 0xF0, 0xF3)
PLATE = (0xFF, 0xFF, 0xFF)
INK = (0x11, 0x16, 0x1B)
INK_2 = (0x4E, 0x5A, 0x66)
RULE = (0xC9, 0xD2, 0xDA)
RULE_SOFT = (0xE2, 0xE8, 0xED)
BLUE = (0x1B, 0x54, 0xC8)
PENCIL = (0x6E, 0x7A, 0x85)

SS = 3  # supersample factor: draw at 3x, resize down with LANCZOS

# Pillow cannot read display/fonts/archivo-latin.woff2, so borrow a system face.
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
)

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


class PlotError(Exception):
    """A run file this tool cannot honestly plot."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Series:
    """A cumulative step curve: xs are bytes spent, ys are usable frames delivered."""

    label: str
    xs: tuple[int, ...]
    ys: tuple[int, ...]

    @property
    def bytes_used(self) -> int:
        return self.xs[-1]

    @property
    def usable(self) -> int:
        return self.ys[-1]


@dataclass(frozen=True)
class RunPlot:
    """One run file, reduced to exactly what the chart is allowed to say."""

    path: Path
    run_id: str
    scenario: str | None
    seed: str | None
    nodes_real: tuple[bool, ...]
    budget_bytes: int
    usable_rule: str
    end_reason: str | None
    gain: float | None
    dropped_full: int | None
    orbit: Series
    baseline: Series

    @property
    def nodes_badge(self) -> str:
        """`real` is false for a simulated node, per docs/event_stream.md. Never imply a radio."""
        n = len(self.nodes_real)
        real = sum(1 for r in self.nodes_real if r)
        if n == 0:
            return "nodes not stated in run_start"
        if real == 0:
            return f"{n} nodes, all simulated"
        if real == n:
            return f"{n} nodes, all real"
        return f"{n} nodes, {real} real"

    @property
    def x_max(self) -> int:
        return max(self.budget_bytes, self.orbit.bytes_used, self.baseline.bytes_used)

    @property
    def y_max(self) -> int:
        return max(self.orbit.usable, self.baseline.usable)


def _cumulative(events: list[Event], label: str) -> Series:
    """Build the step curve. Each arrival spends its bytes, then may add one usable frame."""
    xs: list[int] = [0]
    ys: list[int] = [0]
    spent = 0
    usable = 0
    for ev in events:
        n_bytes = ev.get("bytes")
        if not isinstance(n_bytes, int):
            raise PlotError(f"{label}: an arrival at seq {ev.get('seq')} carries no integer `bytes`")
        spent += n_bytes
        if ev.get("usable") is True:
            usable += 1
        xs.append(spent)
        ys.append(usable)
    return Series(label=label, xs=tuple(xs), ys=tuple(ys))


def _describe_usable_rule(rule: dict[str, Any] | None) -> str:
    if not isinstance(rule, dict):
        return "usable rule not stated in run_start"
    metric = rule.get("metric")
    cap = rule.get("max")
    if metric is None or cap is None:
        return "usable rule not stated in run_start"
    return f"usable = {metric} <= {cap}"


def _nodes_real(start: Event) -> tuple[bool, ...]:
    nodes = start.get("nodes")
    if not isinstance(nodes, list):
        return ()
    return tuple(bool(n.get("real")) for n in nodes if isinstance(n, dict))


def _split_run_id(run_id: str) -> tuple[str | None, str | None]:
    """sim-<scenario>-<seed>[-<tag>] is the simulator's own naming, per docs/event_stream.md."""
    m = re.match(r"^sim-(.+?)-(\d+)(?:-.+)?$", run_id)
    if m is None:
        return None, None
    return m.group(1), m.group(2)


def load_run(path: Path) -> RunPlot:
    """Read a run file into the two cumulative series, checked against run_end."""
    start: Event | None = None
    end: Event | None = None
    orbit_events: list[Event] = []
    baseline_events: list[Event] = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError as e:
                raise PlotError(f"{path}: line {n} is not JSON ({e})") from e
            if not isinstance(ev, dict):
                raise PlotError(f"{path}: line {n} is not a JSON object")
            kind = ev.get("type")
            if kind == "run_start":
                start = ev
            elif kind == "frame_arrived":
                orbit_events.append(ev)
            elif kind == "baseline_arrival":
                baseline_events.append(ev)
            elif kind == "run_end":
                end = ev

    if start is None:
        raise PlotError(f"{path}: no run_start event, so the window budget and the usable rule are unknown")
    if not orbit_events:
        raise PlotError(f"{path}: no frame_arrived events -- nothing was downlinked, there is no curve to draw")
    if not baseline_events:
        raise PlotError(
            f"{path}: no baseline_arrival events. This run was recorded without the FIFO baseline "
            f"(orbit/ground/stream.py, FifoBaseline), so there is nothing to compare Orbit against. "
            f"Re-record the run, or plot a run file that has them."
        )

    orbit = _cumulative(orbit_events, "Orbit (scored priority queue)")
    baseline = _cumulative(baseline_events, "FIFO baseline (no scoring)")

    window = start.get("window")
    budget = window.get("budget_bytes") if isinstance(window, dict) else None
    if not isinstance(budget, int):
        raise PlotError(f"{path}: run_start carries no window.budget_bytes, so the x axis has no scale")

    run_id = str(start.get("run_id", path.stem))
    scenario, seed = _split_run_id(run_id)
    rule = start.get("usable_rule")

    # No run_end means no totals to check the curves against, and the chart says in its own
    # footer that it was checked. Refuse rather than draw an unverifiable figure that claims
    # otherwise -- an interrupted run is exactly when a stream is most likely to be short.
    if end is None:
        raise PlotError(
            f"{path}: no run_end event. This run did not finish, so its curves cannot be held "
            "against the totals the chart claims to have checked them against."
        )
    _check_against_run_end(path, end, orbit, baseline)
    end_reason = end.get("reason")
    gain: float | None = None
    headline = end.get("headline")
    if isinstance(headline, dict) and isinstance(headline.get("gain"), int | float):
        gain = float(headline["gain"])
    dropped_full = _baseline_dropped_full(end)

    return RunPlot(
        path=path,
        run_id=run_id,
        scenario=scenario,
        seed=seed,
        nodes_real=_nodes_real(start),
        budget_bytes=budget,
        usable_rule=_describe_usable_rule(rule if isinstance(rule, dict) else None),
        end_reason=str(end_reason) if end_reason is not None else None,
        gain=gain,
        dropped_full=dropped_full,
        orbit=orbit,
        baseline=baseline,
    )


def _check_against_run_end(path: Path, end: Event, orbit: Series, baseline: Series) -> None:
    """The self-check: the last point of each curve must be what run_end recorded.

    This is the whole reason the chart is allowed to claim anything. If the stream and
    the summary disagree, the run file is wrong and no figure drawn from it is honest.
    """
    for key, series in (("orbit", orbit), ("baseline", baseline)):
        summary = end.get(key)
        if not isinstance(summary, dict):
            continue
        recorded_usable = summary.get("usable_down")
        if isinstance(recorded_usable, int) and series.usable != recorded_usable:
            raise PlotError(
                f"{path}: {key} curve ends at {series.usable} usable frames but "
                f"run_end.{key}.usable_down says {recorded_usable}"
            )
        recorded_bytes = summary.get("bytes_used")
        if isinstance(recorded_bytes, int) and series.bytes_used != recorded_bytes:
            raise PlotError(
                f"{path}: {key} curve ends at {series.bytes_used} bytes but "
                f"run_end.{key}.bytes_used says {recorded_bytes}"
            )


def _baseline_dropped_full(end: Event) -> int | None:
    """How many frames the FIFO baseline threw away because a node's pool was full.

    The number that says whether this run was under buffer pressure at all, and so whether
    the gain beside it is a contention result or a reordering one. Read from run_end.
    """
    summary = end.get("baseline")
    if not isinstance(summary, dict):
        return None
    dropped = summary.get("frames_dropped_full")
    return dropped if isinstance(dropped, int) else None


# ------------------------------------------------------------------------- drawing


@dataclass(frozen=True)
class Fonts:
    display: Font
    headline: Font
    label: Font
    tick: Font
    caption: Font
    truetype: bool


def load_fonts(scale: int = SS) -> Fonts:
    """A system TTF if there is one, otherwise Pillow's own scalable default."""
    for candidate in FONT_CANDIDATES:
        if not Path(candidate).exists():
            continue
        try:
            return Fonts(
                display=ImageFont.truetype(candidate, 46 * scale),
                headline=ImageFont.truetype(candidate, 27 * scale),
                label=ImageFont.truetype(candidate, 21 * scale),
                tick=ImageFont.truetype(candidate, 17 * scale),
                caption=ImageFont.truetype(candidate, 15 * scale),
                truetype=True,
            )
        except OSError:
            continue
    # load_default takes a size and returns a scalable face, so the fallback stays readable.
    return Fonts(
        display=ImageFont.load_default(46 * scale),
        headline=ImageFont.load_default(27 * scale),
        label=ImageFont.load_default(21 * scale),
        tick=ImageFont.load_default(17 * scale),
        caption=ImageFont.load_default(15 * scale),
        truetype=False,
    )


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: Font) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return int(right - left), int(bottom - top)


def _wrap(draw: ImageDraw.ImageDraw, parts: list[str], font: Font, max_w: int, sep: str = "  ") -> list[str]:
    """Pack parts into lines that fit max_w. Nothing on this chart may run off the plate."""
    lines: list[str] = []
    current = ""
    for part in parts:
        candidate = part if not current else current + sep + part
        if current and _text_size(draw, candidate, font)[0] > max_w:
            lines.append(current)
            current = part
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _kib(n: int) -> str:
    return f"{n / 1024:.0f}"


def _nice_step(span: int, target_ticks: int) -> int:
    """A round tick step near span/target_ticks: 1, 2, 5, 10, 20, 50, ..."""
    if span <= 0:
        return 1
    raw = max(span / target_ticks, 1.0)
    magnitude = 1
    while magnitude * 10 <= raw:
        magnitude *= 10
    for mult in (1, 2, 5, 10):
        if magnitude * mult >= raw:
            return magnitude * mult
    return magnitude * 10


def _step_points(
    series: Series,
    x0: int,
    y0: int,
    plot_w: int,
    plot_h: int,
    x_max: int,
    y_max: int,
) -> list[tuple[int, int]]:
    """Bytes are spent first, then the frame lands: every step goes right, then up."""

    def px(value: int) -> int:
        return x0 + round(plot_w * value / x_max) if x_max else x0

    def py(value: int) -> int:
        return y0 - round(plot_h * value / y_max) if y_max else y0

    points: list[tuple[int, int]] = [(px(0), py(0))]
    prev_y = 0
    for x, y in zip(series.xs[1:], series.ys[1:], strict=True):
        points.append((px(x), py(prev_y)))
        if y != prev_y:
            points.append((px(x), py(y)))
        prev_y = y
    return points


def _draw_panel(
    draw: ImageDraw.ImageDraw,
    fonts: Fonts,
    run: RunPlot,
    top: int,
    width: int,
    height: int,
) -> int:
    """Draw one run's panel. Returns the y of its bottom edge. All units already 3x."""
    s = SS
    pad_l, pad_r = 136 * s, 210 * s
    caption_h = 152 * s
    axis_label_h = 76 * s

    x0 = pad_l
    x1 = width - pad_r
    plot_w = x1 - x0
    y_top = top + caption_h
    y0 = top + height - axis_label_h
    plot_h = y0 - y_top

    x_max = run.x_max
    y_step = _nice_step(max(run.y_max, 1), 6)
    y_max = max((max(run.y_max, 1) + y_step - 1) // y_step * y_step, y_step)  # headroom to a round tick

    # --- attribution caption: every figure on this chart names what produced it
    bits = [f"run {run.run_id}"]
    if run.scenario is not None:
        bits.append(f"scenario {run.scenario}")
    if run.seed is not None:
        bits.append(f"seed {run.seed}")
    bits.append(f"window budget {_kib(run.budget_bytes)} KiB")
    bits.append(run.nodes_badge)
    if run.end_reason is not None:
        bits.append(f"ended {run.end_reason}")
    if run.dropped_full is not None:
        # Whether the baseline was ever forced to throw a frame away is what separates a
        # contention result from a reordering one, so it belongs on the face of the chart.
        bits.append(
            f"baseline dropped {run.dropped_full} frames, pool full"
            if run.dropped_full
            else "baseline never overflowed its pool"
        )
    caption_y = top + 4 * s
    for line in _wrap(draw, [f"[ {b} ]" for b in bits], fonts.label, x1 - x0):
        draw.text((x0, caption_y), line, font=fonts.label, fill=INK_2)
        caption_y += 30 * s

    headline = f"{run.orbit.usable} usable frames vs {run.baseline.usable} for FIFO"
    if run.gain is not None:
        headline += f"  ({run.gain:.2f}x)"
    draw.text((x0, caption_y + 6 * s), headline, font=fonts.headline, fill=INK)

    # --- plot plate
    draw.rectangle([x0, y_top, x1, y0], fill=PLATE)

    # --- grid and y ticks
    value = 0
    while value <= y_max:
        y = y0 - round(plot_h * value / y_max)
        draw.line([(x0, y), (x1, y)], fill=RULE_SOFT if value else RULE, width=max(1, s // 2))
        text = str(value)
        tw, th = _text_size(draw, text, fonts.tick)
        draw.text((x0 - 14 * s - tw, y - th // 2 - 3 * s), text, font=fonts.tick, fill=INK_2)
        value += y_step

    # --- x ticks, in KiB of the contact window
    x_step = _nice_step(x_max // 1024, 6) * 1024
    value = 0
    while value <= x_max:
        x = x0 + round(plot_w * value / x_max)
        draw.line([(x, y_top), (x, y0)], fill=RULE_SOFT, width=max(1, s // 2))
        text = _kib(value)
        tw, _ = _text_size(draw, text, fonts.tick)
        draw.text((x - tw // 2, y0 + 12 * s), text, font=fonts.tick, fill=INK_2)
        value += x_step
    if (value - x_step) != x_max:  # the window's own edge always gets a tick
        tw, _ = _text_size(draw, _kib(x_max), fonts.tick)
        draw.line([(x1, y_top), (x1, y0)], fill=RULE, width=max(1, s // 2))
        draw.text((x1 - tw // 2, y0 + 12 * s), _kib(x_max), font=fonts.tick, fill=INK_2)

    draw.line([(x0, y_top), (x0, y0)], fill=INK, width=max(1, s))
    draw.line([(x0, y0), (x1, y0)], fill=INK, width=max(1, s))

    # --- the two curves
    for series, colour in ((run.baseline, PENCIL), (run.orbit, BLUE)):
        pts = _step_points(series, x0, y0, plot_w, plot_h, x_max, y_max)
        draw.line(pts, fill=colour, width=4 * s, joint="curve")
        ex, ey = pts[-1]
        draw.ellipse([ex - 6 * s, ey - 6 * s, ex + 6 * s, ey + 6 * s], fill=colour)
        label = f"{series.usable}"
        draw.text((ex + 16 * s, ey - 16 * s), label, font=fonts.headline, fill=colour)

    # --- axis titles
    draw.text(
        (x0, y0 + 42 * s),
        "contact window spent (KiB)",
        font=fonts.label,
        fill=INK_2,
    )
    draw.text((x0 - 122 * s, y_top - 32 * s), "usable frames", font=fonts.label, fill=INK_2)

    # --- legend, in the empty bottom-right of a rising curve
    lx = x1 - 380 * s
    ly = y0 - 130 * s
    draw.rectangle([lx - 20 * s, ly - 18 * s, x1 - 16 * s, ly + 90 * s], fill=PLATE, outline=RULE, width=max(1, s // 2))
    for i, (text, colour) in enumerate(
        ((run.orbit.label, BLUE), (run.baseline.label, PENCIL)),
    ):
        ry = ly + i * 44 * s
        draw.line([(lx, ry + 13 * s), (lx + 48 * s, ry + 13 * s)], fill=colour, width=6 * s)
        draw.text((lx + 64 * s, ry), text, font=fonts.label, fill=INK)

    return top + height


def render(runs: list[RunPlot], out: Path) -> None:
    s = SS
    width = 1320 * s
    header_h = 176 * s
    panel_h = 620 * s
    footer_h = 104 * s
    height = header_h + panel_h * len(runs) + footer_h

    img = Image.new("RGB", (width, height), STOCK)
    draw = ImageDraw.Draw(img)
    fonts = load_fonts()

    margin = 136 * s
    draw.text((margin, 34 * s), "Usable frames per byte of contact window", font=fonts.display, fill=INK)
    draw.text(
        (margin, 96 * s),
        "Orbit's scored priority queue against an unfiltered FIFO baseline, same frames, same byte budget.",
        font=fonts.label,
        fill=INK_2,
    )
    draw.line([(margin, 150 * s), (width - margin, 150 * s)], fill=RULE, width=max(1, s))

    y = header_h
    for run in runs:
        y = _draw_panel(draw, fonts, run, y, width, panel_h)

    rules = sorted({r.usable_rule for r in runs})
    sources = ", ".join(str(r.path) for r in runs)
    draw.line([(margin, y + 10 * s), (width - margin, y + 10 * s)], fill=RULE, width=max(1, s))
    draw.text(
        (margin, y + 26 * s),
        f"{'; '.join(rules)}. Cloud fraction only -- never the score that ranked the frames.",
        font=fonts.caption,
        fill=INK_2,
    )
    draw.text(
        (margin, y + 52 * s),
        f"Every point read from {sources} (frame_arrived / baseline_arrival), checked against run_end.",
        font=fonts.caption,
        fill=INK_2,
    )
    # The scope of the claim, so a gain measured under buffer pressure is not read as a
    # general one. Only drawn when a panel was actually under pressure, and only then.
    if any(r.dropped_full for r in runs):
        draw.text(
            (margin, y + 78 * s),
            "The gain is in which frames come down, not how many get a slot: where the baseline's "
            "pool never overflows, both paths finish within a frame of each other.",
            font=fonts.caption,
            fill=INK_2,
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((width // s, height // s), Image.Resampling.LANCZOS).save(out)


# ----------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="plot_value.py",
        description="Plot cumulative usable frames against bytes of contact window, Orbit vs the FIFO baseline.",
        epilog="One panel per run file. Every number comes out of the run; none is illustrative.",
    )
    ap.add_argument("paths", nargs="+", metavar="RUN", help="runs/<run_id>.jsonl file(s)")
    ap.add_argument(
        "-o",
        "--out",
        default="results/filtered_vs_fifo.png",
        help="output PNG (default: results/filtered_vs_fifo.png)",
    )
    args = ap.parse_args(argv)

    runs: list[RunPlot] = []
    for p in args.paths:
        path = Path(p)
        if not path.exists():
            print(f"plot_value: no such run file: {path}", file=sys.stderr)
            return 2
        try:
            runs.append(load_run(path))
        except PlotError as e:
            print(f"plot_value: {e}", file=sys.stderr)
            return 2

    out = Path(args.out)
    render(runs, out)
    fonts_note = "" if load_fonts().truetype else " (no system TTF found; drew with Pillow's default face)"
    print(f"wrote {out}{fonts_note}")
    for run in runs:
        gain = f"{run.gain:.3f}x" if run.gain is not None else "gain not in run_end"
        print(
            f"  {run.run_id}: orbit {run.orbit.usable} usable / {run.orbit.bytes_used} bytes, "
            f"baseline {run.baseline.usable} usable / {run.baseline.bytes_used} bytes, {gain}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
