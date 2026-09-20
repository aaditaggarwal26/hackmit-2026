"""Lossless compression of the ``tx_chunk`` payload, and the chunk arithmetic over it.

Only the frame bytes are compressed. Every control message stays plain readable JSON, so a
human watching the bus log during a demo still sees the whole protocol; the one bulk payload
that costs airtime is the one that gets squeezed.

DEFLATE (RFC 1951) inside a zlib wrapper (RFC 1950), and nothing else. The ground re-scores
what it receives with the golden model and compares its sha256 against the digest the
satellite computed over the frame it transmitted: a codec that returns *almost* the frame
breaks both at once, silently. So the rule is not "prefer lossless", it is that a lossy codec
cannot be used here at all — JPEG would make every digest a mismatch and every re-score a
different number, and the determinism the scoring parity rests on would be gone.

The pipeline::

    raw frame (16384 B) -> compress() -> payload -> split() -> base64 -> tx_chunk*
    tx_chunk* -> reassemble -> decompress() -> raw frame -> sha256 -> re-score

``tx_begin`` carries the RAW length as ``total_bytes`` (it never stops meaning "the frame is
this big"), plus ``enc`` and ``enc_bytes`` for the encoded blob the chunks actually carry.
``chunks`` counts chunks of the ENCODED blob. The digest in ``tx_done`` is over the raw
decompressed frame, before and after this change.

WHAT IS PINNED, AND WHAT IS NOT
-------------------------------
Pinned: level 9, window bits 15, memLevel 8, ``Z_DEFAULT_STRATEGY`` — stated explicitly here
and in ``deflateInit2`` in firmware/satellite_esp32/orbit_codec.h, so neither side can drift
onto a library default that changes under it.

NOT pinned, and deliberately: that the two sides emit the *same bytes*. They do not have to.
The ESP32 compresses with miniz and the ground with zlib, and two conforming DEFLATE encoders
are free to make different (equally valid) choices. That is fine because the equality contract
is the sha256 over the RAW frame, never over the compressed blob — the compressed form is a
transport detail, and any conforming inflater recovers the exact 16384 bytes from either
encoder's output. Do not "fix" this by trying to make the encoders bit-identical; the thing
that must be bit-identical is already checked, one layer up.

Failure is loud. ``decompress`` raises ``CodecError`` on an unknown encoding, a truncated or
corrupt stream, trailing bytes after the stream, or a length that is not the frame size the
sender declared. A frame that decompresses to the wrong bytes must never reach the scorer.
"""

from __future__ import annotations

import os
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENC_RAW = "raw"  # the payload IS the frame; also what an absent `enc` means (an older sender)
ENC_ZLIB = "zlib"  # RFC 1950 zlib stream wrapping RFC 1951 DEFLATE

LEVEL = 9
WBITS = 15  # 32 KiB window, zlib header + Adler-32 trailer
MEMLEVEL = 8  # zlib's own default; stated so it is a decision, not an inherited one
STRATEGY = zlib.Z_DEFAULT_STRATEGY

# --- confidentiality (AES-GCM), an optional layer ON TOP of compression ----------------
#
# The MODIS corpus is public placeholder data, but the PRODUCT carries imagery that is not:
# commercial, licensed, or capability-revealing. So the payload can be sealed with AES-256-GCM
# (an AEAD: one pass gives confidentiality AND integrity, so its 16-byte tag replaces the need
# for a separate MAC over the frame). This is off unless a key is configured, so the demo, the
# readable bus log and the deterministic sim are unchanged by default.
#
# Layer order is compress-then-encrypt: ciphertext does not compress, so squeezing first is the
# only order that saves airtime. `enc` records the chain, e.g. "zlib+gcm". The sealed blob is
# self-describing on the wire -- nonce || ciphertext || tag -- and rides the existing chunking
# untouched, so no new wire field is needed.
#
# Nonce: 12 random bytes per frame (never per chunk -- a frame's retransmits must be byte-
# identical for the dedup filter). Random rather than a counter because a counter reused across
# a reboot under a STATIC key is catastrophic for GCM; the product's answer is a rotating
# session key (see docs/security.md), and a random nonce is safe in the meantime.
ENC_GCM_SUFFIX = "+gcm"
ENC_RAW_GCM = ENC_RAW + ENC_GCM_SUFFIX
ENC_ZLIB_GCM = ENC_ZLIB + ENC_GCM_SUFFIX
NONCE_BYTES = 12  # standard GCM nonce
GCM_TAG_BYTES = 16
GCM_OVERHEAD = NONCE_BYTES + GCM_TAG_BYTES
AAD = b"orbit-tx-v1"  # bound into every seal; ties the ciphertext to this protocol/version

ENCODINGS = frozenset({ENC_RAW, ENC_ZLIB, ENC_RAW_GCM, ENC_ZLIB_GCM})


class CodecError(Exception):
    """The payload is not the frame it claims to be. Never swallowed: the transfer fails."""


class CryptoError(CodecError):
    """The payload could not be unsealed: wrong key, tampered bytes, or a missing key. A
    subclass of CodecError so a caller that already fails a transfer on a bad codec also fails
    on a bad seal, without having to know which."""


def check_payload_key(key: bytes) -> bytes:
    """Validate an AES-GCM key length (128/192/256-bit). ``b""`` means the layer is off."""
    if key and len(key) not in (16, 24, 32):
        raise ValueError(f"payload key must be 16, 24 or 32 bytes, got {len(key)}")
    return key


def payload_key_bytes(hexstr: str) -> bytes:
    """An AES-GCM key from its hex config string. ``""`` -> ``b""`` (confidentiality off)."""
    if not hexstr:
        return b""
    try:
        raw = bytes.fromhex(hexstr)
    except ValueError:
        raise ValueError("payload key must be hex (32/48/64 chars for AES-128/192/256)") from None
    return check_payload_key(raw)


def seal(payload: bytes, key: bytes) -> bytes:
    """AES-GCM the payload: returns ``nonce || ciphertext || tag``. ``key`` must be non-empty."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, payload, AAD)


def unseal(sealed: bytes, key: bytes) -> bytes:
    """Reverse :func:`seal`. Raises :class:`CryptoError` on a wrong key or a tampered blob."""
    if not key:
        raise CryptoError("payload is sealed but no key is configured")
    if len(sealed) < GCM_OVERHEAD:
        raise CryptoError("sealed payload is too short to contain a nonce and tag")
    nonce, body = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, body, AAD)
    except InvalidTag:
        raise CryptoError("AES-GCM tag failed: wrong key or tampered payload") from None


def deflate(raw: bytes) -> bytes:
    """The pinned encoder. Identical to ``zlib.compress(raw, 9)``; spelled out so it stays that way."""
    c = zlib.compressobj(LEVEL, zlib.DEFLATED, WBITS, MEMLEVEL, STRATEGY)
    return c.compress(raw) + c.flush()


def compress(raw: bytes, key: bytes = b"") -> tuple[str, bytes]:
    """``(enc, payload)`` for a frame, compressed and — when ``key`` is set — then sealed.

    Falls back to ``("raw", raw)`` when DEFLATE does not actually shrink the frame. An
    incompressible frame is rare on this corpus but not impossible, and sending 16 KB plus a
    zlib header to "save" airtime would be a loss; the fallback also means the firmware has
    somewhere to go when it cannot allocate the encoder's workspace.

    With a ``key``, the (possibly-compressed) payload is AES-GCM-sealed and ``+gcm`` is appended
    to ``enc``. Without one, behaviour is byte-for-byte what it was before encryption existed.
    """
    payload = deflate(raw)
    enc, payload = (ENC_ZLIB, payload) if len(payload) < len(raw) else (ENC_RAW, raw)
    if key:
        enc, payload = enc + ENC_GCM_SUFFIX, seal(payload, key)
    return enc, payload


def decompress(enc: str | None, payload: bytes, raw_bytes: int, key: bytes = b"") -> bytes:
    """Encoded payload -> the exact raw frame, or ``CodecError``.

    ``enc`` is ``None`` for a sender that predates compression: absent means raw, never
    "guess". ``raw_bytes`` is how big the frame is; a stream that inflates to any other length
    is a failure, not something to trim or pad, and it is also the bound the inflater runs
    under. Callers must pass their OWN frame size here, not a number a datagram supplied:
    ``raw_bytes`` is a limit as much as it is an expectation.

    A ``+gcm`` encoding is unsealed with ``key`` first (decrypt-then-decompress, the reverse of
    :func:`compress`); the decompression-bomb bound still applies to the plaintext underneath.
    """
    if raw_bytes < 0:
        raise CodecError(f"negative frame size {raw_bytes}")
    enc = ENC_RAW if enc is None else enc
    if enc.endswith(ENC_GCM_SUFFIX):
        payload = unseal(payload, key)  # decrypt first; a bad key/tag raises CryptoError here
        enc = enc[: -len(ENC_GCM_SUFFIX)]
    if enc == ENC_RAW:
        out = payload
    elif enc == ENC_ZLIB:
        # Bounded at raw_bytes + 1, and never flush()ed. A few hundred bytes of DEFLATE can
        # inflate to megabytes, and the length below is the only thing standing between a
        # hostile datagram and that expansion happening in the ground's address space — so the
        # cap has to be enforced by the inflater, not checked after the fact. One byte of
        # headroom is what distinguishes "exactly the frame" from "longer than the frame".
        d = zlib.decompressobj(WBITS)
        try:
            out = d.decompress(payload, raw_bytes + 1)
        except zlib.error as e:
            raise CodecError(f"zlib stream is corrupt: {e}") from None
        if d.unconsumed_tail:
            raise CodecError(f"zlib stream inflates to more than {raw_bytes} bytes")
        if not d.eof:
            raise CodecError("zlib stream is truncated")
        if d.unused_data:
            raise CodecError(f"{len(d.unused_data)} bytes after the end of the zlib stream")
    else:
        raise CodecError(f"unknown encoding {enc!r}")
    if len(out) != raw_bytes:
        raise CodecError(f"decoded {len(out)} bytes, sender declared {raw_bytes}")
    return out


def chunk_count(payload_bytes: int, chunk_bytes: int) -> int:
    """How many ``tx_chunk`` datagrams a payload of this size takes. Zero bytes is still one chunk.

    Mirrored by ``orbit_chunk_count`` in firmware/satellite_esp32/orbit_codec.h: the ground
    rejects any chunk index outside ``0 <= idx < chunks``, so the two must agree exactly.
    """
    if chunk_bytes <= 0:
        raise ValueError(f"chunk_bytes must be positive, got {chunk_bytes}")
    return max(1, (payload_bytes + chunk_bytes - 1) // chunk_bytes)


def split(payload: bytes, chunk_bytes: int) -> list[bytes]:
    """The payload as the chunks that go on the wire, in index order."""
    if chunk_bytes <= 0:
        raise ValueError(f"chunk_bytes must be positive, got {chunk_bytes}")
    return [payload[i : i + chunk_bytes] for i in range(0, len(payload), chunk_bytes)] or [b""]


__all__ = [
    "AAD",
    "ENCODINGS",
    "ENC_GCM_SUFFIX",
    "ENC_RAW",
    "ENC_RAW_GCM",
    "ENC_ZLIB",
    "ENC_ZLIB_GCM",
    "GCM_OVERHEAD",
    "GCM_TAG_BYTES",
    "LEVEL",
    "MEMLEVEL",
    "NONCE_BYTES",
    "STRATEGY",
    "WBITS",
    "CodecError",
    "CryptoError",
    "check_payload_key",
    "chunk_count",
    "compress",
    "decompress",
    "deflate",
    "payload_key_bytes",
    "seal",
    "split",
    "unseal",
]
