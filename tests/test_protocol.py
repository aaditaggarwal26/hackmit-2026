"""Protocol conformance suite. The spec's worked examples ARE the vectors:
docs/protocol.md must contain every generated hex string verbatim, and
docs/protocol_vectors.json must equal what messages.py generates now."""
import json
import re
from dataclasses import asdict
from pathlib import Path

import pytest

from orbit import params
from orbit.protocol import cobs
from orbit.protocol.crc16 import crc16
from orbit.protocol.messages import (BEGIN, END, EXAMPLES, MESSAGE_TYPES, FrameDecoder, FrameIngest, Grant, Heartbeat,
                                     ProtocolError, StatusQuery, encode, frame_body, spec_markdown, vectors)

ROOT = Path(__file__).resolve().parent.parent
SPEC = (ROOT / "docs" / "protocol.md").read_text()


def test_crc_check_value():
    assert crc16(b"123456789") == 0x29B1


@pytest.mark.parametrize("data", [b"", b"\x00", b"\x00\x00", b"\x01\x02\x00\x03", bytes(range(256)),
                                  bytes([1] * 300), bytes([0] * 300), bytes([1] * 254), bytes([1] * 255)])
def test_cobs_roundtrip(data):
    enc = cobs.encode(data)
    assert 0 not in enc
    assert cobs.decode(enc) == data


def test_cobs_single_overhead_for_short_frames():
    for n in range(0, params.MAX_FRAME + 1):
        assert len(cobs.encode(bytes([1] * n))) == n + 1
        assert len(cobs.encode(bytes([0] * n))) == n + 1
    assert len(cobs.encode(bytes([1] * 254))) == 256  # why the limit is 253, not 254


@pytest.mark.parametrize("name,msg", EXAMPLES, ids=[n for n, _ in EXAMPLES])
def test_example_roundtrip_and_in_spec(name, msg):
    wire = encode(msg)
    assert wire[-1] == 0 and 0 not in wire[:-1]
    assert len(frame_body(msg)) <= params.MAX_FRAME
    assert len(wire) == len(frame_body(msg)) + 2
    dec = FrameDecoder()
    [got] = dec.feed(wire)
    assert got == msg
    assert wire.hex(" ") in SPEC, f"{name}: wire hex missing from docs/protocol.md"
    assert msg.pack().hex(" ") in SPEC.replace(" | ", " "), f"{name}: payload hex missing from spec"


def test_vectors_json_current():
    on_disk = json.loads((ROOT / "docs" / "protocol_vectors.json").read_text())
    assert on_disk == json.loads(json.dumps(vectors())), "run: python -m orbit.protocol.messages --emit-vectors docs/protocol_vectors.json"


def test_spec_size_table_matches_code():
    for cls in MESSAGE_TYPES.values():
        n = cls.size()
        row = re.search(rf"\| 0x{cls.TYPE:02X} \| \w+ \| [^|]+\| (\d+) \| (\d+) \| (\d+) \|", SPEC)
        assert row, f"no size-table row for 0x{cls.TYPE:02X}"
        assert tuple(map(int, row.groups())) == (n, n + 3, n + 5)


def test_all_examples_in_one_stream_with_leading_delimiter():
    stream = b"\x00" + b"".join(encode(m) for _, m in EXAMPLES)
    dec = FrameDecoder()
    got = dec.feed(stream)
    assert got == [m for _, m in EXAMPLES]
    assert (dec.crc_errors, dec.len_errors, dec.unknown_type, dec.rx_overflow) == (0, 0, 0, 0)


def test_error_table():
    hb = Heartbeat(sender=0xFF, seq=1, uptime_ms=1)
    good = encode(hb)
    # CRC mismatch: flip a payload byte after COBS (byte 2 is 'sender' 0xff -> 0xfe, no zero created)
    bad = bytearray(good); bad[2] ^= 0x01
    dec = FrameDecoder(); assert dec.feed(bytes(bad)) == [] and dec.crc_errors == 1
    # Unknown type with valid CRC
    body = bytes([0x7E]) + b"\x01\x02"; pre = body + crc16(body).to_bytes(2, "little")
    dec = FrameDecoder(); assert dec.feed(cobs.encode(pre) + b"\x00") == [] and dec.unknown_type == 1
    # Length mismatch with valid CRC
    body = bytes([Heartbeat.TYPE]) + b"\x01\x02"; pre = body + crc16(body).to_bytes(2, "little")
    dec = FrameDecoder(); assert dec.feed(cobs.encode(pre) + b"\x00") == [] and dec.len_errors == 1
    # Partial frame (<3 bytes) is silent
    dec = FrameDecoder(); assert dec.feed(b"\x02\x05\x00") == [] and dec.crc_errors == 0 and dec.len_errors == 0
    # Mid-frame desync: join the tail of one frame to a good one; only the good one survives
    dec = FrameDecoder(); assert dec.feed(good[10:] + good) == [hb] and dec.crc_errors + dec.len_errors == 1
    # Overflow: 300 non-zero bytes then a delimiter -> dropped and counted, next frame fine
    dec = FrameDecoder(); assert dec.feed(bytes([1] * 300) + b"\x00" + good) == [hb] and dec.rx_overflow == 1


def test_field_range_enforced():
    with pytest.raises(ProtocolError):
        Grant(slot_id=70000, budget_bytes=0).pack()
    with pytest.raises(ProtocolError):
        FrameIngest(frame_id=0, row=0, pixels=(256,) + (0,) * 127).pack()
    with pytest.raises(ProtocolError):
        FrameIngest(frame_id=0, row=0, pixels=(0,) * 127).pack()      # wrong count


def test_row_message_accepts_bytes_and_roundtrips_as_tuple():
    m = FrameIngest(frame_id=1, row=2, pixels=bytes(range(128)))
    assert m.pixels == tuple(range(128)) and m.size() == 131
    [got] = FrameDecoder().feed(encode(m))
    assert got == m


def test_empty_payload_message():
    assert StatusQuery.size() == 0 and len(encode(StatusQuery())) == 5
    [got] = FrameDecoder().feed(encode(StatusQuery()))
    assert got == StatusQuery()


def test_spec_generated_section_current():
    block = SPEC[SPEC.index(BEGIN) + len(BEGIN):SPEC.index(END)].strip()
    assert block == spec_markdown().strip(), "run: python -m orbit.protocol.messages --emit-md docs/protocol.md"


def test_every_message_fits_its_framer():
    for cls in MESSAGE_TYPES.values():
        assert cls.size() + 3 <= params.MAX_FRAME
        if cls.DIR != "O→N":
            assert cls.size() + 3 <= 63, f"{cls.__name__} exceeds framer_tx's 63-byte pre-COBS cap"


def test_params_constants_in_spec():
    for name in ("FRAME_W", "FRAME_H", "FRAME_BYTES", "PIXELS_PER_CYCLE", "QUEUE_DEPTH", "CLOUD_THRESHOLD",
                 "CHANGE_THRESHOLD", "SHARP_SHIFT", "W_CLEAR", "W_SHARP", "W_CHANGE", "CLK_HZ", "BAUD",
                 "HEARTBEAT_MS", "LINK_TIMEOUT_MS", "POWER_PERIOD_MS", "ORCH_ID", "MAX_FRAME", "INA219_ADDR"):
        val = getattr(params, name)
        forms = {str(val), f"{val:_}", f"0x{val:02X}"} if isinstance(val, int) else {str(val)}
        assert any(re.search(rf"`{name}`\s*\|\s*{re.escape(f)}\s*\|", SPEC) for f in forms), f"{name}={val} not in spec table"


def test_rtl_framer_length_table_matches_code():
    """rtl/framer_rx.v hard-codes payload length per type; it must equal Message.size()."""
    rtl = (ROOT / "rtl" / "framer_rx.v").read_text()
    table = {int(t, 16): int(n) for t, n in re.findall(r"8'h([0-9A-Fa-f]{2}): type_len = 9'd(\d+);", rtl)}
    assert table == {t: c.size() for t, c in MESSAGE_TYPES.items()}
