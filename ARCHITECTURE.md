# Orbit v3 — architecture, plainly

## The shape

```
  Satellite A (Arty A7-100T)        Satellite B (Arty A7-100T)     Satellites C..N (simulated,
    frame + ref stores (BRAM)         same bitstream                 golden model in-process,
    scoring kernel, 8 px/clk                                         labelled SIMULATED)
    priority queue (32 ids)
         │  UART: rows in; scores, grants, accounting out
         └──────────────┬──────────────────────┘
                        │
            ┌───────────▼────────────┐
            │  Ground orchestrator   │  Python, laptop
            │  per-slot arbitration  │  starvation guard
            │  contact window model  │  bytes debited, never sent
            │  golden model mirror   │  board vs model checked live
            │  dashboard             │  FastAPI + WebSocket, one HTML file
            └────────────────────────┘
```

One pass = an ingest phase (each satellite captures `frames_per_pass` frames) then a
contact window (slots granted one at a time until the byte budget is spent). The
lead changes hands mid-window because a satellite that wins depletes its best frames.

## What the FPGA does (`rtl/`)

1. **Ingest.** FRAME_INGEST carries one 128-pixel row per message. The controller
   copies the row into the frame store (BRAM, 64-bit words = 8 pixels). Row 127
   starts the kernel. REF_FRAME_SET fills the reference store the same way.
2. **Kernel** (`score_kernel.v`). One streaming pass at `PIXELS_PER_CYCLE` = 8:
   per pixel a cloud comparison, a change comparison against the reference, and a
   3×3 Sobel via two line buffers and a three-word window shift. Sobel taps are ±1
   and ±2, so it is subtractions, one shift and adds: **no multipliers**. Interior
   centres only (the border is masked). 2055 clocks per frame under Verilator.
3. **Composite** (`score_composite.v`). Three saturating normalisations to u16,
   three weight multiplies (the only multipliers in the design), one shift.
4. **Queue** (`priority_queue.v`). 32 systolic cells of `{score, frame_id}`; insert,
   pop and limit change are each one clock, all comparisons in parallel. Full and
   the newcomer beats the tail → tail evicted and reported; else the newcomer is lost.
5. **Controller** (`node_ctrl.v`). A job FSM: score → insert → FRAME_SCORED with no
   idle gap (so the ground's mirror never sees a half-state), GRANT → TX_FRAME →
   pop → TX_DONE, CONFIG_SET validation, BENCH_RUN (re-score the resident frame N
   times with the UART idle), heartbeats, INA219 power telemetry.

The whole node is bit-exact against `orbit/golden/` under cocotb at PPC 1 and 8,
and under Verilator as a virtual board with the real orchestrator.

## What the ground does (`orbit/orchestrator/`)

- **Arbitration** (`arbitration.py`): candidates = nodes with data; highest top
  score wins; ties to the lowest id; after `STARVATION_N` consecutive wins the
  best other candidate is granted and the slot is marked "starvation".
- **Window** (`window.py`): `capacity = duration × rate / 8` bytes; each grant
  debits `FRAME_BYTES`. The demo window is scaled (a 600 s × 10 Mbit/s window fits
  45,776 frames and shows no contention); the snapshot says so.
- **Mirror**: every FRAME_SCORED is inserted into a ground copy of the node's queue
  and re-scored with the golden model; every TX_FRAME pops it; every STATUS_REPLY
  is compared. `mismatches` on the dashboard must read 0 with real boards.
- **FIFO baseline**: the same captures through a same-depth FIFO with round-robin
  grants and no scoring, computed on the ground. Delivered value = sum of scores
  of transmitted frames, both ways. The gap is the headline chart.
- **Scaling**: N simulated satellites on the same window, headless, plotted as a
  table; demand (N × frames per pass) crosses capacity (slots per pass) where the
  curve flattens.

## The two links, kept apart on purpose

| | real UART (115200 baud) | modelled downlink |
|---|---|---|
| carries | frame rows (the stand-in camera), scores, grants, accounting | nothing; it is a byte budget |
| rate | ~11.5 KB/s, ~1.5 s per frame in | scaled from 600 s × 10 Mbit/s to a handful of frames per pass |
| limits | how fast the satellite "captures" | how many frames a pass can deliver |

If pixels went out over the UART for accounting, the cable would be the bottleneck
and the model decoration. So the ground keeps the corpus and the node reports
`frame_id, score, byte_count`. The dashboard shows both rates side by side.

## Source of truth chain

`orbit/params.py` → `rtl/orbit_params.vh` (generated, checked) and `docs/protocol.md`
§1 (checked). `orbit/protocol/messages.py` → `docs/protocol.md` §4 and
`docs/protocol_vectors.json` (generated, checked) and `rtl/framer_rx.v`'s length
table (checked). `orbit/golden/score.py` = `docs/protocol.md` §5.2 = `rtl/score_kernel.v`
+ `score_composite.v` (cocotb). `corpus/gate_result.json` → scenario pacing.

## Honesty labels the UI carries

- SIMULATED / HARDWARE per satellite; hardware count in the provenance strip.
- "SCALED for the demo" on the window, with the unscaled default beside it.
- synthetic: no (corpus source, layer and licence from the manifest).
- Every efficiency figure is `measured` (with its report) or `TBD — pending …`.
- "board vs golden mismatches" count, always visible.
