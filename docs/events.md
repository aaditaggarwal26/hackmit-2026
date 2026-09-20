# Orbit event stream

The contract between the ground station and the display.

The ground station emits one JSON object per line (JSONL) over a WebSocket
and simultaneously appends every line to `runs/<run_id>.jsonl`. The display
reads either one. Live and replay are the same code path.

Whatever the satellites actually send over the wire is the ground station's
problem. The display only ever sees the events below.

---

## Envelope

Every event has these three fields.

| field | type | meaning |
|---|---|---|
| `seq` | int | Starts at 1, increments by 1, never repeats. The display uses this to detect dropped events. |
| `t` | float | Seconds since run start. Drives replay timing. |
| `type` | string | One of the types below. |

Optional on any event: `wall` (ISO 8601 UTC string) for the record.

If the display sees `seq` jump, it shows a gap warning. Do not skip numbers.

---

## `run_start`

First event of every run. Exactly one.

```json
{
  "seq": 1,
  "t": 0.0,
  "type": "run_start",
  "run_id": "2026-09-20T02-14-03",
  "mode": "live",
  "nodes": [
    {"node_id": 0, "label": "SAT-1", "transport": "/dev/ttyUSB0", "real": true},
    {"node_id": 1, "label": "SAT-2", "transport": "/dev/ttyUSB1", "real": true},
    {"node_id": 2, "label": "SAT-3", "transport": "/dev/ttyUSB2", "real": true}
  ],
  "window": {"budget_bytes": 262144, "duration_s": 120},
  "queue_limit": 32,
  "scoring": {
    "w_clear": 21845, "w_sharp": 21845, "w_change": 21845,
    "cloud_thr": 200, "change_thr": 16
  },
  "usable_rule": {"metric": "cloud_frac", "max": 0.35}
}
```

`mode` is `live` or `replay`. `real` is false only for a dev-mode simulated
node; the display shows a badge when any node is false.

---

## `node_status`

One per node per poll. The heartbeat of the live screen.

```json
{
  "seq": 42, "t": 3.21, "type": "node_status",
  "node_id": 0,
  "queue_depth": 7,
  "top_frame_id": 118,
  "top_score": 41203,
  "top_age_s": 12.4,
  "frames_scored": 19,
  "frames_evicted": 2,
  "frames_sent": 3,
  "busy": false,
  "link_ok": true
}
```

---

## `queue_window`

The top few entries of a node's queue. Not the whole queue.

Emit on change, or at most a few times a second. Cap at 5 entries.

```json
{
  "seq": 43, "t": 3.21, "type": "queue_window",
  "node_id": 0,
  "depth": 7,
  "top": [
    {"frame_id": 118, "score": 41203, "age_s": 12.4},
    {"frame_id": 104, "score": 39877, "age_s": 30.1},
    {"frame_id": 122, "score": 31002, "age_s": 4.0}
  ]
}
```

---

## `frame_scored`

A node scored a frame. This is what lets the display rebuild queue history
and lets the ground compute the baseline.

```json
{
  "seq": 44, "t": 3.30, "type": "frame_scored",
  "node_id": 0,
  "frame_id": 123,
  "score": 28110,
  "parts": {"clear": 19000, "sharp": 6200, "change": 2910},
  "cloud_frac": 0.62,
  "queued": true,
  "evicted_frame_id": 97,
  "queue_depth": 7
}
```

`evicted_frame_id` is null when nothing was pushed out.

---

## `grant`

The ground gave the channel to one node. **Include the bids.** This is what
lets the display show *why* a node won, which is the most interesting thing
on screen.

```json
{
  "seq": 45, "t": 3.40, "type": "grant",
  "slot_id": 12,
  "node_id": 0,
  "budget_bytes": 16384,
  "reason": "highest_score",
  "bids": [
    {"node_id": 0, "top_score": 41203, "ready": true},
    {"node_id": 1, "top_score": 38110, "ready": true},
    {"node_id": 2, "top_score": 0, "ready": false}
  ]
}
```

`reason` is one of `highest_score`, `starvation_forced`, `only_ready`.

---

## `frame_arrived`

A transmission completed. One per downlinked frame.

```json
{
  "seq": 46, "t": 4.85, "type": "frame_arrived",
  "slot_id": 12,
  "node_id": 0,
  "frame_id": 118,
  "score": 41203,
  "bytes": 16384,
  "duration_s": 1.45,
  "cloud_frac": 0.11,
  "usable": true
}
```

`usable` comes from the `usable_rule` in `run_start` and is computed from
cloud fraction alone. **It must not be derived from `score`.** Ranking by a
number and then measuring that same number is circular and a judge will
catch it.

---

## `baseline_arrival`

The same window, run first-in-first-out with no scoring. Computed by the
ground from `frame_scored` metadata; no actual transmission happens.

```json
{
  "seq": 47, "t": 4.85, "type": "baseline_arrival",
  "node_id": 1,
  "frame_id": 88,
  "score": 12005,
  "bytes": 16384,
  "cloud_frac": 0.78,
  "usable": false
}
```

---

## `window_update`

Budget accounting. Emit on every change.

```json
{
  "seq": 48, "t": 4.85, "type": "window_update",
  "budget_bytes": 262144,
  "used_bytes": 49152,
  "remaining_bytes": 213000,
  "time_remaining_s": 87.3,
  "open": true
}
```

---

## `node_event`

Anything worth showing in the log: CRC errors, a link dropping, a reconnect.

```json
{
  "seq": 60, "t": 8.0, "type": "node_event",
  "node_id": 2,
  "level": "warn",
  "message": "3 crc errors in last 10s"
}
```

`level` is `info`, `warn`, or `error`.

---

## `run_end`

Last event. The stats screen reads this and nothing else.

```json
{
  "seq": 900, "t": 120.0, "type": "run_end",
  "orbit":    {"frames_down": 16, "usable_down": 14, "bytes_used": 262144, "frames_left_queued": 23},
  "baseline": {"frames_down": 16, "usable_down": 6,  "bytes_used": 262144, "frames_left_queued": 23},
  "headline": {
    "metric": "usable frames downlinked",
    "orbit": 14,
    "baseline": 6,
    "gain": 2.33
  }
}
```

Both paths get the same byte budget. That is the whole comparison: same
window, same bandwidth, different architecture.

---

## Rules

1. `seq` never skips. If the ground drops an event, it still burns the number.
2. Thumbnails are not in the stream. The display keeps a local copy of the
   image set and looks up `frame_id`. It renders a thumbnail only after a
   `frame_arrived` for that id.
3. Every run writes `runs/<run_id>.jsonl`. The stats screen reads a recorded
   file, never live memory, so the numbers survive a demo hiccup.
4. Adding a field is fine. Renaming or removing one breaks the display, so
   say so in the group chat first.