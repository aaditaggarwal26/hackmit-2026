"""ina219_reader + i2c_master against a Python I2C slave model on the
scl/sda lines. The model decodes START/STOP/bits from the open-drain
enables, ACKs its address, stores the register pointer and serves the two
16-bit registers MSB first. Checks: the exact transaction sequence
(pointer write, repeated start, two-byte read, twice), raw register values
and the ok flag; with the slave at another address every write is NACKed,
the sequence still completes and ok=0."""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

from orbit import params

from orbit_tb import reset, run

QUARTER = 8


class I2CSlave:
    """Bit-level I2C slave evaluated once per clock from the master's enables.
    Samples on SCL rising edges, drives on SCL falling edges."""

    def __init__(self, dut, addr, regs):
        self.dut, self.addr, self.regs = dut, addr, regs
        self.log = []                 # (addr, "W"/"R", [bytes])
        self.drive_low = False
        self.scl_prev, self.sda_prev = 1, 1
        self.phase = None             # None (idle), "addr", "wdata", "rdata"
        self.nb, self.bits = 0, 0
        self.matched, self.rw = False, 0
        self.pointer = 0
        self.tx_bits = []
        self.cur = None

    def _start_read_byte(self):
        v = self.regs.get(self.pointer, 0xFFFF)
        idx = len(self.cur[2])
        b = (v >> 8) & 0xFF if idx == 0 else v & 0xFF if idx == 1 else 0xFF
        self.tx_bits = [(b >> i) & 1 for i in range(7, -1, -1)]
        self.cur[2].append(b)
        self.drive_low = self.tx_bits.pop(0) == 0

    async def run(self):
        d = self.dut
        while True:
            await FallingEdge(d.clk)
            scl = 0 if int(d.scl_oe.value) else 1
            sda_m = 0 if int(d.sda_oe.value) else 1
            if scl and self.scl_prev and sda_m != self.sda_prev:      # START / STOP
                if sda_m == 0:
                    self.phase, self.nb, self.bits, self.matched = "addr", 0, 0, False
                    self.drive_low = False
                    if self.cur:                                        # repeated start closes the transfer
                        self.log.append(self.cur)
                        self.cur = None
                else:
                    self.phase = None
                    self.drive_low = False
                    if self.cur:
                        self.log.append(self.cur)
                        self.cur = None
            elif self.phase and scl and not self.scl_prev:              # SCL rising: sample
                if self.phase in ("addr", "wdata"):
                    if self.nb < 8:
                        self.bits = (self.bits << 1) | sda_m
                    self.nb += 1
                else:                                                   # rdata
                    self.nb += 1
                    if self.nb == 9:
                        self.master_ack = (sda_m == 0)
            elif self.phase and not scl and self.scl_prev:              # SCL falling: drive
                if self.phase in ("addr", "wdata"):
                    if self.nb == 8:                                    # byte in: ACK it?
                        if self.phase == "addr":
                            self.rw = self.bits & 1
                            self.matched = (self.bits >> 1) == self.addr
                            if self.matched:
                                self.cur = [self.addr, "R" if self.rw else "W", []]
                        elif self.matched:
                            self.pointer = self.bits
                            self.cur[2].append(self.bits)
                        self.drive_low = self.matched
                    elif self.nb == 9:                                  # ACK bit over
                        self.drive_low = False
                        self.nb, self.bits = 0, 0
                        if self.matched and self.rw:
                            self.phase = "rdata"
                            self._start_read_byte()
                        elif self.matched:
                            self.phase = "wdata"
                        else:
                            self.phase = "idle-nack"
                elif self.phase == "rdata":
                    if self.nb < 8:
                        self.drive_low = self.tx_bits.pop(0) == 0
                    elif self.nb == 8:
                        self.drive_low = False                          # master's ACK/NACK bit
                    else:
                        self.nb = 0
                        if self.master_ack:
                            self._start_read_byte()
            self.scl_prev, self.sda_prev = scl, sda_m
            d.sda_i.value = 0 if (self.drive_low or not sda_m) else 1


@cocotb.test()
async def reads_both_registers(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.trigger.value = 0
    dut.sda_i.value = 1
    await reset(dut)
    slave = I2CSlave(dut, params.INA219_ADDR, {0x02: 0x2648, 0x01: 0x1072})
    cocotb.start_soon(slave.run())

    async def sample():
        dut.trigger.value = 1
        await FallingEdge(dut.clk)
        dut.trigger.value = 0
        for _ in range(200_000):
            await FallingEdge(dut.clk)
            if int(dut.sample_valid.value):
                return int(dut.ok.value), int(dut.bus_raw.value), int(dut.shunt_raw.value)
        raise AssertionError("no sample")

    ok, bus, shunt = await sample()
    assert (ok, bus, shunt) == (1, 0x2648, 0x1072), (ok, hex(bus), hex(shunt), slave.log)
    assert slave.log == [[0x40, "W", [0x02]], [0x40, "R", [0x26, 0x48]], [0x40, "W", [0x01]], [0x40, "R", [0x10, 0x72]]], slave.log
    assert int(dut.scl_oe.value) == 0 and int(dut.sda_oe.value) == 0, "bus released after STOP"

    slave.regs[0x02] = 0x1234
    slave.log.clear()
    ok, bus, shunt = await sample()
    assert (ok, bus, shunt) == (1, 0x1234, 0x1072)

    slave.addr = 0x41                 # nobody home at 0x40: NACKs, but the sequence completes
    slave.log.clear()
    ok, bus, shunt = await sample()
    assert ok == 0 and (bus, shunt) == (0xFFFF, 0xFFFF) and slave.log == []


def test_ina219_reader():
    run("ina219_reader", ["ina219_reader.v", "i2c_master.v"], "test_ina219",
        parameters={"CLKS_PER_QUARTER": QUARTER})
