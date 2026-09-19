"""Wire contract: every type round-trips, the committed vectors match the code, hostile bytes never raise,
and a full-size frame chunk fits in one datagram."""

import json
from pathlib import Path

import pytest

from orbit import config
from orbit.protocol import messages as M

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name,msg", M.examples(), ids=[n for n, _ in M.examples()])
def test_roundtrip(name, msg):
    wire = msg.encode()
    doc = json.loads(wire)
    assert doc["v"] == config.PROTOCOL_VERSION and doc["type"] == str(msg.TYPE) and doc["from"] == msg.sender
    back = M.decode(wire)
    assert back == msg
    assert type(msg) is type(back)


def test_vectors_committed():
    assert json.loads((ROOT / "docs" / "protocol_vectors.json").read_text()) == M.vectors(), \
        "run: uv run python -c 'from orbit.protocol.messages import vectors; import json; json.dump(vectors(), open(\"docs/protocol_vectors.json\",\"w\"), indent=1)'"


def test_every_type_has_exactly_one_class():
    assert set(M.MESSAGE_TYPES) == set(M.MessageType)
    assert set(M.MessageType) == M.GROUND_TYPES | M.SATELLITE_TYPES
    assert not (M.GROUND_TYPES & M.SATELLITE_TYPES)


@pytest.mark.parametrize("raw", [
    b"", b"{", b"[]", b"null", b'"bid"', b"\xff\xfe\x00", b'{"v":1}', b'{"v":2,"type":"bid"}',
    b'{"v":1,"type":"nope","from":"x","seq":1,"t_ms":0}',
    b'{"v":1,"type":"bid","from":"x","seq":1,"t_ms":0}',
    b'{"v":1,"type":"bid","from":7,"seq":1,"t_ms":0}',
    b'{"v":1,"type":"tx_ack","from":"g","seq":1,"t_ms":0,"round_id":1,"to":"s","item_id":1,"ok":"yes","bytes_received":1,"reason":""}',
    b'{"v":1,"type":"tx_chunk","from":"s","seq":1,"t_ms":0,"round_id":1,"item_id":1,"idx":0,"n":1,"data":"!!!"}',
    b'{"v":1,"type":"tx_chunk","from":"s","seq":1,"t_ms":0,"round_id":1,"item_id":1,"idx":0,"n":1,"data":5}',
    b'{"v":1,"type":"offers_open","from":"g","seq":1.5,"t_ms":0,"round_id":1,"window_remaining_bytes":1,"collect_ms":1}',
    b'{"v":1,"type":"offers_open","from":"g","seq":true,"t_ms":0,"round_id":1,"window_remaining_bytes":1,"collect_ms":1}',
    b'{"v":1,"type":"state","from":"g","seq":1,"t_ms":0,"state":"READY","round_id":1,"granted_to":"","window":[]}',
    b'{"v":1,"type":"bid","from":"s","seq":1,"t_ms":0,"round_id":1,"item_id":1,"score":1,"item_age_s":0,"window":{},"buffer":{},"eviction_count":0,"queue_len":0}',
    b'{"v":1,"type":"heartbeat","from":"s","seq":1,"t_ms":0,"buffer":{"slots":1},"eviction_count":0,"queue_len":0,"top_score":0,"top_item_id":0,"uptime_s":0}',
])
def test_hostile_input_is_rejected_not_raised(raw):
    r = M.decode(raw)
    assert isinstance(r, M.DecodeError) and r.reason


def test_oversize_is_rejected():
    big = M.examples()[0][1].encode() + b" " * 70000
    assert isinstance(M.decode(big), M.DecodeError)


def test_unknown_extra_fields_are_tolerated():
    """A newer firmware may add fields; an older ground must not reject them."""
    doc = json.loads(M.examples()[0][1].encode())
    doc["future_field"] = {"anything": 1}
    assert isinstance(M.decode(json.dumps(doc).encode()), M.OffersOpen)


def test_int_accepted_where_float_expected():
    doc = json.loads(dict(M.examples())["tx_done"].encode())
    doc["score"] = 91
    m = M.decode(json.dumps(doc).encode())
    assert isinstance(m, M.TxDone) and m.score == 91.0 and isinstance(m.score, float)


def test_full_chunk_fits_one_datagram():
    s = config.DEFAULTS
    chunk = M.TxChunk("sat-abcdefgh", 2**31 - 1, 2**31 - 1, round_id=2**31 - 1, item_id=2**31 - 1, idx=99, n=99,
                      data=bytes(range(256)) * (s.chunk_bytes // 256 + 1))
    chunk = M.TxChunk("sat-abcdefgh", 2**31 - 1, 2**31 - 1, round_id=2**31 - 1, item_id=2**31 - 1, idx=99, n=99,
                      data=chunk.data[: s.chunk_bytes])
    assert len(chunk.encode()) <= s.bus_max_datagram, (len(chunk.encode()), s.bus_max_datagram)


def test_bid_with_max_window_fits_one_datagram():
    s = config.DEFAULTS
    win = tuple(M.QueueEntry(2**31 - 1, 100.0, 99999.999) for _ in range(s.bid_window_n))
    buf = M.BufferStats(slots=999, capacity_bytes=2**31 - 1, used=999, free=0, occupancy_pct=100.0)
    bid = M.Bid("sat-abcdefgh", 2**31 - 1, 2**31 - 1, round_id=2**31 - 1, item_id=2**31 - 1, score=100.0,
                item_age_s=99999.999, window=win, buffer=buf, eviction_count=2**31 - 1, queue_len=999)
    assert len(bid.encode()) <= s.bus_max_datagram


def test_spec_markdown_lists_every_type():
    md = M.spec_markdown()
    for t in M.MessageType:
        assert f"`{t}`" in md


@pytest.mark.parametrize("raw", [
    b'{"v":1,"type":"tx_done","from":"s","seq":1,"t_ms":0,"round_id":1,"item_id":1,"total_bytes":1,"score":NaN,"cloud_frac":0,"sha256":""}',
    b'{"v":1,"type":"tx_done","from":"s","seq":1,"t_ms":0,"round_id":1,"item_id":1,"total_bytes":1,"score":Infinity,"cloud_frac":0,"sha256":""}',
    b'{"v":1,"type":"tx_done","from":"s","seq":1,"t_ms":0,"round_id":1,"item_id":1,"total_bytes":1,"score":1e999,"cloud_frac":0,"sha256":""}',
    b'{"v":1,"type":"tx_done","from":"s","seq":1,"t_ms":0,"round_id":1,"item_id":1,"total_bytes":1,"score":-1e999,"cloud_frac":0,"sha256":""}',
])
def test_non_finite_numbers_are_hostile(raw):
    """A bid with score=1e999 would win every round and then break every JSON encoder downstream."""
    assert isinstance(M.decode(raw), M.DecodeError)
