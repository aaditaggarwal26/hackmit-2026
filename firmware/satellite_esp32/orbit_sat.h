// The satellite's DECISIONS, with no Arduino in them.
//
// There is no arduino-cli in this repo's toolchain, so anything that lives only in the .ino
// is untested code. Everything here is a pure function of plain values and compiles under a
// host g++ (firmware/test/sat_host.cpp), which is what makes it checkable against the
// executable spec in orbit/sim/satellite.py. The .ino keeps the I/O: LittleFS, WiFi, UDP,
// ArduinoJson, mbedtls — and calls into this header for every judgement it makes.
//
// The mirror is exact and deliberate:
//   admit_decide  <->  FakeSatellite._admit
//   ack_decide    <->  FakeSatellite._acked
//   orbit_part / orbit_cloud_frac  <->  the rounding Item() applies at capture
#pragma once
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdint.h>
#include "orbit_config.h"
#include "orbit_faults.h"

// ---------------------------------------------------------------- one held frame
//
// Widened past (id, score, time): orbit_score_frame already computes the cloud fraction and
// the three score parts, and the `scored` and `tx_done` messages both have to report them.
// Recomputing would mean re-reading 16 KB from the pool and running the kernel again; keeping
// 16 bytes per slot does not. cloud_px is kept raw rather than as a fraction so the division
// happens once, in one place, at the same precision as the ground's.
struct Item {
  bool     used        = false;
  uint16_t item_id     = 0;
  uint16_t raw_score   = 0;      // 0..65535 as the kernel produces it
  uint32_t captured_ms = 0;
  uint32_t cloud_px    = 0;      // pixels over CLOUD_THRESHOLD
  uint16_t clear       = 0;      // the three score parts, raw 0..65535
  uint16_t sharp       = 0;
  uint16_t change      = 0;
};

// ---------------------------------------------------------------- reported numbers
//
// orbit/sim/satellite.py rounds these before they go on the wire, and the ground compares what
// it re-scores against what was sent, so the ROUNDING is part of the protocol, not cosmetics.
//   parts      round(display(raw), 2)
//   cloud_frac round(cloud_px / FRAME_BYTES, 4)
//
// nearbyint(), not lround(): both round to nearest, but they break a .5 tie differently —
// lround() goes away from zero, nearbyint() (in the default FE_TONEAREST mode, which nothing
// here changes) goes to even, which is what Python's round() does. That is not a hypothetical:
// FRAME_BYTES is 16384, so cloud_px = 512 gives exactly 0.03125, and the two rules disagree in
// the fourth decimal. tests/test_firmware_sat.py checks every raw score and every possible
// cloud pixel count against Python rather than reasoning about which ties exist.
//
// Both return double, not float. The rounded value IS a double; narrowing it to float and
// handing that to ArduinoJson undoes the whole exercise, because the nearest float to 61.04 is
// 61.040000915527... and that is what would be serialised. Two decimals cost nothing to keep.
static inline double orbit_part(uint16_t raw) {
  return nearbyint((double)raw * 10000.0 / 65535.0) / 100.0;
}

static inline double orbit_cloud_frac(uint32_t cloud_px) {
  return nearbyint((double)cloud_px * 10000.0 / (double)FRAME_BYTES) / 10000.0;
}

// ---------------------------------------------------------------- admission
//
// Mirrors FakeSatellite._admit. The pool is fixed and full is full: a newcomer either takes a
// free slot, displaces the worst held frame, or is dropped. The frame being transmitted (or
// waiting for its ack) is never a candidate for eviction — losing it would mean the ground is
// reassembling bytes whose owner has already forgotten them.
enum AdmitDecision {
  ADMIT_STORE,       // a slot was free
  ADMIT_EVICT_TAIL,  // the newcomer beat the worst held frame: that one is lost, permanently
  ADMIT_REJECT,      // the newcomer did not beat the worst held frame (or that one is in flight)
};

// `key` and `tail_key` are queue keys (see queue_key in the .ino); `in_flight` is the item_id
// being transmitted or awaiting an ack, or any id held by nothing (NO_FRAME) when idle.
static inline AdmitDecision admit_decide(int32_t key, int free_slots,
                                         int32_t tail_key, uint16_t tail_item, uint16_t in_flight) {
  if (free_slots > 0) return ADMIT_STORE;
  if (key <= tail_key || tail_item == in_flight) return ADMIT_REJECT;
  return ADMIT_EVICT_TAIL;
}

// ---------------------------------------------------------------- tx_ack
//
// Mirrors FakeSatellite._acked, including the case the firmware used to get wrong.
//
// Protocol rule 6: a positive ack for an item we still hold means the ground HAS the frame,
// whether or not we are still waiting for that ack. We may have stopped waiting (timeout, or an
// offers_open for a later round) and re-offered the item; the ground answers a re-offer it has
// already received with tx_ack{ok} instead of a grant. Returning early there left the frame in
// the pool forever: it kept winning rounds the ground would never grant, and the slot never came
// back. Popping is safe precisely because the ack is positive — no frame is counted twice, and
// nothing is ever popped on an ack we cannot corroborate by still holding the item.
enum AckDecision {
  ACK_IGNORE,     // not for us / not ok / not held: do nothing
  ACK_POP,        // the ack we were waiting for, ok: pop the item
  ACK_NACK_KEEP,  // the ack we were waiting for, not ok: stop waiting, KEEP the frame
  ACK_POP_LATE,   // ok, for an item we still hold, while not awaiting it: pop it, never resend
};

static inline AckDecision ack_decide(bool await_ack, uint16_t await_item,
                                     uint16_t ack_item, bool ok,
                                     bool held, bool tx_active, uint16_t tx_item) {
  if (await_ack && ack_item == await_item) return ok ? ACK_POP : ACK_NACK_KEEP;
  // Not the ack we are waiting for. Mid-transmission the item is not poppable: its chunks are
  // still going out, and the ack cannot be about a transfer that has not finished.
  if (ok && held && !(tx_active && tx_item == ack_item)) return ACK_POP_LATE;
  return ACK_IGNORE;
}

// ---------------------------------------------------------------- digest formatting
//
// tx_done carries the sha256 of the frame bytes as 64 lowercase hex characters; the ground
// reassembles the chunks and compares. Anything that is not exactly that — a truncated digest,
// uppercase, a stand-in — is a frame silently marked bad, so the formatting is pinned here and
// tested rather than written inline at the call site.
static inline void orbit_hex64(const uint8_t digest[32], char out[65]) {
  static const char HEX[] = "0123456789abcdef";
  for (int i = 0; i < 32; i++) {
    out[i * 2]     = HEX[digest[i] >> 4];
    out[i * 2 + 1] = HEX[digest[i] & 0x0F];
  }
  out[64] = '\0';
}

// ---------------------------------------------------------------- faults raised before the radio
//
// The four worst things that can happen to this node all happen in setup() BEFORE there is a
// socket to say so on: LittleFS will not mount, the manifest is missing or corrupt, the frame
// pool will not allocate. Calling sendFault() there writes a datagram into a UDP object that has
// not joined the group yet, so the board that most needs to be heard is the one that is silent.
//
// So boot faults are latched here and drained immediately after the multicast join. Fixed
// capacity and no allocation, deliberately: one of the conditions being reported IS an allocation
// failure, and a reporter that needs the heap to report a heap failure reports nothing. Overflow
// is counted rather than hidden — `dropped()` goes out in the boot fault's detail, so "there were
// more problems than this" is on the bus even when the list was too small to carry them.
struct PendingFaults {
  static const int CAP        = 6;
  static const int DETAIL_LEN = 48;   // one line of serial log; the bus datagram has room to spare

  bool push(int code_id, const char *detail) {
    if (n_ >= CAP) { dropped_++; return false; }
    code_[n_] = code_id;
    // strncpy without the terminator guarantee is the classic way to lose the end of a string;
    // the copy is bounded and terminated explicitly instead.
    const size_t len = strlen(detail) < (size_t)(DETAIL_LEN - 1) ? strlen(detail) : (size_t)(DETAIL_LEN - 1);
    memcpy(detail_[n_], detail, len);
    detail_[n_][len] = '\0';
    n_++;
    return true;
  }

  int         size()          const { return n_; }
  int         dropped()       const { return dropped_; }
  int         code(int i)     const { return (i >= 0 && i < n_) ? code_[i] : 0; }
  const char *detail(int i)   const { return (i >= 0 && i < n_) ? detail_[i] : ""; }

 private:
  int  code_[CAP]              = {0};
  char detail_[CAP][DETAIL_LEN] = {{0}};
  int  n_       = 0;
  int  dropped_ = 0;
};

// A fault is EDGE-triggered (docs/protocol.md): it reports an event, not a level, so it is sent
// once when the condition occurs and never repeated. Two sites can re-enter the same condition
// every capture period — a frame that will not load, a kernel pass that runs long — and left
// alone they would put a fault on the bus every three seconds for the rest of the window, which
// is how a fault channel becomes noise nobody reads. They go through here instead.
//
// One bit per code id, so the state costs a word and cannot be desynchronised from the registry.
struct FaultOnce {
  bool first(int code_id) {
    if (code_id <= 0 || code_id >= 32) return true;    // outside the mask: never suppressed
    const uint32_t bit = 1u << code_id;
    if (seen_ & bit) return false;
    seen_ |= bit;
    return true;
  }

  void forget(int code_id) {                           // the condition cleared: re-arm the edge
    if (code_id > 0 && code_id < 32) seen_ &= ~(1u << code_id);
  }

 private:
  uint32_t seen_ = 0;
};

// The bitmask above is only sound while every registry id fits in it. It does today (ids run to
// ORBIT_FAULT_MAX_ID) and this is what makes growing past 31 a compile error rather than a fault
// that silently repeats forever.
static_assert(ORBIT_FAULT_MAX_ID < 32, "FaultOnce's bitmask cannot hold this many fault codes");
