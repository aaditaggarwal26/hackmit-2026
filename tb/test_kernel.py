"""score_kernel + score_composite (through tb/score_path_tb.v) against
orbit.golden.score on every awkward vector and random corpus-like frames, at
PIXELS_PER_CYCLE = 1 and 8. Every §5.2 quantity must match exactly: cloud_px,
changed_px, sobel_sum, and the four composite outputs at several configs."""
import random

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

from orbit import params
from orbit.golden.score import Config, score_frame
from orbit.golden.vectors import awkward_frames, random_frame

from orbit_tb import TB, reset, run, wait_cycles

PPC = int(cocotb.plusargs.get("PPC", 8)) if hasattr(cocotb, "plusargs") else 8


def ppc_of(dut) -> int:
    return len(dut.k.frame_rd_data.value) // 8


async def load(dut, img: np.ndarray, ref: bool):
    ppc = ppc_of(dut)
    flat = img.reshape(-1)
    for a in range(params.FRAME_BYTES // ppc):
        word = int.from_bytes(bytes(flat[a * ppc:(a + 1) * ppc]), "little")
        dut.wr_en.value = 1
        dut.wr_ref.value = int(ref)
        dut.wr_addr.value = a
        dut.wr_data.value = word
        await FallingEdge(dut.clk)
    dut.wr_en.value = 0
    await FallingEdge(dut.clk)


async def score(dut, cfg: Config, iterations: int = 1):
    dut.cloud_thr.value = cfg.cloud_thr
    dut.change_thr.value = cfg.change_thr
    dut.w_clear.value = cfg.w_clear
    dut.w_sharp.value = cfg.w_sharp
    dut.w_change.value = cfg.w_change
    dut.sharp_shift.value = cfg.sharp_shift
    dut.iterations.value = iterations
    dut.start.value = 1
    await FallingEdge(dut.clk)
    dut.start.value = 0
    n = 0
    while not int(dut.done.value):
        await FallingEdge(dut.clk)
        n += 1
        assert n < 40000 * iterations, "kernel never finished"
    raw = (int(dut.cloud_px.value), int(dut.changed_px.value), int(dut.sobel_sum.value))
    cycles = int(dut.cycles.value)
    dut.c_start.value = 1
    await FallingEdge(dut.clk)
    dut.c_start.value = 0
    while not int(dut.c_done.value):
        await FallingEdge(dut.clk)
    comp = (int(dut.clear.value), int(dut.sharp.value), int(dut.change.value), int(dut.score.value))
    return raw, comp, cycles


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for s in ("wr_en", "wr_ref", "wr_addr", "wr_data", "start", "iterations", "c_start"):
        getattr(dut, s).value = 0
    await reset(dut)


def check(name, got_raw, got_comp, exp):
    e_raw = (exp.cloud_px, exp.changed_px, exp.sobel_sum)
    e_comp = (exp.clear, exp.sharp, exp.change, exp.score)
    assert got_raw == e_raw, f"{name}: raw {got_raw} != golden {e_raw}"
    assert got_comp == e_comp, f"{name}: composite {got_comp} != golden {e_comp}"


@cocotb.test()
async def zero_init_reference(dut):
    """Before any REF_FRAME_SET the reference is all zero (§5.1); only the frame is loaded."""
    await setup(dut)
    rng = np.random.default_rng(11)
    frame = random_frame(rng, "cloudy")
    await load(dut, frame, False)
    raw, comp, _ = await score(dut, Config())
    check("zero ref", raw, comp, score_frame(frame, np.zeros_like(frame)))


@cocotb.test()
async def awkward(dut):
    await setup(dut)
    cfg = Config()
    for name, frame, ref in awkward_frames():
        await load(dut, frame, False)
        await load(dut, ref, True)
        raw, comp, cycles = await score(dut, cfg)
        check(name, raw, comp, score_frame(frame, ref, cfg))
        dut._log.info(f"{name}: ok, {cycles} cycles")
    words = params.FRAME_BYTES // ppc_of(dut)
    assert words <= cycles <= words + 16, f"pass took {cycles} cycles for {words} words"


@cocotb.test()
async def random_frames_and_configs(dut):
    await setup(dut)
    rng = np.random.default_rng(5)
    prng = random.Random(5)
    for i in range(6):
        kind = ["smooth", "cloudy", "noise"][i % 3]
        frame, ref = random_frame(rng, kind), random_frame(rng, kind)
        cfg = Config(w_clear=prng.randrange(65536), w_sharp=prng.randrange(65536), w_change=prng.randrange(65536),
                     cloud_thr=prng.randrange(256), change_thr=prng.randrange(256),
                     sharp_shift=prng.randrange(params.SHARP_SHIFT_MAX + 1))
        await load(dut, frame, False)
        await load(dut, ref, True)
        raw, comp, _ = await score(dut, cfg)
        check(f"random{i}/{kind}", raw, comp, score_frame(frame, ref, cfg))


@cocotb.test()
async def repeated_pass_bench(dut):
    """iterations = 5 gives the same result and ~5x the cycles (content-independent work)."""
    await setup(dut)
    rng = np.random.default_rng(9)
    frame, ref = random_frame(rng), random_frame(rng)
    await load(dut, frame, False)
    await load(dut, ref, True)
    raw1, comp1, c1 = await score(dut, Config(), 1)
    raw5, comp5, c5 = await score(dut, Config(), 5)
    exp = score_frame(frame, ref)
    check("bench x1", raw1, comp1, exp)
    check("bench x5", raw5, comp5, exp)
    assert 4.8 * c1 <= c5 <= 5.2 * c1, (c1, c5)
    # the store is zero at power-on: a fresh reset scores a black frame against black
    await reset(dut)
    await wait_cycles(dut.clk, 2)


def _run(ppc):
    run("score_path_tb", ["frame_store.v", "sobel_unit.v", "score_kernel.v", "score_composite.v", str(TB / "score_path_tb.v")],
        "test_kernel", parameters={"PPC": ppc})


def test_kernel_ppc1():
    _run(1)


def test_kernel_ppc8():
    _run(8)
