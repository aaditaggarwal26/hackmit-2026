"""Hold the firmware's decision logic to orbit/sim/satellite.py, the way check_score_parity.py
holds its scoring kernel to the golden model.

There is no arduino-cli here, so the .ino cannot be compiled and nothing in it can be claimed to
work. firmware/satellite_esp32/orbit_sat.h exists so that the parts that are *decisions* rather
than I/O can be: it is Arduino-free, the ESP32 builds the same file, and g++ builds it here.

The expectations are not written out by hand where they can be taken from the simulator instead —
a FakeSatellite is actually driven into each tx_ack state and its real behaviour is compared with
what the firmware would decide in that state.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from orbit import config, corpus
from orbit.golden.score import display
from orbit.protocol import messages as M
from orbit.sim.satellite import FakeSatellite, SatelliteProfile

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "firmware/test/sat_host.cpp"
INC = ROOT / "firmware/satellite_esp32"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="no g++ to build the host harness")

S = config.Settings(chunk_bytes=4096, bid_window_n=3, sat_heartbeat_ms=100000)


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """Build the firmware header into a host binary. A compile error is a test failure."""
    exe = tmp_path_factory.mktemp("fw") / "sat_host"
    subprocess.run(
        ["g++", "-O2", "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{INC}", str(SRC), "-o", str(exe)],
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


def run(exe: Path, mode: str, stdin: str = "") -> list[str]:
    r = subprocess.run([str(exe), mode], input=stdin, capture_output=True, text=True)
    assert r.returncode == 0, f"{mode} failed ({r.returncode}): {r.stdout}{r.stderr}"
    return r.stdout.splitlines()


def test_host_selftest_passes(host):
    """The header's own table of cases: admission, tx_ack, digest formatting."""
    assert run(host, "selftest")[-1].startswith("OK:")


def test_reported_numbers_round_exactly_as_the_simulator(host):
    """The ground compares what it re-scores with what was sent, so the rounding is protocol.

    Exhaustive on both axes: every raw score the kernel can produce and every possible cloud
    pixel count. The two languages break a .5 tie differently (C's lround away from zero,
    Python's round to even), so "no input lands on a tie" is checked rather than argued — it
    was not true: cloud_px 512 is exactly 0.03125.

    Equality here, not approximately: the firmware returns the rounded value as a double, which
    is the same double Python produced. Narrowing it to a float would not be a rounding error to
    tolerate, it would be the wrong number on the wire (the nearest float to 61.04 serialises as
    61.040000916), so the test must fail if anyone narrows it.
    """
    px_n = config.FRAME_BYTES + 1
    pairs = [(raw, raw % px_n) for raw in range(65536)]
    pairs += [(0, px) for px in range(px_n)]
    out = run(host, "round", "\n".join(f"{r} {p}" for r, p in pairs) + "\n")
    assert len(out) == len(pairs)
    seen_px = set()
    for (raw, px), line in zip(pairs, out, strict=True):
        part, cloud = line.split()
        assert part == f"{round(display(raw), 2):.6f}", raw
        assert cloud == f"{round(px / config.FRAME_BYTES, 4):.6f}", px
        seen_px.add(px)
    assert seen_px == set(range(px_n))  # every cloud pixel count really was covered


def _sat(corp, **kw):
    s = FakeSatellite(
        SatelliteProfile("sat-t", capture_period_s=1.0, capture_jitter=0.0, buffer_slots=4, **kw), S, corp
    )
    s.start(0.0)
    return s


def _transmit(s: FakeSatellite) -> int:
    """Drive a satellite to the point where it has sent tx_done and is awaiting the ack."""
    s.on_tick(1.0)
    [b] = s.on_message(M.OffersOpen("g", 1, 0, round_id=1, window_remaining_bytes=1, collect_ms=1), 1.0)
    bd = M.Breakdown(b.score, 0, 0, 0, 0, b.score)
    s.on_message(M.Grant("g", 2, 0, round_id=1, to="sat-t", item_id=b.item_id, pace_bps=1e9, breakdown=bd), 1.0)
    s.on_tick(1.1)
    assert s.awaiting_ack == b.item_id
    return int(b.item_id)


def _ack(s: FakeSatellite, item_id: int, ok: bool, t: float) -> None:
    s.on_message(M.TxAck("g", 9, 0, round_id=1, to="sat-t", item_id=item_id, ok=ok, bytes_received=1, reason=""), t)


def _firmware_says(host: Path, s: FakeSatellite, item_id: int, ok: bool) -> str:
    """What orbit_sat.h would decide, given the simulator's state translated field for field."""
    await_ack = s.awaiting_ack is not None
    line = " ".join(
        str(int(x))
        for x in (
            await_ack,
            s.awaiting_ack or 0,
            item_id,
            ok,
            item_id in s.items,
            s.tx is not None,
            s.tx.item_id if s.tx else 0,
        )
    )
    return run(host, "ack", line + "\n")[0]


@pytest.mark.parametrize(
    "ok,gave_up,expect_kept,decision",
    [
        (True, False, False, "POP"),  # the ack we waited for: the frame leaves
        (False, False, True, "NACK_KEEP"),  # a failed transmission must never lose data
        (True, True, False, "POP_LATE"),  # protocol rule 6: ok for a frame we still hold
    ],
)
def test_ack_decisions_match_the_simulator(host, ok, gave_up, expect_kept, decision):
    """Each case is taken from a real FakeSatellite in that state, not written out by hand."""
    corp = corpus.load()
    s = _sat(corp)
    item_id = _transmit(s)
    if gave_up:
        # the ground opened a later round: we stop waiting and re-offer, then the ok arrives
        s.on_message(M.OffersOpen("g", 3, 0, round_id=2, window_remaining_bytes=1, collect_ms=1), 1.2)
        assert s.awaiting_ack is None and item_id in s.items

    assert _firmware_says(host, s, item_id, ok) == decision

    before = s.counters.transmitted
    _ack(s, item_id, ok, 1.3)
    assert (item_id in s.items) is expect_kept
    assert s.counters.transmitted == before + (0 if expect_kept else 1)


def test_ack_for_an_item_already_gone_is_ignored(host):
    """Nothing is ever popped on an ack we cannot corroborate by still holding the item."""
    corp = corpus.load()
    s = _sat(corp)
    item_id = _transmit(s)
    _ack(s, item_id, True, 1.3)
    assert item_id not in s.items
    assert _firmware_says(host, s, item_id, True) == "IGNORE"
    before = vars(s.counters).copy()
    _ack(s, item_id, True, 1.4)  # a duplicate ack for a frame already popped changes nothing
    assert vars(s.counters) == before


def test_admission_decisions_match_the_simulator(host):
    """Full pool: the newcomer takes the worst held frame's slot only if it beats it outright."""
    corp = corpus.load()
    s = _sat(corp)
    for t in range(1, 6):
        s.on_tick(float(t))
    assert s.buffer.free == 0
    tail_key, tail_item = s.queue.cells[-1]

    rows, expect = [], []
    for key, in_flight in (
        (tail_key + 1, 0xFFFF),
        (tail_key, 0xFFFF),
        (tail_key - 1, 0xFFFF),
        (tail_key + 1, tail_item),
    ):
        rows.append(f"{key} 0 {tail_key} {tail_item} {in_flight}")
        # the simulator's own rule, read off _admit's branch conditions
        expect.append("REJECT" if (key <= tail_key or tail_item == in_flight) else "EVICT_TAIL")
    rows.append(f"{tail_key + 1} 1 {tail_key} {tail_item} 65535")
    expect.append("STORE")
    assert run(host, "admit", "\n".join(rows) + "\n") == expect

    # and the simulator really does reject a better frame while the tail is in flight
    from orbit.sim.satellite import Item

    s.awaiting_ack = tail_item
    out = s._admit(Item(999, 0, 65535, 5.5), bytes(config.FRAME_BYTES), 5.5)
    assert out and out[0].kind == "rejected" and tail_item in s.items
