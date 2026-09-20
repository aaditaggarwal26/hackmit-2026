"""What the authenticated bus does to traffic, as opposed to what the canonicaliser does to bytes.

tests/test_crypto_parity.py proves the two tiers agree on the canonical string and the tag. This
file is about the other half: that a node configured with a policy drops what it should, counts
what it drops, and — the part that matters most — that a node configured WITHOUT one behaves
exactly as it did before any of this existed.

The policy is attached to an already-built LoopbackBus rather than passed to its constructor,
because that is the honest shape of the thing: `Bus.policy` is one attribute with an all-off
default, and every existing caller gets the default.
"""

from __future__ import annotations

from orbit.bus.base import BusStats, Deduper
from orbit.bus.loopback import LoopbackBus, LoopbackHub
from orbit.config import Settings
from orbit.protocol import auth
from orbit.protocol import messages as M

GROUND, SAT = "gx10-f548", "esp32-satellite-b"
KEY = b"not-a-real-key-0000000000000000"


def grant(sender: str, seq: int, t_ms: int = 0) -> M.Grant:
    bd = M.Breakdown(score=91.0, item_age_s=6.0, item_age_term=3.0, sat_wait_s=10.0, sat_wait_term=3.0, total=97.0)
    return M.Grant(sender, seq, t_ms or seq * 1000, round_id=seq, to=SAT, item_id=14, pace_bps=65536.0, breakdown=bd)


def heartbeat(sender: str, seq: int) -> M.Heartbeat:
    buf = M.BufferStats(slots=8, capacity_bytes=131072, used=5, free=3, occupancy_pct=62.5)
    return M.Heartbeat(
        sender,
        seq,
        seq * 1000,
        buffer=buf,
        eviction_count=0,
        queue_len=1,
        top_score=88.0,
        top_item_id=12,
        uptime_s=8.0,
        frames_scored=9,
        frames_sent=4,
    )


def policy(**kw) -> auth.Policy:
    return auth.Policy(key=KEY, ground_name=GROUND, sat_names=(SAT,), **kw)


def pair(pol: auth.Policy | None = None) -> tuple[LoopbackHub, LoopbackBus, LoopbackBus]:
    """A ground and a satellite on one hub, both under the same policy."""
    hub = LoopbackHub()
    g, s = LoopbackBus(hub, GROUND), LoopbackBus(hub, SAT)
    if pol is not None:
        g.policy = s.policy = pol
    return hub, g, s


def test_a_default_bus_is_untouched_by_any_of_this():
    """The guarantee the simulator's digest rests on: no policy, no behaviour change."""
    hub, g, s = pair()
    assert g.policy is auth.OFF and not g.policy.enabled
    g.send(grant(GROUND, 1))
    hub.deliver()
    assert [m.seq for m in s.poll()] == [1]
    assert s.stats.dropped_unauth == 0 and s.stats.dropped_replay == 0
    # and nothing was added to the wire
    assert b'"auth"' not in M.Grant.encode(grant(GROUND, 1))


def test_signed_traffic_flows_and_carries_one_extra_field():
    hub, g, s = pair(policy())
    g.send(grant(GROUND, 1))
    hub.deliver()
    [got] = s.poll()
    assert got.seq == 1 and got.to == SAT  # the message decodes exactly as before
    assert s.stats.dropped_unauth == 0

    # the JSON is still readable and still v1: one string field longer, nothing re-encoded
    raw = auth.sign(grant(GROUND, 1).encode(), KEY)
    assert raw.startswith(b'{"v":1,"type":"grant","from":"gx10-f548"')
    assert raw.endswith(b'"}') and b',"auth":"' in raw
    assert M.decode(raw).to == SAT  # from_doc ignores the extra key, so no decoder change


def test_an_unsigned_or_forged_datagram_never_reaches_the_decoder():
    hub, _g, s = pair(policy())
    attacker = LoopbackBus(hub, "attacker")  # no policy: it sends whatever it likes

    attacker._transmit(grant(GROUND, 99).encode())  # right sender name, no tag
    attacker._transmit(auth.sign(grant(GROUND, 99).encode(), b"guessed-key"))  # wrong key
    tampered = bytearray(auth.sign(grant(GROUND, 99).encode(), KEY))
    tampered[tampered.index(b'"item_id":14') + 11] = ord("5")  # 14 -> 15, tag untouched
    attacker._transmit(bytes(tampered))
    hub.deliver()

    assert s.poll() == []
    assert s.stats.dropped_unauth == 3
    assert s.stats.dropped_malformed == 0  # it never got as far as the decoder


def test_control_messages_are_pinned_to_the_configured_ground():
    """4c: a satellite acts on offers_open/grant/revoke/tx_ack from ONE name and no other."""
    hub, _g, s = pair(policy())
    rogue = LoopbackBus(hub, "rogue")
    rogue.policy = auth.Policy(key=KEY)  # holds the key, but is not the ground
    rogue.send(grant("rogue", 1))
    hub.deliver()
    assert s.poll() == [] and s.stats.last_unauth == "unpinned_ground"

    # and the ground's allowlist works the same way for satellite traffic
    hub2, g2, _s2 = pair(policy())
    stranger = LoopbackBus(hub2, "esp32-satellite-z")
    stranger.policy = auth.Policy(key=KEY)
    stranger.send(heartbeat("esp32-satellite-z", 1))
    hub2.deliver()
    assert g2.poll() == [] and g2.stats.last_unauth == "unpinned_satellite"


def test_pinning_alone_works_without_a_key():
    """Pinning is free and strictly additive: it does not wait on key provisioning."""
    hub, _g, s = pair(auth.Policy(ground_name=GROUND, sat_names=(SAT,)))
    rogue = LoopbackBus(hub, "rogue")
    rogue.send(grant("rogue", 1))
    rogue.send(grant(GROUND, 2))  # an impostor that got the name right is NOT stopped by pinning
    hub.deliver()
    assert [m.seq for m in s.poll()] == [2]
    assert s.stats.dropped_unauth == 1


def test_a_replayed_datagram_is_rejected_after_the_duplicate_window_has_moved_on():
    """4d anti-replay: a verified seq must beat that sender's last VERIFIED seq.

    The duplicate window already catches a replay while it is still IN the window, and that is
    what the four copies BUS_TX_REPEAT sends of every datagram land in — so those keep being
    counted as duplicates rather than raising a security counter on ordinary traffic. What the
    watermark adds is the case the window cannot cover: a datagram captured, held, and played
    back after the window has rolled past it. That is the realistic attack on a bus where the
    control messages are four fixed shapes and an attacker can simply record a `grant`.
    """
    hub = LoopbackHub()
    g = LoopbackBus(hub, GROUND)
    s = LoopbackBus(hub, SAT, dedup_window=2)  # a short window, so a replay can fall out of it
    g.policy = s.policy = policy()

    for seq in range(1, 6):
        g.send(grant(GROUND, seq, t_ms=seq * 1000))
    hub.deliver()
    assert [m.seq for m in s.poll()] == [1, 2, 3, 4, 5]

    captured = auth.sign(grant(GROUND, 1, t_ms=1000).encode(), KEY)  # a recording, tag and all
    for _ in range(3):
        s._ingest(captured)
    assert s.poll() == []
    assert s.stats.dropped_replay == 3 and s.stats.dropped_unauth == 0 and s.stats.dropped_dup == 0

    # a genuine repeat of the LAST datagram is a duplicate, not an attack: still in the window
    s._ingest(auth.sign(grant(GROUND, 5, t_ms=5000).encode(), KEY))
    assert s.stats.dropped_dup == 1 and s.stats.dropped_replay == 3

    # and without the watermark that same recording would simply be acted on again
    lax = LoopbackBus(LoopbackHub(), SAT, dedup_window=2)
    lax.policy = policy(monotonic_seq=False)
    for seq in range(1, 6):
        lax._ingest(auth.sign(grant(GROUND, seq, t_ms=seq * 1000).encode(), KEY))
    lax.poll()
    lax._ingest(captured)
    assert [m.seq for m in lax.poll()] == [1]


def test_a_forgery_cannot_poison_the_replay_watermark():
    """The reason the MAC is checked before the deduper and not after.

    One forged datagram with a huge seq, accepted into the dedup state, would set the real
    ground's watermark beyond anything it will send for the rest of the window — muting it
    completely, with no bad message ever being acted on. Denial of service through the defence.
    """
    hub, g, s = pair(policy())
    attacker = LoopbackBus(hub, "attacker")
    attacker._transmit(grant(GROUND, 10_000_000).encode())  # unsigned, claims to be the ground
    hub.deliver()
    assert s.stats.dropped_unauth == 1

    g.send(grant(GROUND, 1))  # the real ground, still at seq 1, is heard
    hub.deliver()
    assert [m.seq for m in s.poll()] == [1]


def test_a_restarted_sender_is_relearned():
    """An operator restarting the ground mid-demo must not mute it for the rest of the window."""
    hub, g, s = pair(policy())
    for seq in range(1, 6):
        g.send(grant(GROUND, seq, t_ms=60_000 + seq * 100))
    hub.deliver()
    assert len(s.poll()) == 5

    s._ingest(auth.sign(grant(GROUND, 1, t_ms=40).encode(), KEY))  # uptime back near zero
    assert [m.seq for m in s.poll()] == [1]
    assert s.stats.dropped_replay == 0


def test_anti_replay_is_only_armed_when_the_mac_is():
    """Without a key, "seq must increase" would just be a way to lose reordered datagrams to an
    attacker who can spoof a sender name for free."""
    assert not auth.Policy(ground_name=GROUND).replay_checked
    assert auth.Policy(key=KEY).replay_checked
    assert not auth.Policy(key=KEY, monotonic_seq=False).replay_checked
    d = Deduper(16)
    assert d.classify("a", 5, 5) == "new"
    assert d.classify("a", 4, 6) == "new"  # reordering is fine when anti-replay is off
    assert d.classify("a", 3, 7, monotonic=True) == "replay"
    assert d.replays == 1


def test_a_signed_datagram_that_no_longer_fits_is_dropped_not_truncated():
    """The 42 bytes are charged against bus_max_datagram, so the limit is checked after signing."""
    hub, _g, _s = pair(policy())
    tight = LoopbackBus(hub, GROUND, max_datagram=len(grant(GROUND, 1).encode()))
    tight.policy = policy()
    tight.send(grant(GROUND, 1))
    assert tight.stats.dropped_oversize == 1 and tight.stats.sent == 0
    assert hub.deliver() == 0


# --- configuration ---------------------------------------------------------------------


def test_policy_is_built_from_settings_and_is_off_by_default():
    assert auth.Policy.from_settings(Settings()) == auth.OFF
    s = Settings(auth_key="k", ground_name=GROUND, pin_senders=True, nodes_real=True)
    p = auth.Policy.from_settings(s)
    assert p.key == b"k" and p.ground_name == GROUND
    assert p.sat_names == ("esp32-satellite-b", "esp32-satellite-c")  # the roster is sat_names()
    # pin_senders off leaves the MAC on and the names unenforced: the two are independent
    assert auth.Policy.from_settings(Settings(auth_key="k")).sat_names == ()


def test_the_key_comes_from_the_environment_and_is_never_logged():
    s = Settings.from_env({"ORBIT_AUTH_KEY": "s3cr3t-from-the-env", "ORBIT_PIN_SENDERS": "1"})
    assert s.auth_key == "s3cr3t-from-the-env" and s.pin_senders
    # as_dict() is logged verbatim at ground_start and recorded into every bench result
    assert s.as_dict()["auth_key"] == "<set>"
    assert "s3cr3t" not in repr(s.as_dict())
    assert Settings().as_dict()["auth_key"] == ""


def test_bus_stats_still_serialise():
    """The display reads these; new counters must not need a schema change to appear."""
    got = BusStats().as_dict()
    assert got["dropped_unauth"] == 0 and got["dropped_replay"] == 0 and got["last_unauth"] == ""
    assert got["dropped_unsignable"] == 0
