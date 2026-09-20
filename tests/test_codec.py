"""The compressed downlink, end to end over the real wire and the real corpus.

Compression is only allowed here because it is exactly reversible: the ground re-scores what
it receives with the golden model and compares its sha256 against the digest the satellite
took over the frame it scored. So these tests never check "close enough" — every corpus frame
goes out as compressed base64 chunks inside real ``tx_chunk`` datagrams, comes back through
``decode()``, and has to be the same 16384 bytes with the same digest. The rest of the file is
the other half of that contract: a payload that does *not* decode back to the frame has to
fail the transfer loudly rather than reach the scorer.
"""

import hashlib
import zlib

import pytest

from orbit import config, corpus
from orbit.arbiter.fsm import GroundStation
from orbit.protocol import codec
from orbit.protocol import messages as M

S = config.Settings(
    bid_collect_ms=100,
    grant_timeout_ms=500,
    tx_timeout_ms=1000,
    idle_reopen_ms=200,
    state_period_ms=100000,
    window_duration_s=10.0,
    link_rate_bps=config.FRAME_BYTES * 8 * 3 / 10.0,
)
BUF = M.BufferStats(slots=8, capacity_bytes=8 * config.FRAME_BYTES, used=1, free=7, occupancy_pct=12.5)


@pytest.fixture(scope="module")
def frames():
    """Every frame of the real corpus, as the 16384 raw bytes the downlink carries."""
    c = corpus.load()
    return [bytes(f) for f in c.frames]


# --- the encoder is pinned ---------------------------------------------------------------


def test_the_pinned_settings_are_plain_zlib_level_9():
    """If this drifts, the firmware's deflateInit2 and this module no longer describe one codec."""
    raw = bytes(range(256)) * 64
    assert codec.deflate(raw) == zlib.compress(raw, 9)
    assert (codec.LEVEL, codec.WBITS, codec.MEMLEVEL, codec.STRATEGY) == (9, 15, 8, zlib.Z_DEFAULT_STRATEGY)


def test_compressing_the_same_frame_twice_gives_the_same_bytes(frames):
    """TX_PASSES re-sends the whole chunk sequence; the ground keys chunks by idx and the copies must match."""
    for raw in frames[:8]:
        assert codec.compress(raw) == codec.compress(raw)


def test_an_incompressible_frame_falls_back_to_raw():
    """16 KB plus a zlib header is not a saving. `raw` is a normal outcome, not an error."""
    noise = hashlib.sha256(b"seed").digest()
    while len(noise) < config.FRAME_BYTES:
        noise += hashlib.sha256(noise[-32:]).digest()
    enc, payload = codec.compress(noise[: config.FRAME_BYTES])
    assert enc == codec.ENC_RAW and payload == noise[: config.FRAME_BYTES]


# --- chunk arithmetic --------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,chunk,want",
    [(0, 900, 1), (1, 900, 1), (900, 900, 1), (901, 900, 2), (config.FRAME_BYTES, 900, 19), (11245, 900, 13)],
)
def test_chunk_count(payload, chunk, want):
    """Mirrored by orbit_chunk_count in the firmware; the ground rejects any idx outside 0..chunks-1."""
    assert codec.chunk_count(payload, chunk) == want


def test_split_covers_the_payload_exactly(frames):
    _, payload = codec.compress(frames[0])
    parts = codec.split(payload, 900)
    assert len(parts) == codec.chunk_count(len(payload), 900)
    assert b"".join(parts) == payload
    assert all(len(p) <= 900 for p in parts)


def test_an_empty_payload_is_still_one_chunk():
    assert codec.split(b"", 900) == [b""]


# --- the whole downlink, on the real corpus ----------------------------------------------


def test_every_corpus_frame_survives_the_wire(frames):
    """compress -> chunk -> base64 in real datagrams -> decode -> reassemble -> decompress.

    The assertion is the delivery contract itself: exactly 16384 bytes back, and the sha256 the
    satellite put in tx_done taken over them.
    """
    for item_id, raw in enumerate(frames):
        enc, payload = codec.compress(raw)
        parts = codec.split(payload, S.chunk_bytes)
        got: dict[int, bytes] = {}
        for idx, part in enumerate(parts):
            wire = M.TxChunk(
                "sat-a", idx, 0, round_id=1, item_id=item_id, idx=idx, n=len(parts), data=part, enc=enc
            ).encode()
            assert len(wire) <= S.bus_max_datagram, f"chunk {idx} of frame {item_id} does not fit a datagram"
            back = M.decode(wire)
            assert isinstance(back, M.TxChunk) and back.enc == enc
            got[back.idx] = back.data
        frame = codec.decompress(enc, b"".join(got[i] for i in sorted(got)), config.FRAME_BYTES)
        assert len(frame) == config.FRAME_BYTES
        assert hashlib.sha256(frame).hexdigest() == hashlib.sha256(raw).hexdigest()
        assert frame == raw


def test_compression_is_a_net_win_on_this_corpus(frames):
    """Not a ratio to admire: the point is that the base64'd chunks are smaller than they were.

    Measured over the encoded payload and its base64 expansion, because base64 is what is
    actually transmitted -- a codec that shrank the bytes but not the datagrams would be worth
    nothing here.
    """
    raw_b64 = sum(4 * ((len(f) + 2) // 3) for f in frames)
    enc_b64 = sum(4 * ((len(codec.compress(f)[1]) + 2) // 3) for f in frames)
    assert enc_b64 < raw_b64 * 0.75, f"base64 on the wire: {raw_b64} -> {enc_b64}"


# --- a frame that is not the frame fails loudly ------------------------------------------


def test_an_absent_encoding_means_raw_not_a_guess():
    """An older satellite that never learned to compress is a valid sender, not a malformed one."""
    raw = b"\x01" * 64
    assert codec.decompress(None, raw, 64) == raw
    assert codec.decompress(codec.ENC_RAW, raw, 64) == raw


@pytest.mark.parametrize(
    "enc,payload,raw_bytes,why",
    [
        ("gzip", b"", 16384, "unknown encoding"),
        ("zlib", b"not a zlib stream at all", 16384, "corrupt"),
        ("zlib", zlib.compress(b"x" * 100, 9)[:-4], 100, "truncated"),
        ("zlib", zlib.compress(b"x" * 100, 9) + b"junk", 100, "trailing bytes"),
        ("zlib", zlib.compress(b"x" * 100, 9), 200, "wrong length"),
        ("raw", b"x" * 99, 100, "wrong length, raw"),
        ("zlib", zlib.compress(bytes(config.FRAME_BYTES), 9), -1, "negative size"),
    ],
)
def test_a_payload_that_is_not_the_frame_raises(enc, payload, raw_bytes, why):
    with pytest.raises(codec.CodecError):
        codec.decompress(enc, payload, raw_bytes)
    assert why  # the case is named so a failure says which one


def test_a_zip_bomb_is_never_expanded():
    """A few hundred bytes of DEFLATE inflate to megabytes. The bound is the frame size.

    Enforced by the inflater rather than checked afterwards, because checking afterwards means
    the expansion already happened — which is the whole trick.
    """
    bomb = zlib.compress(bytes(64 * 1024 * 1024), 9)
    assert len(bomb) < 70_000
    with pytest.raises(codec.CodecError, match="inflates to more than"):
        codec.decompress(codec.ENC_ZLIB, bomb, config.FRAME_BYTES)


def _bid(host, score, round_id, item_id=1, seq=1):
    return M.Bid(
        host,
        seq,
        0,
        round_id=round_id,
        item_id=item_id,
        score=score,
        item_age_s=0.0,
        window=(),
        buffer=BUF,
        eviction_count=0,
        queue_len=2,
    )


def _granted(now=0.5):
    g = GroundStation(S, "ground")
    g.start(0.0)
    rid = g.round_id
    g.on_message(_bid("sat-a", 90.0, rid), 0.0)
    g.on_tick(now)
    return g, rid


def _transmit(g, rid, enc, payload, done_bytes, sha, now=0.6, item_id=1):
    parts = codec.split(payload, S.chunk_bytes)
    out = g.on_message(
        M.TxBegin(
            "sat-a",
            10,
            0,
            round_id=rid,
            item_id=item_id,
            total_bytes=config.FRAME_BYTES,
            chunks=len(parts),
            enc=enc,
            enc_bytes=len(payload),
        ),
        now,
    )
    for idx, part in enumerate(parts):
        out += g.on_message(
            M.TxChunk("sat-a", 11 + idx, 0, round_id=rid, item_id=item_id, idx=idx, n=len(parts), data=part, enc=enc),
            now,
        )
    out += g.on_message(
        M.TxDone(
            "sat-a",
            50,
            0,
            round_id=rid,
            item_id=item_id,
            total_bytes=done_bytes,
            score=90.0,
            cloud_frac=0.1,
            sha256=sha,
        ),
        now,
    )
    return out


def _ack(msgs):
    return next(m for m in msgs if isinstance(m, M.TxAck))


def test_the_ground_accepts_a_compressed_frame_and_debits_one_whole_frame(frames):
    """The window budgets frames, not datagrams: compression buys airtime, not extra slots."""
    raw = frames[0]
    g, rid = _granted()
    enc, payload = codec.compress(raw)
    ack = _ack(_transmit(g, rid, enc, payload, config.FRAME_BYTES, hashlib.sha256(raw).hexdigest()))
    assert ack.ok and g.counters.completed == 1 and g.counters.corrupt_tx == 0
    assert ack.bytes_received == len(payload) < config.FRAME_BYTES  # airtime, not frame size
    assert g.window.used_bytes == config.FRAME_BYTES


def test_a_frame_that_decompresses_to_other_bytes_is_rejected(frames):
    """The digest is over the RAW frame. A blob that inflates to something else is a failed transfer."""
    raw, other = frames[0], frames[1]
    g, rid = _granted()
    _, payload = codec.compress(other)
    ack = _ack(_transmit(g, rid, codec.ENC_ZLIB, payload, config.FRAME_BYTES, hashlib.sha256(raw).hexdigest()))
    assert not ack.ok and ack.reason == "digest mismatch" and g.counters.corrupt_tx == 1
    assert g.counters.completed == 0 and g.window.used_bytes == 0


def test_a_blob_that_will_not_decompress_is_rejected(frames):
    """Corruption inside a chunk survives base64 and reaches the inflater: it must not be re-scored."""
    raw = frames[0]
    g, rid = _granted()
    _, payload = codec.compress(raw)
    broken = payload[:200] + bytes(b ^ 0xFF for b in payload[200:260]) + payload[260:]
    ack = _ack(_transmit(g, rid, codec.ENC_ZLIB, broken, config.FRAME_BYTES, hashlib.sha256(raw).hexdigest()))
    assert not ack.ok and g.counters.corrupt_tx == 1 and g.counters.completed == 0


def test_a_compressed_blob_labelled_raw_is_rejected(frames):
    """Mislabelling is the failure mode that would otherwise feed 11 KB of DEFLATE to the scorer."""
    raw = frames[0]
    g, rid = _granted()
    _, payload = codec.compress(raw)
    ack = _ack(_transmit(g, rid, codec.ENC_RAW, payload, config.FRAME_BYTES, hashlib.sha256(raw).hexdigest()))
    assert not ack.ok and g.counters.corrupt_tx == 1


def test_a_lost_tx_begin_is_recovered_from_the_chunks(frames):
    """tx_begin is one datagram on a lossy bus, which is why every chunk carries `n` and `enc`."""
    raw = frames[0]
    g, rid = _granted()
    enc, payload = codec.compress(raw)
    parts = codec.split(payload, S.chunk_bytes)
    out = []
    for idx, part in enumerate(parts):  # no tx_begin at all
        out += g.on_message(
            M.TxChunk("sat-a", 11 + idx, 0, round_id=rid, item_id=1, idx=idx, n=len(parts), data=part, enc=enc), 0.6
        )
    out += g.on_message(
        M.TxDone(
            "sat-a",
            50,
            0,
            round_id=rid,
            item_id=1,
            total_bytes=config.FRAME_BYTES,
            score=90.0,
            cloud_frac=0.1,
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
        0.6,
    )
    assert _ack(out).ok and g.counters.completed == 1


def test_a_chunk_with_a_different_encoding_is_not_mixed_in(frames):
    """One transfer, one encoding: mixing would produce bytes no digest could ever match."""
    raw = frames[0]
    g, rid = _granted()
    enc, payload = codec.compress(raw)
    parts = codec.split(payload, S.chunk_bytes)
    g.on_message(
        M.TxBegin(
            "sat-a",
            10,
            0,
            round_id=rid,
            item_id=1,
            total_bytes=config.FRAME_BYTES,
            chunks=len(parts),
            enc=enc,
            enc_bytes=len(payload),
        ),
        0.6,
    )
    before = g.counters.unexpected
    g.on_message(M.TxChunk("sat-a", 11, 0, round_id=rid, item_id=1, idx=0, n=len(parts), data=parts[0], enc=None), 0.6)
    assert g.counters.unexpected == before + 1
    assert 0 not in g.grant.chunks_seen


def test_an_uncompressed_satellite_still_completes(frames):
    """Backward compatibility, measured rather than asserted in a comment: no enc fields at all."""
    raw = frames[0]
    g, rid = _granted()
    parts = codec.split(raw, S.chunk_bytes)
    out = g.on_message(
        M.TxBegin("sat-a", 10, 0, round_id=rid, item_id=1, total_bytes=config.FRAME_BYTES, chunks=len(parts)), 0.6
    )
    for idx, part in enumerate(parts):
        out += g.on_message(
            M.TxChunk("sat-a", 11 + idx, 0, round_id=rid, item_id=1, idx=idx, n=len(parts), data=part), 0.6
        )
    out += g.on_message(
        M.TxDone(
            "sat-a",
            50,
            0,
            round_id=rid,
            item_id=1,
            total_bytes=config.FRAME_BYTES,
            score=90.0,
            cloud_frac=0.1,
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
        0.6,
    )
    assert _ack(out).ok and g.counters.completed == 1


def test_a_tx_done_that_declares_a_frame_size_of_its_own_is_rejected(frames):
    """tx_done is untrusted input. The ground decodes against ITS frame size, never the sender's.

    Without this a satellite could declare a gigabyte and ask the ground to inflate one, which
    is the zip bomb above with the bound handed to the attacker.
    """
    raw = frames[0]
    g, rid = _granted()
    enc, payload = codec.compress(raw)
    ack = _ack(_transmit(g, rid, enc, payload, 2**30, hashlib.sha256(raw).hexdigest()))
    assert not ack.ok and "frames are" in ack.reason and g.counters.corrupt_tx == 1
    assert g.counters.completed == 0 and g.window.used_bytes == 0


def test_absent_and_raw_are_the_same_encoding_to_the_ground(frames):
    """codec.py defines them as synonyms; a transfer that spells it both ways is consistent."""
    raw = frames[0]
    g, rid = _granted()
    parts = codec.split(raw, S.chunk_bytes)
    g.on_message(
        M.TxBegin("sat-a", 10, 0, round_id=rid, item_id=1, total_bytes=config.FRAME_BYTES, chunks=len(parts), enc=None),
        0.6,
    )
    before = g.counters.unexpected
    for idx, part in enumerate(parts):  # tx_begin said nothing; the chunks say "raw"
        g.on_message(
            M.TxChunk(
                "sat-a", 11 + idx, 0, round_id=rid, item_id=1, idx=idx, n=len(parts), data=part, enc=codec.ENC_RAW
            ),
            0.6,
        )
    assert g.counters.unexpected == before
    out = g.on_message(
        M.TxDone(
            "sat-a",
            50,
            0,
            round_id=rid,
            item_id=1,
            total_bytes=config.FRAME_BYTES,
            score=90.0,
            cloud_frac=0.1,
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
        0.6,
    )
    assert _ack(out).ok and g.counters.completed == 1
