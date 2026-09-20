"""Bus behaviour that the WiFi will test for us if we don't: duplicates, garbage, own echoes, reordering."""

from orbit.bus import multicast
from orbit.bus.base import Deduper
from orbit.bus.loopback import Faults, LoopbackBus, LoopbackHub
from orbit.protocol import messages as M

GROUP, PORT = "239.255.42.99", 50007  # the real group, a port no running node uses


def offers(sender, seq):
    return M.OffersOpen(sender, seq, seq * 1000, round_id=seq, window_remaining_bytes=1, collect_ms=1)


def test_deduper_window():
    d = Deduper(3)
    assert d.is_new("a", 1) and not d.is_new("a", 1)
    assert d.is_new("b", 1)  # per sender
    assert d.is_new("a", 2) and d.is_new("a", 3) and not d.is_new("a", 1)  # a's window: 1,2,3
    assert d.is_new("a", 4) and d.is_new("a", 1)  # 1 slid out of a's window (no restart: gap 3 is not > window)
    assert d.is_new("a", 6) and d.is_new("a", 5) and not d.is_new("a", 6)  # out of order is fine


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


def test_sender_restart_is_forgotten_not_deduplicated():
    """A rebooted node starts at seq 1 with uptime near 0. Its old numbers must not make it deaf for minutes."""
    d = Deduper(4096, restart_slack_ms=5000)
    for i in range(1, 501):
        assert d.is_new("sat-a", i, t_ms=i * 1000)
    assert not d.is_new("sat-a", 3, t_ms=499_000)  # a genuine late duplicate: small backward t, known seq
    assert d.is_new("sat-a", 1, t_ms=200)  # reboot: uptime collapsed by far more than the slack
    assert d.restarts == 1 and d.is_new("sat-a", 2, t_ms=1200) and not d.is_new("sat-a", 2, t_ms=1300)
    # a seq far below the max with no uptime signal (e.g. t_ms not trusted) also counts as a restart
    d2 = Deduper(100)
    for i in range(1, 1001):
        d2.is_new("g", i, t_ms=i)
    assert d2.is_new("g", 1, t_ms=1001) and d2.restarts == 1


def test_restarted_ground_is_heard_on_the_loopback_bus():
    hub = LoopbackHub()
    ground, sat = LoopbackBus(hub, "ground"), LoopbackBus(hub, "sat-a")
    for i in range(1, 51):
        ground.send(offers("ground", i))
    hub.deliver()
    assert len(sat.poll()) == 50
    ground2 = LoopbackBus(hub, "ground")  # the process was restarted: seq and uptime start over
    ground2.send(M.OffersOpen("ground", 1, 5, round_id=1, window_remaining_bytes=1, collect_ms=1))
    hub.deliver()
    assert len(sat.poll()) == 1 and sat.stats.dropped_dup == 0


def test_a_real_multicast_socket_can_send_to_its_own_group():
    """The bind that makes a node deaf and mute, caught on the machine it breaks on.

    ``open_socket`` binds the group on Linux, where that filters, and the wildcard everywhere
    else, where binding the group would give the socket a source address that is not a local
    one: the join still succeeds, receiving still works, and only ``sendto`` fails, with
    EADDRNOTAVAIL on the very first datagram. Nothing above the socket can see that -- the
    arbiter simply never hears a bid -- so it is checked here, against a real socket, rather
    than by reading the platform back out of the module.
    """
    sock = multicast.open_socket(GROUP, PORT, "127.0.0.1", ttl=0)  # ttl 0: stays on this host
    try:
        sock.sendto(b"orbit-bind-check", (GROUP, PORT))
    finally:
        sock.close()
