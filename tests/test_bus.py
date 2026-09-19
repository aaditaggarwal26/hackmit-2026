"""Bus behaviour that the WiFi will test for us if we don't: duplicates, garbage, own echoes, reordering."""

from orbit.bus.base import Deduper
from orbit.bus.loopback import Faults, LoopbackBus, LoopbackHub
from orbit.protocol import messages as M


def offers(sender, seq):
    return M.OffersOpen(sender, seq, 0, round_id=seq, window_remaining_bytes=1, collect_ms=1)


def test_deduper_window():
    d = Deduper(3)
    assert d.is_new("a", 1) and not d.is_new("a", 1)
    assert d.is_new("b", 1)  # per sender
    assert d.is_new("a", 2) and d.is_new("a", 3)  # window now (b1, a2, a3): a1 forgotten
    assert d.is_new("a", 1)
    assert d.is_new("a", 5) and d.is_new("a", 4) and not d.is_new("a", 5)  # out of order is fine


def test_loopback_broadcast_and_own_echo_dropped():
    hub = LoopbackHub()
    a, b, c = (LoopbackBus(hub, h) for h in ("a", "b", "c"))
    a.send(offers("a", 1))
    assert hub.deliver() == 1
    assert a.poll() == [] and a.stats.dropped_own == 1
    assert [m.seq for m in b.poll()] == [1] and [m.seq for m in c.poll()] == [1]


def test_duplicates_are_dropped_and_counted():
    hub = LoopbackHub(seed=1, faults=Faults(duplicate=1.0))
    a, b = LoopbackBus(hub, "a"), LoopbackBus(hub, "b")
    for i in range(5):
        a.send(offers("a", i))
    hub.deliver()
    assert [m.seq for m in b.poll()] == list(range(5))
    assert b.stats.dropped_dup == 5 and b.stats.received == 10


def test_garbage_is_counted_never_raised():
    hub = LoopbackHub()
    a, b = LoopbackBus(hub, "a"), LoopbackBus(hub, "b")
    for raw in (b"", b"\x00\xff", b"{}", b'{"v":1,"type":"bid"}', b"x" * 5000):
        hub._post(a, raw)
    a.send(offers("a", 1))
    hub.deliver()
    got = b.poll()
    assert len(got) == 1 and b.stats.dropped_malformed == 5 and b.stats.last_malformed


def test_oversize_send_is_refused():
    hub = LoopbackHub()
    a, b = LoopbackBus(hub, "a", max_datagram=100), LoopbackBus(hub, "b", max_datagram=100)
    a.send(offers("a", 1))
    hub.deliver()
    assert a.stats.dropped_oversize == 1 and b.poll() == []


def test_reorder_and_drop_do_not_break_delivery():
    hub = LoopbackHub(seed=3, faults=Faults(reorder=0.5, drop=0.2))
    a, b = LoopbackBus(hub, "a"), LoopbackBus(hub, "b")
    for i in range(200):
        a.send(offers("a", i))
    got = []
    for _ in range(10):
        hub.deliver()
        got += b.poll()
    seqs = [m.seq for m in got]
    assert len(seqs) == len(set(seqs)) and 100 < len(seqs) < 200
    assert b.stats.dropped_dup == 0
