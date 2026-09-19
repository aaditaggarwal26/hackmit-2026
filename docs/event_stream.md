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
bid (once per round per node). Capped at 5 entries; the first is the item being bid.

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
| `node_event` | `sat_seen`, `eviction`, `revoke`, `tx_failed`, `late_bid`, `unexpected_tx`, `no_bids`, hard/silent flag edges |
| `run_end` | window closed, operator stop, or simulator end |

Implementation: `orbit/ground/stream.py`.
