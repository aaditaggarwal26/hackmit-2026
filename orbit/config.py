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
    # tx_begin but no tx_done → revoke likewise. This is a budget for the WHOLE transmission, not
    # an inter-chunk gap: the deadline is set once at tx_begin and chunks never refresh it
    # (orbit/arbiter/fsm.py, _on_tx_begin / _on_tx_chunk). The ESP32 sends TX_PASSES=3 whole
    # passes of a frame's 19 chunks (orbit_config.h) paced at the link_rate_bps the grant quotes,
    # so 3 x 16384 B at 65536 bps = 6.0 s in theory -- but the firmware author's hardware note in
    # tools/bus_round.py (the --timeout comment) puts one round at ~20 s of airtime before
    # tx_done, and raised that tool's own timeout to 50 s for it. 8000 ms covers neither, and a
    # timeout that is too short revokes every real transmission, while one that is too long only
    # costs a stuck board's slot. Sized for the observed figure with margin.
    tx_timeout_ms: int = 25000
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
    # The hardware roster. There are exactly TWO boards, b and c, and the demo runs those two and no
    # simulated third: a satellite listed but never heard from would sit at ready=false for the whole
    # window. The names are not a choice on this side -- the firmware's HOSTNAME is
    # "esp32-satellite-" + the SAT_ID build flag, fixed at compile time in
    # firmware/satellite_esp32/satellite_esp32.ino and carried in the `from` field of every
    # message, so the ground matches the firmware rather than the other way round.
    expected_sats_real: str = "esp32-satellite-b,esp32-satellite-c"
    nodes_real: bool = False  # True once the ESP32s replace the simulated satellites: selects the roster below
    usable_cloud_max: float = 0.35  # a downlinked frame is "usable" iff cloud_frac <= this; never from score

    # --- satellites (simulated; the ESP32 will report its own real figures) -------------
    sat_buffer_slots: int = 8  # fixed pool allocated once at boot
    sat_heartbeat_ms: int = 1000
    sat_capture_period_s: float = 3.0  # nominal capture cadence per satellite
    sat_ack_timeout_ms: int = 3000  # tx_done sent, no tx_ack: stop waiting, keep the frame, bid again

    # --- control-bus authenticity (orbit/protocol/auth.py) -----------------------------
    #
    # Off by default, all of it, and that is a decision rather than an oversight: this bus is a
    # local-link multicast group carrying public imagery, the simulator runs entirely inside one
    # process where there is nobody to impersonate, and every test written before this existed
    # assumes a datagram means what it says. A deployment turns these on; nothing else has to
    # know they are there, and `uv run orbit sim` keeps producing the digest it always has.
    auth_key: str = ""  # pre-shared HMAC key, used as the RAW BYTES typed here. "" = no MAC.
    # Written down nowhere but the environment: ORBIT_AUTH_KEY=... for the ground, and
    # ORBIT_AUTH_KEY in the gitignored firmware/satellite_esp32/secrets.h for each board -- the
    # same characters on both sides, with no hex or base64 step that the two languages could
    # decode differently. as_dict() redacts it, because that dict is logged at ground_start.
    ground_name: str = ""  # the one sender whose offers_open/grant/revoke/tx_ack may be obeyed
    pin_senders: bool = False  # enforce ground_name, and sat_names() for satellite traffic
    auth_anti_replay: bool = True  # with a key set, a seq must beat that sender's last VERIFIED
    # Targeted Ed25519 hybrid (orbit/protocol/ed25519.py): the ground additionally SIGNS its
    # grant/revoke/tx_ack with an asymmetric key, so even a holder of the shared HMAC key (a
    # satellite whose key leaked) cannot forge a command. The private seed lives only on the
    # ground (ORBIT_GROUND_SIGN_KEY, redacted in as_dict); the public key is distributed to every
    # verifier (ORBIT_GROUND_PUBKEY, and the gitignored firmware secrets.h). Both are 32 bytes as
    # hex; "" on either side disables the layer, so HMAC-only and the sim digest are unchanged.
    ground_sign_key: str = ""  # ground only: 64 hex chars = the 32-byte Ed25519 seed
    ground_pubkey: str = ""  # all verifiers: 64 hex chars = the ground's 32-byte Ed25519 public key

    # --- determinism -------------------------------------------------------------------
    seed: int = 0

    def sat_names(self) -> list[str]:
        """The roster the event stream pre-seeds, in node_id order: the real boards once
        ``nodes_real`` is set, the simulated profiles otherwise. One flat list cannot serve both --
        the simulator's satellites are sat-a/b/c and the boards are esp32-satellite-b/c."""
        names = self.expected_sats_real if self.nodes_real else self.expected_sats
        return [n for n in (part.strip() for part in names.split(",")) if n]

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
        """Every tunable, with the key redacted.

        This dict is logged verbatim at ``ground_start`` and recorded into every bench result,
        so returning ``auth_key`` here would put the pre-shared key in the run file, the event
        stream and results/bench.jsonl -- three places it would then be committed from. Nothing
        reconstructs a Settings from this, so redacting costs nothing.
        """
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["auth_key"] = "<set>" if self.auth_key else ""
        out["ground_sign_key"] = "<set>" if self.ground_sign_key else ""  # the Ed25519 seed; never logged
        return out


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
