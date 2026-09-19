# Vivado build and flashing — Orbit edge node on the Arty A7-100T

Headless flow, no project file. Needs Vivado ML Standard (free, covers Artix-7).
Resource, timing and power numbers: **TBD — pending Vivado report** (this
flow has not been run yet; nothing in this repo claims measured figures).

## Build (Windows, from the repo root)

```
"C:\Xilinx\Vivado\2024.1\bin\vivado.bat" -mode batch -source vivado\build.tcl
```

(Linux/macOS: `vivado -mode batch -source vivado/build.tcl`.) Adjust the
version in the path. The script:

1. reads every `rtl/*.v` except `hello.v` and `vivado/arty_a7_100t.xdc`
2. `synth_design -top top_edge_node -part xc7a100tcsg324-1`, `opt_design`,
   `place_design`, `phys_opt_design`, `route_design`
3. writes `vivado/reports/utilization.txt`, `timing.txt`, `power.txt`,
   `drc.txt` (+ `utilization_synth.txt` per hierarchy)
4. **errors out if worst setup or hold slack is negative** — no bitstream then
5. writes `vivado/build/orbit.bit` and `vivado/build/orbit.mcs` (QSPI image)

Power: if `vivado/build/activity.saif` exists it is read before
`report_power`; otherwise the report is vectorless and should be labelled as
such. Nothing in this repo produces a SAIF (Verilator cannot write one), so
expect the vectorless case unless someone runs a post-implementation
simulation in Vivado's xsim first.

## Flash

Volatile (lost at power cycle), fastest for iteration:

```
openFPGALoader -b arty_a7_100t -f vivado/build/orbit.bit
```

Persistent, into the on-board QSPI flash (the XDC sets `SPI_BUSWIDTH 4`,
`CONFIGRATE 33`; MODE jumper JP1 on the Arty must be in the QSPI position):

```
openFPGALoader -b arty_a7_100t -f vivado/build/orbit.mcs
```

(`-f` selects flash programming; the `.bit` works too, the `.mcs` is the
SPIx4 image `write_cfgmem` produced.) Power-cycle or press PROG afterwards.

## Pins (see `vivado/arty_a7_100t.xdc`, derived from Digilent's master file)

| port | pin | note |
|---|---|---|
| `clk` | E3 | 100 MHz (master file calls it `CLK100MHZ`) |
| `sw[1:0]` | A8, C11 | node id |
| `uart_txd_in` | A9 | FT2232 → FPGA |
| `uart_rxd_out` | D10 | FPGA → FT2232 (channel B on the host) |
| `led[3:0]` | H5, J5, T9, T10 | heartbeat blink, link_ok, queue has data, busy (scoring/bench) |
| `led0_r/g/b` | G6, F6, E1 | BOOT red, IDLE blue, SCREENING green, conflict magenta |
| `ja[0]`, `ja[1]` | G13, B11 | I2C SCL, SDA to the INA219 (Pmod JA pins 1, 2; GND/3V3 on pins 5/6) |

Everything else in the XDC is left commented exactly as in the master file.

## Sanity checks before a real build

- `verilator --lint-only -Wall -Irtl rtl/*.v --top-module top_edge_node` is clean.
- `uv run pytest tb/` (cocotb + Verilator) is green, `uv run pytest tb/ -m slow`
  runs the 1024 x 240 regression.
- `uv run python tools/params_check.py` confirms the RTL constants match `orbit/params.py`.
