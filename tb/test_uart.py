"""uart_rx and uart_tx against Python bit-bang models at a small
CLKS_PER_BIT (the value the top-level tests use), including back-to-back
bytes, an idle gap, a start-bit glitch and a bad stop bit."""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from orbit_tb import reset, run, uart_monitor, uart_send

CPB = 10


@cocotb.test()
async def rx_decodes_bitbang(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rxd.value = 1
    await reset(dut, 5)
    got = []
    errs = [0]

    async def mon():
        while True:
            await FallingEdge(dut.clk)
            if int(dut.valid.value):
                got.append(int(dut.data.value))
            if int(dut.frame_err.value):
                errs[0] += 1

    cocotb.start_soon(mon())
    rng = random.Random(11)
    data = bytes([0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80] + [rng.getrandbits(8) for _ in range(40)])
    await uart_send(dut.clk, dut.rxd, data[:20], CPB)          # back to back
    await uart_send(dut.clk, dut.rxd, data[20:], CPB, gap_bits=3)  # with idle gaps
    await ClockCycles(dut.clk, 3 * CPB)
    assert bytes(got) == data, f"{bytes(got).hex()} != {data.hex()}"
    assert errs[0] == 0
    # a 2-clock low glitch is not a start bit
    dut.rxd.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rxd.value = 1
    await ClockCycles(dut.clk, 12 * CPB)
    assert len(got) == len(data)
    # bad stop bit: framing error, byte dropped, next byte fine
    for bit in [0] + [1, 0, 1, 0, 1, 0, 1, 0] + [0]:
        dut.rxd.value = bit
        await ClockCycles(dut.clk, CPB)
    dut.rxd.value = 1
    await ClockCycles(dut.clk, 2 * CPB)
    await uart_send(dut.clk, dut.rxd, b"\x42", CPB)
    await ClockCycles(dut.clk, 3 * CPB)
    assert errs[0] == 1 and got[-1] == 0x42 and len(got) == len(data) + 1


@cocotb.test()
async def tx_is_decodable(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.start.value = 0
    dut.data.value = 0
    await reset(dut, 5)
    out = bytearray()
    cocotb.start_soon(uart_monitor(dut.clk, dut.txd, CPB, out))
    rng = random.Random(5)
    data = bytes([0x00, 0xFF, 0x0F] + [rng.getrandbits(8) for _ in range(30)])
    for b in data:
        while int(dut.busy.value):
            await FallingEdge(dut.clk)
        dut.start.value = 1
        dut.data.value = b
        await FallingEdge(dut.clk)
        dut.start.value = 0
        await FallingEdge(dut.clk)
    while int(dut.busy.value):
        await FallingEdge(dut.clk)
    await ClockCycles(dut.clk, 2 * CPB)
    assert bytes(out) == data, f"{bytes(out).hex()} != {data.hex()}"
    assert int(dut.txd.value) == 1


def test_uart_rx():
    run("uart_rx", ["uart_rx.v"], "test_uart", parameters={"CLKS_PER_BIT": CPB}, testcase="rx_decodes_bitbang")


def test_uart_tx():
    run("uart_tx", ["uart_tx.v"], "test_uart", parameters={"CLKS_PER_BIT": CPB}, testcase="tx_is_decodable")
