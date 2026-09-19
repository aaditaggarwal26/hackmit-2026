"""Every constant and tunable in the system, in one module.

Two kinds of thing live here and the split is deliberate:

* Module-level constants are *facts* shared by every node and every tier — frame
  geometry, the scoring kernel's power-on defaults, the protocol version. They are
  the same on the ESP32, in the simulator and in the benchmark, so they are plain
  names rather than settings that could drift between processes.

* ``Settings`` holds the *tunables*: aging rates, timeouts, the modelled contact
  window, flag thresholds, addresses. One frozen instance is built at start-up
  (defaults → ``ORBIT_*`` environment → CLI) and passed down explicitly. Nothing
  else in the codebase holds a literal for any of these, so tuning during the demo
  is a single edit or a single flag.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

PROTOCOL_VERSION = 1

# --- frames: facts about the imagery -------------------------------------------------
FRAME_W = 128
FRAME_H = 128
FRAME_BYTES = FRAME_W * FRAME_H  # 16384: 8-bit grayscale, one byte per pixel

# --- scoring kernel defaults (identical on every tier; see orbit/golden/score.py) ----
CLOUD_THRESHOLD = 200  # pixel > this counts as cloud (8-bit brightness)
CHANGE_THRESHOLD = 16  # |pixel - ref| > this counts as changed
SHARP_SHIFT = 5  # sharp_u16 = sat16(sobel_sum >> SHARP_SHIFT); tuned at the corpus gate
SHARP_SHIFT_MAX = 24  # sobel_sum < 2^25, so 24 already floors it
W_CLEAR = 21845  # u16 weights; score = sat16((w.m summed) >> 16)
W_SHARP = 21845
W_CHANGE = 21845
QUEUE_DEPTH = 32  # golden priority-queue default limit (the simulator sizes its own from Settings)

# --- unscaled reference figures: a real pass, quoted on the display as context ---------
REAL_WINDOW_DURATION_S = 600.0
REAL_LINK_RATE_BPS = 10_000_000.0

# --- ground state names: shared by the FSM, the wire and the display -------------------
STATE_READY = "READY"
STATE_BUSY = "BUSY"
STATE_COMPLETE = "COMPLETE"
STATE_CLOSED = "CLOSED"

ENV_PREFIX = "ORBIT_"


@dataclass(frozen=True)
class Settings:
    """All tunables. Frozen so a running system cannot drift from what was logged at start."""

    # --- bus ---------------------------------------------------------------------------
    mcast_group: str = "239.255.42.99"  # IPv4 local scope (RFC 2365), clear of SSDP's 239.255.255.250
    mcast_port: int = 50000
    mcast_ttl: int = 1  # never leaves the link
    bus_iface_ip: str = ""  # "" = the interface that carries the default route; docker0/tailscale0 are never it
    bus_max_datagram: int = 1400  # one message per datagram, under the WiFi MTU with headroom
    dedup_window: int = 4096  # sequence numbers remembered per sender for duplicate suppression
    restart_slack_ms: int = 5000  # a sender whose uptime goes back further than this has rebooted: forget its seqs
    hostname: str = ""  # "" = socket.gethostname(); every message carries it

    # --- arbitration (Section 9) -------------------------------------------------------
    item_aging_rate: float = 0.5  # priority points per second an item has waited on its satellite
    sat_aging_rate: float = 0.3  # priority points per second since the satellite last transmitted
    bid_window_n: int = 4  # queue entries a satellite reports beyond its top item; DIAGNOSTIC ONLY
    bid_collect_ms: int = 200  # how long READY collects bids after "offers open"
    grant_timeout_ms: int = 1000  # granted but no tx_begin → revoke, re-arbitrate excluding that bid
    tx_timeout_ms: int = 8000  # tx_begin but no tx_done → revoke likewise
    tx_straggler_ms: int = 300  # tx_done arrived before the last chunk: wait this long for it before failing
    idle_reopen_ms: int = 500  # nobody bid: wait this long before opening offers again
    state_period_ms: int = 1000  # periodic STATE broadcast between transitions (display heartbeat)

    # --- contact window (Section 11): a total byte budget, never per-satellite slices ---
    window_duration_s: float = 120.0  # scaled for the demo; REAL_* above are the unscaled figures
    link_rate_bps: float = 65_536.0  # 120 s × 65536 bps / 8 = 983040 B = 60 frames
    frame_bytes: int = FRAME_BYTES  # debited per completed transmission
    chunk_bytes: int = 900  # tx_chunk payload: 900 raw → 1200 base64 + ~140 envelope, under bus_max_datagram

    # --- flags (Section 12) ------------------------------------------------------------
    soft_wait_s: float = 20.0  # informational: aging is at work
    hard_wait_s: float = 60.0  # anomaly: aging should have won a slot by now
    memory_pressure_evictions_per_min: float = 6.0  # evictions/min at or above this = memory-pressured
    memory_pressure_window_s: float = 60.0  # sliding window for that rate
    peer_stale_s: float = 5.0  # no message from a satellite for this long = silent

    # --- telemetry (Section 13): fire-and-forget, never in the control path -------------
    telemetry_host: str = "display.local"
    telemetry_port: int = 50010
    telemetry_queue_max: int = 2000  # bounded; oldest dropped when the display cannot keep up
    telemetry_resolve_s: float = 5.0  # how often an unresolved display hostname is retried

    # --- event stream (docs/event_stream.md): JSONL to the display over WebSocket + runs/<run_id>.jsonl ---
    stream_host: str = "0.0.0.0"
    stream_port: int = 8766
    stream_queue_max: int = 5000  # per WebSocket client; a client that falls this far behind is dropped
    stream_backlog_max: int = 200_000  # events kept in memory to catch up a late display; the run file has all
    runs_dir: str = "runs"
    expected_sats: str = "sat-a,sat-b,sat-c"  # node_id order for run_start; late joiners get the next id
    nodes_real: bool = False  # True once the ESP32s replace the simulated satellites
    usable_cloud_max: float = 0.35  # a downlinked frame is "usable" iff cloud_frac <= this; never from score

    # --- satellites (simulated; the ESP32 will report its own real figures) -------------
    sat_buffer_slots: int = 8  # fixed pool allocated once at boot
    sat_heartbeat_ms: int = 1000
    sat_capture_period_s: float = 3.0  # nominal capture cadence per satellite
    sat_ack_timeout_ms: int = 3000  # tx_done sent, no tx_ack: stop waiting, keep the frame, bid again

    # --- determinism -------------------------------------------------------------------
    seed: int = 0

    @property
    def window_capacity_bytes(self) -> int:
        return int(self.window_duration_s * self.link_rate_bps / 8)

    @property
    def window_slots(self) -> int:
        return self.window_capacity_bytes // self.frame_bytes

    def with_overrides(self, **kw: Any) -> Settings:
        """Typed overrides; unknown names are an error rather than silently ignored."""
        unknown = set(kw) - {f.name for f in fields(self)}
        if unknown:
            raise KeyError(f"unknown settings: {sorted(unknown)}")
        return replace(self, **{k: v for k, v in kw.items() if v is not None})

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """ORBIT_<FIELD>=value for any field; values are coerced to the field's type."""
        source: Mapping[str, str] = os.environ if env is None else env
        out: dict[str, Any] = {}
        for f in fields(cls):
            raw = source.get(ENV_PREFIX + f.name.upper())
            if raw is None:
                continue
            out[f.name] = _coerce(raw, type(getattr(cls, f.name)))  # every field has a typed default
        return cls().with_overrides(**out)

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _coerce(raw: str, typ: type) -> Any:
    if typ is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if typ is str and raw.strip().lower() in ("none", "null"):
        return ""
    try:
        return typ(raw)
    except ValueError as e:
        raise ValueError(f"cannot read {raw!r} as {typ.__name__}") from e


DEFAULTS = Settings()


@dataclass(frozen=True)
class BenchSettings:
    """Tunables for orbit.bench. Frozen and recorded verbatim into every result so a
    number can always be traced back to the window that produced it."""

    idle_s: float = 30.0  # idle baseline recorded with all samplers running before any load
    warmup_s: float = 3.0  # one discarded run: JIT/caches/clocks settle before anything is timed
    duration_s: float = 10.0  # each timed repetition
    reps: int = 3  # timed repetitions; mean/stddev across them
    sample_hz: float = 10.0  # power/thermal/util poll rate
    results_dir: str = "results"  # bench.jsonl, bench.md and raw/ go here; only ever appended to
    big_core: int | None = None  # CPU tier pins here; None = highest cpuinfo_max_freq core
    percentiles: tuple[int, ...] = (50, 95, 99)  # latency percentiles reported per rep
    identity_frames: int = 32  # corpus pairs checked numpy==golden and torch==golden before a run
    tick_s: float = 0.05  # granularity of the idle sleep, so Ctrl-C and the clock stay responsive

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}
