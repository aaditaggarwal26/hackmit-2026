"""crc16 module vs orbit.protocol.crc16 (CRC-16/CCITT-FALSE): the check value
and random byte strings, one byte per clock."""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

from orbit.protocol.crc16 import crc16

from orbit_tb import reset, run


async def feed(dut, data: bytes) -> int:
    dut.clear.value = 1
    await FallingEdge(dut.clk)
    dut.clear.value = 0
    for b in data:
        dut.byte_valid.value = 1
        dut.byte_in.value = b
        await FallingEdge(dut.clk)
    dut.byte_valid.value = 0
    await FallingEdge(dut.clk)
    return int(dut.crc.value)


@cocotb.test()
async def check_value_and_random(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.clear.value = 0
    dut.byte_valid.value = 0
    dut.byte_in.value = 0
    await reset(dut)
    assert int(dut.crc.value) == 0xFFFF
    assert await feed(dut, b"123456789") == 0x29B1
    rng = random.Random(3)
    for _ in range(50):
        data = bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 60)))
        assert await feed(dut, data) == crc16(data), f"crc mismatch on {data.hex()}"


def test_crc16():
    run("crc16", ["crc16.v"], "test_crc16")
