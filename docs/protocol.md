# Orbit wire protocol v3

Link: UART 8N1 at `BAUD` between the ground orchestrator (O) and each edge node (N).
One frame = one message. COBS framing, `0x00` delimiter, CRC-16/CCITT-FALSE. All
multi-byte integers **little-endian**, unsigned unless marked. Sufficient to write a
parser in Python or Verilog without asking questions; anything not stated is undefined.

**What this link is and is not.** It stands in for two things at once: the *camera*
(FRAME_INGEST rows are the satellite capturing a frame) and the *metadata channel* to the
ground (scores, grants, accounting). It is **not** the downlink being modelled: no pixels
travel from node to ground. TX_FRAME reports what would have been sent; the ground holds
the same corpus and debits the modelled contact window by `byte_count` (§6). Cloud cover
affects frame *quality* (an input to the score); whether a satellite can transmit at all is
a matter of line of sight, which the ground models as the contact window.

## 1. Constants (`orbit/params.py`; `rtl/orbit_params.vh` is generated from it; one test asserts they match)

| Name | Value | Meaning |
|---|---|---|
| `FRAME_W` | 128 | pixels per row |
| `FRAME_H` | 128 | rows per frame |
| `FRAME_BYTES` | 16384 | 8-bit grayscale; also the bytes debited per transmitted frame |
| `PIXELS_PER_CYCLE` | 8 | kernel width (RTL parameter; not on the wire) |
| `QUEUE_DEPTH` | 32 | priority-queue cells; `queue_limit` ≤ this |
| `CLOUD_THRESHOLD` | 200 | power-on default; runtime via CONFIG_SET |
| `CHANGE_THRESHOLD` | 16 | power-on default; runtime via CONFIG_SET |
| `SHARP_SHIFT` | 5 | power-on default; runtime via CONFIG_SET (tuned at the corpus gate) |
| `W_CLEAR` | 21845 | power-on default weight |
| `W_SHARP` | 21845 | power-on default weight |
| `W_CHANGE` | 21845 | power-on default weight |
| `CLK_HZ` | 100_000_000 | |
| `BAUD` | 115200 | |
| `HEARTBEAT_MS` | 1000 | Both directions |
| `LINK_TIMEOUT_MS` | 3000 | Node clears `LINK_OK` |
| `POWER_PERIOD_MS` | 250 | POWER telemetry rate |
| `ORCH_ID` | 0xFF | Orchestrator's sender id |
| `MAX_FRAME` | 253 | Bytes pre-COBS, invariant (§2.3) |
| `INA219_ADDR` | 0x40 | 7-bit I2C address (A0=A1=GND) |
| `INA219_SHUNT_OHM` | 0.1 | Breakout default; Python-side conversion only |

Ground-only (never on the wire): `WINDOW_DURATION_S`, `LINK_RATE_BPS`, `STARVATION_N` (§6).

## 2. Framing

### 2.1 Frame (pre-COBS)
```
off  size  field
0    1     type
1    N     payload (fixed N per type)
1+N  2     crc16 over bytes [0,1+N), little-endian
```

### 2.2 CRC
CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xorout.
Check: `crc16(b"123456789") == 0x29B1`.
```python
def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc
```

### 2.3 COBS
Standard COBS; encoded frame contains no 0x00; one 0x00 delimiter appended.
Decoder: read code `c` (1..255), copy `c−1` bytes, emit 0x00 if `c != 0xFF`
and more bytes remain before the delimiter.
**Invariant:** every frame ≤ 253 bytes pre-COBS (largest is FRAME_INGEST / REF_FRAME_SET
at 134), so COBS overhead is exactly one byte, code 0xFF never occurs, RTL omits that case.
Wire length = pre-COBS + 2. Node→orchestrator frames are additionally ≤ 63 bytes pre-COBS
(the RTL transmit framer's index width); `orbit/protocol/messages.py` asserts both.

### 2.4 Direction, timing, flow
- O→N: FRAME_INGEST, CONFIG_SET, REF_FRAME_SET, BENCH_RUN, STATUS_QUERY, GRANT, HEARTBEAT.
- N→O: STATUS_REPLY, FRAME_SCORED, BENCH_DONE, TX_FRAME, TX_DONE, POWER, HEARTBEAT.
- No flow control. Node parses at line rate. A frame row (137 wire bytes) takes ~11.9 ms
  at 115200 baud; a whole frame ~1.5 s. That is the *capture* rate of this stand-in camera,
  not the kernel rate (§5.2) and not the downlink rate (§6).
- Node TX FIFO full → the node stalls emission (`TX_STALLED`), never drops.
- Each side sends one 0x00 on link open so the peer starts on a boundary.

## 3. Error handling (identical on both endpoints)

| Condition | Action |
|---|---|
| CRC mismatch | drop, `crc_errors++` |
| Unknown type | drop, `unknown_type++` |
| Length ≠ type's length | drop, `len_errors++` |
| Decoded frame < 3 bytes | drop silently (normal after desync / link open) |
| RX overflow (node) | discard to next 0x00, `rx_overflow++` |
| Mid-frame desync | partial frame fails length/CRC, dropped; next 0x00 resyncs |
| FRAME_INGEST / REF_FRAME_SET / GRANT / BENCH_RUN while BUSY (§5.6) | drop, `busy_drops++` |
| FRAME_INGEST / REF_FRAME_SET with `row ≥ FRAME_H` | drop, `busy_drops++` |
| CONFIG_SET field out of range | reject whole message, `busy_drops++`, `CONFIG_VALID=0`, old config kept |

Counters u16, wrap, reset only by power cycle.

## 4. Messages

<!-- generated: messages begin -->
| ID | Name | Dir | Payload | Pre-COBS | Wire |
|---|---|---|---|---|---|
| 0x01 | FRAME_INGEST | O→N | 131 | 134 | 136 |
| 0x02 | CONFIG_SET | O→N | 10 | 13 | 15 |
| 0x03 | REF_FRAME_SET | O→N | 131 | 134 | 136 |
| 0x04 | BENCH_RUN | O→N | 4 | 7 | 9 |
| 0x10 | STATUS_QUERY | O→N | 0 | 3 | 5 |
| 0x11 | STATUS_REPLY | N→O | 30 | 33 | 35 |
| 0x12 | FRAME_SCORED | N→O | 14 | 17 | 19 |
| 0x13 | BENCH_DONE | N→O | 9 | 12 | 14 |
| 0x20 | GRANT | O→N | 6 | 9 | 11 |
| 0x21 | TX_FRAME | N→O | 11 | 14 | 16 |
| 0x22 | TX_DONE | N→O | 10 | 13 | 15 |
| 0x40 | HEARTBEAT | both | 7 | 10 | 12 |
| 0x41 | POWER | N→O | 10 | 13 | 15 |

Types: `u8/u16/u32`, `i16`, `u8[128]` (128 bytes). Reserved, not implemented: 0x30 RELAY_CMD, 0x31 RELAY_DATA (stretch goal; decode as `unknown_type`).

### 4.1 0x01 FRAME_INGEST
One row of a captured frame into the frame buffer. Stands in for the camera.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 2 | u16 | frame_id | ground-assigned id; latched from every row, used when row 127 arrives |
| 2 | 1 | u8 | row | 0..FRAME_H−1; ≥ FRAME_H is rejected (`busy_drops++`) |
| 3 | 128 | u8[128] | pixels | 8-bit grayscale, x = 0..127 left to right |

Example — frame_id=7, row=127, pixels=…:
```
payload : 07 00 7f 00 02 04 06 08 0a 0c 0e 10 12 14 16 18 1a 1c 1e 20 22 24 26 28 2a 2c 2e 30 32 34 36 38 3a 3c 3e 40 42 44 46 48 4a 4c 4e 50 52 54 56 58 5a 5c 5e 60 62 64 66 68 6a 6c 6e 70 72 74 76 78 7a 7c 7e 80 82 84 86 88 8a 8c 8e 90 92 94 96 98 9a 9c 9e a0 a2 a4 a6 a8 aa ac ae b0 b2 b4 b6 b8 ba bc be c0 c2 c4 c6 c8 ca cc ce d0 d2 d4 d6 d8 da dc de e0 e2 e4 e6 e8 ea ec ee f0 f2 f4 f6 f8 fa fc fe
crc     : 0x7E10 → 10 7e
wire    : 03 01 07 02 7f 82 02 04 06 08 0a 0c 0e 10 12 14 16 18 1a 1c 1e 20 22 24 26 28 2a 2c 2e 30 32 34 36 38 3a 3c 3e 40 42 44 46 48 4a 4c 4e 50 52 54 56 58 5a 5c 5e 60 62 64 66 68 6a 6c 6e 70 72 74 76 78 7a 7c 7e 80 82 84 86 88 8a 8c 8e 90 92 94 96 98 9a 9c 9e a0 a2 a4 a6 a8 aa ac ae b0 b2 b4 b6 b8 ba bc be c0 c2 c4 c6 c8 ca cc ce d0 d2 d4 d6 d8 da dc de e0 e2 e4 e6 e8 ea ec ee f0 f2 f4 f6 f8 fa fc fe 10 7e 00
```

### 4.2 0x02 CONFIG_SET
Scoring weights, thresholds and queue limit. Rejected as a whole if any field is out of range (STATUS `CONFIG_VALID` cleared, old config kept). Does not clear the queue: send it before ingesting. Node answers with STATUS_REPLY.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 2 | u16 | w_clear | score = sat16((w_clear·clear + w_sharp·sharp + w_change·change) >> 16) |
| 2 | 2 | u16 | w_sharp |  |
| 4 | 2 | u16 | w_change |  |
| 6 | 1 | u8 | cloud_thr | pixel > cloud_thr counts as cloud |
| 7 | 1 | u8 | change_thr | abs(pixel − ref) > change_thr counts as changed |
| 8 | 1 | u8 | sharp_shift | 0..SHARP_SHIFT_MAX; sharp = sat16(sobel_sum >> sharp_shift) |
| 9 | 1 | u8 | queue_limit | 1..QUEUE_DEPTH cells in use |

Example — w_clear=21845, w_sharp=21845, w_change=21845, cloud_thr=200, change_thr=16, sharp_shift=6, queue_limit=32:
```
payload : 55 55 55 55 55 55 c8 10 06 20
crc     : 0xC571 → 71 c5
wire    : 0e 02 55 55 55 55 55 55 c8 10 06 20 71 c5 00
```

### 4.3 0x03 REF_FRAME_SET
One row of the change-detection reference into the reference buffer. Row 127 sets STATUS `REF_LOADED`. The buffer is zero at power-on, so scores before the first reference count nearly every pixel as changed; the ground always sends one first.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 2 | u16 | frame_id | ground-assigned id; latched from every row, used when row 127 arrives |
| 2 | 1 | u8 | row | 0..FRAME_H−1; ≥ FRAME_H is rejected (`busy_drops++`) |
| 3 | 128 | u8[128] | pixels | 8-bit grayscale, x = 0..127 left to right |

Example — frame_id=0, row=0, pixels=…:
```
payload : 00 00 00 7f 7e 7d 7c 7b 7a 79 78 77 76 75 74 73 72 71 70 6f 6e 6d 6c 6b 6a 69 68 67 66 65 64 63 62 61 60 5f 5e 5d 5c 5b 5a 59 58 57 56 55 54 53 52 51 50 4f 4e 4d 4c 4b 4a 49 48 47 46 45 44 43 42 41 40 3f 3e 3d 3c 3b 3a 39 38 37 36 35 34 33 32 31 30 2f 2e 2d 2c 2b 2a 29 28 27 26 25 24 23 22 21 20 1f 1e 1d 1c 1b 1a 19 18 17 16 15 14 13 12 11 10 0f 0e 0d 0c 0b 0a 09 08 07 06 05 04 03 02 01 00
crc     : 0x3EAE → ae 3e
wire    : 02 03 01 01 80 7f 7e 7d 7c 7b 7a 79 78 77 76 75 74 73 72 71 70 6f 6e 6d 6c 6b 6a 69 68 67 66 65 64 63 62 61 60 5f 5e 5d 5c 5b 5a 59 58 57 56 55 54 53 52 51 50 4f 4e 4d 4c 4b 4a 49 48 47 46 45 44 43 42 41 40 3f 3e 3d 3c 3b 3a 39 38 37 36 35 34 33 32 31 30 2f 2e 2d 2c 2b 2a 29 28 27 26 25 24 23 22 21 20 1f 1e 1d 1c 1b 1a 19 18 17 16 15 14 13 12 11 10 0f 0e 0d 0c 0b 0a 09 08 07 06 05 04 03 02 01 03 ae 3e 00
```

### 4.4 0x04 BENCH_RUN
Score the resident frame buffer `iterations` times without touching the queue or emitting FRAME_SCORED, then BENCH_DONE. This is how energy per frame is measured with the UART idle: the kernel is fixed-function, so work per frame does not depend on content. Node is BUSY meanwhile (rows and grants → `busy_drops++`).

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 4 | u32 | iterations | 0 → immediate BENCH_DONE(0, 0) |

Example — iterations=1000:
```
payload : e8 03 00 00
crc     : 0xC073 → 73 c0
wire    : 04 04 e8 03 01 03 73 c0 00
```

### 4.5 0x10 STATUS_QUERY
Empty payload. Node answers with STATUS_REPLY.

No payload.

Example:
```
payload : (empty)
crc     : 0xF3C1 → c1 f3
wire    : 04 10 c1 f3 00
```

### 4.6 0x11 STATUS_REPLY
Sent on STATUS_QUERY, after CONFIG_SET, and with every node HEARTBEAT.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 1 | u8 | node_id |  |
| 1 | 1 | u8 | state | 0 IDLE, 1 SCORING, 2 BENCH |
| 2 | 1 | u8 | flags | bit0 HAS_DATA, bit1 LINK_OK, bit2 CONFIG_VALID, bit3 REF_LOADED, bit4 BUSY, bit5 TX_STALLED, bit6 INA219_PRESENT |
| 3 | 1 | u8 | queue_depth | entries in the queue |
| 4 | 2 | u16 | top_score | head of queue; 0 when empty |
| 6 | 2 | u16 | top_frame_id | 0xFFFF when empty |
| 8 | 2 | u16 | frames_scored | wraps |
| 10 | 2 | u16 | frames_evicted | wraps |
| 12 | 2 | u16 | frames_sent | wraps |
| 14 | 2 | u16 | rows_rx | FRAME_INGEST + REF_FRAME_SET rows accepted; wraps |
| 16 | 2 | u16 | busy_drops | rows/grants/bench dropped while BUSY, bad rows, CONFIG_SET rejects |
| 18 | 2 | u16 | crc_errors | §3 |
| 20 | 2 | u16 | len_errors | §3 |
| 22 | 2 | u16 | unknown_type | §3 |
| 24 | 2 | u16 | rx_overflow | §3 |
| 26 | 4 | u32 | cycles_last_frame | kernel clocks for the last scoring pass; golden model reports 0 |

Example — node_id=0, state=0, flags=15, queue_depth=3, top_score=51234, top_frame_id=7, frames_scored=12, frames_evicted=0, frames_sent=9, rows_rx=1664, busy_drops=0, crc_errors=0, len_errors=0, unknown_type=0, rx_overflow=0, cycles_last_frame=2051:
```
payload : 00 00 0f 03 22 c8 07 00 0c 00 00 00 09 00 80 06 00 00 00 00 00 00 00 00 00 00 03 08 00 00
crc     : 0x7209 → 09 72
wire    : 02 11 01 06 0f 03 22 c8 07 02 0c 01 01 02 09 03 80 06 01 01 01 01 01 01 01 01 01 03 03 08 01 03 09 72 00
```

### 4.7 0x12 FRAME_SCORED
Emitted once per scored frame (row 127 of FRAME_INGEST), after the queue insert. Carries the three component metrics so the ground can show them and check the board against the golden model frame by frame.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 1 | u8 | node_id |  |
| 1 | 2 | u16 | frame_id |  |
| 3 | 2 | u16 | clear | sat16((FRAME_BYTES − cloud_pixels) << 2) |
| 5 | 2 | u16 | sharp | sat16(sobel_sum >> sharp_shift) |
| 7 | 2 | u16 | change | sat16(changed_pixels << 2) |
| 9 | 2 | u16 | score | composite, §5.2 |
| 11 | 2 | u16 | evicted_id | frame lost by this insert (tail, or this frame), else 0xFFFF |
| 13 | 1 | u8 | queue_depth | after the insert |

Example — node_id=0, frame_id=7, clear=61440, sharp=30000, change=8192, score=33210, evicted_id=65535, queue_depth=3:
```
payload : 00 07 00 00 f0 30 75 00 20 ba 81 ff ff 03
crc     : 0xBA76 → 76 ba
wire    : 02 12 02 07 01 04 f0 30 75 09 20 ba 81 ff ff 03 76 ba 00
```

### 4.8 0x13 BENCH_DONE
Answer to BENCH_RUN.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 1 | u8 | node_id |  |
| 1 | 4 | u32 | iterations | as requested |
| 5 | 4 | u32 | cycles | total kernel clocks; golden model reports 0 |

Example — node_id=0, iterations=1000, cycles=2051000:
```
payload : 00 e8 03 00 00 b8 4b 1f 00
crc     : 0xF802 → 02 f8
wire    : 02 13 03 e8 03 01 04 b8 4b 1f 03 02 f8 00
```

### 4.9 0x20 GRANT
One downlink slot. If the node has data and FRAME_BYTES ≤ budget_bytes it pops its head and answers TX_FRAME then TX_DONE; otherwise TX_DONE alone with bytes_consumed 0.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 2 | u16 | slot_id | echoed in TX_FRAME / TX_DONE |
| 2 | 4 | u32 | budget_bytes | remaining window capacity |

Example — slot_id=42, budget_bytes=4194304:
```
payload : 2a 00 00 00 40 00
crc     : 0x21FE → fe 21
wire    : 03 20 2a 01 01 02 40 03 fe 21 00
```

### 4.10 0x21 TX_FRAME
Accounting record of a transmitted frame. No pixels cross this link: the ground holds the corpus and debits `byte_count` from the modelled window (§6).

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 1 | u8 | node_id |  |
| 1 | 2 | u16 | slot_id |  |
| 3 | 2 | u16 | frame_id | popped head |
| 5 | 2 | u16 | score | its score |
| 7 | 4 | u32 | byte_count | FRAME_BYTES |

Example — node_id=0, slot_id=42, frame_id=7, score=51234, byte_count=16384:
```
payload : 00 2a 00 07 00 22 c8 00 40 00 00
crc     : 0x1188 → 88 11
wire    : 02 21 02 2a 02 07 03 22 c8 02 40 01 03 88 11 00
```

### 4.11 0x22 TX_DONE
Ends every GRANT.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 1 | u8 | node_id |  |
| 1 | 2 | u16 | slot_id |  |
| 3 | 4 | u32 | bytes_consumed | FRAME_BYTES or 0 |
| 7 | 2 | u16 | new_top_score | head after the pop; 0 when empty |
| 9 | 1 | u8 | flags | bit0 HAS_DATA |

Example — node_id=0, slot_id=42, bytes_consumed=16384, new_top_score=48001, flags=1:
```
payload : 00 2a 00 00 40 00 00 81 bb 01
crc     : 0x220E → 0e 22
wire    : 02 22 02 2a 01 02 40 01 06 81 bb 01 0e 22 00
```

### 4.12 0x40 HEARTBEAT
Every HEARTBEAT_MS in both directions. The node follows its own with STATUS_REPLY and clears LINK_OK after LINK_TIMEOUT_MS without one from the ground.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 1 | u8 | sender | node id, or ORCH_ID |
| 1 | 2 | u16 | seq | wraps |
| 3 | 4 | u32 | uptime_ms | wraps |

Example — sender=255, seq=42, uptime_ms=123456:
```
payload : ff 2a 00 40 e2 01 00
crc     : 0x4269 → 69 42
wire    : 04 40 ff 2a 04 40 e2 01 03 69 42 00
```

### 4.13 0x41 POWER
INA219 telemetry every POWER_PERIOD_MS; raw registers, converted on the ground (orbit/bench/ina219.py). `flags` bit0 VALID is 0 when no INA219 answered.

| off | size | type | field | notes |
|---|---|---|---|---|
| 0 | 1 | u8 | node_id |  |
| 1 | 1 | u8 | flags | bit0 VALID |
| 2 | 2 | u16 | bus_raw | INA219 bus register |
| 4 | 2 | i16 | shunt_raw | INA219 shunt register |
| 6 | 4 | u32 | uptime_ms |  |

Example — node_id=0, flags=1, bus_raw=9800, shunt_raw=4210, uptime_ms=5000:
```
payload : 00 01 48 26 72 10 88 13 00 00
crc     : 0x0C27 → 27 0c
wire    : 02 41 08 01 48 26 72 10 88 13 01 03 27 0c 00
```

<!-- generated: messages end -->

## 5. Node semantics (normative for RTL and golden model)

### 5.1 Buffers and ingest
Two `FRAME_BYTES` buffers, *frame* and *ref*, both zero at power-on. FRAME_INGEST writes
row `row` of *frame* and latches `frame_id`; REF_FRAME_SET writes row `row` of *ref*.
`rows_rx++` per accepted row. Rows may arrive in any order; a missing row leaves that
buffer row unchanged (the ground sends rows in order, so a dropped row is visible as a
`rows_rx` gap). When `row == FRAME_H−1` of FRAME_INGEST is accepted the node enters
SCORING, runs the kernel once over *frame* against *ref* (§5.2), inserts into the queue
(§5.3), emits FRAME_SCORED, `frames_scored++`, returns to IDLE. When `row == FRAME_H−1`
of REF_FRAME_SET is accepted, `REF_LOADED` is set.

### 5.2 Kernel — the integer contract
All quantities are unsigned integers; `sat16(x) = min(x, 65535)`; `>>` is a plain shift.
Pixels `p[y][x]`, reference `r[y][x]`, `0 ≤ x,y < 128`.
```
cloud_px   = count of (p > cloud_thr)                       over all 16384 pixels
changed_px = count of (|p − r| > change_thr)                over all 16384 pixels
Gx = (p[y−1][x+1] − p[y−1][x−1]) + 2·(p[y][x+1] − p[y][x−1]) + (p[y+1][x+1] − p[y+1][x−1])
Gy = (p[y+1][x−1] − p[y−1][x−1]) + 2·(p[y+1][x] − p[y−1][x]) + (p[y+1][x+1] − p[y−1][x+1])
sobel_sum  = Σ (|Gx| + |Gy|)   over the interior 1 ≤ x,y ≤ 126 only (border pixels skipped)

clear  = sat16((16384 − cloud_px) << 2)
sharp  = sat16(sobel_sum >> sharp_shift)
change = sat16(changed_px << 2)
score  = sat16((w_clear·clear + w_sharp·sharp + w_change·change) >> 16)
```
Widths: `|Gx|+|Gy| ≤ 2040` per pixel, `sobel_sum ≤ 15876·2040 < 2^25`; the weighted sum
`< 3·2^32 < 2^34`. Sobel uses no multipliers (coefficients ±1, ±2 → add/subtract/shift);
the only multiplies are the three per-frame weight products. The kernel result is a pure
function of the two buffers and the config: the same bytes always give the same score, on
the board and in `orbit/golden/score.py`.

### 5.3 Priority queue
`queue_limit` cells (CONFIG_SET; power-on `QUEUE_DEPTH`), each `{score u16, frame_id u16}`,
ordered by score descending; among equal scores the earlier insert is nearer the head.
Insert `{score, id}`:
- not full → insert after every entry with `score' ≥ score`; `evicted_id = 0xFFFF`.
- full and `score > tail.score` → tail is dropped, then insert as above; `evicted_id = tail.frame_id`, `frames_evicted++`.
- full and `score ≤ tail.score` → nothing stored; `evicted_id = frame_id`, `frames_evicted++`.

`HAS_DATA` = queue non-empty. `top_score`/`top_frame_id` = head, or `0`/`0xFFFF` when empty.
The queue holds ids only: the pixels of a queued frame are not kept (the ground has them).

### 5.4 GRANT
On GRANT `{slot_id, budget_bytes}` in IDLE:
- if `HAS_DATA` and `FRAME_BYTES ≤ budget_bytes`: pop the head; emit
  TX_FRAME `{slot_id, frame_id, score, FRAME_BYTES}` then TX_DONE `{slot_id, FRAME_BYTES, new top_score, HAS_DATA}`; `frames_sent++`.
- else: emit TX_DONE `{slot_id, 0, top_score, HAS_DATA}`.

### 5.5 CONFIG_SET
Valid iff `1 ≤ queue_limit ≤ QUEUE_DEPTH` and `sharp_shift ≤ SHARP_SHIFT_MAX`. Valid →
all seven fields replace the current config, `CONFIG_VALID=1`. Invalid → §3. Either way
STATUS_REPLY follows. Power-on config is the §1 defaults with `CONFIG_VALID=1`. Lowering
`queue_limit` below the current depth drops entries from the tail (`frames_evicted++` each).

### 5.6 BENCH_RUN and BUSY
BENCH_RUN `{n}`: `n = 0` → BENCH_DONE `{0, 0}` at once. Else state BENCH (`BUSY`), the
kernel runs `n` times over the resident buffers with no queue insert and no FRAME_SCORED,
then BENCH_DONE `{n, total cycles}`, state IDLE. SCORING is also `BUSY` (2048 clocks at
`PIXELS_PER_CYCLE = 8`, i.e. shorter than one UART byte, so ingest never sees it).

### 5.7 Timers
Every `HEARTBEAT_MS`: HEARTBEAT then STATUS_REPLY. Every `POWER_PERIOD_MS`: POWER.
`LINK_OK` set on any orchestrator HEARTBEAT, cleared `LINK_TIMEOUT_MS` after the last one.

### 5.8 Emission order
FRAME_INGEST row 127 → FRAME_SCORED. GRANT → TX_FRAME, TX_DONE (or TX_DONE alone).
CONFIG_SET → STATUS_REPLY. STATUS_QUERY → STATUS_REPLY. BENCH_RUN → BENCH_DONE.
Heartbeat pair and POWER may interleave between, never inside, these sequences.

### 5.9 Cycle counters
`cycles_last_frame` and BENCH_DONE.`cycles` count kernel clocks on the board. The golden
model reports 0 for both; equivalence tests compare every other field.

## 6. Orchestrator (ground) — the modelled contact window

Both sides hold the corpus. Per satellite the ground streams FRAME_INGEST rows from that
satellite's deterministic sequence at the configured capture rate, having first sent
CONFIG_SET and a REF_FRAME_SET. A contact window has `capacity_bytes = WINDOW_DURATION_S ×
LINK_RATE_BPS / 8`. Per slot while capacity ≥ `FRAME_BYTES`:

1. STATUS_QUERY every reachable node (or use the `new_top_score` from its last TX_DONE).
2. Candidates = nodes with `HAS_DATA`. Winner = highest `top_score`; ties → lowest node id.
   Starvation guard: if the same node has won the last `STARVATION_N` slots and another
   candidate exists, the best *other* candidate wins instead.
3. GRANT `{slot_id, remaining capacity}` to the winner; wait for TX_DONE; debit
   `bytes_consumed`. Record `{slot, node, frame_id, score, reason}` for the dashboard.

Additional satellites beyond the two boards are simulated in-process on the same golden
model and labelled as simulated everywhere they appear. The unfiltered baseline (FIFO)
runs the same window with grants in capture order and no scoring; the difference in
summed delivered score is the headline chart.
