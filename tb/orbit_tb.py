"""Shared cocotb helpers for the Orbit RTL testbenches.

Conventions (see tb/test_hello.py): drive and sample on FallingEdge only.
Every value read right after a FallingEdge reflects the preceding RisingEdge.
"""
import os
from pathlib import Path

from cocotb.triggers import ClockCycles, FallingEdge
from cocotb_tools.runner import get_runner

ROOT = Path(__file__).resolve().parent.parent
RTL = ROOT / "rtl"
TB = Path(__file__).resolve().parent

MASK48 = (1 << 48) - 1
MASK32 = (1 << 32) - 1



def run(toplevel: str, sources: list[str], test_module: str, parameters: dict | None = None,
        build_args: list[str] | None = None, testcase=None):
    """Build with Verilator and run one cocotb module against `toplevel`."""
    runner = get_runner("verilator")
    build_dir = TB / "sim_build" / (toplevel if not parameters else
                                    toplevel + "_" + "_".join(f"{k}{v}" for k, v in parameters.items()))
    runner.build(sources=[RTL / s for s in sources], hdl_toplevel=toplevel, includes=[RTL],
                 parameters=parameters or {}, build_args=["-Wall", "-Wno-fatal"] + (build_args or []),
                 build_dir=build_dir, always=True)
    env = {"PYTHONPATH": os.pathsep.join([str(ROOT), str(TB), os.environ.get("PYTHONPATH", "")])}
    # results.xml per build dir so independent pytest processes can run side by side
    runner.test(hdl_toplevel=toplevel, test_module=test_module, test_dir=TB, testcase=testcase, extra_env=env,
                results_xml=str(build_dir / "results.xml"))


def s48(v: int) -> int:
    """Interpret a 48-bit field as signed."""
    v &= MASK48
    return v - (1 << 48) if v >> 47 else v


def s32(v: int) -> int:
    v &= MASK32
    return v - (1 << 32) if v >> 31 else v



async def wait_cycles(clk, n: int):
    """n falling edges. Unlike ClockCycles (which returns ON a rising edge, so a
    drive right after it races the DUT's sampling edge) this always leaves the
    caller on a falling edge, where driving is safe."""
    for _ in range(n):
        await FallingEdge(clk)


async def reset(dut, cycles=3):
    dut.rst_n.value = 0
    for _ in range(cycles):
        await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)


# ---------------------------------------------------------------- UART models
async def uart_send(clk, pin, data: bytes, clks_per_bit: int, gap_bits: int = 0):
    """Bit-bang 8N1 bytes onto `pin` (LSB first). Timing in clock cycles."""
    for b in data:
        bits = [0] + [(b >> i) & 1 for i in range(8)] + [1]
        for bit in bits:
            pin.value = bit
            await ClockCycles(clk, clks_per_bit)
        if gap_bits:
            await ClockCycles(clk, clks_per_bit * gap_bits)


async def uart_monitor(clk, pin, clks_per_bit: int, out: bytearray):
    """Decode 8N1 from `pin` into `out` forever (start on a falling edge,
    sample each bit at its centre)."""
    while True:
        await FallingEdge(pin)
        await ClockCycles(clk, clks_per_bit // 2)
        if int(pin.value) != 0:
            continue                      # glitch, not a start bit
        v = 0
        for i in range(8):
            await ClockCycles(clk, clks_per_bit)
            v |= int(pin.value) << i
        await ClockCycles(clk, clks_per_bit)
        assert int(pin.value) == 1, "stop bit missing on TX line"
        out.append(v)
