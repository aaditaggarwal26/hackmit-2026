"""The fault-code and message-type registry: one authoritative table, everything else derived.

Three artefacts used to be written by hand and therefore used to disagree: the fault codes the
firmware raises, the tables in ``docs/protocol.md`` / ``docs/event_stream.md``, and the C header
the ESP32 compiles against. A fault code that means "PSRAM fell back to internal SRAM" in the
header and "manifest corrupt" in the docs is worse than no fault code at all, because the number
on the bus still looks authoritative. So there is exactly one definition of each fact here, and
``--write`` regenerates the rest::

    uv run python -m orbit.protocol.registry --write   # regenerate the header and both doc tables
    uv run python -m orbit.protocol.registry           # check them; exit 1 on any drift

Why a Python module rather than a JSON/TOML file plus a loader: ``orbit/`` is already under mypy
``strict``, so the table's shape is checked by the type checker at no cost, a wrong severity name
is a mypy error rather than a runtime surprise, and the generators sit beside the data exactly as
``messages.py::spec_markdown`` does. A data file would need a loader, a schema and a test for the
loader — machinery that exists only to re-acquire what the dataclass already gives. The one real
consumer that cannot read Python (the ESP32) cannot read TOML either: it gets generated C.

INTEGER IDS ARE PERMANENT. They travel on the wire in ``fault.code_id`` and are burned into
flashed firmware that may outlive this checkout, so an id is never renumbered and never reused.
A code that stops being raised keeps its id and becomes ``Status.RETIRED``; that is why retired
codes stay in :class:`FaultCode` rather than being deleted — an id that is still present cannot
be handed to something else by accident. ``tests/test_registry.py`` pins the whole id→slug map.

Routing. Every entry says who publishes it, over which transport, and who subscribes:

* ``bus`` — UDP multicast (``Settings.mcast_group`` / ``Settings.mcast_port``). Node to node:
  every node on the link hears every datagram and filters locally, so a satellite's fault is
  heard by the ground *and* by its peers. The ground mirrors what it hears to the laptop.
* ``event-stream`` — the display contract in ``docs/event_stream.md``, WebSocket JSONL on
  ``Settings.stream_port``. Ground to laptop only; these never touch the bus, and a satellite
  can neither send nor hear one.
* ``telemetry`` — fire-and-forget unicast UDP to ``Settings.telemetry_host``/``telemetry_port``.
  Ground to display only, never in the control path.

The laptop publishes nothing at all on any of the three (``Source.LAPTOP_NEVER``): it is a pure
subscriber, which is what makes "unplug the display" a non-event for arbitration.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path

from orbit import config
from orbit.protocol.messages import GROUND_TYPES, MessageType

ROOT = Path(__file__).resolve().parents[2]
FAULTS_HEADER = ROOT / "firmware/satellite_esp32/orbit_faults.h"
PROTOCOL_DOC = ROOT / "docs/protocol.md"
EVENT_STREAM_DOC = ROOT / "docs/event_stream.md"

REGEN = "uv run python -m orbit.protocol.registry --write"


class Transport(StrEnum):
    """How an entry reaches its subscribers. The three are physically different links."""

    BUS = "bus"  # UDP multicast, node to node: everyone on the link hears it
    EVENT_STREAM = "event-stream"  # WebSocket JSONL, ground → laptop, NEVER on the bus
    TELEMETRY = "telemetry"  # unicast UDP, ground → display, fire and forget


class Source(StrEnum):
    """Who publishes. The laptop is listed so that "never" is stated rather than implied."""

    SATELLITE = "satellite"
    GROUND = "ground"
    LAPTOP_NEVER = "laptop-never"  # the laptop publishes nothing, on any transport
    SECURITY = "security"  # the auth layer, which does not exist yet: a reserved source


class Severity(StrEnum):
    """Fault severity. Four levels, because the display has four things it can do with one."""

    INFO = "info"  # worth recording; nothing is wrong
    DEGRADED = "degraded"  # still working, but not as designed
    ANOMALY = "anomaly"  # something that should not happen, happened
    FATAL = "fatal"  # this node cannot do its job


class Audience(StrEnum):
    GROUND = "ground"
    PEERS = "peers"  # the other satellites, which hear every bus datagram
    SATELLITES = "satellites"
    LAPTOP = "laptop"  # the display, over the event stream
    DISPLAY = "display"  # the display, over unicast telemetry


class Status(StrEnum):
    ACTIVE = "active"  # something raises it today
    RESERVED = "reserved"  # the id is burned and the meaning fixed; no emitter yet
    RETIRED = "retired"  # was raised once. The id stays here forever so it cannot be reused


# --- message types ----------------------------------------------------------------------


@dataclass(frozen=True)
class MessageSpec:
    """One message type, on one transport, with its publisher and its subscribers."""

    id: int
    slug: str
    source: Source
    transport: Transport
    subscribers: tuple[Audience, ...]
    description: str
    mirrored_to_laptop: bool = False  # the ground re-emits it on the event stream / telemetry

    @property
    def crosses_bus(self) -> bool:
        """True = node-to-node traffic. False = it never leaves the ground↔laptop pair."""
        return self.transport is Transport.BUS


_G = (Audience.SATELLITES,)
_S = (Audience.GROUND, Audience.PEERS)
_L = (Audience.LAPTOP,)
_D = (Audience.DISPLAY,)


def _bus(id_: int, slug: str, src: Source, desc: str) -> MessageSpec:
    """A bus type: multicast, heard by every node, and mirrored to the laptop by the ground."""
    return MessageSpec(
        id=id_,
        slug=slug,
        source=src,
        transport=Transport.BUS,
        subscribers=_G if src is Source.GROUND else _S,
        description=desc,
        mirrored_to_laptop=True,
    )


def _event(id_: int, slug: str, desc: str) -> MessageSpec:
    return MessageSpec(id_, slug, Source.GROUND, Transport.EVENT_STREAM, _L, desc)


def _telem(id_: int, slug: str, desc: str) -> MessageSpec:
    return MessageSpec(id_, slug, Source.GROUND, Transport.TELEMETRY, _D, desc)


# Ids are permanent and append-only. New entries take the next free number whatever their
# transport; the blocks below are ordered for reading, not to reserve ranges.
MESSAGES: tuple[MessageSpec, ...] = (
    # --- bus: the wire protocol, orbit/protocol/messages.py::MessageType -------------------
    _bus(1, "offers_open", Source.GROUND, "a new round: bids are being collected for collect_ms"),
    _bus(2, "grant", Source.GROUND, "exactly one satellite may transmit exactly one item"),
    _bus(3, "revoke", Source.GROUND, "the grant holder went quiet; the slot is taken back"),
    _bus(4, "tx_ack", Source.GROUND, "transmission confirmed or not; the satellite pops only on ok"),
    _bus(5, "state", Source.GROUND, "FSM state and window budget, on every transition and periodically"),
    _bus(6, "bid", Source.SATELLITE, "top item, diagnostic window and buffer telemetry for one round"),
    _bus(7, "tx_begin", Source.SATELLITE, "a granted transmission starts: total bytes and chunk count"),
    _bus(8, "tx_chunk", Source.SATELLITE, "one base64 slice of the frame, paced at the granted rate"),
    _bus(9, "tx_done", Source.SATELLITE, "every chunk sent, with the sha256 the ground must reproduce"),
    _bus(10, "heartbeat", Source.SATELLITE, "buffer, queue and health levels while no round is open"),
    _bus(11, "eviction", Source.SATELLITE, "a frame was lost to onboard storage limits, never to arbitration"),
    _bus(12, "scored", Source.SATELLITE, "a frame went through the onboard kernel: parts, cloud fraction, verdict"),
    _bus(13, "fault", Source.SATELLITE, "an edge-triggered onboard fault, named by a code_id in this registry"),
    # --- event stream: the display contract, docs/event_stream.md -------------------------
    _event(14, "run_start", "first event of a run: roster, window, scoring and bus configuration"),
    _event(15, "node_status", "one node's queue, counters and link state, per bid or heartbeat heard"),
    _event(16, "queue_window", "the top few entries of a node's queue, per bid"),
    _event(17, "frame_scored", "a node scored a frame; feeds the FIFO baseline and the usable verdict"),
    _event(18, "grant", "the arbitration decision with every bidder's itemised priority"),
    _event(19, "frame_arrived", "a transmission completed and was confirmed; one per distinct frame"),
    _event(20, "baseline_arrival", "the same slot as served by the no-scoring FIFO model"),
    _event(21, "window_update", "contact-window byte accounting, on every change"),
    _event(22, "ground_status", "the ground's own heartbeat to the laptop: miss it and the ground is unreachable"),
    _event(23, "bus_health", "the ground bus's counters as rates over a sliding window, with threshold alerts"),
    _event(24, "node_event", "the human log line: joins, evictions, revokes, flags, faults"),
    _event(25, "run_end", "last event: the orbit-vs-baseline headline the stats screen reads"),
    # --- telemetry: orbit/ground/telemetry.py::KINDS --------------------------------------
    _telem(26, "round_open", "a round opened and bids are being collected"),
    _telem(27, "decision", "the arbitration result with the itemised breakdown of every candidate"),
    _telem(28, "tx_begin", "the granted satellite started transmitting"),
    _telem(29, "complete", "a transmission was confirmed and the window debited"),
    _telem(30, "tx_failed", "a transmission ended without every chunk, or with a bad digest"),
    _telem(31, "revoke", "a grant was taken back and the round re-arbitrated"),
    _telem(32, "no_bids", "a round opened and nobody bid"),
    _telem(33, "late_bid", "a bid arrived for a round that had already closed"),
    _telem(34, "unexpected_tx", "tx traffic from a node or for an item that holds no grant"),
    _telem(35, "sat_seen", "a satellite was heard from for the first time"),
    _telem(36, "eviction", "a satellite reported losing a frame to its own storage limits"),
    _telem(37, "window_closed", "the contact window ended"),
    _telem(38, "state", "a ground FSM transition"),
    _telem(39, "flags", "the per-satellite flag set: waiting, starved, memory-pressured, silent"),
    _telem(40, "bus", "every bus datagram, mirrored verbatim for the display's log"),
    _telem(41, "snapshot", "the whole ground-station state, for a display that joined late"),
    _telem(42, "bus_stats", "datagram counters: accepted, malformed, duplicate, too long"),
    _telem(43, "telemetry_stats", "this transport's own counters, including what it dropped"),
)


# --- fault codes ------------------------------------------------------------------------


class FaultCode(IntEnum):
    """The ``code_id`` on the wire. PERMANENT: never renumber, never reuse, never delete.

    The member name is the slug (lowercased), so an id and its name are defined in exactly one
    place. A code that stops being raised becomes ``Status.RETIRED`` in :data:`FAULTS` and stays
    here, which is what makes reuse structurally impossible rather than merely discouraged.
    """

    SAT_BOOT = 1
    LITTLEFS_MOUNT_FAILED = 2
    MANIFEST_MISSING = 3
    MANIFEST_CORRUPT = 4
    FRAME_CHECKSUM_FAILED = 5
    BUFFER_ALLOC_FAILED = 6
    PSRAM_FALLBACK = 7
    GRANT_UNKNOWN_ITEM = 8
    SCORING_LATENCY_HIGH = 9
    GROUND_DOWN = 10
    AUTH_REJECT = 11
    FLASH_IMAGE_CORRUPT = 12


@dataclass(frozen=True)
class FaultSpec:
    """Everything about a code except its id and slug, which are the :class:`FaultCode` member."""

    source: Source
    severity: Severity
    transport: Transport
    subscribers: tuple[Audience, ...]
    description: str
    emitter: str  # where it is raised, or why nothing raises it yet
    status: Status = Status.ACTIVE
    mirrored_to_laptop: bool = True

    @property
    def crosses_bus(self) -> bool:
        return self.transport is Transport.BUS


def _sat(sev: Severity, desc: str, emitter: str, status: Status = Status.ACTIVE) -> FaultSpec:
    """A satellite fault: a ``fault`` datagram on the multicast bus, heard by ground and peers."""
    return FaultSpec(Source.SATELLITE, sev, Transport.BUS, _S, desc, emitter, status)


def _ground(sev: Severity, desc: str, emitter: str, status: Status = Status.RESERVED) -> FaultSpec:
    """A ground fault. There is no ground→bus fault message: ``fault`` is a satellite type
    (``SATELLITE_TYPES`` in messages.py), so these reach the laptop over the event stream and
    never cross the bus. A ground that is down could not multicast its own obituary anyway."""
    return FaultSpec(Source.GROUND, sev, Transport.EVENT_STREAM, _L, desc, emitter, status)


FAULTS: dict[FaultCode, FaultSpec] = {
    FaultCode.SAT_BOOT: _sat(
        Severity.INFO,
        "the node finished booting and joined the bus; the first thing it ever says",
        "firmware/satellite_esp32/satellite_esp32.ino setup(), once the multicast join succeeds",
    ),
    FaultCode.LITTLEFS_MOUNT_FAILED: _sat(
        Severity.FATAL,
        "LittleFS would not mount: there are no frames to capture and the node is inert",
        "firmware/satellite_esp32/satellite_esp32.ino setup(), LittleFS.begin(false) false",
    ),
    FaultCode.MANIFEST_MISSING: _sat(
        Severity.FATAL,
        "/manifest.json is not on the flash image, so no frame knows its scene reference",
        "firmware/satellite_esp32/satellite_esp32.ino setup(), LittleFS.open(/manifest.json) null",
    ),
    FaultCode.MANIFEST_CORRUPT: _sat(
        Severity.FATAL,
        "/manifest.json is present but is not valid JSON; same effect as it being absent",
        "firmware/satellite_esp32/satellite_esp32.ino setup(), deserializeJson != Ok",
    ),
    FaultCode.FRAME_CHECKSUM_FAILED: _ground(
        Severity.ANOMALY,
        "the reassembled frame did not match the sha256 in tx_done; the bytes are not what was sent",
        "orbit/ground/station.py answers tx_ack{ok:false, reason:'digest mismatch'}; not yet labelled with this code",
    ),
    FaultCode.BUFFER_ALLOC_FAILED: _sat(
        Severity.FATAL,
        "the fixed frame pool could not be allocated in PSRAM or in internal SRAM: nothing can be held",
        "firmware/satellite_esp32/satellite_esp32.ino setup(), fbuf.begin(SAT_BUFFER_SLOTS) false",
    ),
    FaultCode.PSRAM_FALLBACK: _sat(
        Severity.DEGRADED,
        "the frame pool landed in internal SRAM because PSRAM was absent or full; it works, with less headroom",
        "firmware/satellite_esp32/satellite_esp32.ino setup(), fbuf.begin ok but fbuf.inPsram() false",
    ),
    FaultCode.GRANT_UNKNOWN_ITEM: _sat(
        Severity.ANOMALY,
        "the ground granted an item this node does not hold; the two views of the queue have diverged",
        "firmware/satellite_esp32/satellite_esp32.ino onGrant(), findItem(item_id) null",
    ),
    FaultCode.SCORING_LATENCY_HIGH: _sat(
        Severity.DEGRADED,
        "one frame took longer through the kernel than the capture cadence can absorb",
        "firmware/satellite_esp32/satellite_esp32.ino capture(), kernel time over SCORING_LATENCY_WARN_US",
    ),
    FaultCode.GROUND_DOWN: _ground(
        Severity.ANOMALY,
        "the bus went quiet or its health thresholds were breached: the ground is not arbitrating",
        "reserved for orbit/ground/stream.py bus-health monitoring, which is owned elsewhere",
    ),
    FaultCode.AUTH_REJECT: FaultSpec(
        Source.SECURITY,
        Severity.ANOMALY,
        Transport.EVENT_STREAM,
        _L,
        "a datagram was rejected by the authentication layer",
        "stub: there is no auth layer yet. The id is burned now so adding one is not a renumbering",
        Status.RESERVED,
    ),
    FaultCode.FLASH_IMAGE_CORRUPT: _sat(
        Severity.FATAL,
        "a flashed blob does not match the CRC its manifest records; the image is not what was built",
        "firmware/satellite_esp32/satellite_esp32.ino setup(), orbit_fs_verify returned a bad count",
    ),
}


def slug(code: FaultCode) -> str:
    """The human name of a code. Derived from the member, never written twice."""
    return code.name.lower()


def satellite_faults() -> tuple[FaultCode, ...]:
    """The codes the firmware is expected to raise: satellite-sourced and still active."""
    return tuple(c for c, s in FAULTS.items() if s.source is Source.SATELLITE and s.status is Status.ACTIVE)


# --- generated artefacts ------------------------------------------------------------------


def _subs(spec: MessageSpec | FaultSpec) -> str:
    return " + ".join(str(a) for a in spec.subscribers)


def bus_disagreements() -> list[str]:
    """Where the registry's bus rows and ``orbit/protocol/messages.py`` disagree.

    The bus rows are not a second opinion about the wire protocol. ``messages.py`` owns the type
    set and the direction of each type; this says so out loud rather than leaving two hand-kept
    lists to drift, and it is what makes adding a message type without a route a build failure.
    """
    out: list[str] = []
    rows = {m.slug: m for m in MESSAGES if m.transport is Transport.BUS}
    declared = {str(t) for t in MessageType}
    out += [f"{n}: a MessageType with no registry row" for n in sorted(declared - set(rows))]
    out += [f"{n}: a registry bus row that is not a MessageType" for n in sorted(set(rows) - declared)]
    for name in sorted(declared & set(rows)):
        want = Source.GROUND if MessageType(name) in GROUND_TYPES else Source.SATELLITE
        if rows[name].source is not want:
            out.append(f"{name}: registry publisher is {rows[name].source}, messages.py says {want}")
    return out


def bus_routing_markdown() -> str:
    """Publisher → subscribers for every type on the multicast bus (docs/protocol.md)."""
    s = config.DEFAULTS
    out = [
        f"Transport `bus`: UDP multicast `{s.mcast_group}:{s.mcast_port}`, TTL {s.mcast_ttl}. Every node on the",
        "link hears every datagram, so `subscribers` names who *acts* on it, not who receives it.",
        "",
        "| id | type | publisher | transport | subscribers | mirrored to laptop |",
        "|---|---|---|---|---|---|",
    ]
    for m in MESSAGES:
        if m.transport is not Transport.BUS:
            continue
        out.append(
            f"| {m.id} | `{m.slug}` | {m.source} | `{m.transport}` | {_subs(m)} | "
            f"{'yes' if m.mirrored_to_laptop else 'no'} |"
        )
    return "\n".join(out)


def fault_table_markdown() -> str:
    """The whole fault-code matrix (docs/protocol.md)."""
    out = [
        "| id | slug | source | severity | transport | subscribers | status | description |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for code, spec in FAULTS.items():
        out.append(
            f"| {int(code)} | `{slug(code)}` | {spec.source} | {spec.severity} | `{spec.transport}` | "
            f"{_subs(spec)} | {spec.status} | {spec.description} |"
        )
    return "\n".join(out)


def off_bus_markdown() -> str:
    """The two ground→laptop transports, and how a fault reaches the display (docs/event_stream.md)."""
    s = config.DEFAULTS
    out = [
        f"Neither transport below touches the bus. `event-stream` is WebSocket JSONL on port {s.stream_port}",
        f"(`Settings.stream_port`); `telemetry` is fire-and-forget unicast UDP to `{s.telemetry_host}:"
        f"{s.telemetry_port}`. Both are ground → laptop: a satellite can neither send nor hear one.",
        "",
        "| id | type | transport | publisher | subscribers | description |",
        "|---|---|---|---|---|---|",
    ]
    for m in MESSAGES:
        if m.transport is Transport.BUS:
            continue
        out.append(f"| {m.id} | `{m.slug}` | `{m.transport}` | {m.source} | {_subs(m)} | {m.description} |")
    out += [
        "",
        "Fault codes as the display sees them. A satellite fault is a `fault` datagram on the bus that",
        "the ground turns into a `node_event`; a ground fault never touches the bus and reaches the",
        "laptop on this stream only.",
        "",
        "| id | slug | severity | reaches the laptop via | crosses the bus |",
        "|---|---|---|---|---|",
    ]
    for code, spec in FAULTS.items():
        via = "`fault` → `node_event`" if spec.crosses_bus else "`node_event` (ground-side only)"
        out.append(
            f"| {int(code)} | `{slug(code)}` | {spec.severity} | {via} | {'yes' if spec.crosses_bus else 'no'} |"
        )
    return "\n".join(out)


def faults_header() -> str:
    """firmware/satellite_esp32/orbit_faults.h, in full."""
    width = max(len(slug(c)) for c in FAULTS) + len("ORBIT_FAULT_")
    lines = [
        "// GENERATED from orbit/protocol/registry.py -- do not edit by hand.",
        f"//   {REGEN}",
        "//",
        "// The integer values are the `code_id` of the `fault` message (orbit/protocol/messages.py).",
        "// They are PERMANENT: a flashed board outlives this checkout, so an id is never renumbered",
        "// and never reused. A retired code keeps its number and simply stops being raised.",
        "//",
        "// tools/check_firmware_sync.py fails the build if this file and the registry disagree.",
        "#pragma once",
        "#include <stdint.h>",
        "",
        "enum OrbitFaultCode {",
    ]
    for code, spec in FAULTS.items():
        name = f"ORBIT_FAULT_{slug(code).upper()}"
        lines.append(f"  {name:<{width}} = {int(code):>2},  // {spec.severity}, {spec.source}, {spec.status}")
    lines += [
        "};",
        "",
        f"#define ORBIT_FAULT_COUNT  {len(FAULTS)}",
        f"#define ORBIT_FAULT_MAX_ID {max(int(c) for c in FAULTS)}",
        "",
        "// The registry's severity name for a code. Call sites pass this to sendFault() rather than a",
        '// literal: a hand-typed "warn" is exactly the drift this registry exists to prevent.',
        '// Returns "" for an id this build does not know, which is never a valid severity.',
        "static inline const char *orbit_fault_severity(int code_id) {",
        "  switch (code_id) {",
    ]
    for code, spec in FAULTS.items():
        lines.append(f'    case ORBIT_FAULT_{slug(code).upper()}: return "{spec.severity}";')
    lines += [
        '    default: return "";',
        "  }",
        "}",
        "",
        "// The human name of a code, for the serial log. Never goes on the wire: the bus carries the",
        "// id, and a receiver that does not know it still has severity and detail.",
        "static inline const char *orbit_fault_slug(int code_id) {",
        "  switch (code_id) {",
    ]
    for code in FAULTS:
        lines.append(f'    case ORBIT_FAULT_{slug(code).upper()}: return "{slug(code)}";')
    lines += [
        '    default: return "?";',
        "  }",
        "}",
        "",
        "// True for a code THIS NODE may raise. Ground-sourced and reserved codes are in the enum so",
        "// that their ids stay burned, but the firmware must never put one on the bus.",
        "static inline bool orbit_fault_is_satellite(int code_id) {",
        "  switch (code_id) {",
    ]
    for code in satellite_faults():
        lines.append(f"    case ORBIT_FAULT_{slug(code).upper()}:")
    lines += [
        "      return true;",
        "    default: return false;",
        "  }",
        "}",
        "",
    ]
    return "\n".join(lines)


# --- writing the generated artefacts into their files ---------------------------------------

BLOCKS = {
    "registry:bus-routing": (PROTOCOL_DOC, bus_routing_markdown),
    "registry:faults": (PROTOCOL_DOC, fault_table_markdown),
    "registry:off-bus": (EVENT_STREAM_DOC, off_bus_markdown),
}


def _marker(name: str, end: bool = False) -> str:
    return f"<!-- {name}:{'end' if end else 'begin'} -->"


def block_re(name: str) -> re.Pattern[str]:
    return re.compile(re.escape(_marker(name)) + r"(?P<body>.*?)" + re.escape(_marker(name, end=True)), re.S)


def replace_block(text: str, name: str, body: str) -> str:
    """Swap one marker-delimited block. A missing marker is an error, never a silent no-op."""
    pat = block_re(name)
    if pat.search(text) is None:
        raise KeyError(f"{_marker(name)} … {_marker(name, end=True)} not found")
    return pat.sub(lambda _: f"{_marker(name)}\n{body}\n{_marker(name, end=True)}", text, count=1)


def read_block(text: str, name: str) -> str | None:
    """The generated body currently in the file, or None when the markers are gone."""
    m = block_re(name).search(text)
    return None if m is None else m.group("body").strip("\n")


def artefacts() -> dict[Path, str]:
    """Every generated file, in full, as it should be on disk."""
    out: dict[Path, str] = {FAULTS_HEADER: faults_header()}
    for name, (path, gen) in BLOCKS.items():
        out[path] = replace_block(out.get(path, path.read_text()), name, gen())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="rewrite the generated files instead of checking them")
    a = ap.parse_args(argv)

    if bad := bus_disagreements():
        print("the registry's bus rows disagree with orbit/protocol/messages.py:\n", file=sys.stderr)
        for b in bad:
            print("  " + b, file=sys.stderr)
        return 2  # not drift in a generated file: the table itself is wrong, so generating is unsafe

    stale = []
    for path, want in artefacts().items():
        rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        if a.write:
            if not path.exists() or path.read_text() != want:
                path.write_text(want)
                print(f"wrote {rel}")
            continue
        if not path.exists() or path.read_text() != want:
            stale.append(str(rel))
    if a.write:
        print(f"registry: {len(FAULTS)} fault codes, {len(MESSAGES)} message types")
        return 0
    if stale:
        print("generated from the registry but out of date:\n")
        for s in stale:
            print("  " + s)
        print(f"\nregenerate: {REGEN}")
        return 1
    print(
        f"OK: {len(FAULTS)} fault codes and {len(MESSAGES)} message types; "
        f"orbit_faults.h and both doc tables match the registry"
    )
    return 0


__all__ = [
    "FAULTS",
    "MESSAGES",
    "REGEN",
    "Audience",
    "FaultCode",
    "FaultSpec",
    "MessageSpec",
    "Severity",
    "Source",
    "Status",
    "Transport",
    "artefacts",
    "bus_disagreements",
    "bus_routing_markdown",
    "fault_table_markdown",
    "faults_header",
    "off_bus_markdown",
    "read_block",
    "replace_block",
    "satellite_faults",
    "slug",
]

if __name__ == "__main__":
    raise SystemExit(main())
