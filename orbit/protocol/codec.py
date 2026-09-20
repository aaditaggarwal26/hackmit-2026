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

import zlib

ENC_RAW = "raw"  # the payload IS the frame; also what an absent `enc` means (an older sender)
ENC_ZLIB = "zlib"  # RFC 1950 zlib stream wrapping RFC 1951 DEFLATE

LEVEL = 9
WBITS = 15  # 32 KiB window, zlib header + Adler-32 trailer
MEMLEVEL = 8  # zlib's own default; stated so it is a decision, not an inherited one
STRATEGY = zlib.Z_DEFAULT_STRATEGY

ENCODINGS = frozenset({ENC_RAW, ENC_ZLIB})


class CodecError(Exception):
    """The payload is not the frame it claims to be. Never swallowed: the transfer fails."""


def deflate(raw: bytes) -> bytes:
    """The pinned encoder. Identical to ``zlib.compress(raw, 9)``; spelled out so it stays that way."""
    c = zlib.compressobj(LEVEL, zlib.DEFLATED, WBITS, MEMLEVEL, STRATEGY)
    return c.compress(raw) + c.flush()


def compress(raw: bytes) -> tuple[str, bytes]:
    """``(enc, payload)`` for a frame.

    Falls back to ``("raw", raw)`` when DEFLATE does not actually shrink the frame. An
    incompressible frame is rare on this corpus but not impossible, and sending 16 KB plus a
    zlib header to "save" airtime would be a loss; the fallback also means the firmware has
    somewhere to go when it cannot allocate the encoder's workspace.
    """
    payload = deflate(raw)
    return (ENC_ZLIB, payload) if len(payload) < len(raw) else (ENC_RAW, raw)


def decompress(enc: str | None, payload: bytes, raw_bytes: int) -> bytes:
    """Encoded payload -> the exact raw frame, or ``CodecError``.

    ``enc`` is ``None`` for a sender that predates compression: absent means raw, never
    "guess". ``raw_bytes`` is how big the frame is; a stream that inflates to any other length
    is a failure, not something to trim or pad, and it is also the bound the inflater runs
    under. Callers must pass their OWN frame size here, not a number a datagram supplied:
    ``raw_bytes`` is a limit as much as it is an expectation.
    """
    if raw_bytes < 0:
        raise CodecError(f"negative frame size {raw_bytes}")
    enc = ENC_RAW if enc is None else enc
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
    "ENCODINGS",
    "ENC_RAW",
    "ENC_ZLIB",
    "LEVEL",
    "MEMLEVEL",
    "STRATEGY",
    "WBITS",
    "CodecError",
    "chunk_count",
    "compress",
    "decompress",
    "deflate",
    "split",
]
