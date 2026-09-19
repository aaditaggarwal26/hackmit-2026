"""Whole-node regression: edge_node_core (and the top_edge_node board wrapper)
driven over its UART pins with the exact frames encode(msg) produces, decoded
back with FrameDecoder, and compared with ScoringNode message for message.

Policy: the FRAME_SCORED / TX_FRAME / TX_DONE / BENCH_DONE stream must equal the
golden stream exactly except BENCH_DONE.cycles (> 0 on the board, 0 in the model);
every STATUS_REPLY answering a query or CONFIG_SET must equal the golden reply
except cycles_last_frame and the LINK_OK flag (the sim's link timeout is shorter
than one frame's UART time). HEARTBEAT/POWER are timer-driven and checked
separately. Framer error counters are checked against FrameDecoder on the same
corrupted bytes via STATUS_QUERY.

Sim parameters: CLK_HZ=1 MHz so a "millisecond" is 1000 clocks, BAUD=100 k so a
bit is 10 clocks (a 137-byte row is ~13.7 sim-ms, a frame ~1.75 M clocks).
"""
import dataclasses

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

from orbit import params
from orbit.golden.node import ScoringNode, rows
from orbit.golden.vectors import random_frame
from orbit.protocol import messages as M
from orbit.protocol.messages import FrameDecoder, encode

from orbit_tb import reset, run, uart_monitor, uart_send, wait_cycles

CLK_HZ = 1_000_000
BAUD = 100_000
CPB = CLK_HZ // BAUD
HB_MS, LINK_MS, POWER_MS = 50, 120, 30
NODE_ID = 2
ROW_TIMEOUT = 400_000            # cycles to wait for a reply after the last row (kernel + composite + tx)
CORE_SOURCES = ["edge_node_core.v", "node_ctrl.v", "frame_store.v", "sobel_unit.v", "score_kernel.v",
                "score_composite.v", "priority_queue.v", "uart_rx.v", "uart_tx.v", "crc16.v", "byte_fifo.v",
                "framer_rx.v", "framer_tx.v", "i2c_master.v", "ina219_reader.v"]
STREAM = (M.FrameScored, M.TxFrame, M.TxDone, M.BenchDone)
PERIODIC = (M.Heartbeat, M.Power)


class Link:
    """UART both ways + FrameDecoder on the node's TX bytes."""

    def __init__(self, dut, rxd, txd):
        self.dut, self.rxd, self.txd = dut, rxd, txd
        self.raw = bytearray()
        self.dec = FrameDecoder()
        self.msgs = []
        self.fed = 0

    def start(self):
        self.rxd.value = 1
        cocotb.start_soon(uart_monitor(self.dut.clk, self.txd, CPB, self.raw))

    def poll(self):
        if len(self.raw) > self.fed:
            self.msgs += self.dec.feed(bytes(self.raw[self.fed:]))
            self.fed = len(self.raw)

    async def send_bytes(self, data: bytes):
        await uart_send(self.dut.clk, self.rxd, data, CPB)

    async def send(self, msg):
        await self.send_bytes(encode(msg))

    async def wait_for(self, cls, timeout_cycles, start=0):
        waited = 0
        while True:
            self.poll()
            for i in range(start, len(self.msgs)):
                if isinstance(self.msgs[i], cls):
                    return i
            await wait_cycles(self.dut.clk, 100)
            waited += 100
            assert waited < timeout_cycles, f"timeout waiting for {cls.__name__}; last: {self.msgs[-3:]}"

    def stream(self):
        self.poll()
        return [m for m in self.msgs if isinstance(m, STREAM)]

    def statuses(self):
        self.poll()
        return [m for m in self.msgs if isinstance(m, M.StatusReply)]


def norm_status(s: M.StatusReply) -> dict:
    d = dataclasses.asdict(s)
    d["cycles_last_frame"] = 0
    d["flags"] &= ~M.StatusReply.F_LINK_OK
    return d


def norm_stream(ms):
    return [dataclasses.replace(m, cycles=0) if isinstance(m, M.BenchDone) else m for m in ms]


class Pair:
    """The DUT link and the golden node, driven with the same messages."""

    def __init__(self, dut, rxd, txd):
        self.link = Link(dut, rxd, txd)
        self.gold = ScoringNode(NODE_ID)
        self.gold_out = []
        self.dut = dut

    async def send(self, msg, now_ms=0):
        self.gold_out += self.gold.handle(msg, now_ms)
        await self.link.send(msg)

    async def send_frame(self, frame, fid, cls=M.FrameIngest):
        for m in rows(frame, fid, cls):
            await self.send(m)

    def gold_stream(self):
        return [m for m in self.gold_out if isinstance(m, STREAM)]

    def gold_statuses(self):
        return [m for m in self.gold_out if isinstance(m, M.StatusReply)]

    async def query(self, _n_before=0):
        """STATUS_QUERY; wait until the DUT has sent as many STATUS_REPLYs as the golden model
        (heartbeats are off in these builds, so replies match by ordinal); compare the last."""
        await self.send(M.StatusQuery())
        exp_all = self.gold_statuses()
        waited = 0
        while len(self.link.statuses()) < len(exp_all):
            await wait_cycles(self.dut.clk, 100)
            waited += 100
            assert waited < ROW_TIMEOUT, f"timeout: {len(self.link.statuses())} of {len(exp_all)} STATUS replies"
        got, exp = self.link.statuses()[len(exp_all) - 1], exp_all[-1]
        assert norm_status(got) == norm_status(exp), f"STATUS differs:\n rtl {got}\n gold {exp}"
        return len(self.link.msgs)


def is_wrapper(dut) -> bool:
    return hasattr(dut, "sw")                        # top_edge_node has sw/led ports; the core has node_id/rst_n


def dut_ports(dut):
    if is_wrapper(dut):
        return dut.uart_txd_in, dut.uart_rxd_out
    return dut.uart_rxd, dut.uart_txd


async def bring_up(dut):
    cocotb.start_soon(Clock(dut.clk, 1000, unit="ns").start())
    rxd, txd = dut_ports(dut)
    p = Pair(dut, rxd, txd)
    p.link.start()
    if not is_wrapper(dut):
        dut.node_id.value = NODE_ID
        dut.sda_i.value = 1                          # no INA219 on the bus
        await reset(dut)
    else:
        dut.sw.value = NODE_ID
        await wait_cycles(dut.clk, 300)              # power-on reset counter
    await wait_cycles(dut.clk, 50)
    return p


@cocotb.test()
async def ingest_score_grant_bench(dut):
    p = await bring_up(dut)
    rng = np.random.default_rng(21)
    # config (lower the queue limit so an eviction happens within three frames)
    cfg = M.ConfigSet(w_clear=21845, w_sharp=21845, w_change=21845, cloud_thr=200, change_thr=16, sharp_shift=5,
                      queue_limit=2)
    await p.send(cfg)
    n = await p.query(0)
    # reference, then three frames; every FRAME_SCORED must match the golden exactly
    ref = random_frame(rng, "smooth")
    await p.send_frame(ref, 100, M.RefFrameSet)
    frames = [random_frame(rng, "cloudy"), random_frame(rng, "smooth"), random_frame(rng, "noise")]
    for k, f in enumerate(frames):
        await p.send_frame(f, 10 + k)
        await p.link.wait_for(M.FrameScored, ROW_TIMEOUT, start=len(p.link.msgs))
        rtl, gold = p.link.stream(), p.gold_stream()
        assert rtl == gold, f"after frame {k}:\n rtl  {rtl[-1]}\n gold {gold[-1]}"
    assert any(m.evicted_id != 0xFFFF for m in p.link.stream()), "queue_limit=2 with 3 frames must evict"
    n = await p.query(len(p.link.msgs))
    # grants: too small a budget, then two real ones, then an empty queue
    for budget in (params.FRAME_BYTES - 1, 1 << 20, 1 << 20, 1 << 20):
        before = len(p.link.msgs)
        await p.send(M.Grant(slot_id=7, budget_bytes=budget))
        await p.link.wait_for(M.TxDone, ROW_TIMEOUT, start=before)
    assert norm_stream(p.link.stream()) == norm_stream(p.gold_stream())
    n = await p.query(len(p.link.msgs))
    # bench: 0 iterations answers at once; 3 iterations reports cycles > 0
    await p.send(M.BenchRun(iterations=0))
    await p.send(M.BenchRun(iterations=3))
    i = await p.link.wait_for(M.BenchDone, ROW_TIMEOUT, start=len(p.link.msgs) - 1)
    await p.link.wait_for(M.BenchDone, ROW_TIMEOUT, start=i + 1)
    dones = [m for m in p.link.stream() if isinstance(m, M.BenchDone)]
    assert [d.iterations for d in dones] == [0, 3] and dones[0].cycles == 0 and dones[1].cycles > 3 * 2000
    assert norm_stream(p.link.stream()) == norm_stream(p.gold_stream())
    dut._log.info(f"bench: 3 iterations = {dones[1].cycles} cycles; last frame {p.link.statuses()[-1].cycles_last_frame} cycles")


@cocotb.test()
async def rejects_and_error_counters(dut):
    p = await bring_up(dut)
    bad = M.ConfigSet(w_clear=1, w_sharp=1, w_change=1, cloud_thr=1, change_thr=1, sharp_shift=params.SHARP_SHIFT_MAX + 1,
                      queue_limit=4)
    await p.send(bad)
    n = await p.query(0)
    assert not (p.link.statuses()[-1].flags & M.StatusReply.F_CONFIG_VALID) and p.link.statuses()[-1].busy_drops == 1
    await p.send(M.FrameIngest(frame_id=1, row=200, pixels=bytes(128)))        # row out of range
    # corrupted bytes: the RTL counters must equal FrameDecoder's on the same stream
    dec = FrameDecoder()
    hb = encode(M.Heartbeat(sender=params.ORCH_ID, seq=1, uptime_ms=1))
    flipped = bytearray(hb); flipped[2] ^= 1
    junk = bytes([0x7E, 0x01, 0x02, 0x00])
    for b in (bytes(flipped), junk, hb[6:] + hb):
        dec.feed(b)
        await p.link.send_bytes(b)
    p.gold.handle(M.Heartbeat(sender=params.ORCH_ID, seq=1, uptime_ms=1), 0)    # the good one that got through
    p.gold.crc_errors, p.gold.len_errors, p.gold.unknown_type, p.gold.rx_overflow = (
        dec.crc_errors, dec.len_errors, dec.unknown_type, dec.rx_overflow)
    n = await p.query(len(p.link.msgs))
    st = p.link.statuses()[-1]
    assert st.busy_drops == 2 and (st.crc_errors, st.len_errors, st.unknown_type) == (dec.crc_errors, dec.len_errors, dec.unknown_type)
    assert sum((dec.crc_errors, dec.len_errors, dec.unknown_type)) >= 2


@cocotb.test()
async def timers_and_link(dut):
    p = await bring_up(dut)
    # the board wrapper only exposes CLK_HZ/BAUD: its timers run at the real periods
    hb, link, power = ((params.HEARTBEAT_MS, params.LINK_TIMEOUT_MS, params.POWER_PERIOD_MS) if is_wrapper(dut)
                       else (HB_MS, LINK_MS, POWER_MS))
    await p.link.send(M.Heartbeat(sender=params.ORCH_ID, seq=0, uptime_ms=0))
    await p.link.wait_for(M.StatusReply, 3 * hb * 1000)
    st = p.link.statuses()[-1]
    assert st.flags & M.StatusReply.F_LINK_OK and st.node_id == NODE_ID
    i = await p.link.wait_for(M.Power, 3 * power * 1000)
    assert p.link.msgs[i].flags == 0                                            # no INA219 answered
    hbs = [m for m in p.link.msgs if isinstance(m, M.Heartbeat)]
    assert hbs and hbs[0].sender == NODE_ID
    await wait_cycles(dut.clk, (link + 2 * hb) * 1000)
    p.link.poll()
    assert not (p.link.statuses()[-1].flags & M.StatusReply.F_LINK_OK), "link_ok must drop after LINK_TIMEOUT_MS"


# message-level tests: heartbeats effectively off, so every STATUS_REPLY seen is an answer
QUIET = {"CLK_HZ": CLK_HZ, "BAUD": BAUD, "I2C_HZ": 25000, "HEARTBEAT_MS": 60000, "LINK_TIMEOUT_MS": 120000,
         "POWER_PERIOD_MS": POWER_MS}
TIMED = {"CLK_HZ": CLK_HZ, "BAUD": BAUD, "I2C_HZ": 25000, "HEARTBEAT_MS": HB_MS, "LINK_TIMEOUT_MS": LINK_MS,
         "POWER_PERIOD_MS": POWER_MS}


def test_core():
    run("edge_node_core", CORE_SOURCES, "test_top", parameters=QUIET,
        testcase=["ingest_score_grant_bench", "rejects_and_error_counters"])


def test_core_timers():
    run("edge_node_core", CORE_SOURCES, "test_top", parameters=TIMED, testcase="timers_and_link")


def test_core_ppc1():
    run("edge_node_core", CORE_SOURCES, "test_top", parameters=dict(QUIET, PPC=1),
        testcase=["ingest_score_grant_bench"])


def test_top_wrapper():
    """The board wrapper: same UART behaviour through the tristate/POR glue at sim rates."""
    run("top_edge_node", CORE_SOURCES + ["top_edge_node.v"], "test_top", parameters={"CLK_HZ": CLK_HZ, "BAUD": BAUD},
        testcase="timers_and_link")
