"""AES-GCM payload confidentiality: the seal/unseal primitive, the compress+encrypt pipeline,
and that turning it on is transparent to arbitration (the ground still reconstructs every frame).
"""

from __future__ import annotations

import os

import pytest

from orbit import config
from orbit.protocol import codec
from orbit.sim.run import run_scenario

KEY = bytes(range(32))  # AES-256
OTHER = bytes(range(1, 33))


def test_seal_unseal_roundtrip() -> None:
    payload = b"the compressed frame blob" * 10
    sealed = codec.seal(payload, KEY)
    assert sealed != payload
    assert len(sealed) == codec.GCM_OVERHEAD + len(payload)
    assert codec.unseal(sealed, KEY) == payload


def test_seal_hides_the_plaintext() -> None:
    payload = b"SECRET-IMAGERY-MARKER" + os.urandom(200)
    sealed = codec.seal(payload, KEY)
    assert b"SECRET-IMAGERY-MARKER" not in sealed


def test_fresh_nonce_each_seal() -> None:
    payload = b"x" * 100
    assert codec.seal(payload, KEY) != codec.seal(payload, KEY)  # random nonce -> different bytes


def test_unseal_rejects_wrong_key_tamper_and_missing_key() -> None:
    sealed = codec.seal(b"frame", KEY)
    with pytest.raises(codec.CryptoError):
        codec.unseal(sealed, OTHER)  # wrong key
    with pytest.raises(codec.CryptoError):
        codec.unseal(sealed[:-1] + bytes([sealed[-1] ^ 1]), KEY)  # flipped tag byte
    with pytest.raises(codec.CryptoError):
        codec.unseal(sealed, b"")  # no key configured
    with pytest.raises(codec.CryptoError):
        codec.unseal(b"tooshort", KEY)  # shorter than nonce+tag


@pytest.mark.parametrize("raw", [b"\x00" * 16384, os.urandom(16384)])  # compressible and incompressible
def test_compress_encrypt_roundtrip_through_chunks(raw: bytes) -> None:
    enc, payload = codec.compress(raw, KEY)
    assert enc.endswith(codec.ENC_GCM_SUFFIX)
    assert enc in codec.ENCODINGS
    assert raw not in payload  # the frame is not on the wire in the clear
    # reassemble exactly as the ground does, from the chunks that would go out
    chunks = codec.split(payload, 900)
    reassembled = b"".join(chunks)
    assert codec.decompress(enc, reassembled, len(raw), KEY) == raw


def test_decompress_rejects_wrong_key_and_tamper() -> None:
    raw = bytes(range(256)) * 64
    enc, payload = codec.compress(raw, KEY)
    with pytest.raises(codec.CryptoError):
        codec.decompress(enc, payload, len(raw), OTHER)
    with pytest.raises(codec.CryptoError):
        codec.decompress(enc, payload[:-1] + bytes([payload[-1] ^ 1]), len(raw), KEY)
    with pytest.raises(codec.CryptoError):
        codec.decompress(enc, payload, len(raw), b"")  # sealed but no key on the ground


def test_off_by_default_is_unchanged() -> None:
    raw = b"\x11" * 16384
    assert codec.compress(raw) == codec.compress(raw, b"")  # no key == unencrypted
    enc, _ = codec.compress(raw)
    assert codec.ENC_GCM_SUFFIX not in enc


def test_payload_key_bytes_validation() -> None:
    assert codec.payload_key_bytes("") == b""
    assert len(codec.payload_key_bytes("ab" * 32)) == 32  # AES-256
    with pytest.raises(ValueError):
        codec.payload_key_bytes("ab" * 10)  # 20 bytes: not a valid AES key length
    with pytest.raises(ValueError):
        codec.payload_key_bytes("nothex!!")


def test_encryption_is_transparent_to_arbitration() -> None:
    """With a key set, satellites seal and the ground unseals; the arbitration outcome is
    identical because the ground reconstructs the exact same frames it would have in the clear."""
    plain = config.Settings()
    sealed = plain.with_overrides(payload_key="ab" * 32)
    a = run_scenario("nominal", 12, seed=42, settings=plain, write_run=False)
    b = run_scenario("nominal", 12, seed=42, settings=sealed, write_run=False)
    assert a.stream.orbit_frames_down > 0
    assert b.stream.orbit_frames_down == a.stream.orbit_frames_down
    assert b.stream.orbit_usable_down == a.stream.orbit_usable_down
