"""Virtual-board acceptance: the same scenario on the real RTL (Verilator, UART over
a pty) and on the golden model must produce the same FRAME_SCORED / TX_FRAME /
TX_DONE stream above the transport layer, and the ground's live mirror must never
disagree with the board. Slow (builds and runs the virtual board)."""
import asyncio
import dataclasses
import shutil

import pytest

from orbit.orchestrator import scenarios
from orbit.protocol import messages as M

pytestmark = pytest.mark.slow
if shutil.which("verilator") is None:
    pytest.skip("verilator not installed", allow_module_level=True)

STREAM = (M.FrameScored, M.TxFrame, M.TxDone)


def _run(nodes, scenario, passes, **kw):
    orch = scenarios.build(scenario, nodes=nodes, passes=passes, virtual=all(n.startswith("sim://") for n in nodes),
                           speed=16, **kw)
    asyncio.run(orch.run())
    orch.close()
    return orch


def _stream(orch, idx):
    return [m for m in orch.nodes[idx].rx_log if isinstance(m, STREAM)]


@pytest.mark.parametrize("scenario,passes", [("nominal", 2), ("lead_change", 2), ("starvation", 2)])
def test_verilator_matches_golden(scenario, passes):
    rtl = _run(["verilator://0", "sim://1"], scenario, passes, frames_per_pass=6, slots_per_pass=3)
    gold = _run(["sim://0", "sim://1"], scenario, passes, frames_per_pass=6, slots_per_pass=3)
    s_rtl, s_gold = _stream(rtl, 0), _stream(gold, 0)
    assert s_rtl, "no frames from the virtual board"
    assert s_rtl == s_gold
    assert rtl.nodes[0].timeouts == 0 and rtl.nodes[0].mismatches == 0
    assert [s["node"] for s in rtl.slots] == [s["node"] for s in gold.slots]
    assert rtl.value_filtered == gold.value_filtered
    st = rtl.nodes[0].status
    assert st["cycles_last_frame"] > 0 and st["frames_scored"] == rtl.nodes[0].captures


def test_verilator_bench_run_reports_cycles():
    from orbit.protocol.messages import FrameDecoder, encode
    from orbit.protocol.transport import open_transport
    import time
    tr = open_transport("verilator://0", 115200, 0)
    dec = FrameDecoder()
    got = []
    tr.send(encode(M.BenchRun(iterations=50)))
    t0 = time.monotonic()
    while not any(isinstance(m, M.BenchDone) for m in got) and time.monotonic() - t0 < 30:
        d = tr.recv(0.1)
        if d:
            got += dec.feed(d)
    tr.close()
    done = [m for m in got if isinstance(m, M.BenchDone)]
    assert done and done[0].iterations == 50 and done[0].cycles >= 50 * 2048
