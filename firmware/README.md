# Orbit ESP32-S3 satellite firmware

One binary for every satellite; identity is a build flag. Speaks the v1 wire protocol
(`orbit/protocol/messages.py`) on the UDP multicast bus, and behaves as
`orbit/sim/satellite.py` does — that file is the executable spec for this one.

## Board configuration

ESP32-S3-DevKitC-1, **8 MB flash, 8 MB PSRAM (OPI)**. Verified on the board with
`esptool flash-id` and `ESP.getPsramSize()`, not assumed:

```
esp32:esp32:esp32s3:CDCOnBoot=cdc,PartitionScheme=default_8MB,FlashSize=8M,PSRAM=opi
```

| option | why |
|---|---|
| `CDCOnBoot=cdc` | this board is **native USB** (`303a:1001`), not a UART bridge. Without it `Serial` goes to physical UART0 pins and the board looks dead. |
| `PartitionScheme=default_8MB` | 3 MB app / **1.5 MB SPIFFS** at `0x670000`. The image set needs it. |
| `FlashSize=8M` | the chip has 8 MB; the 4 MB default puts the filesystem at the wrong offset. |
| `PSRAM=opi` | embedded PSRAM is **octal**. With `PSRAM=enabled` (quad) it reports `PSRAM chip is not connected` and the 128 KB frame pool falls back to internal RAM. |

> **Never pass `--build-property build.extra_flags=...`.** For esp32s3 that property
> carries `-DARDUINO_USB_MODE` and `-DARDUINO_USB_CDC_ON_BOOT`; overriding it silently
> disables USB serial. Use `compiler.cpp.extra_flags`, which is empty by default.

## Rebuild and reflash

```sh
export PATH=$HOME/.local/bin:$PATH
FQBN="esp32:esp32:esp32s3:CDCOnBoot=cdc,PartitionScheme=default_8MB,FlashSize=8M,PSRAM=opi"

# firmware (satellite B)
arduino-cli compile -b "$FQBN" --build-property "compiler.cpp.extra_flags=-DSAT_ID=b" \
  -u -p /dev/ttyACM0 firmware/satellite_esp32

# image set (only when the corpus or frame count changes)
uv run --with numpy python tools/build_fs_image.py --sat b --frames 40
esptool --chip esp32s3 --port /dev/ttyACM0 --baud 921600 \
  write-flash 0x670000 firmware/build/littlefs_b.bin
```

Serial at 115200. The board resets on RTS; `arduino-cli` and `esptool` handle it.

## What changes for satellite C

Three things, nothing else:

| | B | C |
|---|---|---|
| build flag | `-DSAT_ID=b` | `-DSAT_ID=c` |
| hostname / mDNS | `esp32-satellite-b(.local)` | `esp32-satellite-c(.local)` |
| image set | `littlefs_b.bin` | `littlefs_c.bin` |

The hostname is derived from `SAT_ID` at compile time and is what every message carries
in its `from` field, so the flag is the only edit. The two image sets hold **different
frames** — `tools/build_fs_image.py` follows the corpus's own per-node capture sequence
(`corpus.sequence(node_id)`), so B and C see different imagery from one corpus.

```sh
arduino-cli compile -b "$FQBN" --build-property "compiler.cpp.extra_flags=-DSAT_ID=c" \
  -u -p /dev/ttyACM0 firmware/satellite_esp32
uv run --with numpy python tools/build_fs_image.py --sat c --frames 40
esptool --chip esp32s3 --port /dev/ttyACM0 --baud 921600 \
  write-flash 0x670000 firmware/build/littlefs_c.bin
```

## Checks that run without hardware

```sh
# the firmware's scoring kernel, compiled by g++, vs the Python golden model.
# It builds firmware/test/score_host itself, with -Wall -Wextra -Werror, and rebuilds
# whenever a header is newer than the binary. Do not pre-build one by hand: a binary
# built with looser flags looks fresh and the strict build never runs.
uv run --with numpy python tools/check_score_parity.py --n 40

# firmware constants vs orbit/config.py
uv run python tools/check_firmware_sync.py

# the whole firmware suite: canonicaliser/HMAC/Ed25519 vs the ground, message schema, codec,
# faults, satellite decisions -- each host header built with g++/gcc and held to Python
uv run pytest tests/test_crypto_parity.py tests/test_check_message_schema.py \
              tests/test_firmware_codec.py tests/test_registry.py tests/test_firmware_sat.py -q
```

All must pass before flashing. The ground re-scores every frame it receives with the
golden model, so a kernel that drifts shows up as dashboard mismatches rather than as a
build error; the crypto parity is what lets the board's HMAC and Ed25519 verify be trusted
without a logic analyser on the bus.

## Security: provisioning and bring-up

Everything is off until keyed, so a board flashed from `secrets.h.example` behaves exactly like
an unauthenticated one. Four independent switches — `secrets.h` on the board, the environment on
the ground — and the design doc behind them is `docs/security.md`:

| `secrets.h` (board) | ground environment | protects against |
|---|---|---|
| `ORBIT_AUTH_KEY` (hex) | `ORBIT_AUTH_KEY`, `ORBIT_PIN_SENDERS=1` | outsider forgery + tampering (HMAC) |
| `ORBIT_GROUND_NAME` | `ORBIT_GROUND_NAME` (its short hostname) | acting on anyone else's commands |
| `ORBIT_GROUND_PUBKEY` (hex) | `ORBIT_GROUND_SIGN_KEY` (hex seed) | a *leaked shared key* forging a grant (Ed25519) |
| `ORBIT_PAYLOAD_KEY` (hex) | `ORBIT_PAYLOAD_KEY` (hex) | imagery read off the air (AES-256-GCM) |

Generate the keys once, on the ground, and paste the SAME hex into both places (the Ed25519
seed goes to the ground, the public half to every board):

```sh
python3 -c "import secrets; print('ORBIT_AUTH_KEY   ', secrets.token_hex(32))"
python3 -c "import secrets; print('ORBIT_PAYLOAD_KEY', secrets.token_hex(32))"
uv run python -c "import os; from orbit.protocol import ed25519; s=os.urandom(32); \
  print('ORBIT_GROUND_SIGN_KEY', s.hex()); print('ORBIT_GROUND_PUBKEY   ', ed25519.secret_to_public(s).hex())"
```

Run the ground with the matching values in its environment:

```sh
ORBIT_AUTH_KEY=<hex> ORBIT_PIN_SENDERS=1 ORBIT_GROUND_NAME=<hostname> \
ORBIT_GROUND_SIGN_KEY=<hex> ORBIT_PAYLOAD_KEY=<hex> uv run orbit ground
```

Bring it up **one layer at a time**, so a failure is isolated instead of four-way ambiguous:

1. **Nothing keyed.** Flash `secrets.h.example` unchanged. The board captures, bids and
   downlinks; the bus log is plain JSON. This is the baseline nothing else may regress.
2. **HMAC + pin.** Set `ORBIT_AUTH_KEY` + `ORBIT_GROUND_NAME` on the board and the matching
   `ORBIT_AUTH_KEY`/`ORBIT_PIN_SENDERS=1`/`ORBIT_GROUND_NAME` on the ground. Grants still act; a
   datagram from anyone else, or with a wrong tag, is dropped before the JSON parser ever sees it.
3. **Payload encryption.** Add `ORBIT_PAYLOAD_KEY` on both. Frames now arrive `enc:"zlib+gcm"`,
   the ground decrypts and its re-scored digest still matches, and serial prints `payload
   encryption ON: AES-256-GCM`. Mismatch the two keys once and confirm the ground drops the frame
   (bad GCM tag) rather than scoring garbage.
4. **Command signatures.** Add `ORBIT_GROUND_PUBKEY` on the board and `ORBIT_GROUND_SIGN_KEY` on
   the ground. Serial prints `ground command signatures: VERIFIED (Ed25519)` and grants act as
   before. Point the board at the WRONG pubkey once and confirm it now ignores every grant
   (dropped as a bad signature) — proof the check is live, not cosmetic.

`tweetnacl.c` (the Ed25519 verify) is an ordinary source file in the sketch folder, so
`arduino-cli` compiles and links it automatically — nothing extra to install. All four layers
are validated on the host before a board is touched: `uv run pytest tests/test_crypto_parity.py`
builds the same headers with g++/gcc and holds the C verify against the Python signer and the
RFC 8032 vectors.

## WiFi

Credentials live in `firmware/satellite_esp32/secrets.h`, which is **gitignored**. Copy
`secrets.h.example` and fill it in.

Two traps, both of which cost real time here and both of which the firmware now reports
rather than hiding:

1. **The ESP32-S3 radio is 2.4 GHz only.** It cannot see a 5 GHz SSID at all, and the
   failure is indistinguishable from a wrong password unless the scan is printed.
   `HackMIT.2026` is 5 GHz-only (5660 MHz) — the board can never join it.
2. **iOS hotspot SSIDs contain U+2019**, a typographic apostrophe, not ASCII `'`.
   `Ritvik’s iPhone` is `52 69 74 76 69 6b e2 80 99 73 ...`. An ASCII `'` in `secrets.h`
   fails to associate and looks exactly like a bad password.

On failure the firmware retries every 15 s and prints a full 2.4 GHz scan with the target
flagged, so the board recovers on its own once the network appears — no reflash.

## WiFi multicast is lossy, and what the firmware does about it

Measured on the demo network (iPhone Personal Hotspot), counting gaps in this node's own
`seq` numbers:

| configuration | loss |
|---|---|
| 1 copy per datagram | **35.7%** |
| 3 copies, 4 ms apart | 13.3% |
| 4 copies, 20 ms apart | **3.6%** |

WiFi multicast has no link-layer ack or retry — APs send it at the lowest basic rate —
and phone hotspots are especially poor at it. Three mechanisms in the firmware, each
earning its keep:

1. **`WiFi.setSleep(false)`.** With modem sleep on (the Arduino default) the station wakes
   only for DTIM beacons and the AP's buffered *multicast* is dropped. The node keeps
   heartbeating, because its own transmits are unaffected, while silently missing
   `offers_open` and `grant`. It looks exactly like the ground ignoring the node.

2. **`BUS_TX_REPEAT` copies, `BUS_TX_REPEAT_GAP_MS` apart.** Loss is *bursty*, not
   independent, so separation in time matters more than the number of copies — going from
   3 copies 4 ms apart to 4 copies 20 ms apart cut loss by nearly 4x.

3. **`TX_PASSES` whole-sequence repeats for frame chunks.** A single ~500 ms outage took
   out chunks 2–6 together; per-chunk redundancy cannot survive that, because every copy
   lands inside the same outage. Repeating the entire 19-chunk sequence puts each chunk's
   copies seconds apart.

**Receive-side dedup is mandatory, not optional.** The moment anything repeats datagrams,
a duplicate `grant` processed twice restarts the transmission from chunk 0 — the copies
meant to add reliability instead corrupt the transfer. `seenBefore()` keys on
`(sender, seq)`, mirroring the ground's `dedup_window`.

A corollary for anything that talks to this node: **`seq` must be monotonic across the
sender's lifetime.** A test harness that restarts its counter at 0 per run looks like a
replay and is correctly ignored.

On a wired link or a well-behaved AP, set `BUS_TX_REPEAT = 1` and `TX_PASSES = 1`.

## Frame storage

`/frames/NNN.bin` (raw 128×128 8-bit gray, 16384 B), `/refs/RRR.bin`, `/manifest.json`
mapping each frame to its scene reference. Each frame is scored against **its own scene's
reference**, as `corpus.reference_for` does; a single global reference would mismatch the
ground on every frame.

The frame pool is 8 slots × 16 KB, allocated once at boot from PSRAM. There is no growth
path, deliberately: full is full, and admission is a local decision announced on the bus
as an `eviction`.
