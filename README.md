# Orbit — onboard image scoring and downlink prioritization on an FPGA

Two Arty A7-100T boards are two Earth-observation satellites running the same
bitstream. Each scores every captured frame on the FPGA (cloud fraction, Sobel
sharpness, change against a reference) in one streaming pass, keeps a priority
queue of frame ids ordered by score, and offers its top score to a ground
orchestrator on a laptop. When a contact window opens, the ground grants one
slot at a time to the satellite with the best frame; a starvation guard stops
one satellite monopolising the pass. The claim defended on stage is energy per
frame scored at the edge; every figure carries where it was measured, and
anything unmeasured says TBD. HackMIT 2026, Sustainability track.

What crosses the wire is scores, grants and accounting, never downlink pixels:
the ground holds the same corpus and debits a modelled window. Cloud lowers a
frame's score; whether a satellite can transmit at all is line of sight, which
the ground models as the contact window. Both points are on the dashboard.

Read `ARCHITECTURE.md` for how it fits together and `docs/protocol.md` for
every byte on the wire.

## macOS development

```sh
brew install verilator                          # RTL simulation (cocotb runs on it)
uv sync                                          # Python env; add --extra corpus for Pillow (corpus fetch only)
uv run pytest                                    # tests/ (Python) + tb/ (cocotb under Verilator), ~5 min
uv run pytest -m slow                            # Verilator virtual board end to end (run alone)
uv run python tb/run_all.py                      # verilator --lint-only -Wall x4, params check, every bench
uv run python tools/offline_check.py             # proves the demo needs no network
```

Everything runs through `uv run`; nothing is installed globally.

### Corpus and the gate

The frames are a committed corpus: 194 NASA GIBS MODIS Terra true-colour tiles
over 14 fixed scenes and 16 dates in 2024, converted to 128×128 grayscale
(`corpus/frames.npz`, thumbnails in `corpus/png/`, provenance and licence text in
`corpus/manifest.json`). Nothing needs the network unless you rebuild it:

```sh
uv run python -m orbit.corpus.fetch             # refetch from GIBS (needs --extra corpus)
uv run python -m orbit.corpus.gate              # golden model over the corpus -> corpus/gate_result.json
```

The gate picks the demo pacing by rule: a pass captures 12
frames per satellite, the scaled window fits 3. The dashboard labels the window
as scaled and says where the pacing came from.

### Dashboard and orchestrator

```sh
uv run python -m viz.server --scenario lead_change --speed 2      # http://localhost:8000
uv run python -m orbit.orchestrator.main --scenario starvation --passes 3   # headless summary
```

Scenarios: `nominal`, `lead_change`, `starvation`, `filtered_vs_fifo`, `scaling`
(`--nodes sim://0 … sim://N`). Which nodes it talks to is a transport chosen
purely by path scheme (`orbit/protocol/transport.py`); nothing else changes:

| node path | what runs |
|---|---|
| `sim://<n>` | `SimulatedNode`: the bit-exact NumPy golden model, in-process |
| `verilator://<n>` | `VerilatorNode`: the real RTL compiled by Verilator, UART over a pty (`sim/`, built on first use) |
| `/dev/tty.usbserial-<serial>B` | `HardwareNode`: pyserial to an Arty |

Set them with `--nodes`, e.g. `--nodes verilator://0 sim://1` or
`--nodes /dev/tty.usbserial-XXXXXXB /dev/tty.usbserial-YYYYYYB`. The dashboard's
"+ simulated sats" control adds `sim://` nodes to whatever is physical, labelled
SIMULATED everywhere they appear.

### FTDI ttys on macOS

Each Arty's FT2232H shows up as **two** ttys, `/dev/tty.usbserial-<serial>A` and
`...B`. The UART is channel **B**; channel A is JTAG. Pick ports by FTDI serial
number, not by glob order. `ls /dev/tty.usbserial-*` lists both channels of every board.

## Windows: headless Vivado build

Vivado ML Standard (free, covers Artix-7) on a Windows machine. No GUI:

```bat
vivado -mode batch -source vivado/build.tcl
```

Synthesises `rtl/*.v` with `top_edge_node` on `xc7a100tcsg324-1`, fails loudly on
negative slack, and writes `vivado/build/orbit.bit` plus `vivado/reports/{utilization,timing,power}.txt`.
Then `uv run python -m tools.vivado_reports` turns the reports into
`vivado/reports/summary.json`, which the dashboard's efficiency panel reads
(commit the reports and the summary). See `vivado/README.md`.

## Flashing (macOS)

```sh
brew install openfpgaloader
openFPGALoader -b arty_a7_100t -f vivado/build/orbit.bit      # volatile; -f with the .mcs and JP1 set for QSPI boot
```

Board LEDs: `led0` heartbeat blink, `led1` link ok, `led2` queue has data,
`led3` busy (scoring or bench); RGB blue idle, green scoring, red bench.

## Benchmarks

```sh
uv run python -m orbit.bench.arty_run --port /dev/tty.usbserial-XXXXB --seconds 20   # Arty, INA219 on the supply input
sh orbit/bench/jetson_run.sh 20                                                       # Jetson Orin Nano: NumPy (and CuPy) baseline
uv run python -m orbit.bench.run --platform none --seconds 5                          # any machine: throughput only, energy TBD
```

Reports land in `bench/results/<stamp>.{md,json}` with a provenance label per row
(`whole-board`, `device-level`, `module-level`, `estimate`, `TBD`); rows are
comparable only where the labels match. The dashboard shows the newest.

## Repo map

| path | what |
|---|---|
| `orbit/params.py` | every constant; generates `rtl/orbit_params.vh` |
| `orbit/protocol/` | COBS + CRC framing, message set (generates `docs/protocol.md` §4), transports |
| `orbit/golden/` | bit-exact scoring kernel, priority queue, node model, worked example |
| `orbit/corpus/` | corpus loader, fetch CLI (the only network code), the gate |
| `orbit/orchestrator/` | arbitration, contact window, scenarios, main loop |
| `orbit/bench/` | energy samplers, CPU/GPU baseline, Arty runner, report |
| `rtl/` | plain Verilog: kernel, composite, queue, controller, UART/framers, board top |
| `tb/` | cocotb benches (kernel PPC 1 and 8, queue, whole node, wrapper) |
| `sim/` | Verilator virtual board (UART on a pty) |
| `vivado/` | headless build script, XDC |
| `viz/` | FastAPI + WebSocket server and the single-file dashboard |
| `tools/` | params check, Vivado report parser, offline check |
| `corpus/` | committed frames, thumbnails, manifest, gate result |
