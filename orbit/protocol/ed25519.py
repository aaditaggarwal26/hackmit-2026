"""Ed25519 (RFC 8032), pure Python, no dependency.

This is the ground's half of the *targeted* asymmetric layer: the ground **signs** its
control messages (``grant``/``revoke``/``tx_ack``) with a private key that never leaves the
GX10, and every other node **verifies** them with the ground's public key. HMAC
(``orbit/protocol/auth.py``) already stops an outsider on the WiFi from speaking; this stops
a holder of the *shared* HMAC key — a satellite whose key leaked — from forging a grant.
Only the ground can produce a signature that verifies against its public key.

Why pure Python rather than a library: the same reasons the rest of ``orbit.protocol`` is
hand-written. There is no ``cryptography``/``libsodium`` on the ESP32 either, so the firmware
carries its own Ed25519 verify (``firmware/satellite_esp32/orbit_crypto.h``); keeping the
ground on a second, independently-written implementation of the *same* standard, both held to
the RFC 8032 known-answer vectors (``tests/test_ed25519.py``) and to each other by
``tests/test_crypto_parity.py``, is exactly the discipline used for the scoring kernel and the
HMAC canonicaliser. Signing happens on a handful of low-rate control datagrams on the GX10, so
the speed of a big-integer implementation is irrelevant.

This is a transcription of the RFC 8032 reference code (Appendix / Section 6). It is NOT
constant-time, which is acceptable here: the secret is the *ground's* signing key on a laptop,
not something an attacker on the bus can time, and the verify path handles only public values.
"""

from __future__ import annotations

import hashlib

# --- field and group constants (RFC 8032 §5.1) ----------------------------------------
_p = 2**255 - 19
_q = 2**252 + 27742317777372353535851937790883648493  # group order
KEY_BYTES = 32
SIG_BYTES = 64


def _sha512(s: bytes) -> bytes:
    return hashlib.sha512(s).digest()


def _sha512_modq(s: bytes) -> int:
    return int.from_bytes(_sha512(s), "little") % _q


def _modp_inv(x: int) -> int:
    return pow(x, _p - 2, _p)


_d = -121665 * _modp_inv(121666) % _p
_modp_sqrt_m1 = pow(2, (_p - 1) // 4, _p)

# Points are (X, Y, Z, T) in extended coordinates: x = X/Z, y = Y/Z, x*y = T/Z.
_Point = tuple[int, int, int, int]


def _point_add(P: _Point, Q: _Point) -> _Point:
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _point_mul(s: int, P: _Point) -> _Point:
    Q: _Point = (0, 1, 1, 0)  # neutral element
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _point_equal(P: _Point, Q: _Point) -> bool:
    if (P[0] * Q[2] - Q[0] * P[2]) % _p != 0:
        return False
    return (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _p:
        return None
    x2 = (y * y - 1) * _modp_inv(_d * y * y + 1) % _p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _modp_sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_g_y = 4 * _modp_inv(5) % _p
_g_x = _recover_x(_g_y, 0)
assert _g_x is not None
_G: _Point = (_g_x, _g_y, 1, _g_x * _g_y % _p)


def _point_compress(P: _Point) -> bytes:
    zinv = _modp_inv(P[2])
    x = P[0] * zinv % _p
    y = P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _point_decompress(s: bytes) -> _Point | None:
    if len(s) != 32:
        raise ValueError("invalid input length for decompression")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _secret_expand(secret: bytes) -> tuple[int, bytes]:
    if len(secret) != KEY_BYTES:
        raise ValueError("bad length for an Ed25519 secret key")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def secret_to_public(secret: bytes) -> bytes:
    """The 32-byte public key for a 32-byte secret seed."""
    a, _ = _secret_expand(secret)
    return _point_compress(_point_mul(a, _G))


def sign(secret: bytes, msg: bytes) -> bytes:
    """Deterministic 64-byte Ed25519 signature (RFC 8032). ``secret`` is the 32-byte seed."""
    a, prefix = _secret_expand(secret)
    A = _point_compress(_point_mul(a, _G))
    r = _sha512_modq(prefix + msg)
    Rs = _point_compress(_point_mul(r, _G))
    h = _sha512_modq(Rs + A + msg)
    s = (r + h * a) % _q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public: bytes, msg: bytes, signature: bytes) -> bool:
    """True iff ``signature`` is a valid Ed25519 signature of ``msg`` under ``public``.

    Never raises on a bad signature or key: a malformed input is a failed verification, not an
    exception, so the bus can treat it as a drop like any other."""
    if len(public) != KEY_BYTES or len(signature) != SIG_BYTES:
        return False
    A = _point_decompress(public)
    if A is None:
        return False
    Rs = signature[:32]
    R = _point_decompress(Rs)
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _q:
        return False
    h = _sha512_modq(Rs + public + msg)
    sB = _point_mul(s, _G)
    hA = _point_mul(h, A)
    return _point_equal(sB, _point_add(R, hA))


__all__ = ["KEY_BYTES", "SIG_BYTES", "secret_to_public", "sign", "verify"]
