"""Hold the firmware's control-bus authenticator to orbit/protocol/auth.py, byte for byte.

This is the deliverable of the auth work, not the HMAC. HMAC-SHA256 is a solved problem; two
independent canonicalisers agreeing on what bytes to feed it is not, and an unverified
canonicaliser is worse than none — it fails closed on the day, silently, on one tier only.

So, exactly as tools/check_score_parity.py does for the scoring kernel and
tests/test_firmware_sat.py for the satellite's decisions: the SAME header the ESP32 compiles is
built here with g++ and driven over every protocol vector plus an adversarial case for each
ambiguity the two languages could disagree about, and both the CANONICAL STRING and the TAG are
compared byte for byte. Agreement on *rejection* counts too: a datagram one tier refuses to
canonicalise and the other accepts is the same bug wearing a different hat.

Everything crosses the process boundary hex-encoded, because a good third of the adversarial
corpus contains raw newlines, NUL bytes and invalid UTF-8 inside JSON strings, which a
line-oriented plain-text channel would quietly eat.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from orbit.protocol import auth
from orbit.protocol import messages as M

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "firmware/test/crypto_host.cpp"
INC = ROOT / "firmware/satellite_esp32"
HEADER = INC / "orbit_crypto.h"
VECTORS = ROOT / "docs/protocol_vectors.json"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="no g++ to build the host harness")

# Obviously not a key. Long enough to exercise the ordinary path, shaped so that nobody can
# mistake it for something that was ever used: never put a production-looking secret in a
# fixture, even a fake one.
TEST_KEY = b"not-a-real-key-0000000000000000"


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """Build the firmware header into a host binary. A compile error is a test failure."""
    exe = tmp_path_factory.mktemp("fw") / "crypto_host"
    subprocess.run(
        ["g++", "-O2", "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{INC}", str(SRC), "-o", str(exe)],
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


def run(exe: Path, *args: str, stdin: str = "") -> list[str]:
    r = subprocess.run([str(exe), *args], input=stdin, capture_output=True, text=True)
    assert r.returncode == 0, f"{args} failed ({r.returncode}): {r.stdout}{r.stderr}"
    return r.stdout.splitlines()


def feed(exe: Path, mode: str, payloads: list[bytes], *args: str) -> list[str]:
    out = run(exe, mode, *args, stdin="\n".join(p.hex() for p in payloads) + "\n")
    assert len(out) == len(payloads), f"{mode}: {len(out)} lines for {len(payloads)} inputs"
    return out


# --- the corpus -----------------------------------------------------------------------


def dg(body: str = "", *, v: str = "1", t: str = '"x"', f: str = '"sat-a"', seq: str = "1", ms: str = "2") -> bytes:
    """A well-formed envelope with an arbitrary body fragment spliced in."""
    inner = f'"v":{v},"type":{t},"from":{f},"seq":{seq},"t_ms":{ms}'
    return ("{" + inner + ("," + body if body else "") + "}").encode()


def _good() -> list[tuple[str, bytes]]:
    """Real traffic: the published vectors, and everything encode() can produce."""
    cases = [(f"vector:{v['name']}", v["wire"].encode()) for v in json.loads(VECTORS.read_text())]
    cases += [(f"example:{n}", m.encode()) for n, m in M.examples()]
    return cases


def _ambiguities() -> list[tuple[str, bytes]]:
    """One case per place ArduinoJson and Python's json could have been made to disagree.

    Everything here canonicalises; what is being checked is that the two tiers produce the SAME
    canonical bytes for it, and (below) that the cases meant to be different really are.
    """
    u2019 = b"\xe2\x80\x99"  # the U+2019 an iOS hotspot puts in its SSID; see secrets.h.example
    return [
        # float formatting: the narrowing bug from orbit_sat.h, MACed as the digits sent
        ("float:2dp", dg('"score":61.04')),
        ("float:narrowed", dg('"score":61.040000916')),
        ("float:long", dg('"score":97.33333333333333')),
        ("float:tiny", dg('"score":0.0001')),
        # integer vs float for a whole number: three different messages, three different tags
        ("num:int", dg('"score":91')),
        ("num:float", dg('"score":91.0')),
        ("num:trailing-zeros", dg('"score":91.00')),
        ("num:exp-lower", dg('"score":9.1e1')),
        ("num:exp-upper", dg('"score":9.1E1')),
        ("num:exp-plus", dg('"score":9.1e+1')),
        ("num:exp-neg", dg('"score":9100e-2')),
        ("num:zero", dg('"score":0')),
        ("num:negative-zero", dg('"score":-0')),
        ("num:negative-zero-float", dg('"score":-0.0')),
        ("num:u32-max", dg(seq="4294967295", ms="4294967295")),
        # bools and null: null and absent are different messages, because encode() omits None
        ("bool:true", dg('"psram_ok":true')),
        ("bool:false", dg('"psram_ok":false')),
        ("null:explicit", dg('"rssi_dbm":null')),
        ("null:absent", dg()),
        # nested objects and arrays
        ("nest:empty-object", dg('"buffer":{}')),
        ("nest:empty-array", dg('"window":[]')),
        ("nest:array-order", dg('"window":[{"a":1},{"a":2}]')),
        ("nest:array-order-swapped", dg('"window":[{"a":2},{"a":1}]')),
        ("nest:inner-key-order", dg('"buffer":{"used":5,"free":3,"slots":8}')),
        ("nest:inner-key-order-sorted", dg('"buffer":{"free":3,"slots":8,"used":5}')),
        ("nest:depth-3", dg('"a":[{"b":[1,2]}]')),
        # key ordering at the top level, and the sort rule's prefix tie
        ("key:order-a", dg('"a":1,"b":2,"c":3')),
        ("key:order-c", dg('"c":3,"b":2,"a":1')),
        ("key:prefix-tie", dg('"ab":1,"a":2,"abc":3')),
        ("key:non-ascii", b'{"v":1,"type":"x","from":"sat-a","seq":1,"t_ms":2,"k' + u2019 + b'":1}'),
        # whitespace: the ONLY thing normalised away
        ("ws:none", dg('"a":1,"b":2')),
        ("ws:pretty", b'{ "v" : 1 , "type" : "x" , "from" : "sat-a" ,\n "seq" : 1 ,\t"t_ms" : 2 , "a" : 1 , "b" : 2 }'),
        ("ws:in-array", dg('"w":[ 1 , 2 ]')),
        # string escaping: nothing is decoded, so every spelling is its own message
        ("str:plain-A", dg('"reason":"A"')),
        ("str:escaped-A", dg(r'"reason":"\u0041"')),
        ("str:solidus", dg('"reason":"/"')),
        ("str:escaped-solidus", dg(r'"reason":"\/"')),
        ("str:newline", dg(r'"reason":"a\nb"')),
        ("str:escaped-nul", dg(r'"reason":"\u0000"')),
        ("str:quote", dg(r'"reason":"say \"hi\""')),
        ("str:backslash", dg(r'"reason":"c:\\x"')),
        ("str:surrogate-pair", dg(r'"reason":"\ud83d\ude80"')),
        ("str:lone-surrogate", dg(r'"reason":"\ud800"')),
        ("str:uppercase-hex-escape", dg(r'"reason":"\u00E9"')),
        ("str:lowercase-hex-escape", dg(r'"reason":"\u00e9"')),
        ("str:empty", dg('"reason":""')),
        # non-ASCII: raw UTF-8 and its escape are two literals, never transcoded
        ("utf8:raw", b'{"v":1,"type":"x","from":"sat-a","seq":1,"t_ms":2,"reason":"Ritvik' + u2019 + b's"}'),
        ("utf8:escaped", dg(r'"reason":"Ritvik\u2019s"')),
        ("utf8:invalid", b'{"v":1,"type":"x","from":"sat-a","seq":1,"t_ms":2,"reason":"\xff\xfe"}'),
        ("utf8:in-from", b'{"v":1,"type":"x","from":"sat-' + u2019 + b'","seq":1,"t_ms":2}'),
        # the separator cannot be forged out of a hostname, because type/from stay quoted
        ("sep:pipe-in-type", dg(t='"a|b"', f='"c"')),
        ("sep:pipe-in-from", dg(t='"a"', f='"b|c"')),
        ("sep:quote-in-from", dg(t='"a"', f=r'"b\"c"')),
        ("sep:brace-in-from", dg(t='"a"', f='"b}{c"')),
        # `auth` is excluded at the top level and MACed anywhere else
        ("auth:present", dg('"a":1')[:-1] + b',"auth":"' + b"f" * 32 + b'"}'),
        ("auth:nested", dg('"x":{"auth":1}')),
        ("auth:body-key-lookalike", dg('"auth_":1')),
        # the member limit, from both sides. A datagram with ORBIT_JSON_MAX_MEMBERS top-level
        # members canonicalises but CANNOT BE SIGNED, because the tag is one more member; the
        # widest real message (heartbeat) has 16, so the protocol is nowhere near either edge.
        ("wide", dg(",".join(f'"k{i}":{i}' for i in range(18)))),  # 23, signs to 24
        ("wide:at-limit", dg(",".join(f'"k{i}":{i}' for i in range(19)))),  # 24, cannot be signed
    ]


def _rejects() -> list[tuple[str, bytes]]:
    """Datagrams NEITHER tier may canonicalise. Agreement on refusal is half the property."""
    return [
        ("bad:duplicate-key", dg('"a":1,"a":2')),
        ("bad:duplicate-key-nested", dg('"x":{"a":1,"a":2}')),
        ("bad:duplicate-envelope", b'{"v":1,"type":"x","from":"sat-a","seq":1,"t_ms":2,"seq":3}'),
        ("bad:trailing-bytes", dg() + b"x"),
        ("bad:trailing-object", dg() + dg()),
        ("bad:missing-v", b'{"type":"x","from":"sat-a","seq":1,"t_ms":2}'),
        ("bad:missing-type", b'{"v":1,"from":"sat-a","seq":1,"t_ms":2}'),
        ("bad:missing-from", b'{"v":1,"type":"x","seq":1,"t_ms":2}'),
        ("bad:missing-seq", b'{"v":1,"type":"x","from":"sat-a","t_ms":2}'),
        ("bad:missing-t_ms", b'{"v":1,"type":"x","from":"sat-a","seq":1}'),
        ("bad:from-not-a-string", b'{"v":1,"type":"x","from":7,"seq":1,"t_ms":2}'),
        ("bad:type-not-a-string", b'{"v":1,"type":7,"from":"sat-a","seq":1,"t_ms":2}'),
        ("bad:seq-negative", dg(seq="-1")),
        ("bad:seq-float", dg(seq="1.0")),
        ("bad:seq-overflow", dg(seq="4294967296")),
        ("bad:t_ms-negative", dg(ms="-1")),
        ("bad:nan", dg('"score":nan')),
        ("bad:infinity", dg('"score":Infinity')),
        ("bad:leading-zero", dg('"score":01')),
        ("bad:leading-plus", dg('"score":+1')),
        ("bad:bare-fraction", dg('"score":.5')),
        ("bad:trailing-point", dg('"score":1.')),
        ("bad:empty-exponent", dg('"score":1e')),
        ("bad:hex-number", dg('"score":0x10')),
        ("bad:bool-capitalised", dg('"ok":True')),
        ("bad:single-quotes", b"{'v':1}"),
        ("bad:unquoted-key", b'{v:1,"type":"x","from":"sat-a","seq":1,"t_ms":2}'),
        ("bad:raw-newline-in-string", dg('"reason":"a\nb"').replace(b"\\n", b"\n")),
        ("bad:raw-tab-in-string", b'{"v":1,"type":"x","from":"sat-a","seq":1,"t_ms":2,"r":"a\tb"}'),
        ("bad:raw-nul-in-string", b'{"v":1,"type":"x","from":"sat-a","seq":1,"t_ms":2,"r":"a\x00b"}'),
        ("bad:undefined-escape", dg(r'"reason":"\q"')),
        ("bad:short-u-escape", dg(r'"reason":"\u41"')),
        ("bad:non-hex-u-escape", dg(r'"reason":"\uzzzz"')),
        ("bad:unterminated-string", b'{"v":1,"type":"x","from":"sat-a","seq":1,"t_ms":2,"r":"a}'),
        ("bad:unterminated-object", b'{"v":1,"type":"x","from":"sat-a","seq":1,"t_ms":2'),
        ("bad:unterminated-array", dg('"w":[1,2')),
        ("bad:trailing-comma", dg('"a":1,')),
        ("bad:empty", b""),
        ("bad:not-an-object", b"[1,2,3]"),
        ("bad:bare-number", b"5"),
        ("bad:depth-bomb", dg('"a":' + "[" * 8 + "1" + "]" * 8)),
        ("bad:too-many-members", dg(",".join(f'"k{i}":{i}' for i in range(40)))),
        ("bad:missing-colon", b'{"v" 1,"type":"x","from":"sat-a","seq":1,"t_ms":2}'),
    ]


CASES = _good() + _ambiguities() + _rejects()
REJECT_NAMES = {n for n, _ in _rejects()}


# --- the parity checks -----------------------------------------------------------------


def test_canonical_string_matches_the_firmware_byte_for_byte(host):
    """The whole corpus through both canonicalisers, compared as bytes.

    Agreement on a refusal counts: ``ERR`` on one side and a canonical string on the other is
    the failure mode this test exists to catch, because it is the one that would only show up
    as "the ground ignores that board" on the day.
    """
    got = feed(host, "canon", [d for _, d in CASES])
    for (name, d), line in zip(CASES, got, strict=True):
        try:
            want = auth.canonical(d)[0].hex()
        except auth.CanonicalError:
            want = "ERR"
        assert line == want, (
            f"{name}: firmware={line[:160]} python={want[:160]}\n  firmware={bytes.fromhex(line)!r}"
            if line != "ERR"
            else f"{name}: firmware=ERR python={want[:160]}"
        )


def test_the_corpus_really_does_exercise_both_outcomes(host):
    """Guards the test above from passing because everything happens to be ERR (or none is)."""
    got = feed(host, "canon", [d for _, d in CASES])
    errs = {n for (n, _), line in zip(CASES, got, strict=True) if line == "ERR"}
    assert errs == REJECT_NAMES, f"unexpected: {sorted(errs - REJECT_NAMES)}, missed: {sorted(REJECT_NAMES - errs)}"
    assert len(REJECT_NAMES) >= 40 and len(CASES) - len(REJECT_NAMES) >= 70


def test_tags_match_the_firmware(host):
    """The tag itself, over the same corpus and a key of every interesting length."""
    ok = [(n, d) for n, d in CASES if n not in REJECT_NAMES]
    for key in (TEST_KEY, b"k", b"0" * 64, b"0" * 65, b"\x01\x02\xff" * 30):
        got = feed(host, "tag", [d for _, d in ok], key.hex())
        for (name, d), line in zip(ok, got, strict=True):
            canon, _env = auth.canonical(d)
            assert line == auth.tag(key, canon), f"{name} under a {len(key)}-byte key"


def test_hmac_primitive_matches_hashlib(host):
    """The header carries its own SHA-256 rather than calling mbedtls, precisely so this can
    be checked here; mbedtls on the device would be code no host test ever touches."""
    msgs = [b"", b"a", b"abc", bytes(range(256)), b"x" * 55, b"x" * 56, b"x" * 63, b"x" * 64, b"x" * 65, b"y" * 1000]
    for key in (b"", b"k", b"0" * 63, b"0" * 64, b"0" * 65, b"\x01\xff" * 100):
        got = feed(host, "hmac", msgs, key.hex())
        for m, line in zip(msgs, got, strict=True):
            assert line == hmac.new(key, m, hashlib.sha256).hexdigest(), (len(key), len(m))


def test_signing_produces_identical_datagrams(host):
    """Both tiers splice the same 42 bytes in the same place, so either can verify the other."""
    ok = [(n, d) for n, d in CASES if n not in REJECT_NAMES and b'"auth"' not in d and n != "wide:at-limit"]
    got = feed(host, "sign", [d for _, d in ok], TEST_KEY.hex())
    for (name, d), line in zip(ok, got, strict=True):
        want = auth.sign(d, TEST_KEY)
        assert bytes.fromhex(line) == want, name
        assert len(want) == len(d) + auth.AUTH_OVERHEAD, name
        assert auth.Policy(key=TEST_KEY).check(want) == "", name


def test_a_datagram_at_the_member_limit_cannot_be_signed(host):
    """The tag is a top-level member like any other, so the limit has to leave room for it.

    No real message comes near this — the widest is heartbeat at 16 of 24 — but a canonicaliser
    that signs something it will then refuse to verify is the kind of asymmetry that only shows
    up under load, so it is pinned here rather than argued about.
    """
    at_limit = dict(CASES)["wide:at-limit"]
    assert auth.canonical(at_limit)  # it verifies fine as long as nobody adds a 25th member
    with pytest.raises(auth.CanonicalError):
        auth.sign(at_limit, TEST_KEY)
    assert feed(host, "sign", [at_limit], TEST_KEY.hex()) == ["ERR"]
    # and the same refusal for a datagram that already carries a tag: re-signing would leave two
    # `auth` keys in the object, which is the duplicate-key case neither tier will canonicalise
    signed = auth.sign(dict(CASES)["wide"], TEST_KEY)
    with pytest.raises(auth.CanonicalError):
        auth.sign(signed, TEST_KEY)
    assert feed(host, "sign", [signed], TEST_KEY.hex()) == ["ERR"]


def test_distinct_messages_get_distinct_canonical_strings():
    """The ambiguities are ambiguities only if the decisions actually separate them.

    Each group below is a set the two languages could plausibly have collapsed together. If any
    pair inside one collapses here, a sender and a receiver could disagree about what was signed
    while the tag still verified — which is the only way a canonicaliser silently lies.
    """
    groups = [
        ["num:int", "num:float", "num:trailing-zeros", "num:exp-lower", "num:exp-upper", "num:exp-plus"],
        ["num:zero", "num:negative-zero", "num:negative-zero-float"],
        ["null:explicit", "null:absent"],
        ["bool:true", "bool:false"],
        ["str:plain-A", "str:escaped-A"],
        ["str:solidus", "str:escaped-solidus"],
        ["str:uppercase-hex-escape", "str:lowercase-hex-escape"],
        ["utf8:raw", "utf8:escaped"],
        ["nest:array-order", "nest:array-order-swapped"],
        ["nest:empty-object", "nest:empty-array"],
        ["sep:pipe-in-type", "sep:pipe-in-from"],  # a `|` in a hostname cannot fake a boundary
        ["auth:present", "auth:nested", "auth:body-key-lookalike"],
        ["float:2dp", "float:narrowed"],
    ]
    by_name = dict(CASES)
    for group in groups:
        seen = {}
        for name in group:
            canon = auth.canonical(by_name[name])[0]
            assert canon not in seen, f"{name} and {seen.get(canon)} canonicalise the same"
            seen[canon] = name


def test_equivalent_spellings_get_the_same_canonical_string():
    """The flip side: whitespace and key order are exactly what MAY be normalised, and the
    top-level tag must never be its own input or nothing could ever verify."""
    by_name = dict(CASES)
    c = lambda n: auth.canonical(by_name[n])[0]  # noqa: E731
    assert c("ws:none") == c("ws:pretty")
    assert c("key:order-a") == c("key:order-c")
    assert c("nest:inner-key-order") == c("nest:inner-key-order-sorted")
    assert c("auth:present") == auth.canonical(dg('"a":1'))[0]


def test_header_constants_match_this_module():
    """The tag length is a wire fact. If the header and auth.py disagree about it, every
    datagram from one tier is 'no_tag' to the other, and nothing else here would notice."""
    text = HEADER.read_text()

    def define(name: str) -> str:
        m = re.search(rf"^#define\s+{name}\s+(.+?)\s*(?://.*)?$", text, re.M)
        assert m, name
        return m.group(1)

    assert int(define("ORBIT_TAG_BYTES")) == auth.TAG_BYTES
    assert int(define("ORBIT_JSON_MAX_DEPTH")) == auth.MAX_DEPTH
    assert int(define("ORBIT_JSON_MAX_MEMBERS")) == auth.MAX_MEMBERS
    assert define("ORBIT_AUTH_OVERHEAD") == "(ORBIT_TAG_HEX + 10)"
    assert auth.AUTH_OVERHEAD == auth.TAG_HEX + 10


def test_a_signed_worst_case_tx_chunk_still_fits_the_datagram_limit():
    """Why the tag is truncated to 128 bits, as an assertion rather than a comment.

    A full 32-byte tag pushes the largest frame chunk past bus_max_datagram, and sendDoc's own
    `n >= sizeof(buf)` guard would then drop every chunk of every frame — the failure would look
    like "the boards never transmit" rather than like a crypto decision.
    """
    from orbit import config

    s = config.Settings()
    worst = M.TxChunk(
        "esp32-satellite-b",
        4294967295,
        4294967295,
        round_id=-1,
        item_id=65535,
        idx=65535,
        n=65535,
        data=bytes(s.chunk_bytes),
    ).encode()
    assert len(auth.sign(worst, TEST_KEY)) < s.bus_max_datagram
    assert len(worst) + 2 * 32 + 10 > s.bus_max_datagram  # an untruncated tag would not fit


# --- policy: pinning, tags and anti-replay ---------------------------------------------


def _signed(seq: int, t_ms: int, type_: str, sender: str, key: bytes = TEST_KEY) -> bytes:
    return auth.sign(dg(t=f'"{type_}"', f=f'"{sender}"', seq=str(seq), ms=str(t_ms)), key)


def test_accept_decisions_match_the_firmware(host):
    """One stateful stream of datagrams through orbit_auth_accept, and the same rules in Python.

    The sequence deliberately interleaves the three ways a datagram gets refused, because the
    order they are checked in is itself a decision: pinning before the tag (cheaper, and it does
    not need a key), the tag before the replay watermark (so a forgery can never move it).
    """
    GROUND, SAT = "gx10-f548", "esp32-satellite-b"
    stream: list[tuple[bytes, str]] = [
        (_signed(1, 60000, "grant", GROUND), "OK"),
        (_signed(1, 60100, "grant", GROUND), "REPLAY"),  # a BUS_TX_REPEAT copy: acted on once
        (_signed(2, 60200, "grant", GROUND), "OK"),
        (_signed(2, 60300, "revoke", GROUND), "REPLAY"),  # seq must strictly increase
        (_signed(1, 60400, "grant", "attacker"), "PIN"),  # control traffic from anyone else
        (_signed(9, 60500, "heartbeat", SAT), "OK"),  # peer traffic is not pinned to the ground
        (_signed(3, 60600, "grant", GROUND, key=b"wrong-key"), "BAD_TAG"),
        (dg(t='"grant"', f=f'"{GROUND}"', seq="4", ms="60700"), "NO_TAG"),
        (b'{"v":1,"type":"grant"', "CANON"),
        (_signed(3, 60800, "grant", GROUND), "OK"),  # a bad tag never moved the watermark
        (_signed(1, 60900, "grant", GROUND), "REPLAY"),
        # the ground was restarted: uptime collapses by far more than restart_slack_ms and its
        # sequence starts again at 1. Without this the operator's Ctrl-C mid-demo is fatal.
        (_signed(1, 10, "grant", GROUND), "OK"),
        (_signed(1, 20, "grant", GROUND), "REPLAY"),
    ]
    got = feed(host, "accept", [d for d, _ in stream], TEST_KEY.hex(), GROUND)
    assert got == [want for _, want in stream]


def test_without_a_key_the_watermark_is_not_armed_on_either_tier(host):
    """Pinning is free and ships first; the watermark is not, and must not arm without the MAC.

    "seq must increase" without a MAC is not anti-replay — a sender name costs nothing to spoof,
    so one datagram with a huge seq would mute the real node for the rest of the window — and it
    would throw away the reordering tolerance the duplicate window was built for. Both tiers
    therefore gate the watermark on the key, and this is the test that says so; every other
    `accept` case here runs with a key and would not have noticed.
    """
    GROUND = "gx10-f548"
    same = auth.sign(dg(t='"grant"', f=f'"{GROUND}"', seq="7", ms="900"), TEST_KEY)
    unsigned = dg(t='"grant"', f=f'"{GROUND}"', seq="7", ms="900")
    rogue = dg(t='"grant"', f='"attacker"', seq="7", ms="900")

    # no key, pinning on: the pin bites, repeats do not, and an untagged control message is fine
    assert feed(host, "accept", [same, same, unsigned, unsigned, rogue], "", GROUND) == [
        "OK",
        "OK",
        "OK",
        "OK",
        "PIN",
    ]
    p = auth.Policy(key=b"", ground_name=GROUND)
    assert not p.replay_checked
    assert [p.check(d) for d in (same, same, unsigned, unsigned)] == ["", "", "", ""]
    assert p.check(rogue) == "unpinned_ground"

    # nothing configured at all: a pass-through on both tiers, garbage included
    assert feed(host, "accept", [same, same, b"{not json"], "", "") == ["OK", "OK", "OK"]
    assert [auth.OFF.check(d) for d in (same, same, b"{not json")] == ["", "", ""]


def test_python_policy_agrees_about_pinning_and_tags(host):
    """Policy.check is the ground's half of the same rules; the Deduper owns the watermark."""
    GROUND, SAT = "gx10-f548", "esp32-satellite-b"
    p = auth.Policy(key=TEST_KEY, ground_name=GROUND, sat_names=(SAT, "esp32-satellite-c"))
    assert p.check(_signed(1, 1, "grant", GROUND)) == ""
    assert p.check(_signed(1, 1, "heartbeat", SAT)) == ""
    assert p.check(_signed(1, 1, "grant", "attacker")) == "unpinned_ground"
    assert p.check(_signed(1, 1, "heartbeat", "rogue-sat")) == "unpinned_satellite"
    assert p.check(_signed(1, 1, "grant", GROUND, key=b"wrong-key")) == "bad_tag"
    assert p.check(dg(t='"grant"', f=f'"{GROUND}"')) == "no_tag"
    assert p.check(b"{").startswith("canonical:")
    # a single flipped bit anywhere in the body
    signed = bytearray(_signed(1, 1, "grant", GROUND))
    signed[10] ^= 0x01
    assert p.check(bytes(signed)) != ""

    # and the same datagrams are what the firmware accepts, so neither tier is alone in this
    good = [_signed(1, 1, "grant", GROUND), _signed(1, 1, "heartbeat", SAT)]
    assert feed(host, "accept", good, TEST_KEY.hex(), GROUND) == ["OK", "OK"]


def test_an_all_default_policy_is_a_pass_through():
    """Nothing in this module may change what an unconfigured node does with a datagram —
    that is what keeps `uv run orbit sim` deterministic and every older test untouched."""
    assert auth.OFF.check(dg()) == ""
    assert auth.OFF.check(b"{not json") == ""
    assert auth.OFF.sign(dg()) == dg()
    assert not auth.OFF.enabled and not auth.OFF.signing and not auth.OFF.replay_checked


def test_a_key_with_a_nul_is_refused():
    """It reaches the firmware as a C string, where it would be silently truncated at the zero."""
    with pytest.raises(ValueError):
        auth.check_key(b"ab\x00cd")
