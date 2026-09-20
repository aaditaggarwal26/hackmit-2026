"""The message-schema checker itself: parsing, and the four diff verdicts.

Deliberately synthetic. The live firmware and the live protocol module are what the tool
is pointed at in CI, but they move, so nothing here reads them -- a test that breaks when
someone adds a field is a test nobody will trust.
"""

from dataclasses import dataclass

from tools import check_message_schema as C

ENVELOPE = frozenset({"v", "type", "from", "seq", "t_ms"})

# One sketch exercising every idiom the real firmware uses: an envelope helper, a nested
# object filled by a helper, an array of objects, and a handler whose incoming document is
# read (never written) while its outgoing one is built beside it.
INO = """
// a comment holding a brace { and a decoy d["nope"] = 1; that must not be parsed
static void fillEnvelope(JsonDocument &d, const char *type) {
  d["v"] = ORBIT_PROTOCOL_VERSION;
  d["type"] = type;
  d["from"] = HOSTNAME;
  d["seq"] = ++g_seq;
  d["t_ms"] = (uint32_t)millis();
}

static void addParts(JsonObject o, const Item &it) {
  o["clear"] = orbit_part(it.clear);
  o["sharp"] = orbit_part(it.sharp);
  o["change"] = orbit_part(it.change);
}

static void sendScored(const Item &it) {
  JsonDocument d;
  fillEnvelope(d, "scored");
  d["item_id"] = it.item_id;
  addParts(d["parts"].to<JsonObject>(), it);
  d["cloud_frac"] = orbit_cloud_frac(it.cloud_px);
  JsonArray w = d["window"].to<JsonArray>();
  for (int i = 0; i < 2; i++) {
    JsonObject e = w.add<JsonObject>();
    e["item_id"] = i;
    e["score"] = 0.0f;
  }
  sendDoc(d);
}

static void onGrant(JsonDocument &d) {
  const uint16_t item_id = d["item_id"] | 0;
  if ((d["v"] | 0) != ORBIT_PROTOCOL_VERSION) return;
  JsonDocument o;
  fillEnvelope(o, "tx_begin");
  o["item_id"] = item_id;
  sendDoc(o);
}
"""


def _fields():
    return C.firmware_fields(INO)


def test_parses_one_entry_per_emitted_type():
    assert set(_fields()) == {"scored", "tx_begin"}


def test_envelope_helper_is_followed():
    assert _fields()["scored"] >= ENVELOPE
    assert _fields()["tx_begin"] >= ENVELOPE


def test_nested_helper_and_array_elements_are_attributed_to_the_caller():
    body = _fields()["scored"] - ENVELOPE
    assert body == {
        "item_id",
        "parts.clear",
        "parts.sharp",
        "parts.change",
        "cloud_frac",
        "window[].item_id",
        "window[].score",
    }


def test_incoming_documents_are_never_counted_as_emitted():
    # onGrant reads d["item_id"] and d["v"] and writes only its own `o`.
    assert _fields()["tx_begin"] - ENVELOPE == {"item_id"}


def test_clean_match_reports_nothing():
    fw = {"scored": {"item_id", "cloud_frac"} | ENVELOPE}
    (d,) = C.diff(fw, {"scored": {"item_id", "cloud_frac"}}, {}, ENVELOPE)
    assert d.emitted and not d.missing and not d.extra and not d.envelope_missing
    assert C.report([d]) == 0


def test_missing_field_is_an_error():
    fw = {"scored": {"item_id"} | ENVELOPE}
    (d,) = C.diff(fw, {"scored": {"item_id", "cloud_frac"}}, {}, ENVELOPE)
    assert d.missing == ("cloud_frac",)
    assert C.report([d]) == 1


def test_extra_field_is_only_a_warning():
    fw = {"scored": {"item_id", "debug_us"} | ENVELOPE}
    (d,) = C.diff(fw, {"scored": {"item_id"}}, {}, ENVELOPE)
    assert d.extra == ("debug_us",) and not d.missing
    assert C.report([d]) == 0


def test_optional_field_is_neither_missing_nor_extra():
    fw = {"heartbeat": {"queue_len", "rssi_dbm"} | ENVELOPE}
    (d,) = C.diff(fw, {"heartbeat": {"queue_len"}}, {"heartbeat": {"rssi_dbm", "psram_ok"}}, ENVELOPE)
    assert not d.missing and not d.extra


def test_nested_object_is_compared_field_by_field():
    fw = {"scored": {"parts.clear", "parts.sharp", "parts.tilt"} | ENVELOPE}
    required = {"scored": {"parts.clear", "parts.sharp", "parts.change"}}
    (d,) = C.diff(fw, required, {}, ENVELOPE)
    assert d.missing == ("parts.change",)
    assert d.extra == ("parts.tilt",)
    assert C.report([d]) == 1


def test_a_type_the_firmware_never_emits_fails_with_all_its_fields():
    (d,) = C.diff({}, {"fault": {"code_id", "severity"}}, {}, ENVELOPE)
    assert not d.emitted and d.missing == ("code_id", "severity")
    assert not d.envelope_missing  # an unsent type is reported once, not twice
    assert C.report([d]) == 1


CONST_INO = """
#define TYPE_FAULT "fault"

static void fillEnvelope(JsonDocument &d, const char *type) {
  d["type"] = type;
}

static void sendFault(int code) {
  JsonDocument d;
  fillEnvelope(d, TYPE_FAULT);
  d["code_id"] = code;
}
"""


def test_a_type_name_reached_through_a_constant_still_resolves():
    # Otherwise the type looks un-emitted and every one of its fields reports MISSING.
    assert C.firmware_fields(CONST_INO) == {"fault": {"type", "code_id"}}


@dataclass(frozen=True)
class _Entry:
    item_id: int
    score: float


def test_an_optional_field_expands_as_the_type_it_holds():
    # `X | None` must flatten like X, or the nested leaves read as EXTRA and X as MISSING.
    assert C._expand("window", tuple[_Entry, ...] | None) == ["window[].item_id", "window[].score"]
    assert C._expand("rssi_dbm", int | None) == ["rssi_dbm"]


def test_a_dropped_envelope_field_is_an_error():
    fw = {"scored": {"item_id", "v", "type", "from", "seq"}}  # no t_ms
    (d,) = C.diff(fw, {"scored": {"item_id"}}, {}, ENVELOPE)
    assert d.envelope_missing == ("t_ms",) and not d.extra
    assert C.report([d]) == 1
