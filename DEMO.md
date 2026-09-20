# Running the demo

Everything here is a command that has been run, or a figure read out of a file in this
repository. Nothing needs the authors present.

There are four ways to show Orbit, in order of how little can go wrong:

1. **The deterministic simulator** — no network, no hardware, same answer every time.
2. **The display from a run file** — stdlib Python only, nothing installed.
3. **The live demo** — real processes on a real multicast bus.
4. **Real boards** — two ESP32-S3s on the bus. This one needs a working 2.4 GHz network.

## Prerequisites

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # once; uv brings its own Python 3.13
uv sync                                           # --extra gpu adds the torch benchmark tier
```

`uv sync` is the only install step. The project requires Python 3.13 or newer
(`pyproject.toml`), so run anything under `uv run` rather than a bare `python -m`.
`tools/replay.py` and `tools/check_run.py` are the exceptions: they are stdlib-only and
run on any Python 3.9+, including the system `python3`.

## 1. The deterministic path

Always works. No network, no hardware, no corpus download — the corpus is committed.

```bash
uv run orbit sim --scenario nominal --seed 42 --digest
```

The digest is the **last line printed**; a JSON log line comes before it. The same seed
gives the same digest every time, on any machine:

| scenario | `--seed 42` digest | what it exercises |
|---|---|---|
| `nominal` | `4952d34abffadd60` | three similar satellites; capture slightly outpaces the link |
| `memory_pressure` | `331bed139ed08465` | sat-c captures every 0.5 s into 8 slots: constant eviction |
| `low_scorer` | `2eca8bb575c8610e` | sat-b scores 15 points lower on every frame; satellite aging lifts it |
| `revoke` | `40eb116c4c7da70d` | sat-c is granted slots and never transmits |
| `lossy` | `97c978368f14f204` | bus duplicates 20 %, reorders 10 %, drops 2 % of datagrams |
| `late_joiner` | `bf0364aac0a68916` | sat-c boots 40 s into the pass with no configuration anywhere |

These six are the whole list (`orbit/sim/scenarios.py`). Fifty slots by default;
`--rounds N` changes that and changes the digest with it — `--rounds 10` on `nominal`
at seed 42 gives `9b9cfde9c072fdea`.

Each run also writes `runs/sim-<scenario>-<seed>.jsonl`, and a second run of the same
scenario writes `-2`, `-3`, and so on rather than overwriting. Add `--no-runs` to leave
`runs/` alone, or `--run-id <name>` to choose the filename.

Running the same scenario twice and diffing the two run files is the honest way to show
determinism to someone who does not believe the digest.

## 2. The display, from a run file

Nothing installed, no network, no venv — this opens on a borrowed laptop:

```bash
python3 tools/replay.py runs/sample.jsonl     # then open http://localhost:8000/
```

Verified with the system `python3`; `GET /` returns `display/live.html` from the same
origin as the WebSocket, so there is no CORS step and no second server.

* `--speed 20` to skim a 120-second run in a few seconds.
* `--loop` to leave it running on a screen unattended.
* `--start 60` to jump in partway; the state up to that point is applied silently.
* `--pause N` seconds between loops.

`runs/sample.jsonl` is a **fixture**, not a recorded run: `tools/make_sample.py` walks
three imaginary satellites through a contact window. The frames in it are real — real
corpus ids, scored with the golden model — so the thumbnails match the scores, but no
arbiter produced it. Say so if asked. The `runs/sim-*.jsonl` files are genuine ground
output from the simulator, and any of them replays the same way.

To check a run file against its contract instead of watching it:

```bash
python3 tools/check_run.py runs/sample.jsonl          # exit 1 on a contract violation
```

## 3. The live path

```bash
uv run orbit demo --scenario nominal --sats 3 --display   # then open the dashboard
```

This starts a ground station and three simulated satellites as separate OS processes
(`subprocess.Popen`, `orbit/cli.py`) and serves the dashboard locally. Verified here:
twelve seconds of it produced three satellites on the bus, six grants and six completed
transmissions. **It needs working UDP multicast between processes on this
machine, and between machines if the display is elsewhere.** Check that first, in one
command, before trusting it:

```bash
uv run orbit bus-smoke --listen          # on the second device, same SSID
uv run orbit bus-smoke --send            # here
uv run orbit bus-smoke --send --unicast <ip-of-the-listener>   # the control
```

If the sender sees its own packets but the listener sees nothing after 10 s while
`--unicast` gets through, the access point is filtering client-to-client multicast. That
is a network fault, not a bug, and the answer is the phone hotspot.

With no default route at all — nothing plugged in, no WiFi — the bus falls back to
loopback (`orbit/bus/multicast.py`), so the ground and the satellites still find each
other on one box and the demo runs unplugged.

The pieces can also be started separately, one per terminal: `orbit ground`,
`orbit sat --profile sat-a`, `orbit display`. Any setting can be overridden inline with
`--set name=value` or `ORBIT_<NAME>=…`.

## 4. The network requirement — read this before the venue

This is the most common way the demo dies, and it does not look like a network problem.

* **The ESP32-S3 radio is 2.4 GHz only silicon.** It cannot see a 5 GHz SSID at all.
  The venue network `HackMIT.2026` is 5 GHz-only (5660 MHz), so the boards can never
  join it, and the failure is **indistinguishable from a wrong password**. The firmware
  prints a full 2.4 GHz scan on every failed attempt precisely so this is visible
  (`firmware/satellite_esp32/secrets.h.example`, `firmware/README.md` §WiFi).
* **Use a phone hotspot with iPhone's "Maximize Compatibility" turned on** — the
  setting that makes the hotspot reachable by a 2.4 GHz-only client. Keep the phone on a
  charger: the entire network is that phone, and it dies quietly. (This one is team
  knowledge, not something a file in this repository states.)
* **iOS names hotspots with U+2019**, a typographic right single quote, not an ASCII
  apostrophe — `Ritvik’s iPhone` is `52 69 74 76 69 6b e2 80 99 73 …`. An ASCII `'` in
  `secrets.h` fails to associate and looks exactly like a bad password. Copy the SSID,
  do not retype it.
* On failure the firmware retries every 15 s and prints the scan with the target
  flagged, so a board recovers on its own once the network appears. **No reflash.**
* WiFi multicast is lossy and the firmware compensates. Measured on the demo hotspot by
  counting gaps in a node's own `seq`: 35.7 % loss with one copy per datagram, 13.3 %
  with 3 copies 4 ms apart, 3.6 % with 4 copies 20 ms apart (`firmware/README.md`). On a
  wired link or a well-behaved AP, set `BUS_TX_REPEAT = 1` and `TX_PASSES = 1`.

## 5. The webcam demo

```bash
uv run python camera/demo.py             # then open http://localhost:8100/
```

Take a photo with the laptop's camera; it is reduced to the 128×128 8-bit gray the board
works in and scored here by `orbit/golden/score.py` — the same kernel, at the queue's
real depth. This is the fastest way to show a sceptic that the scoring is real and not a
recording.

It falls back on its own: **"Use a sample photo"** pulls a frame from the committed
corpus and scores it identically, so no camera and no granted permission are not a
failure. It is its own process on its own port and is deliberately **not** in the
arbitration path — nothing it does can reach a decision.

## 6. Flashing the boards

See `firmware/README.md`. It covers board configuration, the rebuild and reflash
commands, what changes between satellite B and satellite C, the checks that run without
hardware, and key provisioning. It is not duplicated here, so it cannot drift from here.

## When it breaks at the venue

Every fallback below already exists in the repository. None of them needs code written
on the day.

| symptom | what to do |
|---|---|
| Boards will not join the WiFi | Almost always §4. Check the SSID's apostrophe, check the hotspot is 2.4 GHz, watch the board's scan output. Do not reflash — it retries every 15 s and recovers by itself. |
| A board dies on stage | Keep going. Nothing about arbitration depends on who is real; the ground runs whatever answers, and the display shows each satellite as a real board or simulated. Start an `orbit sat` in its place if you want three again. |
| No network at all | The bus falls back to loopback with no default route, so `orbit demo` still runs on one box. |
| Multicast blocked by the venue AP | `orbit bus-smoke --listen` / `--send`, with `--unicast <ip>` as the control, names the fault in one command. Switch to the phone hotspot. |
| Nothing will install, or the laptop is not yours | `python3 tools/replay.py runs/sample.jsonl` — stdlib only, no uv, no venv, no network. `--loop` and walk away. |
| The live run looks wrong and you need to know whether it is | `python3 tools/check_run.py runs/<run_id>.jsonl` holds the file against `docs/event_stream.md` and prints what violated it. Read the errors, not the exit code: a long run currently reports a handful of known `queue_depth` / `frames_sent` divergences where a satellite's heartbeat crosses a `tx_ack` on the wire, which are bookkeeping in `orbit/ground/stream.py` and touch no arrival, byte or `run_end` total. The committed fixtures pass clean. |
| A judge doubts the numbers | Run `uv run orbit sim --scenario nominal --seed 42 --digest` twice, or `camera/demo.py` for a live score on a photo taken in front of them. |
| A port is taken, or `runs/` is unwritable | The arbiter keeps going regardless — an unwritable `runs/` or a busy stream port does not stop a decision. Change what you need with `--set` or `ORBIT_<NAME>=…`. |
| The benchmark has no power numbers | Correct behaviour. CPU-rail and whole-board power read `"unavailable"` with the reason; per-frame energy on the ESP32 has never been measured. Do not estimate one on stage. |
