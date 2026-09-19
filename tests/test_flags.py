"""Soft vs hard, and the four classifications that stop wait time alone producing false flags."""

import pytest

from orbit import config
from orbit.arbiter.flags import Level, SatelliteRecord, WaitKind, assess

S = config.Settings(soft_wait_s=20.0, hard_wait_s=60.0, memory_pressure_evictions_per_min=6.0,
                    memory_pressure_window_s=60.0, peer_stale_s=5.0)


def rec(*, first=0.0, seen=None, last_tx=None, qlen=1, evictions=0, now=0.0):
    r = SatelliteRecord("sat-x", first_seen_s=first, last_seen_s=seen if seen is not None else now,
                        last_tx_complete_s=last_tx, queue_len=qlen)
    for i in range(evictions):
        r.note_eviction(now - i * 0.1, S)
    return r


@pytest.mark.parametrize("last_tx,now,qlen,kind,level", [
    (100.0, 110.0, 1, WaitKind.NOMINAL, Level.NONE),  # transmitted 10 s ago, holding items: fine
    (100.0, 130.0, 1, WaitKind.STARVED, Level.SOFT),  # 30 s: aging at work, informational
    (100.0, 170.0, 1, WaitKind.STARVED, Level.HARD),  # 70 s: aging should have won by now
    (100.0, 170.0, 0, WaitKind.IDLE, Level.NONE),  # 70 s but nothing to send: never a flag
    (None, 10.0, 1, WaitKind.NEVER_TRANSMITTED, Level.NONE),  # just arrived
    (None, 30.0, 1, WaitKind.NEVER_TRANSMITTED, Level.SOFT),  # bidding 30 s, never won
    (None, 70.0, 1, WaitKind.NEVER_TRANSMITTED, Level.HARD),  # bidding 70 s, never won: anomaly
    (None, 70.0, 0, WaitKind.IDLE, Level.NONE),  # never transmitted, nothing to send
])
def test_wait_classification(last_tx, now, qlen, kind, level):
    a = assess(rec(first=0.0, last_tx=last_tx, qlen=qlen, now=now), now, S)
    assert (a.kind, a.level) == (kind, level), a.reason


def test_thresholds_are_strict():
    assert assess(rec(last_tx=0.0, now=20.0), 20.0, S).level is Level.NONE
    assert assess(rec(last_tx=0.0, now=20.01), 20.01, S).level is Level.SOFT
    assert assess(rec(last_tx=0.0, now=60.0), 60.0, S).level is Level.SOFT
    assert assess(rec(last_tx=0.0, now=60.01), 60.01, S).level is Level.HARD


def test_memory_pressure_is_independent_of_wait():
    a = assess(rec(last_tx=99.0, now=100.0, evictions=6), 100.0, S)
    assert a.kind is WaitKind.NOMINAL and a.level is Level.NONE and a.memory_pressured and a.attention
    b = assess(rec(last_tx=99.0, now=100.0, evictions=5), 100.0, S)
    assert not b.memory_pressured and not b.attention


def test_eviction_rate_uses_a_sliding_window():
    r = rec(last_tx=0.0, now=0.0)
    for t in range(10):
        r.note_eviction(float(t), S)
    assert r.evictions_per_min(10.0, S) == 10.0
    assert r.evictions_per_min(65.0, S) == 5.0  # the first five fell out of the 60 s window
    assert r.evictions_per_min(200.0, S) == 0.0


def test_silence_overrides_everything():
    a = assess(rec(last_tx=0.0, seen=100.0, now=200.0), 200.0, S)
    assert a.kind is WaitKind.SILENT and a.silent and a.attention and a.level is Level.NONE


def test_soft_alone_is_not_attention():
    a = assess(rec(last_tx=100.0, now=130.0), 130.0, S)
    assert a.level is Level.SOFT and not a.attention
    assert assess(rec(last_tx=100.0, now=170.0), 170.0, S).attention


def test_wait_epoch_is_first_seen_before_any_transmission():
    r = rec(first=50.0, last_tx=None, now=80.0)
    assert r.wait_s(80.0) == 30.0
    r.last_tx_complete_s = 75.0
    assert r.wait_s(80.0) == 5.0
