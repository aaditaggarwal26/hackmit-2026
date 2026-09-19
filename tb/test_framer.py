"""framer_rx / framer_tx / byte_fifo against orbit.protocol.

framer_rx: every vector in docs/protocol_vectors.json decodes to the right
type and payload; the §3 error table (CRC, unknown type, length, short
frame, desync, overflow) gives the same counters as FrameDecoder on the same
bytes; a randomised stream of good and corrupted frames agrees with
FrameDecoder message for message and counter for counter.
framer_tx: every vector whose payload fits the N->O size encodes to the
exact wire hex, plus random messages vs encode(); `ready` respects the FIFO
room; the link-open 0x00 is sent once after reset.
"""
import json
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

from orbit import params
from orbit.protocol import cobs
from orbit.protocol import messages as M
from orbit.protocol.crc16 import crc16
from orbit.protocol.messages import EXAMPLES, MESSAGE_TYPES, FrameDecoder, encode

from orbit_tb import ROOT, reset, run, wait_cycles

VECTORS = json.loads((ROOT / "docs" / "protocol_vectors.json").read_text())
RX_MAX = 131
TX_MAX = 30
BYTE_GAP = 4          # cycles between bytes when driving framer_rx directly (UART gives >= 80)


# ------------------------------------------------------------------ framer_rx
class RxHarness:
    def __init__(self, dut):
        self.dut = dut
        self.msgs = []

    async def start(self):
        self.dut.rx_valid.value = 0
        self.dut.rx_data.value = 0
        await reset(self.dut)
        cocotb.start_soon(self._mon())

    async def _mon(self):
        while True:
            await FallingEdge(self.dut.clk)
            if int(self.dut.msg_valid.value):
                t = int(self.dut.msg_type.value)
                payload = int(self.dut.msg_payload.value).to_bytes(RX_MAX, "little")
                cls = MESSAGE_TYPES[t]
                self.msgs.append(cls.unpack(payload[:cls.size()]))

    async def feed(self, data: bytes):
        for b in data:
            self.dut.rx_valid.value = 1
            self.dut.rx_data.value = b
            await FallingEdge(self.dut.clk)
            self.dut.rx_valid.value = 0
            await wait_cycles(self.dut.clk, BYTE_GAP - 1)
        await wait_cycles(self.dut.clk, 4)

    def counters(self):
        d = self.dut
        return (int(d.crc_errors.value), int(d.len_errors.value), int(d.unknown_type.value), int(d.rx_overflow.value))


def py_counters(dec: FrameDecoder):
    return (dec.crc_errors, dec.len_errors, dec.unknown_type, dec.rx_overflow)


@cocotb.test()
async def rx_vectors(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    h = RxHarness(dut)
    await h.start()
    await h.feed(b"\x00")                      # link-open delimiter: silent
    for v in VECTORS:
        await h.feed(bytes.fromhex(v["wire_hex"]))
    assert h.msgs == [m for _, m in EXAMPLES], f"{h.msgs}"
    assert h.counters() == (0, 0, 0, 0)


@cocotb.test()
async def rx_error_table(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    h = RxHarness(dut)
    await h.start()
    dec = FrameDecoder()
    hb = M.Heartbeat(sender=0xFF, seq=1, uptime_ms=1)
    good = encode(hb)
    bad = bytearray(good); bad[2] ^= 0x01                       # CRC mismatch
    body = bytes([0x7E]) + b"\x01\x02"
    unknown = cobs.encode(body + crc16(body).to_bytes(2, "little")) + b"\x00"
    body = bytes([M.Heartbeat.TYPE]) + b"\x01\x02"
    badlen = cobs.encode(body + crc16(body).to_bytes(2, "little")) + b"\x00"
    trailing_zero_body = bytes([M.BenchRun.TYPE]) + M.BenchRun(iterations=0).pack()   # payload ends in 0x00
    tz = cobs.encode(trailing_zero_body + crc16(trailing_zero_body).to_bytes(2, "little")) + b"\x00"
    open_block = b"\x05\x01\x02\x00"                             # code runs past end
    cases = [bytes(bad), unknown, badlen, b"\x02\x05\x00", good[10:] + good, bytes([1] * 300) + b"\x00" + good,
             tz, open_block, bytes([1] * 254) + b"\x00", bytes([1] * 255) + b"\x00", good]
    exp_msgs = []
    for c in cases:
        exp_msgs += dec.feed(c)
        await h.feed(c)
        assert h.counters() == py_counters(dec), f"after {c[:12].hex()}..: rtl {h.counters()} py {py_counters(dec)}"
    assert h.msgs == exp_msgs
    assert all(c > 0 for c in h.counters()), h.counters()      # every counter exercised


@cocotb.test()
async def rx_random_stream(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    h = RxHarness(dut)
    await h.start()
    rng = random.Random(21)
    dec = FrameDecoder()
    stream = bytearray()
    for _ in range(120):
        name, m = rng.choice(EXAMPLES)          # includes the two 131-byte row messages
        f = bytearray(encode(m))
        r = rng.random()
        if r < 0.2:                                # flip a bit somewhere inside
            i = rng.randrange(0, len(f) - 1)
            f[i] ^= 1 << rng.randrange(8)
        elif r < 0.3:                              # truncate (desync)
            f = f[rng.randrange(1, len(f) - 1):]
        elif r < 0.35:                             # oversize junk
            f = bytearray(rng.choice([1, 2, 3]) for _ in range(rng.randrange(250, 300))) + b"\x00"
        stream += f
    exp = dec.feed(bytes(stream))
    await h.feed(bytes(stream))
    assert h.msgs == exp, f"{len(h.msgs)} vs {len(exp)} messages"
    assert h.counters() == py_counters(dec), f"rtl {h.counters()} py {py_counters(dec)}"
    assert sum(py_counters(dec)) > 0


# ------------------------------------------------------------------ framer_tx
class TxHarness:
    def __init__(self, dut):
        self.dut = dut
        self.out = bytearray()

    async def start(self):
        self.dut.msg_start.value = 0
        self.dut.msg_type.value = 0
        self.dut.msg_len.value = 0
        self.dut.msg_payload.value = 0
        self.dut.fifo_count.value = 0
        cocotb.start_soon(self._mon())       # before reset release: the link-open byte comes at once
        await reset(self.dut)

    async def _mon(self):
        while True:
            await FallingEdge(self.dut.clk)
            if int(self.dut.fifo_wr.value):
                self.out.append(int(self.dut.fifo_wdata.value))

    async def send(self, m: M.Message):
        d = self.dut
        while not int(d.ready.value):
            await FallingEdge(d.clk)
        d.msg_type.value = m.TYPE
        d.msg_len.value = m.size()
        d.msg_payload.value = int.from_bytes(m.pack(), "little")
        d.msg_start.value = 1
        await FallingEdge(d.clk)
        d.msg_start.value = 0
        while not int(d.ready.value):
            await FallingEdge(d.clk)
        await wait_cycles(d.clk, 2)


@cocotb.test()
async def tx_vectors(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    h = TxHarness(dut)
    await h.start()
    await wait_cycles(dut.clk, 4)
    assert bytes(h.out) == b"\x00", "exactly one link-open delimiter after reset"
    h.out.clear()
    msgs = [m for _, m in EXAMPLES if m.size() <= TX_MAX]
    assert len(msgs) >= 7
    for m in msgs:
        h.out.clear()
        await h.send(m)
        assert bytes(h.out) == encode(m), f"{type(m).__name__}: {bytes(h.out).hex(' ')} != {encode(m).hex(' ')}"
    # random messages, including zero-heavy ones (many implicit zeros, trailing zero)
    rng = random.Random(9)
    classes = [c for c in MESSAGE_TYPES.values() if c.size() <= TX_MAX]
    for _ in range(60):
        cls = rng.choice(classes)
        vals = {}
        for name, kind in cls.FIELDS:
            size, signed, count = M.KINDS[kind]
            lo, hi = (-(1 << (8 * size - 1)), (1 << (8 * size - 1)) - 1) if signed else (0, (1 << (8 * size)) - 1)
            pick = lambda: rng.choice([0, lo, hi, rng.randint(lo, hi)])
            vals[name] = tuple(pick() for _ in range(count)) if count > 1 else pick()
        m = cls(**vals)
        h.out.clear()
        await h.send(m)
        assert bytes(h.out) == encode(m), f"{m}: {bytes(h.out).hex(' ')} != {encode(m).hex(' ')}"


@cocotb.test()
async def tx_respects_room(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    h = TxHarness(dut)
    await h.start()
    await wait_cycles(dut.clk, 2)
    cap = 1 << 12
    wire_max = TX_MAX + 5                      # type + payload + crc + COBS code + delimiter
    dut.fifo_count.value = cap - (wire_max - 1)   # one byte short of a worst-case frame: not ready
    await FallingEdge(dut.clk)
    assert int(dut.ready.value) == 0
    dut.fifo_count.value = cap - wire_max
    await FallingEdge(dut.clk)
    assert int(dut.ready.value) == 1
    # Boundary: a frame is sent with exactly enough room. In the cycle the
    # framer writes the delimiter, fifo_count (which lags by a cycle) still
    # shows the old value; ready must NOT be asserted there, or the caller
    # would start a frame into a cycle without room and lose it.
    m = M.Heartbeat(sender=1, seq=2, uptime_ms=3)
    wire = len(encode(m))
    dut.fifo_count.value = cap - wire_max - wire   # exactly wire_max free once this frame has landed
    dut.msg_type.value = m.TYPE
    dut.msg_len.value = m.size()
    dut.msg_payload.value = int.from_bytes(m.pack(), "little")
    dut.msg_start.value = 1
    await FallingEdge(dut.clk)
    dut.msg_start.value = 0
    written = 0
    for _ in range(200):
        await FallingEdge(dut.clk)
        if int(dut.fifo_wr.value):
            written += 1
            dut.fifo_count.value = cap - wire_max - wire + written    # model the FIFO's one-cycle-later count
            assert int(dut.ready.value) == 0, "ready while a FIFO write is in flight"
        elif int(dut.ready.value):
            break
    assert written == wire
    assert int(dut.ready.value) == 1 and int(dut.fifo_count.value) == cap - wire_max


# ------------------------------------------------------------------ byte_fifo
@cocotb.test()
async def fifo_order_and_flags(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.wr_en.value = 0
    dut.rd_en.value = 0
    await reset(dut)
    assert int(dut.empty.value) == 1 and int(dut.count.value) == 0
    n = 1 << 4
    for i in range(n):
        dut.wr_en.value = 1
        dut.wr_data.value = (i * 37) & 0xFF
        await FallingEdge(dut.clk)
    dut.wr_en.value = 0
    await FallingEdge(dut.clk)
    assert int(dut.full.value) == 1 and int(dut.count.value) == n
    dut.wr_en.value = 1          # write while full is dropped
    dut.wr_data.value = 0xEE
    await FallingEdge(dut.clk)
    dut.wr_en.value = 0
    got = []
    for i in range(n):
        dut.rd_en.value = 1
        await FallingEdge(dut.clk)
        dut.rd_en.value = 0
        await FallingEdge(dut.clk)
        got.append(int(dut.rd_data.value))
    assert got == [(i * 37) & 0xFF for i in range(n)]
    assert int(dut.empty.value) == 1


def test_framer_rx():
    run("framer_rx", ["framer_rx.v", "crc16.v"], "test_framer", parameters={"MAX_PAYLOAD": RX_MAX},
        testcase=["rx_vectors", "rx_error_table", "rx_random_stream"])


def test_framer_tx():
    run("framer_tx", ["framer_tx.v", "crc16.v"], "test_framer",
        parameters={"MAX_PAYLOAD": TX_MAX, "FIFO_ADDR_W": 12}, testcase=["tx_vectors", "tx_respects_room"])


def test_byte_fifo():
    run("byte_fifo", ["byte_fifo.v"], "test_framer", parameters={"ADDR_W": 4}, testcase="fifo_order_and_flags")
