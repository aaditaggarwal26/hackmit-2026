"""Ed25519 held to the RFC 8032 known-answer vectors.

A roundtrip test only proves the module agrees with itself; a self-consistent but wrong curve
would pass it. The RFC 8032 §7.1 vectors are what prove this is *real* Ed25519 — the same bytes
any other implementation (the ESP32's, PyNaCl's) produces and accepts — so the ground and the
firmware can interoperate.
"""

from __future__ import annotations

import pytest

from orbit.protocol import ed25519

# RFC 8032 §7.1, TEST 1 / TEST 2 / TEST 3: (secret, public, message, signature), all hex.
RFC8032 = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize("sk_hex,pk_hex,msg_hex,sig_hex", RFC8032)
def test_rfc8032_vectors(sk_hex: str, pk_hex: str, msg_hex: str, sig_hex: str) -> None:
    sk, pk, msg, sig = (bytes.fromhex(h) for h in (sk_hex, pk_hex, msg_hex, sig_hex))
    assert ed25519.secret_to_public(sk) == pk  # public key derivation
    assert ed25519.sign(sk, msg) == sig  # deterministic signature matches the vector
    assert ed25519.verify(pk, msg, sig) is True  # and verifies


def test_roundtrip_and_determinism() -> None:
    sk = bytes(range(32))
    pk = ed25519.secret_to_public(sk)
    msg = b"grant round 7 to sat-a"
    sig = ed25519.sign(sk, msg)
    assert ed25519.sign(sk, msg) == sig  # deterministic
    assert ed25519.verify(pk, msg, sig)


def test_tampered_message_or_signature_fails() -> None:
    sk = bytes(range(1, 33))
    pk = ed25519.secret_to_public(sk)
    msg = b"tx_ack ok item 14"
    sig = ed25519.sign(sk, msg)
    assert not ed25519.verify(pk, msg + b"!", sig)  # changed message
    assert not ed25519.verify(pk, msg, sig[:-1] + bytes([sig[-1] ^ 1]))  # flipped bit in sig
    other = ed25519.secret_to_public(bytes(range(2, 34)))
    assert not ed25519.verify(other, msg, sig)  # wrong public key


def test_malformed_inputs_return_false_not_raise() -> None:
    sk = bytes(range(32))
    pk = ed25519.secret_to_public(sk)
    sig = ed25519.sign(sk, b"x")
    assert ed25519.verify(pk, b"x", sig[:63]) is False  # short signature
    assert ed25519.verify(pk[:31], b"x", sig) is False  # short public key
    assert ed25519.verify(b"\xff" * 32, b"x", sig) is False  # non-canonical/invalid point
