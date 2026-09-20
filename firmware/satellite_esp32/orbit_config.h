// Constants shared with orbit/config.py. These are FACTS, identical on every tier —
// frame geometry, kernel defaults, protocol version. If one changes in config.py it
// must change here; tools/check_firmware_sync.py fails the build otherwise.
#pragma once
#include <stdint.h>

#define ORBIT_PROTOCOL_VERSION 1

// --- frames -------------------------------------------------------------------------
static const int      FRAME_W     = 128;
static const int      FRAME_H     = 128;
static const uint32_t FRAME_BYTES = (uint32_t)FRAME_W * FRAME_H;   // 16384, 8-bit gray

// --- scoring kernel defaults (orbit/golden/score.py) --------------------------------
static const uint8_t  CLOUD_THRESHOLD  = 200;
static const uint8_t  CHANGE_THRESHOLD = 16;
static const uint8_t  SHARP_SHIFT      = 5;
static const uint16_t W_CLEAR          = 21845;
static const uint16_t W_SHARP          = 21845;
static const uint16_t W_CHANGE         = 21845;

// --- bus (Settings defaults in orbit/config.py) -------------------------------------
#define MCAST_GROUP      "239.255.42.99"
static const uint16_t MCAST_PORT       = 50000;
static const uint8_t  MCAST_TTL        = 1;
static const size_t   BUS_MAX_DATAGRAM = 1400;
static const size_t   CHUNK_BYTES      = 900;   // 900 raw -> 1200 base64 + envelope

// Redundant transmission of every datagram. WiFi multicast has no link-layer ack or
// retry; an iPhone hotspot measured 35.7% loss here. The ground dedups by (sender, seq),
// so repeats are free of protocol consequence. 1 = no redundancy (wired / good AP).
static const int      BUS_TX_REPEAT        = 4;
static const uint32_t BUS_TX_REPEAT_GAP_MS = 20;  // loss is bursty: separation beats count

// Frame chunks repeat as whole passes rather than per-chunk, so each chunk's copies are
// seconds apart -- longer than the observed ~500 ms loss bursts, which per-chunk
// redundancy cannot survive. Costs TX_PASSES x the airtime for one frame.
static const int      TX_PASSES = 3;

// tx_done is re-sent (identical bytes, so the ground dedups) this often while waiting for
// the ack. Without it, losing tx_done alone strands a frame the ground already holds.
static const uint32_t TX_DONE_RESEND_MS = 600;

// --- satellite ----------------------------------------------------------------------
static const int      BID_WINDOW_N       = 4;   // diagnostic entries below the top
static const uint32_t SAT_HEARTBEAT_MS   = 1000;
static const uint32_t SAT_ACK_TIMEOUT_MS = 3000;
static const float    SAT_CAPTURE_PERIOD_S = 3.0f;

// Buffer slots. 8 x 16384 = 128 KB; allocated once at boot, PSRAM when present.
// Lowered automatically at boot if the allocation cannot be met (reported on the bus).
static const int      SAT_BUFFER_SLOTS = 8;

static const uint16_t U16_MAX_ = 0xFFFF;
