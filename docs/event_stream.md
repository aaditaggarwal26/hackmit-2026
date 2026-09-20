# Orbit event stream

The contract between the ground station and the display.

The ground station emits one JSON object per line (JSONL) over a WebSocket
(`ws://<ground>:8766/`, `Settings.stream_port`) and simultaneously appends every
line to `runs/<run_id>.jsonl`. The display reads either one. Live and replay are
the same code path. A display that connects late receives the whole run so far,
from `seq` 1, then live events.

Whatever the satellites actually send over the wire (`docs/protocol.md`) is the
ground station's problem. The display only ever sees the events below.

The simulator writes the same file for every run (`runs/sim-<scenario>-<seed>.jsonl`,
or `--run-id`), so the display can be rehearsed against a recorded run with no radio.

---

## Envelope

Every event has these three fields.

| field | type | meaning |
|---|---|---|
| `seq` | int | Starts at 1, increments by 1, never repeats. The display uses this to detect dropped events. |
| `t` | float | Seconds since run start. Drives replay timing. |
| `type` | string | One of the types below. |

Optional on any event: `wall` (ISO 8601 UTC string) for the record (present on live runs).

If the display sees `seq` jump, it shows a gap warning. Do not skip numbers.

Scores everywhere in this stream are the satellites' onboard scores on a **0–100** scale
(the kernel's u16 composite ÷ 655.35). Nodes are identified by `node_id`; every node event
also carries `label` (the hostname, e.g. `sat-a`).

---

## `run_start`

First event of every run. Exactly one.

```json
{
  "seq": 1, "t": 0.0, "type": "run_start",
  "run_id": "2026-09-20T02-14-03",
  "mode": "live",
  "nodes": [
    {"node_id": 0, "label": "sat-a", "transport": "udp-multicast://239.255.42.99:50000", "real": false},
    {"node_id": 1, "label": "sat-b", "transport": "udp-multicast://239.255.42.99:50000", "real": false},
    {"node_id": 2, "label": "sat-c", "transport": "udp-multicast://239.255.42.99:50000", "real": false}
  ],
  "window": {"budget_bytes": 983040, "duration_s": 120.0},
  "queue_limit": 8,
  "scoring": {"w_clear": 21845, "w_sharp": 21845, "w_change": 21845, "cloud_thr": 200, "change_thr": 16},
  "usable_rule": {"metric": "cloud_frac", "max": 0.35},
  "priority": {"item_aging_rate": 0.5, "sat_aging_rate": 0.3},
  "bus": {"group": "239.255.42.99", "port": 50000}
}
```

`mode` is `live` or `replay`. `real` is false for a simulated node (the current
satellites; `Settings.nodes_real` flips it when the ESP32s arrive); the display
shows a badge when any node is false. `nodes` lists `Settings.expected_sats`; a
satellite that appears with another hostname gets the next `node_id` and a
`node_event` announcing it.

---

## `node_status`

One per node per poll — emitted whenever the ground hears a `bid` or a
`heartbeat` from that node. The heartbeat of the live screen.

```json
{
  "seq": 42, "t": 3.21, "type": "node_status",
  "node_id": 0, "label": "sat-a",
  "queue_depth": 7,
  "top_frame_id": 118,
  "top_score": 62.9,
  "top_age_s": 12.4,
  "frames_scored": 19,
  "frames_evicted": 2,
  "frames_sent": 3,
  "busy": false,
  "link_ok": true
}
```

`busy` = this node currently holds the grant. `link_ok` = heard from within
`peer_stale_s`. `frames_evicted` counts frames lost to onboard storage (evicted or
rejected), never frames that lost arbitration.

---

## `queue_window`

The top few entries of a node's queue. Not the whole queue. Emitted with every
bid a node makes (normally once per round per node). Capped at 5 entries; the first
is the item being bid.

```json
{
  "seq": 43, "t": 3.21, "type": "queue_window",
  "node_id": 0, "label": "sat-a",
  "depth": 7,
  "top": [
    {"frame_id": 118, "score": 62.9, "age_s": 12.4},
    {"frame_id": 104, "score": 60.8, "age_s": 30.1},
    {"frame_id": 122, "score": 47.3, "age_s": 4.0}
  ]
}
```

---

## `frame_scored`

A node scored a frame (from the satellite's `scored` bus message). This is what
lets the display rebuild queue history and lets the ground compute the baseline.

```json
{
  "seq": 44, "t": 3.30, "type": "frame_scored",
  "node_id": 0, "label": "sat-a",
  "frame_id": 123,
  "score": 42.9,
  "parts": {"clear": 29.0, "sharp": 9.5, "change": 4.4},
  "cloud_frac": 0.62,
  "queued": true,
  "evicted_frame_id": 97,
  "queue_depth": 7
}
```

`evicted_frame_id` is null when nothing was pushed out. `queued` is false when the
frame was rejected on arrival (pool full, did not beat the worst held).

---

## `grant`

The ground gave the channel to one node. **Includes the bids**, and, because
Orbit's arbitration is `score + item_age×rate + sat_wait×rate`, the itemised
priority of each bidder so the display can show *why* a node won.

```json
{
  "seq": 45, "t": 3.40, "type": "grant",
  "slot_id": 12,
  "node_id": 0,
  "frame_id": 118,
  "budget_bytes": 16384,
  "reason": "highest_score",
  "bids": [
    {"node_id": 0, "top_score": 62.9, "ready": true, "priority": 66.8, "item_age_term": 2.6, "sat_wait_term": 1.4,
     "item_age_s": 5.2, "sat_wait_s": 4.7, "frame_id": 118},
    {"node_id": 1, "top_score": 58.1, "ready": true, "priority": 58.8, "item_age_term": 0.1, "sat_wait_term": 0.6,
     "item_age_s": 0.2, "sat_wait_s": 2.0, "frame_id": 40},
    {"node_id": 2, "top_score": 0, "ready": false}
  ],
  "priority": {"score": 62.9, "item_age_term": 2.6, "sat_wait_term": 1.4, "total": 66.8},
  "margin": 8.0
}
```

`slot_id` is the ground's round id (unique, increasing; a revoked slot re-uses it
with a new `grant`). `reason` is one of `highest_score`, `starvation_forced` (the
aging terms, not the raw score, decided the slot), `only_ready`.

---

## `frame_arrived`

A transmission completed and was confirmed. One per downlinked frame — the ground keeps the set of
`(node_id, frame_id)` it has confirmed and re-acks a re-offered frame instead of granting it, so a frame
never appears here twice and `run_end` counts distinct frames.

```json
{
  "seq": 46, "t": 4.85, "type": "frame_arrived",
  "slot_id": 12,
  "node_id": 0,
  "frame_id": 118,
  "score": 62.9,
  "bytes": 16384,
  "duration_s": 1.45,
  "cloud_frac": 0.11,
  "usable": true
}
```

`usable` comes from the `usable_rule` in `run_start` and is computed from cloud
fraction alone (`tx_done.cloud_frac`, falling back to the frame's `scored`
message). **It is not derived from `score`.** Ranking by a number and then
measuring that same number is circular and a judge will catch it.

---

## `baseline_arrival`

The same window, run first-in-first-out with no scoring. Computed by the ground
from `frame_scored` metadata; no actual transmission happens. Each `frame_arrived`
gives the baseline one slot of the same size: nodes are served round-robin, each
node's FIFO holds `queue_limit` frames and drops the newest when full.

```json
{
  "seq": 47, "t": 4.85, "type": "baseline_arrival",
  "node_id": 1,
  "frame_id": 88,
  "score": 18.3,
  "bytes": 16384,
  "cloud_frac": 0.78,
  "usable": false
}
```

---

## `window_update`

Budget accounting. Emitted on every change.

```json
{
  "seq": 48, "t": 4.85, "type": "window_update",
  "budget_bytes": 983040,
  "used_bytes": 49152,
  "remaining_bytes": 933888,
  "time_remaining_s": 115.2,
  "open": true,
  "slots_remaining": 57
}
```

---

## `ground_status`

Periodic, once per ground tick (`period_s`, 1 s). **The ground's heartbeat to the display.**

The satellites prove they are alive with `heartbeat`; nothing proved the *ground* was alive,
and because every other event flows through the ground, a crashed ground and a quiet pass
looked identical — the stream simply stopped. This event is the difference: it arrives on a
fixed period whatever else is happening, so the display can time it out.

```json
{
  "seq": 61, "t": 8.0, "type": "ground_status",
  "uptime_s": 8.0,
  "state": "READY",
  "rounds": 12,
  "period_s": 1.0,
  "nodes": {"expected": 3, "seen": 3, "linked": 2},
  "window": {"open": true, "budget_bytes": 983040, "used_bytes": 49152, "remaining_bytes": 933888,
             "slots_used": 3, "slots_remaining": 57, "time_remaining_s": 112.0}
}
```

`uptime_s` is seconds since the ground started — the same clock as `t`. `state` is the arbiter
FSM state (`READY`, `BUSY`, `COMPLETE`, `CLOSED`). `nodes.expected` is the roster from
`run_start`, `seen` those ever heard from, `linked` those heard from within `peer_stale_s`.
`period_s` is how often the display should expect the next one: **miss a few and the ground is
unreachable, not quiet.** This is not a bus message and never goes on the bus.

---

## `bus_health`

Periodic, alongside `ground_status`. A rollup of the ground bus's counters, with the drops as
**rates over a sliding window** — not totals, and never one event per dropped datagram.

```json
{
  "seq": 62, "t": 8.0, "type": "bus_health",
  "window_s": 60.0,
  "measured_s": 8.0,
  "received": 141, "delivered": 96,
  "dropped": {"malformed": 0, "dup": 12, "oversize": 0, "own": 33},
  "malformed_by_type": {},
  "rates_per_min": {"malformed": 0.0, "dup": 90.0, "oversize": 0.0},
  "alerts": [{"metric": "dup", "rate_per_min": 90.0, "threshold_per_min": 60.0}]
}
```

`dropped` are since-start totals and `malformed_by_type` splits the malformed ones by message
type. `rates_per_min` are measured over the last `window_s` of counter samples (`measured_s` is
how much of that window exists yet). `alerts` lists only the metrics whose rate is **strictly
above** its threshold and carries the threshold with it, so the display never needs its own copy
of the numbers. A bus that loses one datagram an hour is healthy, and a panel that says
otherwise teaches people to ignore it.

Crossing a threshold also emits one `node_event` (`node_id: null`, `level: warn`); dropping back
under emits one at `level: info`. **Edge-triggered**, exactly like the HARD and silent flags, so
a sustained alert is one line in the log rather than one per second.

The thresholds currently live in `orbit/ground/stream.py` (`BUS_ALERT_PER_MIN`,
`BUS_HEALTH_WINDOW_S`); they are tunables of the same kind as `peer_stale_s` and belong in
`Settings`.

---

## `node_event`

Anything worth showing in the log: a satellite joining, an eviction (with what
displaced it), a revoked grant, a failed transmission, a late bid, a HARD flag,
silence. `node_id` is null for ground-level events (a round with no bids).

```json
{
  "seq": 60, "t": 8.0, "type": "node_event",
  "node_id": 2, "label": "sat-c",
  "level": "warn",
  "message": "grant for #14 revoked: grant_timeout"
}
```

`level` is `info`, `warn`, or `error`.

---

## `run_end`

Last event. The stats screen reads this and nothing else.

```json
{
  "seq": 900, "t": 120.0, "type": "run_end",
  "reason": "window_closed",
  "orbit":    {"frames_down": 60, "usable_down": 51, "bytes_used": 983040, "frames_left_queued": 23},
  "baseline": {"frames_down": 60, "usable_down": 33, "bytes_used": 983040, "frames_left_queued": 21, "frames_dropped_full": 140},
  "headline": {"metric": "usable frames downlinked", "orbit": 51, "baseline": 33, "gain": 1.545}
}
```

Both paths get the same byte budget. That is the whole comparison: same window,
same bandwidth, different architecture. `reason` is `window_closed`, `stopped`
(operator stopped the ground) or `rounds_done` (simulator). `gain` is null when
the baseline downlinked nothing usable.

---

## Rules

1. `seq` never skips. If the ground drops an event, it still burns the number.
2. Thumbnails are not in the stream. The display keeps a local copy of the image
   set and looks up `frame_id`. It renders a thumbnail only after a
   `frame_arrived` for that id.
3. Every run writes `runs/<run_id>.jsonl`. The stats screen reads a recorded
   file, never live memory, so the numbers survive a demo hiccup.
4. Adding a field is fine. Renaming or removing one breaks the display, so say
   so in the group chat first.
5. `ground_status` and `bus_health` exist only here. They are not bus message types and the
   ground never puts them on the bus — they are the ground telling the *laptop* it is alive.
   The display is observe-only: it reads this stream and transmits nothing, anywhere.
6. If `ground_status` stops arriving, the ground is unreachable — not idle. The display says so
   and marks every satellite unknown, because the flags it is still showing are last-known
   assessments from a ground it can no longer hear.

## Where each event comes from (ground side)

| event | produced from |
|---|---|
| `run_start` | ground start (`Settings`) |
| `node_status` | every `bid` / `heartbeat` heard on the bus |
| `queue_window` | every `bid` (top item + diagnostic window) |
| `frame_scored` | every `scored` bus message |
| `grant` | every arbitration decision (`GroundStation._arbitrate`) |
| `frame_arrived` | `tx_done` confirmed with all chunks (`tx_ack{ok}`) |
| `baseline_arrival` | the FIFO model, one slot per `frame_arrived` |
| `window_update` | every completed transmission and window close |
| `ground_status` | the ground's periodic tick (`EventStream.tick`), every `period_s` |
| `bus_health` | the same tick, from the ground bus's `BusStats` |
| `node_event` | `sat_seen`, `eviction`, `revoke`, `tx_failed`, `late_bid`, `unexpected_tx`, `no_bids`, hard/silent flag edges, bus-rate threshold edges |
| `run_end` | window closed, operator stop, or simulator end |

Implementation: `orbit/ground/stream.py`.

## Off-bus routing and fault codes

Generated from `orbit/protocol/registry.py` — the one place message types and fault codes are
defined — and pinned by `tests/test_registry.py`. Regenerate with
`uv run python -m orbit.protocol.registry --write`.

<!-- registry:off-bus:begin -->
Neither transport below touches the bus. `event-stream` is WebSocket JSONL on port 8766
(`Settings.stream_port`); `telemetry` is fire-and-forget unicast UDP to `display.local:50010`. Both are ground → laptop: a satellite can neither send nor hear one.

| id | type | transport | publisher | subscribers | description |
|---|---|---|---|---|---|
| 14 | `run_start` | `event-stream` | ground | laptop | first event of a run: roster, window, scoring and bus configuration |
| 15 | `node_status` | `event-stream` | ground | laptop | one node's queue, counters and link state, per bid or heartbeat heard |
| 16 | `queue_window` | `event-stream` | ground | laptop | the top few entries of a node's queue, per bid |
| 17 | `frame_scored` | `event-stream` | ground | laptop | a node scored a frame; feeds the FIFO baseline and the usable verdict |
| 18 | `grant` | `event-stream` | ground | laptop | the arbitration decision with every bidder's itemised priority |
| 19 | `frame_arrived` | `event-stream` | ground | laptop | a transmission completed and was confirmed; one per distinct frame |
| 20 | `baseline_arrival` | `event-stream` | ground | laptop | the same slot as served by the no-scoring FIFO model |
| 21 | `window_update` | `event-stream` | ground | laptop | contact-window byte accounting, on every change |
| 22 | `ground_status` | `event-stream` | ground | laptop | the ground's own heartbeat to the laptop: miss it and the ground is unreachable |
| 23 | `bus_health` | `event-stream` | ground | laptop | the ground bus's counters as rates over a sliding window, with threshold alerts |
| 24 | `node_event` | `event-stream` | ground | laptop | the human log line: joins, evictions, revokes, flags, faults |
| 25 | `run_end` | `event-stream` | ground | laptop | last event: the orbit-vs-baseline headline the stats screen reads |
| 26 | `round_open` | `telemetry` | ground | display | a round opened and bids are being collected |
| 27 | `decision` | `telemetry` | ground | display | the arbitration result with the itemised breakdown of every candidate |
| 28 | `tx_begin` | `telemetry` | ground | display | the granted satellite started transmitting |
| 29 | `complete` | `telemetry` | ground | display | a transmission was confirmed and the window debited |
| 30 | `tx_failed` | `telemetry` | ground | display | a transmission ended without every chunk, or with a bad digest |
| 31 | `revoke` | `telemetry` | ground | display | a grant was taken back and the round re-arbitrated |
| 32 | `no_bids` | `telemetry` | ground | display | a round opened and nobody bid |
| 33 | `late_bid` | `telemetry` | ground | display | a bid arrived for a round that had already closed |
| 34 | `unexpected_tx` | `telemetry` | ground | display | tx traffic from a node or for an item that holds no grant |
| 35 | `sat_seen` | `telemetry` | ground | display | a satellite was heard from for the first time |
| 36 | `eviction` | `telemetry` | ground | display | a satellite reported losing a frame to its own storage limits |
| 37 | `window_closed` | `telemetry` | ground | display | the contact window ended |
| 38 | `state` | `telemetry` | ground | display | a ground FSM transition |
| 39 | `flags` | `telemetry` | ground | display | the per-satellite flag set: waiting, starved, memory-pressured, silent |
| 40 | `bus` | `telemetry` | ground | display | every bus datagram, mirrored verbatim for the display's log |
| 41 | `snapshot` | `telemetry` | ground | display | the whole ground-station state, for a display that joined late |
| 42 | `bus_stats` | `telemetry` | ground | display | datagram counters: accepted, malformed, duplicate, too long |
| 43 | `telemetry_stats` | `telemetry` | ground | display | this transport's own counters, including what it dropped |

Fault codes as the display sees them. A satellite fault is a `fault` datagram on the bus that
the ground turns into a `node_event`; a ground fault never touches the bus and reaches the
laptop on this stream only.

| id | slug | severity | reaches the laptop via | crosses the bus |
|---|---|---|---|---|
| 1 | `sat_boot` | info | `fault` → `node_event` | yes |
| 2 | `littlefs_mount_failed` | fatal | `fault` → `node_event` | yes |
| 3 | `manifest_missing` | fatal | `fault` → `node_event` | yes |
| 4 | `manifest_corrupt` | fatal | `fault` → `node_event` | yes |
| 5 | `frame_checksum_failed` | anomaly | `node_event` (ground-side only) | no |
| 6 | `buffer_alloc_failed` | fatal | `fault` → `node_event` | yes |
| 7 | `psram_fallback` | degraded | `fault` → `node_event` | yes |
| 8 | `grant_unknown_item` | anomaly | `fault` → `node_event` | yes |
| 9 | `scoring_latency_high` | degraded | `fault` → `node_event` | yes |
| 10 | `ground_down` | anomaly | `node_event` (ground-side only) | no |
| 11 | `auth_reject` | anomaly | `node_event` (ground-side only) | no |
<!-- registry:off-bus:end -->

The `fault` bus message itself (`docs/protocol.md`) is a satellite → all type: the display never
receives one directly, only the `node_event` the ground makes of it.
