# Orbit — satellites score their own imagery; the ground decides who downlinks

Earth-observation satellites capture far more than they can send. A satellite only
downlinks while passing over a ground station, a few minutes at a time, and most
frames are worthless — cloud, blur, nothing changed. So the scarce contact window gets
spent on junk while good frames sit unsent.

Orbit: **each satellite scores every frame onboard, keeps a priority queue, and bids
its best frame when the ground opens a slot. The ground grants one slot to one
satellite, that satellite transmits to completion, and arbitration runs again.**
ESA's Φ-sat-1 (2020) proved one satellite can filter its own imagery onboard; the
multi-satellite arbitration is the part nobody has flown.

Three ESP32-S3 satellites (simulated in software today, firmware is a separate task)
share one ground station — this repository — over a UDP multicast bus. HackMIT 2026.

## How a slot is decided

```
priority = score
         + item_age_seconds       × ITEM_AGING_RATE    (0.5)
         + satellite_wait_seconds × SAT_AGING_RATE     (0.3)
```

Per slot, event driven, on each satellite's **top item only**. No time slicing, no
round robin, no preemption: a satellite that keeps winning genuinely holds the best
frames. Fairness comes from the two aging terms — item aging stops a frame being
stranded behind newer arrivals; satellite aging stops a whole satellite that scores a
little lower from being locked out, which item aging alone can never fix. Ties go to
the lowest hostname, so a run replays identically from its seed.

The bid also carries a *window* of the next few queue entries. It is diagnostic: the
arbiter never reads it, the display does — it is how an operator sees queue shape.

## Run it

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # once; uv brings its own Python 3.13
uv sync                                                 # add --extra gpu for the torch benchmark tier

uv run orbit sim --scenario memory_pressure --rounds 50 --seed 42   # deterministic 50-slot table
uv run orbit demo --scenario nominal --display          # ground + 3 satellites + dashboard, real multicast
uv run orbit bench --tier cpu,gpu                       # energy/efficiency baseline (see results/)
uv run orbit bus-smoke --listen      # on a second device on the same WiFi …
uv run orbit bus-smoke --send        # … then here: does client-to-client multicast work?
```

Live pieces, each its own process (a real satellite replaces an `orbit sat`):

| command | what |
|---|---|
| `orbit ground` | the arbiter on `239.255.42.99:50000`; JSONL event stream on `ws://:8766` and `runs/<run_id>.jsonl`; UDP telemetry to `display.local:50010` |
| `orbit sat --profile sat-a` | one simulated satellite: fixed frame pool, local eviction, onboard scoring on the committed MODIS corpus |
| `orbit display` | the monitoring page (normally on the laptop), fed only by telemetry — never in the control path |
| `python -m viz.replay runs/x.jsonl` | replay a recorded run into the display; no radio, no hardware |

Any setting: `--set name=value` anywhere on the command line, or `ORBIT_<NAME>=…`.
Everything tunable is one frozen dataclass in `orbit/config.py`.

Scenarios: `nominal`, `memory_pressure` (one satellite captures 8× faster than the
link drains and evicts constantly), `low_scorer` (one satellite scores 15 points
lower; watch satellite aging lift it), `revoke` (a satellite accepts grants and never
transmits), `lossy` (20 % duplicates, 10 % reorder, 2 % drop), `late_joiner`.

## What is measured

`results/bench.jsonl` holds the identical scoring kernel timed on the GX10 CPU and
GPU with the same 194 real MODIS frames. Every figure carries `method`, `scope` and
`measured`; anything this machine cannot measure says `"unavailable"` and why. On
the GX10 only GPU-die power is instrumented (NVML); CPU and whole-board power need
an inline USB-C PD meter and are reported as unavailable, not estimated.

The demo's headline is `run_end` in every run file: usable frames downlinked by Orbit
versus a first-in-first-out baseline given the *same byte budget*, where "usable"
is cloud fraction ≤ 0.35 — never the score that did the ranking.

## Map

```
orbit/config.py            every constant and tunable
orbit/protocol/messages.py the bus wire protocol            docs/protocol.md
orbit/protocol/auth.py     control-bus authenticity (HMAC + ground Ed25519)  docs/security.md
orbit/arbiter/             priority formula, ground FSM, contact window, flags
orbit/bus/                 multicast bus, loopback twin, dedup with restart detection
orbit/sim/                 simulated satellites, scenarios, deterministic harness
orbit/ground/              live station, telemetry, display event stream   docs/event_stream.md
orbit/bench/               energy/efficiency benchmark
orbit/golden/              the scoring kernel (integers, bit-exact) and queue
corpus/                    194 NASA GIBS MODIS frames, offline
viz/                       the monitoring page + replay
tests/                     184 tests: uv run pytest
```

`uv run pytest`, `uv run mypy`, `uv run ruff check` and `uv run ruff format --check` are all expected clean; tests run with warnings as errors.
See `ARCHITECTURE.md` for the design and the reasons behind it.
