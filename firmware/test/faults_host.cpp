// Host harness for the fault registry as the firmware sees it, the way sat_host.cpp is the
// harness for the satellite's decisions: the SAME headers the ESP32 builds, compiled with no
// Arduino present, so tests/test_firmware_faults.py can hold them against
// orbit/protocol/registry.py.
//
// The point of `table` is that nothing here transcribes the registry. The test prints the whole
// id space out of the compiled header and compares it with the Python table, so a header that
// was hand-edited, half-regenerated, or generated from an older registry fails as a data
// mismatch rather than as a compile error nobody would see until flashing day.
//
//   g++ -O2 -std=c++17 -I../satellite_esp32 faults_host.cpp -o faults_host
//   ./faults_host selftest        # PendingFaults + FaultOnce, exit 0 on success
//   ./faults_host table           # "<id> <slug> <severity> <satellite|other>" for 0..MAX_ID+2
#include <cstdio>
#include <cstring>
#include "orbit_faults.h"
#include "orbit_sat.h"

static int failures = 0;

static void check(bool cond, const char *what) {
  if (!cond) { printf("FAIL %s\n", what); failures++; }
}

static void selftest() {
  // --- the boot latch ---------------------------------------------------------------
  PendingFaults p;
  check(p.size() == 0 && p.dropped() == 0, "empty at boot");
  check(p.push(ORBIT_FAULT_LITTLEFS_MOUNT_FAILED, "mount failed"), "first push accepted");
  check(p.push(ORBIT_FAULT_PSRAM_FALLBACK, "internal SRAM"), "second push accepted");
  check(p.size() == 2, "two latched");
  // Order is the order the faults happened: the mount failure explains the ones after it.
  check(p.code(0) == ORBIT_FAULT_LITTLEFS_MOUNT_FAILED, "order kept 0");
  check(p.code(1) == ORBIT_FAULT_PSRAM_FALLBACK, "order kept 1");
  check(strcmp(p.detail(0), "mount failed") == 0, "detail 0");
  check(strcmp(p.detail(1), "internal SRAM") == 0, "detail 1");
  // Out of range reads are answers, not crashes: this drains in a loop on a board with no debugger.
  check(p.code(-1) == 0 && p.code(99) == 0, "out of range code");
  check(p.detail(99)[0] == '\0', "out of range detail");

  // A detail longer than the slot is truncated AND terminated. An unterminated buffer here would
  // be serialised by ArduinoJson as whatever followed it in memory.
  PendingFaults q;
  char big[200];
  memset(big, 'x', sizeof(big));
  big[sizeof(big) - 1] = '\0';
  q.push(ORBIT_FAULT_MANIFEST_CORRUPT, big);
  check((int)strlen(q.detail(0)) == PendingFaults::DETAIL_LEN - 1, "long detail truncated");

  // Overflow counts rather than hides: "there were more problems than this" still reaches the bus.
  PendingFaults r;
  for (int i = 0; i < PendingFaults::CAP; i++) check(r.push(ORBIT_FAULT_SAT_BOOT, "x"), "fits");
  check(!r.push(ORBIT_FAULT_SAT_BOOT, "x"), "overflow refused");
  check(r.size() == PendingFaults::CAP && r.dropped() == 1, "overflow counted");

  // --- the edge trigger -------------------------------------------------------------
  FaultOnce once;
  check(once.first(ORBIT_FAULT_SCORING_LATENCY_HIGH), "first occurrence fires");
  check(!once.first(ORBIT_FAULT_SCORING_LATENCY_HIGH), "second occurrence suppressed");
  check(once.first(ORBIT_FAULT_GRANT_UNKNOWN_ITEM), "a different code is independent");
  once.forget(ORBIT_FAULT_SCORING_LATENCY_HIGH);
  check(once.first(ORBIT_FAULT_SCORING_LATENCY_HIGH), "re-armed after the condition cleared");

  // --- the registry itself ----------------------------------------------------------
  // An unknown id must not look like a valid severity: "" is not one of the four names, so a
  // receiver reading an empty severity knows the sender's table is older than the id it sent.
  check(orbit_fault_severity(0)[0] == '\0', "unknown id has no severity");
  check(orbit_fault_severity(ORBIT_FAULT_MAX_ID + 1)[0] == '\0', "past the end has no severity");
  check(strcmp(orbit_fault_slug(0), "?") == 0, "unknown id slug");
  check(!orbit_fault_is_satellite(0), "unknown id is not ours to raise");
  // Ground-sourced and reserved codes keep their ids but must never leave this node.
  check(!orbit_fault_is_satellite(ORBIT_FAULT_GROUND_DOWN), "ground fault is not satellite-raised");
  check(!orbit_fault_is_satellite(ORBIT_FAULT_AUTH_REJECT), "reserved stub is not satellite-raised");
  check(orbit_fault_is_satellite(ORBIT_FAULT_SAT_BOOT), "boot is satellite-raised");

  if (failures == 0) printf("OK: 24 fault latch, edge and registry checks\n");
}

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "selftest";

  if (!strcmp(mode, "selftest")) {
    selftest();
    return failures ? 1 : 0;
  }
  if (!strcmp(mode, "table")) {
    // Two past the end on purpose: the test checks that ids the registry does not define are
    // reported as unknown rather than as some neighbouring code.
    for (int id = 0; id <= ORBIT_FAULT_MAX_ID + 2; id++)
      printf("%d %s %s %s\n", id, orbit_fault_slug(id), orbit_fault_severity(id),
             orbit_fault_is_satellite(id) ? "satellite" : "other");
    return 0;
  }
  fprintf(stderr, "usage: %s selftest|table\n", argv[0]);
  return 2;
}
