# Orbit v4 — architecture, plainly

## The shape

```
  sat-a                 sat-b                 sat-c               ESP32-S3 · three simulated,
  score · buffer ·      score · buffer ·      score · buffer ·    two boards on the bench
  queue · bid · send    queue · bid · send    queue · bid · send  fully autonomous
      │                     │                     │
      └─────────────────────┼─────────────────────┘
                            │  UDP multicast 239.255.42.99:50000 — every node hears everything
                    ┌───────▼────────┐
                    │  ground (GX10) │  arbiter · READY/BUSY/COMPLETE/CLOSED · window budget · flags
                    └───┬────────┬───┘
      telemetry (UDP,   │        │  event stream (WebSocket + runs/<run_id>.jsonl)
      fire-and-forget)  │        │
                    ┌───▼────────▼───┐
                    │ display laptop │  watches; can never block a decision
                    └────────────────┘
```

The ground decides. The laptop watches. Satellites manage themselves.

## What a satellite does (and the ground never does)

Captures, scores each frame itself (cloud fraction, Sobel sharpness, change against a
stored reference — `orbit/golden/score.py`, integers, bit-exact on every tier), and
admits it to a **fixed frame pool allocated once at boot**. The priority queue is a
separate, small structure of references into that pool. When the pool is full, a new
frame that beats the worst held frame evicts it; otherwise the new frame is rejected.
Either way something is lost *to onboard storage*, which is a different failure from
losing arbitration, and every loss is announced on the bus.

On `offers_open` a satellite bids its top item (plus a diagnostic window). On
`grant` it sends the frame in paced chunks and `tx_done` with a sha256. It pops the
item **only** on `tx_ack{ok}`. A failed or revoked transmission keeps the frame. A
lost ack cannot wedge it: after a timeout, or when the ground opens a later round, it
stops waiting and bids again — and if the ground already has the frame it answers
the re-offer with an ack instead of a grant, so nothing is ever sent or counted twice.

`orbit/sim/satellite.py` is this behaviour as a pure event machine, driven by a
virtual clock in the simulator and by asyncio in the live demo. The ESP32 firmware
implements the same rules (`docs/protocol.md` §"Satellite rules"): `firmware/satellite_esp32/`
is a complete sketch plus eight headers — scoring kernel, queue, codec, LittleFS frame
store, TweetNaCl with Ed25519 and AES-GCM — and `firmware/test/` compiles those headers
on the host and holds them to this Python model term by term
(`tools/check_score_parity.py`, `tools/check_firmware_sync.py`). The roster is three
satellites in simulation and exactly two boards, `esp32-satellite-b` and
`esp32-satellite-c` (`orbit/config.py`). The boards have not yet run a pass end to end
on the bus; what is proven is the kernel, the codec and the crypto, off the board.

## What the ground does

`orbit/arbiter/fsm.py` is a pure event machine too: `on_message(msg, now)` and
`on_tick(now)` return messages to broadcast; it owns no timers and no sockets.

```
READY     broadcast offers_open, collect bids for BID_COLLECT_MS, price the top item of each,
          grant the highest priority (ties → lowest hostname)
BUSY      one satellite transmits to completion; bids ignored; no preemption
COMPLETE  all chunks in and the digest matches → tx_ack{ok}, debit one frame from the window
CLOSED    the next frame no longer fits the byte budget
```

Granted but silent → `revoke` after a timeout and re-arbitrate the *same round's bids*
without that satellite. Missing chunks or a digest mismatch → `tx_ack{ok:false}`,
nothing debited, same re-arbitration. `tx_done` that overtakes the last chunk gets a
short grace. Every (satellite, item) confirmed is remembered for the pass.

Fairness is entirely the aging terms (`orbit/arbiter/priority.py`). An earlier design
had time slices; they were removed on purpose and must not come back — a slot handed
out because it is someone's turn is spent on whatever that satellite happens to hold.

**Flags** (`orbit/arbiter/flags.py`) separate *soft* (long wait, aging is working — a
note) from *hard* (aging should have won by now and did not — an anomaly), and
classify a satellite as starved / idle / never-transmitted / memory-pressured /
silent, because wait time alone makes those look identical and a flag that fires on
ordinary aging teaches people to ignore the panel.

The **contact window** (`orbit/arbiter/window.py`) is one byte budget for the whole
pass: `duration × rate / 8`, one fixed frame debited per confirmed transmission. It is
capacity, not allocation; there are no per-satellite slices.

## The bus

One multicast group; every node joins; every message carries its sender's hostname
and a per-sender seq. Nodes drop their own echoes by hostname (several processes on
one box share the port), deduplicate by `(from, seq)`, and forget a sender whose
uptime collapses — a reboot must be heard immediately, not treated as duplicates for
minutes. Malformed, oversize, non-finite or wrong-version datagrams are counted and
dropped; they never reach the arbiter. Satellites hear each other, which is the seed
of relay (not built). A fourth satellite needs no configuration anywhere. With no
default route the bus falls back to loopback so the demo runs unplugged.

`orbit/bus/loopback.py` is the same bus in-process, through the same codec, with
seeded duplication/reordering/loss for the simulator and the tests.

The bus authenticates the control path, and can encrypt the imagery: HMAC on every datagram, an
Ed25519 signature on the ground's `grant`/`revoke`/`tx_ack` (so even a leaked shared key cannot
forge a command), sender pinning, anti-replay, and optional AES-256-GCM on the frame payload —
all off by default, since this repo's MODIS corpus is public placeholder data and the bus log is
worth keeping readable. The reasoning, what is implemented vs. the firmware port, and the
per-node-key / X25519-ECDH forward-secrecy path for a real constellation, are in
`docs/security.md`.

## Observation, never control

Two outputs, both fire-and-forget:

* **Telemetry** (`orbit/ground/telemetry.py`): UDP JSON to `display.local`, bounded
  queue, oldest dropped, hostname resolved off the event loop. Feeds `viz/`.
* **Event stream** (`orbit/ground/stream.py`, contract in `docs/event_stream.md`):
  seq-numbered JSONL over a WebSocket the ground hosts, mirrored line for line to
  `runs/<run_id>.jsonl`. It also runs the **FIFO baseline** — same frames, same byte
  budget, no scoring, round-robin — and decides `usable` from cloud fraction alone.
  `run_end` is the pitch's number.

A sink that raises costs a log line, never a slot. A slow WebSocket client is closed,
not waited for. An unwritable `runs/` or a busy port does not stop the arbiter.

## Determinism and honesty

The simulator steps a virtual clock; every random choice is seeded; the same seed
yields the same table and a byte-identical run file, five times for five judges.

Every benchmark figure names its method and scope. The GX10 exposes GPU-die power
only; CPU and board power say `"unavailable"` rather than quoting a datasheet.
Scores are 0–100; "usable" is a cloud-fraction rule, never the score, because ranking
by a number and then measuring that number is circular.

## What is deliberately not here

FPGA/HDL (the previous design), orbital propagation, an NPU tier (HailoRT is gated),
relay, time-sliced scheduling, a message broker. One box, two days.
