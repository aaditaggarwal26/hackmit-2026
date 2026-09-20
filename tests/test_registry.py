"""The drift test: the registry, the code, the docs and the firmware header must agree.

orbit/protocol/registry.py is the single definition of every message type's routing and every
fault code. Three things are generated from it — the C header the ESP32 compiles, the routing and
fault tables in docs/protocol.md, the off-bus tables in docs/event_stream.md — and one more thing
is checked against it: the .ino's fault call sites. Each of those can be edited by hand, and each
failure below names what to run instead.

The ids get their own pin. Everything else here compares two live things and stays correct as the
system grows; ``EXPECTED_FAULT_IDS`` is deliberately the opposite, a written-out copy that only a
human may change. That is the whole enforcement of "ids are permanent": renumbering a code, or
reusing a retired number, cannot be done without editing the list of numbers, which is exactly the
moment someone should be asked why.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from orbit.protocol import messages as M
from orbit.protocol import registry as R

ROOT = Path(__file__).resolve().parent.parent
INO = ROOT / "firmware/satellite_esp32/satellite_esp32.ino"

# The permanent id → slug map. APPEND ONLY. Never change a number, never delete a line, never
# give a retired code's number to something else: boards flashed months ago still send these.
EXPECTED_FAULT_IDS = {
    1: "sat_boot",
    2: "littlefs_mount_failed",
    3: "manifest_missing",
    4: "manifest_corrupt",
    5: "frame_checksum_failed",
    6: "buffer_alloc_failed",
    7: "psram_fallback",
    8: "grant_unknown_item",
    9: "scoring_latency_high",
    10: "ground_down",
    11: "auth_reject",
}


# --- ids ---------------------------------------------------------------------------------


def test_fault_ids_are_permanent():
    """A renumbered or reused code fails here and nowhere else, which is the point.

    If this fails because a code was ADDED, append it to EXPECTED_FAULT_IDS with its new number.
    If it fails for any other reason, the change is a renumbering: do not "fix" the list.
    """
    assert {int(c): R.slug(c) for c in R.FAULTS} == EXPECTED_FAULT_IDS


def test_retired_codes_keep_their_numbers():
    """A retired code stays in the enum forever, which is what makes reuse impossible.

    Deleting it would free the number for the next code to pick up, and the ground would then read
    an old board's fault as something it is not. Retiring is a status change, never a deletion.
    """
    live = {int(c) for c, s in R.FAULTS.items() if s.status is not R.Status.RETIRED}
    retired = {int(c) for c, s in R.FAULTS.items() if s.status is R.Status.RETIRED}
    assert not (live & retired)
    assert set(EXPECTED_FAULT_IDS) >= retired, "a retired id vanished from the registry"


def test_ids_are_unique_positive_and_well_formed():
    # The one place the enum and the spec table could drift apart: a FaultCode member with no
    # FaultSpec would be in no generated artefact at all (everything iterates FAULTS), so it would
    # exist in Python, mean nothing on the wire, and break no other test here.
    assert set(R.FAULTS) == set(R.FaultCode), "every FaultCode needs a FaultSpec and vice versa"

    ids = [int(c) for c in R.FAULTS]
    assert len(set(ids)) == len(ids) and min(ids) >= 1
    for code, spec in R.FAULTS.items():
        assert R.slug(code) == code.name.lower()
        assert spec.description and spec.emitter, R.slug(code)
        # A severity outside the four names is unreadable to the display, which switches on them.
        assert spec.severity in set(R.Severity)

    msg_ids = [m.id for m in R.MESSAGES]
    assert len(set(msg_ids)) == len(msg_ids) and min(msg_ids) >= 1
    keys = [(m.transport, m.slug) for m in R.MESSAGES]
    # `grant` is both a bus message and an event-stream event, and they are different things:
    # uniqueness is per transport, not per name.
    assert len(set(keys)) == len(keys)


# --- the registry against the code it describes -------------------------------------------


def test_bus_rows_agree_with_messages_py():
    """messages.py owns the wire type set; the registry only says how each one is routed."""
    assert R.bus_disagreements() == [], R.REGEN


def test_every_message_type_has_exactly_one_route():
    bus = {m.slug for m in R.MESSAGES if m.transport is R.Transport.BUS}
    assert bus == {str(t) for t in M.MessageType}


def test_event_stream_rows_match_the_display_contract():
    """Every `## \\`name\\`` section of docs/event_stream.md is an event the display is promised."""
    doc = (ROOT / "docs/event_stream.md").read_text()
    documented = set(re.findall(r"^## `(\w+)`", doc, re.M))
    rows = {m.slug for m in R.MESSAGES if m.transport is R.Transport.EVENT_STREAM}
    missing = documented - rows
    assert rows == documented, (
        f"event stream types with no registry row: {sorted(missing)}. Regenerating cannot fix this — "
        f"add an _event(<next free id>, ...) row to MESSAGES in orbit/protocol/registry.py, then: {R.REGEN}"
    )


def test_telemetry_rows_match_the_telemetry_kinds():
    from orbit.ground import telemetry

    rows = {m.slug for m in R.MESSAGES if m.transport is R.Transport.TELEMETRY}
    assert rows == set(telemetry.KINDS), (
        "add a _telem(<next free id>, ...) row to MESSAGES in orbit/protocol/registry.py for each new "
        f"kind, then: {R.REGEN}"
    )


def test_routing_says_what_crosses_the_bus():
    """The distinction the registry exists to make explicit: node-to-node vs ground→laptop."""
    for m in R.MESSAGES:
        assert m.crosses_bus is (m.transport is R.Transport.BUS)
        if not m.crosses_bus:
            # Only the ground publishes off the bus, and the laptop publishes nothing anywhere.
            assert m.source is R.Source.GROUND, m.slug
            assert R.Audience.PEERS not in m.subscribers, m.slug
    assert not [m for m in R.MESSAGES if m.source is R.Source.LAPTOP_NEVER]


def test_ground_faults_never_claim_the_bus():
    """`fault` is a satellite → all type, so a ground fault has no bus form to take."""
    assert M.MessageType.FAULT in M.SATELLITE_TYPES
    for code, spec in R.FAULTS.items():
        if spec.source is R.Source.SATELLITE:
            assert spec.transport is R.Transport.BUS, R.slug(code)
        else:
            assert spec.transport is R.Transport.EVENT_STREAM, R.slug(code)


# --- the generated artefacts ---------------------------------------------------------------


@pytest.mark.parametrize("path", [R.FAULTS_HEADER, R.PROTOCOL_DOC, R.EVENT_STREAM_DOC], ids=lambda p: p.name)
def test_generated_files_match_the_registry(path):
    """The header and both doc tables, byte for byte. Modelled on test_vectors_committed."""
    assert path.exists(), f"{path.relative_to(ROOT)} was never generated; run: {R.REGEN}"
    assert path.read_text() == R.artefacts()[path], (
        f"{path.relative_to(ROOT)} has drifted from orbit/protocol/registry.py; run: {R.REGEN}"
    )


def test_the_drift_check_can_actually_fail(tmp_path, monkeypatch):
    """A drift test that has never been shown to fail has never been shown to work.

    One code is renumbered in a copy of the header; the checker must say so, ``--write`` must put
    it back, and the checker must then be quiet again.
    """
    header = tmp_path / "orbit_faults.h"
    header.write_text(R.faults_header().replace("=  7,", "= 70,"))
    monkeypatch.setattr(R, "FAULTS_HEADER", header)
    monkeypatch.setattr(R, "BLOCKS", {})  # the docs are covered by the test above

    assert R.main([]) == 1
    assert R.main(["--write"]) == 0
    assert header.read_text() == R.faults_header()
    assert R.main([]) == 0


@pytest.mark.parametrize(
    "corruption,expect",
    [
        ("=  7,", "= 70,"),  # the same name, a different number: the worst kind
        ('case ORBIT_FAULT_PSRAM_FALLBACK: return "degraded";', 'case ORBIT_FAULT_PSRAM_FALLBACK: return "warn";'),
    ],
    ids=["renumbered", "reclassified"],
)
def test_check_firmware_sync_catches_a_hand_edited_header(tmp_path, monkeypatch, corruption, expect):
    """The tool reads the header the compiler reads, so it catches an edit the generator never made."""
    from tools import check_firmware_sync as S

    bad = tmp_path / "orbit_faults.h"
    bad.write_text(R.faults_header().replace(corruption, expect))
    monkeypatch.setattr(S, "FAULTS_HEADER", bad)
    drift = S.fault_drift()
    assert [d for d in drift if "psram_fallback" in d], drift

    bad.write_text(R.faults_header())
    assert S.fault_drift() == []


def test_doc_blocks_are_where_the_generator_expects_them():
    for name, (path, gen) in R.BLOCKS.items():
        assert R.read_block(path.read_text(), name) == gen(), f"{name} in {path.name}; run: {R.REGEN}"


def test_the_fault_table_carries_the_whole_matrix():
    """Every column the registry promises is actually in the docs, for every code."""
    md = R.fault_table_markdown()
    for code, spec in R.FAULTS.items():
        row = next(line for line in md.splitlines() if f"`{R.slug(code)}`" in line)
        for cell in (str(int(code)), str(spec.source), str(spec.severity), str(spec.transport), str(spec.status)):
            assert cell in row, (R.slug(code), cell)


# --- the firmware -----------------------------------------------------------------------------


def _ino() -> str:
    return INO.read_text()


def test_the_firmware_raises_every_active_satellite_fault():
    """Not "a fault message exists": each code the registry says the board raises, at a call site.

    A code defined and documented but never emitted is the failure this whole exercise is about —
    it looks complete everywhere except on the bus.
    """
    ino = _ino()
    for code in R.satellite_faults():
        name = f"ORBIT_FAULT_{R.slug(code).upper()}"
        # Either sent directly, or latched at boot and drained onto the bus by the loop after the
        # multicast join. Both end in the same sendFault; the latch is checked separately below.
        raised = f"sendFault({name}," in ino or f"g_boot_faults.push({name}," in ino
        assert raised, f"{R.slug(code)} is active but the sketch never raises it"


def test_the_firmware_raises_nothing_it_should_not():
    """A ground-sourced or reserved code on a satellite's bus would be a lie about its origin."""
    ino = _ino()
    for code, spec in R.FAULTS.items():
        if spec.source is R.Source.SATELLITE and spec.status is R.Status.ACTIVE:
            continue
        name = f"ORBIT_FAULT_{R.slug(code).upper()}"
        assert f"sendFault({name}," not in ino, R.slug(code)
        assert f"g_boot_faults.push({name}," not in ino, R.slug(code)


def test_severity_is_never_typed_by_hand_at_a_call_site():
    """Every sendFault passes orbit_fault_severity(...). A literal there is the drift itself.

    The vector in messages.py uses severity "warn", which is not one of the four registry names —
    exactly the mistake a hand-typed string invites, and the reason the call sites look it up.
    """
    # `[^;]` stops the match at the first statement end, which is what keeps sendFault's own
    # definition (whose body starts with `JsonDocument d;`) out of the call list.
    calls = re.findall(r"\bsendFault\(([^;]*?)\);", _ino(), re.S)
    assert calls, "no sendFault call sites at all"
    for call in calls:
        code, severity = (a.strip() for a in call.split(",")[:2])
        assert severity == f"orbit_fault_severity({code})", call
    assert not re.search(r'\bsendFault\([^;]*"(info|degraded|anomaly|fatal|warn)"', _ino())


def test_boot_faults_are_latched_until_the_bus_exists():
    """The four fatal boot conditions all occur before beginMulticast: they must be latched."""
    ino = _ino()
    join = ino.index("beginMulticast")
    for code in (
        R.FaultCode.LITTLEFS_MOUNT_FAILED,
        R.FaultCode.MANIFEST_MISSING,
        R.FaultCode.MANIFEST_CORRUPT,
        R.FaultCode.BUFFER_ALLOC_FAILED,
        R.FaultCode.PSRAM_FALLBACK,
    ):
        name = f"ORBIT_FAULT_{R.slug(code).upper()}"
        assert f"g_boot_faults.push({name}," in ino, R.slug(code)
        # ...and pushed before the join, or the latch would be pointless
        assert ino.index(f"g_boot_faults.push({name},") < join, R.slug(code)
    assert ino.index("sendFault(ORBIT_FAULT_SAT_BOOT,") > join, "sat_boot must wait for the socket"


# --- the firmware header, through an actual compiler --------------------------------------------

SRC = ROOT / "firmware/test/faults_host.cpp"
INC = ROOT / "firmware/satellite_esp32"


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """Build the fault headers into a host binary. A compile error is a test failure."""
    if shutil.which("g++") is None:
        pytest.skip("no g++ to build the host harness")
    exe = tmp_path_factory.mktemp("fw") / "faults_host"
    subprocess.run(
        ["g++", "-O2", "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{INC}", str(SRC), "-o", str(exe)],
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


def test_host_selftest_passes(host):
    """The latch, the edge trigger and the header's answers for ids it does not know."""
    out = subprocess.run([str(host), "selftest"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines()[-1].startswith("OK:")


def test_compiled_header_answers_match_the_registry(host):
    """The strongest link in the chain: what a C++ compiler makes of the header, against Python.

    Not a re-read of the file the generator wrote — the enum, both lookup switches and the
    satellite-only predicate are exercised as compiled code, across the whole id space including
    two ids past the end.
    """
    out = subprocess.run([str(host), "table"], capture_output=True, text=True, check=True).stdout
    rows = {}
    for line in out.splitlines():
        id_s, slug, severity, kind = line.split(" ")
        rows[int(id_s)] = (slug, severity, kind)

    known = {int(c) for c in R.FAULTS}
    assert set(rows) == set(range(max(known) + 3)), "the harness must cover past the last id"
    for id_, (slug, severity, kind) in rows.items():
        if id_ not in known:
            assert (slug, severity, kind) == ("?", "", "other"), id_
            continue
        spec = R.FAULTS[R.FaultCode(id_)]
        assert slug == R.slug(R.FaultCode(id_))
        assert severity == str(spec.severity)
        want = "satellite" if (spec.source is R.Source.SATELLITE and spec.status is R.Status.ACTIVE) else "other"
        assert kind == want, slug
