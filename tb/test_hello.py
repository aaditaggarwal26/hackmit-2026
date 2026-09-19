"""Phase-zero verification loop: Verilator + cocotb + pytest, one module, one test.

Convention for every testbench in this repo: drive inputs and sample outputs on
FallingEdge. Right after RisingEdge, Verilator still shows pre-edge register
values, so sampling there off-by-one's every check.
"""
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge
from cocotb_tools.runner import get_runner

RTL = Path(__file__).resolve().parent.parent / "rtl"


@cocotb.test()
async def counts_when_enabled(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.en.value = 0
    for _ in range(2):
        await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)
    assert int(dut.count.value) == 0
    for expected in range(1, 6):
        dut.en.value = 1
        await FallingEdge(dut.clk)
        assert int(dut.count.value) == expected, f"count={int(dut.count.value)} expected {expected}"
        dut.en.value = 0
        await FallingEdge(dut.clk)
        assert int(dut.count.value) == expected, "counted while disabled"


def test_hello():
    runner = get_runner("verilator")
    runner.build(sources=[RTL / "hello.v"], hdl_toplevel="hello",
                 build_dir=Path(__file__).parent / "sim_build" / "hello", always=True)
    runner.test(hdl_toplevel="hello", test_module="test_hello",
                test_dir=Path(__file__).parent)
