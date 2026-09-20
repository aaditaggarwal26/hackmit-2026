"""Control-bus authenticity: sender pinning and HMAC-SHA256 over a canonical byte string.

The threat model is a local-link UDP multicast group on a shared WiFi carrying public NASA
imagery. Confidentiality is not a goal and nothing here encrypts: the bus log has to stay
readable during the demo. What must not be forgeable is the CONTROL path — a stranger on the
AP must not be able to say ``grant``, ``revoke`` or ``tx_ack`` and steer the arbiter.

This module is the Python half of a pair. The other half is
``firmware/satellite_esp32/orbit_crypto.h``, which the ESP32 compiles and which
``tests/test_crypto_parity.py`` builds with g++ and holds against this file byte for byte over
the protocol vectors plus an adversarial set. The two implementations are written from the same
rules below; the test is what makes that claim checkable rather than aspirational.

THE CANONICAL FORM
------------------
ArduinoJson and Python's :mod:`json` do not serialise the same bytes for the same values, so
the raw datagram cannot be MACed directly. The MAC input is built in a fixed order::

    v|type|from|seq|t_ms|<body>

``type`` and ``from`` are emitted as their JSON *string literals*, quotes included, so a ``|``
or a ``"`` inside a hostname cannot forge a field boundary. ``<body>`` is every other top-level
member, as a JSON object with keys sorted. The top-level ``auth`` member is EXCLUDED — it is
the field that carries the tag.

The idea that makes this checkable at all: **scalar tokens are copied verbatim**. Numbers,
string literals (escapes and all), ``true``, ``false`` and ``null`` are copied byte for byte
out of the datagram, and nothing is ever re-serialised. Every place the two languages disagree
is therefore unreachable rather than merely tested. The decisions that follow, each of which is
a divergence this would otherwise have:

* **float formatting** — not normalised. ``61.04`` MACs as ``61.04``; the float-narrowed
  ``61.040000916`` of ``orbit_sat.h``'s note MACs as ``61.040000916`` and is simply a different
  message. A sender MACs the digits it actually transmits, so no float printer has to agree.
* **integer vs float for whole numbers** — ``91`` and ``91.0`` are different canonical strings
  and get different tags. No unification is attempted; none is needed.
* **exponents / negative zero** — ``1e2``, ``1E2`` and ``100`` are distinct, as are ``-0`` and
  ``0``. Copied as written.
* **bool spelling** — only ``true``/``false`` parse; ``True``, ``1`` and ``"true"`` are other
  things entirely.
* **null vs absent** — distinct. ``encode()`` omits ``None``, so a null on the wire is a
  different message from an absent field and gets a different tag.
* **string escaping** — not normalised. ``"\\u0041"``, ``"A"`` and ``"A "`` are three different
  literals; ``"\\/"`` differs from ``"/"``.
* **non-ASCII** — the canonicaliser is BYTE-oriented and never decodes UTF-8, so raw UTF-8 (an
  iOS hotspot name with U+2019) and its ``\\uXXXX`` spelling are two different literals and no
  transcoding can diverge. Invalid UTF-8 is passed through rather than rejected, for the same
  reason: a codec is one more thing that could disagree.
* **key ordering** — every object's members are sorted by their raw key LITERAL bytes (memcmp,
  shorter first on a prefix tie), at every depth. Arrays keep their order: that is data.
* **whitespace** — the only thing normalised away, so a pretty-printed datagram and a compact
  one agree.
* **duplicate keys** — REJECTED. Python's json keeps the last, ArduinoJson the first; rather
  than pick a winner, a datagram with two identical key literals in one object cannot be signed
  or verified at all.
* **trailing bytes** — REJECTED. Anything after the closing brace fails.

Not covered, stated plainly: two spellings of the SAME key (``"a"`` and ``"\\u0061"``) are
treated as distinct members and both survive into the canonical string, so a *key holder* could
build a datagram two receivers read differently. That needs the pre-shared key, so it is not a
forgery path.

THE TAG
-------
``tag = HMAC-SHA256(key, canonical)[:16]``, 32 lowercase hex characters, carried as ``auth``
and spliced in as the last member: ``,"auth":"<32 hex>"`` before the closing brace.

128 bits rather than 256 because of a measurement, not a preference: the worst-case ``tx_chunk``
encodes to 1349 bytes and ``bus_max_datagram`` is 1400. A 32-byte tag adds 74 (1423) and every
frame chunk would be dropped as oversize; a 16-byte tag adds 42 (1391) and fits with room.
Truncated HMAC is the ordinary construction — RFC 4868 specifies HMAC-SHA-256-128 for exactly
this reason — and 2^-128 forgery odds are not the weak point of anything on this link.

KEYS
----
The key is used as raw bytes, with no decoding step, so C and Python cannot disagree about it:
whatever characters ``ORBIT_AUTH_KEY``'s string literal contains on the firmware side are the
same bytes ``ORBIT_AUTH_KEY=`` puts in the environment on the ground. It reaches the firmware
as a C string, so it may not contain a NUL byte and that is rejected here too. An empty key
disables the MAC, which is the default and is how the bus behaved before this module existed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orbit.protocol import ed25519

if TYPE_CHECKING:
    from orbit.config import Settings

# Mirrors of the #defines in orbit_crypto.h. tests/test_crypto_parity.py asserts they match.
TAG_BYTES = 16
TAG_HEX = TAG_BYTES * 2
AUTH_OVERHEAD = TAG_HEX + 10  # the bytes ,"auth":"..." adds to a datagram
MAX_DEPTH = 5
MAX_MEMBERS = 24

SIG_BYTES = ed25519.SIG_BYTES  # 64: an Ed25519 signature
SIG_B64_LEN = 88  # base64 of 64 bytes, with padding; the length of the `sig` field value

# The control messages a satellite must only ever act on from the configured ground.
CONTROL_TYPES = (b'"offers_open"', b'"grant"', b'"revoke"', b'"tx_ack"')

# The ground commands that actually change a satellite's state. These additionally carry an
# Ed25519 signature (the `sig` field) over the same canonical bytes as the HMAC tag, so that a
# holder of the SHARED HMAC key -- a satellite whose key leaked -- still cannot forge one; only
# the ground's private seed can produce a `sig` that verifies against its public key. A subset of
# CONTROL_TYPES: `offers_open` merely invites bids and is left to the HMAC + sender pin.
SIGNED_TYPES = (b'"grant"', b'"revoke"', b'"tx_ack"')

_WS = b" \t\n\r"
_DIGITS = b"0123456789"
_HEXDIGITS = b"0123456789abcdefABCDEF"
_ESCAPES = b'"\\/bfnrt'


class CanonicalError(Exception):
    """These bytes cannot be canonicalised, so they can be neither signed nor verified."""


@dataclass(frozen=True)
class Envelope:
    """What the canonicaliser saw on its way past, so callers need not re-parse."""

    type_lit: bytes  # JSON string literals, quotes included
    from_lit: bytes
    auth_lit: bytes | None  # the HMAC tag literal, or None when the datagram carries no `auth`
    sig_lit: bytes | None  # the Ed25519 signature literal, or None when there is no `sig`
    seq: int
    t_ms: int
    members: int  # top-level members, so sign() can see whether another would still fit


# --- the scanner ----------------------------------------------------------------------
#
# Index-for-pointer transliteration of orbit_crypto.h. Kept deliberately literal, loops and
# all: the two have to be readable side by side when one of them is wrong.


def _ws(d: bytes, i: int) -> int:
    n = len(d)
    while i < n and d[i] in _WS:
        i += 1
    return i


def _scan_string(d: bytes, i: int) -> int:
    """A JSON string literal starting at ``i``. Returns the index past the closing quote.

    Rejects an unescaped control byte and any escape the grammar does not define, so a datagram
    either scans the same way on both tiers or on neither. Bytes >= 0x80 pass through untouched.
    """
    n = len(d)
    if i >= n or d[i] != 0x22:
        raise CanonicalError("expected a string")
    i += 1
    while i < n:
        c = d[i]
        if c == 0x22:
            return i + 1
        if c == 0x5C:  # backslash
            i += 1
            if i >= n:
                break
            if d[i] == 0x75:  # \uXXXX
                if n - i < 5 or any(d[i + k] not in _HEXDIGITS for k in (1, 2, 3, 4)):
                    raise CanonicalError("bad \\u escape")
                i += 5
            elif d[i] in _ESCAPES:
                i += 1
            else:
                raise CanonicalError("undefined escape")
        elif c < 0x20:
            raise CanonicalError("raw control byte in a string")
        else:
            i += 1
    raise CanonicalError("unterminated string")


def _scan_number(d: bytes, i: int) -> int:
    """``-?(0|[1-9][0-9]*)(\\.[0-9]+)?([eE][+-]?[0-9]+)?``.

    No leading ``+``, no leading zeros, no bare ``.5``, no trailing ``.``, and no ``nan``/``inf``
    — which is what ArduinoJson emits for a non-finite double, so such a datagram fails to sign
    rather than going out unauthenticated.
    """
    n = len(d)
    if i < n and d[i] == 0x2D:  # -
        i += 1
    if i >= n:
        raise CanonicalError("truncated number")
    if d[i] == 0x30:  # 0
        i += 1
    elif 0x31 <= d[i] <= 0x39:
        while i < n and d[i] in _DIGITS:
            i += 1
    else:
        raise CanonicalError("not a value")
    if i < n and d[i] == 0x2E:  # .
        i += 1
        if i >= n or d[i] not in _DIGITS:
            raise CanonicalError("trailing decimal point")
        while i < n and d[i] in _DIGITS:
            i += 1
    if i < n and d[i] in b"eE":
        i += 1
        if i < n and d[i] in b"+-":
            i += 1
        if i >= n or d[i] not in _DIGITS:
            raise CanonicalError("empty exponent")
        while i < n and d[i] in _DIGITS:
            i += 1
    return i


def _scan_value(d: bytes, i: int, depth: int) -> int:
    """The end of one value, without emitting anything."""
    if depth > MAX_DEPTH:
        raise CanonicalError("too deeply nested")
    n = len(d)
    i = _ws(d, i)
    if i >= n:
        raise CanonicalError("truncated value")
    c = d[i]
    if c == 0x7B:  # {
        i = _ws(d, i + 1)
        if i < n and d[i] == 0x7D:
            return i + 1
        while True:
            i = _ws(d, i)
            i = _ws(d, _scan_string(d, i))
            if i >= n or d[i] != 0x3A:  # :
                raise CanonicalError("expected ':'")
            i = _ws(d, _scan_value(d, i + 1, depth + 1))
            if i < n and d[i] == 0x2C:  # ,
                i += 1
                continue
            if i < n and d[i] == 0x7D:
                return i + 1
            raise CanonicalError("expected ',' or '}'")
    if c == 0x5B:  # [
        i = _ws(d, i + 1)
        if i < n and d[i] == 0x5D:
            return i + 1
        while True:
            i = _ws(d, _scan_value(d, i, depth + 1))
            if i < n and d[i] == 0x2C:
                i += 1
                continue
            if i < n and d[i] == 0x5D:
                return i + 1
            raise CanonicalError("expected ',' or ']'")
    if c == 0x22:
        return _scan_string(d, i)
    for lit in (b"true", b"false", b"null"):
        if d[i : i + len(lit)] == lit:
            return i + len(lit)
    return _scan_number(d, i)


def _scan_object(d: bytes, i: int, depth: int) -> tuple[list[tuple[int, int, int, int]], int]:
    """``{...}`` into member spans ``(key_start, key_end, value_start, value_end)``, sorted.

    Sorting is on the raw key literal: memcmp over the shared prefix, shorter first on a tie —
    which is what ``bytes`` comparison already does. Duplicate literals are rejected.
    """
    if depth > MAX_DEPTH:
        raise CanonicalError("too deeply nested")
    n = len(d)
    if i >= n or d[i] != 0x7B:
        raise CanonicalError("expected an object")
    i = _ws(d, i + 1)
    if i < n and d[i] == 0x7D:
        return [], i + 1
    members: list[tuple[int, int, int, int]] = []
    while True:
        if len(members) >= MAX_MEMBERS:
            raise CanonicalError("too many members")
        i = _ws(d, i)
        ks = i
        ke = _scan_string(d, i)
        i = _ws(d, ke)
        if i >= n or d[i] != 0x3A:
            raise CanonicalError("expected ':'")
        vs = _ws(d, i + 1)
        ve = _scan_value(d, vs, depth + 1)
        members.append((ks, ke, vs, ve))
        i = _ws(d, ve)
        if i < n and d[i] == 0x2C:
            i += 1
            continue
        if i < n and d[i] == 0x7D:
            i += 1
            break
        raise CanonicalError("expected ',' or '}'")
    members.sort(key=lambda m: d[m[0] : m[1]])
    for a, b in itertools.pairwise(members):
        if d[a[0] : a[1]] == d[b[0] : b[1]]:
            raise CanonicalError("duplicate key")
    return members, i


def _emit(d: bytes, i: int, depth: int, out: bytearray) -> int:
    """One value, canonical, appended to ``out``. Returns the index past it."""
    if depth > MAX_DEPTH:
        raise CanonicalError("too deeply nested")
    n = len(d)
    i = _ws(d, i)
    if i >= n:
        raise CanonicalError("truncated value")
    c = d[i]
    if c == 0x7B:
        members, end = _scan_object(d, i, depth)
        out += b"{"
        for k, (ks, ke, vs, _ve) in enumerate(members):
            if k:
                out += b","
            out += d[ks:ke]
            out += b":"
            _emit(d, vs, depth + 1, out)
        out += b"}"
        return end
    if c == 0x5B:
        i = _ws(d, i + 1)
        out += b"["
        if i < n and d[i] == 0x5D:
            out += b"]"
            return i + 1
        first = True
        while True:
            if not first:
                out += b","
            first = False
            i = _ws(d, _emit(d, i, depth + 1, out))
            if i < n and d[i] == 0x2C:
                i = _ws(d, i + 1)
                continue
            if i < n and d[i] == 0x5D:
                out += b"]"
                return i + 1
            raise CanonicalError("expected ',' or ']'")
    if c == 0x22:
        end = _scan_string(d, i)
        out += d[i:end]  # verbatim, escapes and all
        return end
    for lit in (b"true", b"false", b"null"):
        if d[i : i + len(lit)] == lit:
            out += lit
            return i + len(lit)
    end = _scan_number(d, i)
    out += d[i:end]  # verbatim: the digits that were transmitted
    return end


def _u32(d: bytes, s: int, e: int) -> int:
    tok = d[s:e]
    if not tok or any(c not in _DIGITS for c in tok):
        raise CanonicalError("seq/t_ms must be a non-negative integer")
    v = int(tok)
    if v > 0xFFFFFFFF:
        raise CanonicalError("seq/t_ms does not fit in 32 bits")
    return v


def canonical(data: bytes) -> tuple[bytes, Envelope]:
    """Datagram bytes → ``(canonical bytes, envelope)``. Raises :class:`CanonicalError`."""
    members, end = _scan_object(data, _ws(data, 0), 1)
    if _ws(data, end) != len(data):
        raise CanonicalError("trailing bytes after the object")

    found: dict[bytes, tuple[int, int, int, int]] = {}
    for m in members:
        k = data[m[0] : m[1]]
        if k in (b'"v"', b'"type"', b'"from"', b'"seq"', b'"t_ms"', b'"auth"', b'"sig"'):
            found[k] = m
    missing = [k for k in (b'"v"', b'"type"', b'"from"', b'"seq"', b'"t_ms"') if k not in found]
    if missing:
        raise CanonicalError(f"missing envelope field {missing[0].decode()}")
    mv, mt, mf, ms, mu = (found[k] for k in (b'"v"', b'"type"', b'"from"', b'"seq"', b'"t_ms"'))
    ma = found.get(b'"auth"')
    msig = found.get(b'"sig"')
    if data[mt[2]] != 0x22 or data[mf[2]] != 0x22:
        raise CanonicalError("type/from must be strings")

    out = bytearray()
    _emit(data, mv[2], 2, out)  # v
    out += b"|"
    out += data[mt[2] : mt[3]]  # "type", quoted
    out += b"|"
    out += data[mf[2] : mf[3]]  # "from", quoted
    out += b"|"
    out += data[ms[2] : ms[3]]  # seq
    out += b"|"
    out += data[mu[2] : mu[3]]  # t_ms
    out += b"|{"
    skip = {mv, mt, mf, ms, mu} | ({ma} if ma is not None else set()) | ({msig} if msig is not None else set())
    first = True
    for span in members:
        ks, ke, vs, _ve = span
        if span in skip:
            continue  # the five envelope fields are already out; the tag is never its own input
        if not first:
            out += b","
        first = False
        out += data[ks:ke]
        out += b":"
        _emit(data, vs, 2, out)
    out += b"}"

    env = Envelope(
        type_lit=data[mt[2] : mt[3]],
        from_lit=data[mf[2] : mf[3]],
        auth_lit=data[ma[2] : ma[3]] if ma else None,
        sig_lit=data[msig[2] : msig[3]] if msig else None,
        seq=_u32(data, ms[2], ms[3]),
        t_ms=_u32(data, mu[2], mu[3]),
        members=len(members),
    )
    return bytes(out), env


# --- the tag --------------------------------------------------------------------------


def check_key(key: bytes) -> bytes:
    """A key reaches the firmware as a C string, so a NUL would silently truncate it there."""
    if b"\0" in key:
        raise ValueError("the auth key may not contain a NUL byte")
    return key


def _decode_key(hexstr: str, what: str) -> bytes:
    """A 32-byte Ed25519 key from its hex config string. ``""`` -> ``b""`` (the layer is off)."""
    if not hexstr:
        return b""
    try:
        raw = bytes.fromhex(hexstr)
    except ValueError:
        raise ValueError(f"{what} must be {ed25519.KEY_BYTES * 2} hex characters") from None
    if len(raw) != ed25519.KEY_BYTES:
        raise ValueError(f"{what} must be {ed25519.KEY_BYTES} bytes ({ed25519.KEY_BYTES * 2} hex chars)")
    return raw


def tag(key: bytes, canon: bytes) -> str:
    """The 32 lowercase hex characters that go in the ``auth`` field."""
    return hmac.new(check_key(key), canon, hashlib.sha256).hexdigest()[:TAG_HEX]


def signature(seed: bytes, canon: bytes) -> bytes:
    """The base64 Ed25519 signature that goes in the ``sig`` field, over the canonical bytes."""
    return base64.b64encode(ed25519.sign(seed, canon))


def _apply(data: bytes, key: bytes, sign_seed: bytes) -> bytes:
    """Splice the authenticity fields into a serialised datagram, mirroring ``orbit_auth_sign``.

    For a control message in :data:`SIGNED_TYPES` with a ``sign_seed`` present, an Ed25519
    ``sig`` is added; when ``key`` is set, the HMAC ``auth`` tag is added. Both are computed over
    the SAME canonical bytes, from which both fields are excluded, so their order does not matter
    and either can be present without the other.

    Raises :class:`CanonicalError` if the bytes cannot be canonicalised — the caller must then
    DROP the datagram rather than send it unauthenticated.
    """
    if not key and not sign_seed:
        return data
    if len(data) < 2 or data[-1:] != b"}":
        raise CanonicalError("a datagram to sign must be a JSON object")
    canon, env = canonical(data)
    # An auth field is a top-level member like any other: it must fit under the member limit and
    # may not already be present -- re-signing would put a duplicate key in the object, which is
    # exactly what neither tier will canonicalise, so it would sign cleanly and verify nowhere.
    if env.auth_lit is not None or env.sig_lit is not None:
        raise CanonicalError("already signed")
    additions: list[tuple[bytes, bytes]] = []
    if sign_seed and env.type_lit in SIGNED_TYPES:
        additions.append((b"sig", signature(sign_seed, canon)))
    if key:
        additions.append((b"auth", tag(key, canon).encode("ascii")))
    if env.members + len(additions) > MAX_MEMBERS:
        raise CanonicalError("no room for the auth field(s): at the top-level member limit")
    out = data[:-1]
    for name, value in additions:
        out += b',"' + name + b'":"' + value + b'"'
    return out + b"}"


def sign(data: bytes, key: bytes) -> bytes:
    """HMAC-only signing (splice ``,"auth":"<tag>"``). See :func:`_apply` for the hybrid."""
    return _apply(data, key, b"")


def name_literal(name: str) -> bytes:
    """A node name as the ``from`` literal it must appear as: plain, unescaped ASCII.

    Compared as a literal rather than as a decoded string on purpose. Node names are ASCII by
    construction (the firmware builds ``"esp32-satellite-" + SAT_ID``, the ground uses its short
    hostname), and an escaped spelling of one is exactly the confusion this refuses to resolve.
    """
    return b'"' + name.encode("utf-8") + b'"'


# --- policy ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """What a node requires of a datagram before it is allowed to mean anything.

    Every field is off by default and an all-default policy is a pass-through, which is what
    keeps the simulator's digest and every pre-existing test byte-identical: auth is something
    a deployment turns on, never something a unit test has to know about.
    """

    key: bytes = b""  # b"" disables the MAC
    ground_name: str = ""  # "" disables the control-message pin
    sat_names: tuple[str, ...] = ()  # () disables the satellite allowlist
    monotonic_seq: bool = True  # anti-replay, only ever consulted when the MAC is on
    sign_seed: bytes = b""  # the ground's Ed25519 seed; only the ground holds it, and only it signs
    ground_pubkey: bytes = b""  # the ground's Ed25519 public key; verifiers require a valid `sig` with it

    @classmethod
    def from_settings(cls, settings: Settings) -> Policy:
        """Built once at start-up from ``Settings``; the roster is ``Settings.sat_names()``.

        ``ground_sign_key`` is present only on the ground (its env sets it), so only the ground
        ends up signing; ``ground_pubkey`` is distributed to every node, so every node verifies.
        Both are independent of ``pin_senders`` -- the asymmetric layer is its own switch.
        """
        key = check_key(settings.auth_key.encode("utf-8"))
        pin = settings.pin_senders
        return cls(
            key=key,
            ground_name=settings.ground_name if pin else "",
            sat_names=tuple(settings.sat_names()) if pin else (),
            monotonic_seq=settings.auth_anti_replay,
            sign_seed=_decode_key(settings.ground_sign_key, "ground_sign_key"),
            ground_pubkey=_decode_key(settings.ground_pubkey, "ground_pubkey"),
        )

    @property
    def signing(self) -> bool:
        return bool(self.key)

    @property
    def ground_signing(self) -> bool:
        """This node signs control messages with the ground's private key (i.e. it is the ground)."""
        return bool(self.sign_seed)

    @property
    def enabled(self) -> bool:
        return bool(self.key) or bool(self.ground_name) or bool(self.sat_names) or bool(self.ground_pubkey)

    @property
    def replay_checked(self) -> bool:
        """Anti-replay needs the MAC: without it an attacker could push a sender's watermark
        forward with one forged datagram and mute the real node for the rest of the window."""
        return self.monotonic_seq and bool(self.key)

    def sign(self, data: bytes) -> bytes:
        return _apply(data, self.key, self.sign_seed)

    def check(self, data: bytes) -> str:
        """``""`` if the datagram may be acted on, else a short reason for the drop counter.

        Sender pinning first, because it is the cheaper rejection and does not depend on a key
        being configured at all; then the tag.

        An all-default policy returns "" without even parsing. That is not an optimisation: it
        is the guarantee that a node with auth switched off behaves EXACTLY as it did before
        this module existed, right down to a malformed datagram reaching decode() and being
        counted as malformed rather than as unauthenticated.
        """
        if not self.enabled:
            return ""
        try:
            canon, env = canonical(data)
        except CanonicalError as e:
            return f"canonical: {e}"

        control = env.type_lit in CONTROL_TYPES
        if self.ground_name and control and env.from_lit != name_literal(self.ground_name):
            return "unpinned_ground"
        if self.sat_names and not control and env.from_lit not in {name_literal(n) for n in self.sat_names}:
            return "unpinned_satellite"

        if self.key:
            if env.auth_lit is None or len(env.auth_lit) != TAG_HEX + 2:
                return "no_tag"
            got = env.auth_lit[1:-1].decode("ascii", "replace")
            if not hmac.compare_digest(got, tag(self.key, canon)):
                return "bad_tag"

        # The asymmetric layer: a state-changing ground command must additionally carry a valid
        # Ed25519 signature from the ground. This survives a leaked shared HMAC key -- a holder of
        # it can still forge `offers_open` or a satellite's `bid`, but not a `grant`/`revoke`/`tx_ack`.
        if self.ground_pubkey and env.type_lit in SIGNED_TYPES:
            if env.sig_lit is None:
                return "no_sig"
            try:
                sig = base64.b64decode(env.sig_lit[1:-1], validate=True)
            except ValueError:
                return "bad_sig"
            if len(sig) != SIG_BYTES or not ed25519.verify(self.ground_pubkey, canon, sig):
                return "bad_sig"
        return ""


OFF = Policy()

__all__ = [
    "AUTH_OVERHEAD",
    "CONTROL_TYPES",
    "MAX_DEPTH",
    "MAX_MEMBERS",
    "OFF",
    "SIGNED_TYPES",
    "SIG_BYTES",
    "TAG_BYTES",
    "TAG_HEX",
    "CanonicalError",
    "Envelope",
    "Policy",
    "canonical",
    "check_key",
    "name_literal",
    "sign",
    "signature",
    "tag",
]
