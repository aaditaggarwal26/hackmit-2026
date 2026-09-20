// Host harness for the satellite's decision logic, the way score_host.cpp is the harness for
// its scoring kernel: the SAME header the ESP32 builds, compiled with no Arduino present, so
// tests/test_firmware_sat.py can hold it against orbit/sim/satellite.py.
//
//   g++ -O2 -std=c++17 -I../satellite_esp32 sat_host.cpp -o sat_host
//   ./sat_host selftest          # decision tables + digest formatting, exit 0 on success
//   ./sat_host round             # stdin: "<raw> <cloud_px>" per line -> "<part> <cloud_frac>"
//   ./sat_host ack               # stdin: 7 ints per line -> the AckDecision name
//   ./sat_host admit             # stdin: 5 ints per line -> the AdmitDecision name
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include "orbit_sat.h"

static int failures = 0;

static void check(bool cond, const char *what) {
  if (!cond) { printf("FAIL %s\n", what); failures++; }
}

static const char *ack_name(AckDecision d) {
  switch (d) {
    case ACK_POP:       return "POP";
    case ACK_NACK_KEEP: return "NACK_KEEP";
    case ACK_POP_LATE:  return "POP_LATE";
    default:            return "IGNORE";
  }
}

static const char *admit_name(AdmitDecision d) {
  switch (d) {
    case ADMIT_STORE:      return "STORE";
    case ADMIT_EVICT_TAIL: return "EVICT_TAIL";
    default:               return "REJECT";
  }
}

// The cases the firmware has to get right, stated as a table rather than as control flow.
static void selftest() {
  const uint16_t ME = 7, OTHER = 9, NONE = 0xFFFF;

  // --- tx_ack, mirroring FakeSatellite._acked ---------------------------------------
  check(ack_decide(true,  ME, ME, true,  true,  false, 0)  == ACK_POP,       "awaited ok -> pop");
  check(ack_decide(true,  ME, ME, false, true,  false, 0)  == ACK_NACK_KEEP, "awaited nack -> keep");
  // The regression this header exists for: a positive ack for a frame we still hold while we
  // are NOT waiting (we timed out, or the ground moved on and we re-offered it). Holding it
  // forever wedges the slot; the ground already has the frame.
  check(ack_decide(false, 0,  ME, true,  true,  false, 0)  == ACK_POP_LATE,  "late ok, held -> pop");
  check(ack_decide(true,  OTHER, ME, true, true, false, 0) == ACK_POP_LATE,  "ok for other held item -> pop");
  // A negative ack we are not waiting for says nothing we can act on.
  check(ack_decide(false, 0,  ME, false, true,  false, 0)  == ACK_IGNORE,    "late nack -> ignore");
  // Already popped, or never ours.
  check(ack_decide(false, 0,  ME, true,  false, false, 0)  == ACK_IGNORE,    "ok, not held -> ignore");
  // Mid-transmission: the chunks are still going out, the ack cannot be about this transfer.
  check(ack_decide(false, 0,  ME, true,  true,  true,  ME) == ACK_IGNORE,    "ok while sending it -> ignore");
  check(ack_decide(false, 0,  ME, true,  true,  true,  OTHER) == ACK_POP_LATE, "sending something else -> pop");

  // --- admission, mirroring FakeSatellite._admit ------------------------------------
  check(admit_decide(500, 3, 900, OTHER, NONE) == ADMIT_STORE,      "free slot -> store");
  check(admit_decide(500, 0, 400, OTHER, NONE) == ADMIT_EVICT_TAIL, "beats the tail -> evict");
  check(admit_decide(400, 0, 400, OTHER, NONE) == ADMIT_REJECT,     "ties the tail -> reject");
  check(admit_decide(300, 0, 400, OTHER, NONE) == ADMIT_REJECT,     "below the tail -> reject");
  // The in-flight frame is never evicted, however good the newcomer is.
  check(admit_decide(9999, 0, 400, ME, ME) == ADMIT_REJECT,         "tail in flight -> reject");
  check(admit_decide(9999, 0, 400, ME, OTHER) == ADMIT_EVICT_TAIL,  "other in flight -> evict");

  // --- reported numbers -------------------------------------------------------------
  check(orbit_part(0) == 0.0, "part(0)");
  check(orbit_part(65535) == 100.0, "part(max)");
  check(orbit_cloud_frac(0) == 0.0, "cloud_frac(0)");
  check(orbit_cloud_frac(FRAME_BYTES) == 1.0, "cloud_frac(all)");
  // 512/16384 is exactly 0.03125: the tie that half-away-from-zero gets wrong.
  check(orbit_cloud_frac(512) == 0.0312, "cloud_frac ties to even");

  // --- digest formatting ------------------------------------------------------------
  // sha256(b"") -- lowercase, 64 characters, no truncation. The ground compares this string.
  const uint8_t empty[32] = {
    0xe3,0xb0,0xc4,0x42,0x98,0xfc,0x1c,0x14,0x9a,0xfb,0xf4,0xc8,0x99,0x6f,0xb9,0x24,
    0x27,0xae,0x41,0xe4,0x64,0x9b,0x93,0x4c,0xa4,0x95,0x99,0x1b,0x78,0x52,0xb8,0x55};
  char hex[65];
  orbit_hex64(empty, hex);
  check(strlen(hex) == 64, "digest is 64 chars");
  check(strcmp(hex, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") == 0, "digest hex");

  if (failures == 0) printf("OK: 19 decision and formatting checks\n");
}

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "selftest";

  if (!strcmp(mode, "selftest")) {
    selftest();
    return failures ? 1 : 0;
  }
  if (!strcmp(mode, "round")) {
    long raw, px;
    while (scanf("%ld %ld", &raw, &px) == 2)
      printf("%.6f %.6f\n", orbit_part((uint16_t)raw), orbit_cloud_frac((uint32_t)px));
    return 0;
  }
  if (!strcmp(mode, "ack")) {
    long aw, ai, ki, ok, held, txa, txi;
    while (scanf("%ld %ld %ld %ld %ld %ld %ld", &aw, &ai, &ki, &ok, &held, &txa, &txi) == 7)
      printf("%s\n", ack_name(ack_decide(aw, (uint16_t)ai, (uint16_t)ki, ok, held, txa, (uint16_t)txi)));
    return 0;
  }
  if (!strcmp(mode, "admit")) {
    long key, freeslots, tkey, titem, inflight;
    while (scanf("%ld %ld %ld %ld %ld", &key, &freeslots, &tkey, &titem, &inflight) == 5)
      printf("%s\n", admit_name(admit_decide((int32_t)key, (int)freeslots, (int32_t)tkey,
                                             (uint16_t)titem, (uint16_t)inflight)));
    return 0;
  }
  fprintf(stderr, "usage: %s selftest|round|ack|admit\n", argv[0]);
  return 2;
}
