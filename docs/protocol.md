# Orbit bus protocol v1

One UDP multicast group. Every node — the ground station and every satellite — joins it,
sends everything to it, and hears everything on it. Each node filters locally. There is
no point-to-point traffic and no addressing beyond a `to` field inside a few messages.

| item | value | where |
|---|---|---|
| group / port | `239.255.42.99` / `50000` | `Settings.mcast_group`, `Settings.mcast_port` |
| TTL | 1 (never routed) | `Settings.mcast_ttl` |
| encoding | UTF-8 JSON, one object per datagram, ≤ 1400 bytes | `Settings.bus_max_datagram` |
| addressing | hostnames (`sat-a`, `sat-b`, `sat-c`, `gx10-f548`) — mDNS `.local` resolves them, but the bus never needs to | — |
| version | `"v": 1` | `config.PROTOCOL_VERSION` |

Why multicast and not a server: a satellite that can hear a peer being granted and never
transmitting is the seed of relay. Why JSON: the ESP32 has ArduinoJson, and a human can read
the bus log during a demo. Why every node drops its own echoes by hostname rather than by
socket: several processes on one box (the demo) share the port and must hear each other.

## Envelope

```json
{"v": 1, "type": "<name>", "from": "<hostname>", "seq": <int>, "t_ms": <int>, ...body}
```

* `from` — sender hostname. Every message carries it; it is how the ground tells satellites apart
  and how a fourth satellite joins with zero configuration.
* `seq` — per-sender counter. `(from, seq)` is the duplicate-suppression key; UDP on WiFi
  duplicates and reorders, and receivers keep a window of recent keys.
* `t_ms` — the *sender's* uptime in milliseconds. Clocks never need to agree: every age in the
  protocol is a duration computed by whoever owns the timer.

Unknown extra fields are ignored (a newer firmware may add some). A missing field, wrong type,
unknown `type` or wrong `v` makes the datagram invalid; invalid datagrams are counted and
dropped, never fatal. Integers are accepted where floats are expected.

## Message set

The table is generated from `orbit/protocol/messages.py`; `docs/protocol_vectors.json` holds one
encoded example per type and `tests/test_messages.py` pins both.

| type | direction | fields |
|---|---|---|
| `offers_open` | ground → all | `round_id`, `window_remaining_bytes`, `collect_ms` |
| `grant` | ground → all | `round_id`, `to`, `item_id`, `pace_bps`, `breakdown` |
| `revoke` | ground → all | `round_id`, `to`, `item_id`, `reason` |
| `tx_ack` | ground → all | `round_id`, `to`, `item_id`, `ok`, `bytes_received`, `reason` |
| `state` | ground → all | `state`, `round_id`, `granted_to`, `window` |
| `bid` | satellite → all | `round_id`, `item_id`, `score`, `item_age_s`, `window`, `buffer`, `eviction_count`, `queue_len` |
| `tx_begin` | satellite → all | `round_id`, `item_id`, `total_bytes`, `chunks` |
| `tx_chunk` | satellite → all | `round_id`, `item_id`, `idx`, `n`, `data` (base64) |
| `tx_done` | satellite → all | `round_id`, `item_id`, `total_bytes`, `score`, `cloud_frac`, `sha256` |
| `heartbeat` | satellite → all | `buffer`, `eviction_count`, `queue_len`, `top_score`, `top_item_id`, `uptime_s`, `frames_scored`, `frames_sent` |
| `eviction` | satellite → all | `item_id`, `score`, `kind`, `displaced_by`, `displaced_by_score` |
| `scored` | satellite → all | `item_id`, `score`, `parts{clear,sharp,change}`, `cloud_frac`, `queued`, `evicted_item_id`, `queue_depth` |

Nested records:

* `buffer` = `{slots, capacity_bytes, used, free, occupancy_pct}` — the satellite's own account of its fixed frame pool.
* `window` (in `bid`) = `[{item_id, score, item_age_s}, ...]` — the next `BID_WINDOW_N` queue entries *below* the top.
* `breakdown` (in `grant`) = `{score, item_age_s, item_age_term, sat_wait_s, sat_wait_term, total}`.
* `window` (in `state`) = `{capacity_bytes, used_bytes, remaining_bytes, slots_remaining}`.

Scores are 0–100 floats as the satellite computed them onboard. `item_id` is an integer unique per
satellite (paired with `from` it is unique on the bus).

## One slot, end to end

```
ground                                   satellite (granted)          other satellites
  │ offers_open{round_id=n}  ─────────────►│                                │
  │◄── bid{round_id=n, top item, window} ──┤◄──────── bid ──────────────────┤
  │   (collect for collect_ms)             │                                │
  │ grant{to, item_id, pace_bps, breakdown}►│  (hear it: peer_grants_seen++) │
  │◄────────── tx_begin{chunks} ───────────┤                                │
  │◄────────── tx_chunk × n (paced) ───────┤                                │
  │◄────────── tx_done ────────────────────┤                                │
  │ tx_ack{ok=true} ───────────────────────►│  pop item, free buffer slot    │
  │ state{COMPLETE}, offers_open{n+1} ─────►│                                │
```

### Ground rules (Section 10 of the brief)

* `READY`: broadcast `offers_open`, collect bids for `collect_ms`, price each bid's **top item only**
  with `priority = score + item_age_s × ITEM_AGING_RATE + satellite_wait_s × SAT_AGING_RATE`, grant
  the highest; ties go to the lowest hostname. `satellite_wait_s` is the ground's own timer: seconds
  since that hostname's last confirmed transmission (or since first sight if it has none).
* `BUSY`: exactly one satellite transmits to completion. Bids are ignored. No preemption.
* Grant but no `tx_begin` within `grant_timeout_ms`, or `tx_begin` but no `tx_done` within
  `tx_timeout_ms` → `revoke`, and arbitration re-runs **on the same round's bids** with that
  satellite excluded. If nobody is left, a fresh round opens.
* `tx_done` with every chunk seen and the reassembled bytes matching `tx_done.sha256` → `tx_ack{ok=true}`, the window is debited one frame, `COMPLETE`,
  then `READY` (or `CLOSED` when the next frame no longer fits). Chunks missing → `tx_ack{ok=false}`,
  nothing debited, re-arbitrate as for a revoke. A digest mismatch is treated the same way (`tx_ack{ok:false, reason:"digest mismatch"}`).
* Only the granted hostname's `tx_*` for the granted `item_id` and `round_id` count; anything else
  is counted as `unexpected` and ignored.
* `state` is broadcast on every transition and every `state_period_ms`.

### Satellite rules (Section 5 of the brief) — what the firmware must do

1. Score every frame locally. The ground never sees an unscored frame.
2. Keep a fixed frame pool and a separate priority queue of references into it. On a full pool, a new
   frame that beats the worst held one evicts it; otherwise the new frame is rejected. Announce every
   loss with `eviction{kind: evicted|rejected}`. The item being transmitted is never evicted.
3. On `offers_open`, if the queue is non-empty and no transmission is in flight, send exactly one
   `bid` for the top item with `round_id` copied from the offer. The `window` is the next entries
   below the top — it is diagnostic, the ground does not price it, send it anyway.
4. On `grant{to: me}`: `tx_begin`, then `tx_chunk`s paced at `pace_bps` (`chunk_bytes` raw bytes
   each, base64 on the wire), then `tx_done`. Ignore grants for other hostnames (but you may count them).
5. **Pop the item only on `tx_ack{ok: true}`.** `ok: false` or `revoke` → keep the item and bid again.
6. **A lost `tx_ack` must not wedge you.** If no `tx_ack` arrives within `sat_ack_timeout_ms`, or an
   `offers_open` with a *later* `round_id` arrives first, stop waiting: keep the frame (you cannot know
   whether it was counted; never lose data on uncertainty) and resume bidding. If the ground already has
   the frame it answers the re-offer with `tx_ack{ok}` instead of a grant, and a late `tx_ack{ok}` for an item
   you still hold means the same thing: pop it, never resend it. No frame is counted twice.
7. Send `heartbeat` every `sat_heartbeat_ms` so the ground can tell idle from starved between rounds.
   Send `scored` for every frame that goes through the kernel (kept or rejected), with the three score
   parts and the cloud fraction: the ground does not arbitrate on it, but it is what feeds the display's
   `frame_scored`, the FIFO baseline and the `usable` verdict (see `docs/event_stream.md`).
8. Drop your own echoes and duplicates by `(from, seq)`.

## Failure modes and what the bus does about them

| what happens | effect |
|---|---|
| duplicated datagram | dropped by `(from, seq)` at every receiver |
| reordered datagram | tolerated; a `bid` for an old round is counted as `late_bid` and ignored |
| dropped `bid` | that satellite sits this round out; its wait keeps growing, so its next bid is stronger |
| dropped `grant` | grant timeout → revoke → re-arbitrate; the frame is still on the satellite |
| dropped `tx_chunk` | `tx_ack{ok:false}`, nothing debited, frame kept, re-arbitrate |
| dropped `tx_ack` | satellite gives up waiting (rule 6), frame kept; ground has counted it |
| malformed / hostile datagram | counted, logged, dropped; never reaches the arbiter |
| satellite vanishes | no bids from it; after `peer_stale_s` it is flagged `silent` |
| display vanishes | telemetry drops; arbitration unaffected (telemetry is unicast UDP off the bus) |
| a fourth satellite appears | it bids with its own hostname; nothing to configure anywhere |
| a node reboots (ground or satellite) | its seq restarts at 1 and its `t_ms` collapses; receivers forget that sender's old seqs (`restart_slack_ms`) and hear it immediately |
| a satellite re-offers a frame the ground already has | the ground re-acks `tx_ack{ok, reason:"already delivered"}` without a grant; nothing is counted twice |
| `tx_done` overtakes the last chunk | the ground waits `tx_straggler_ms` for it before failing the slot |
| `tx_begin` is lost | the first `tx_chunk` (which carries `n`) stands in for it |
| a datagram carries `NaN`/`Infinity`/`1e999` | rejected as malformed; it never reaches the arbiter or the JSON outputs |

## Telemetry (not on the bus)

The ground sends one JSON object per UDP datagram, fire-and-forget, to
`TELEMETRY_HOST:TELEMETRY_PORT` (default `display.local:50010`). Event kinds are listed in
`orbit/ground/telemetry.py::KINDS`; every arbitration `decision` carries the itemised breakdown of
every candidate, and every bus message is mirrored as a `bus` event. The display consumes these and
nothing else.
