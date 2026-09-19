"""Arty energy run over the wire protocol: configure the node, load one corpus
frame and its reference (checked bit for bit against the golden model), then
BENCH_RUN back to back for >= --seconds while integrating POWER frames (INA219)
and summing iterations from BENCH_DONE. The kernel is fixed-function, so
re-scoring the resident frame is the same work per frame as scoring new ones;
the UART stays idle during the timed window, which is the point (loading a
frame over UART takes ~1.5 s, scoring it ~20 us).

Energy fallback chain: INA219 (whole-board, measured) -> Vivado estimate
(device-level, estimate) -> TBD, labelled. Cycles per frame come from the
board's own counter (BENCH_DONE.cycles / iterations), never computed here.

    uv run python -m orbit.bench.arty_run --port /dev/tty.usbserial-XXXXB --seconds 20
    uv run python -m orbit.bench.arty_run --port sim://0 --seconds 1      # control-flow check: energy and cycles TBD
"""
from __future__ import annotations

import argparse
import time

from orbit import corpus as C, params
from orbit.golden.node import rows
from orbit.golden.score import Config, score_frame
from orbit.protocol import messages as M
from orbit.protocol.messages import FrameDecoder, encode
from orbit.protocol.transport import NodeTransport, open_transport
from .energy import INA219Sampler, VivadoPowerEstimate
from .report import BenchReport, first_available

ITERATIONS = 50_000          # ~1 s per BENCH_RUN at 100 MHz and ~2055 clocks per frame


class RunTimeout(RuntimeError):
    pass


def pump(tr: NodeTransport, dec: FrameDecoder, sink: INA219Sampler, want: type | None, timeout: float):
    """Drain frames, feeding every POWER to the sampler; return the first `want`
    message (or None once `timeout` passes with nothing wanted)."""
    deadline = time.monotonic() + timeout
    while True:
        chunk = tr.recv(min(0.005, max(0.0, deadline - time.monotonic())))
        found = None
        for m in dec.feed(chunk) if chunk else ():
            if isinstance(m, M.Power):
                sink.feed(m)
            elif want is not None and found is None and isinstance(m, want):
                found = m
        if found is not None or time.monotonic() >= deadline:
            return found


def bench_arty(tr: NodeTransport, seconds: float, iterations: int = ITERATIONS, run_timeout: float = 60.0,
               frame_id: int = 0, estimate=None) -> tuple[dict, dict, dict | None]:
    """-> (workload dict, INA219 sampler result, `estimate` sampler result or None). The timed
    window starts at the first BENCH_RUN; an `estimate` sampler (Vivado watts x seconds) sees the same window."""
    dec, ina = FrameDecoder(), INA219Sampler()
    cfg = Config()
    corpus = C.load()
    frame, ref = corpus.by_id(frame_id), corpus.by_id(corpus.reference_for(frame_id))
    tr.send(encode(M.Heartbeat(sender=params.ORCH_ID, seq=0, uptime_ms=0)))
    tr.send(encode(M.ConfigSet(w_clear=cfg.w_clear, w_sharp=cfg.w_sharp, w_change=cfg.w_change, cloud_thr=cfg.cloud_thr,
                               change_thr=cfg.change_thr, sharp_shift=cfg.sharp_shift, queue_limit=cfg.queue_limit)))
    if pump(tr, dec, ina, M.StatusReply, run_timeout) is None:
        raise RunTimeout("no STATUS_REPLY after CONFIG_SET")
    for m in rows(ref, corpus.reference_for(frame_id), M.RefFrameSet):
        tr.send(encode(m))
    for m in rows(frame, frame_id):
        tr.send(encode(m))
    scored = pump(tr, dec, ina, M.FrameScored, run_timeout)
    if scored is None:
        raise RunTimeout("no FRAME_SCORED after the frame rows")
    exp = score_frame(frame, ref, cfg)
    board_ok = (scored.clear, scored.sharp, scored.change, scored.score) == (exp.clear, exp.sharp, exp.change, exp.score)
    # timed window: BENCH_RUN back to back
    ina.start()
    if estimate is not None:
        estimate.start()
    t0 = time.monotonic()
    total_iters, total_cycles, runs = 0, 0, 0
    while time.monotonic() - t0 < seconds:
        tr.send(encode(M.BenchRun(iterations=iterations)))
        done = pump(tr, dec, ina, M.BenchDone, run_timeout)
        if done is None:
            raise RunTimeout("no BENCH_DONE")
        total_iters += done.iterations
        total_cycles += done.cycles
        runs += 1
    t = time.monotonic() - t0
    energy = ina.stop()
    est = estimate.stop() if estimate is not None else None
    cycles_per_frame = total_cycles / total_iters if total_iters and total_cycles else None
    work = dict(workload=f"BENCH_RUN x{iterations} on the resident corpus frame {frame_id} (kernel + composite, UART idle)",
                frames=total_iters, seconds=t, runs=runs, window="first BENCH_RUN to last BENCH_DONE",
                cycles_per_frame=cycles_per_frame,
                kernel_frames_per_s=(params.CLK_HZ / cycles_per_frame) if cycles_per_frame else None,
                kernel_rate_label="derived from the board's cycle counter at CLK_HZ" if cycles_per_frame else "TBD (no cycle count)",
                board_matches_golden=board_ok, scored=dict(clear=scored.clear, sharp=scored.sharp, change=scored.change,
                                                          score=scored.score))
    return work, energy, est


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="/dev/tty.usbserial-XXXXB, sim://0 or verilator://0")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--iterations", type=int, default=ITERATIONS)
    ap.add_argument("--vivado", default="vivado/reports/power.txt", help="report_power text for the estimate fallback")
    ap.add_argument("--out", default="bench/results")
    a = ap.parse_args(argv)
    tr = open_transport(a.port, params.BAUD, 0)
    try:
        work, ina, est = bench_arty(tr, a.seconds, a.iterations, estimate=VivadoPowerEstimate(a.vivado))
    finally:
        tr.close()
    energy = first_available(ina, est)
    rep = BenchReport()
    rep.add("arty-a7-100t", work["frames"], work["seconds"], energy,
            iterations=a.iterations, runs=work["runs"], cycles_per_frame=work["cycles_per_frame"],
            kernel_frames_per_s=work["kernel_frames_per_s"], kernel_rate=work["kernel_rate_label"],
            board_matches_golden=work["board_matches_golden"], port=a.port)
    print(rep.markdown())
    print("wrote", *rep.save(a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
