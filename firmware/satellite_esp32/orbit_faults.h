// GENERATED from orbit/protocol/registry.py -- do not edit by hand.
//   uv run python -m orbit.protocol.registry --write
//
// The integer values are the `code_id` of the `fault` message (orbit/protocol/messages.py).
// They are PERMANENT: a flashed board outlives this checkout, so an id is never renumbered
// and never reused. A retired code keeps its number and simply stops being raised.
//
// tools/check_firmware_sync.py fails the build if this file and the registry disagree.
#pragma once
#include <stdint.h>

enum OrbitFaultCode {
  ORBIT_FAULT_SAT_BOOT              =  1,  // info, satellite, active
  ORBIT_FAULT_LITTLEFS_MOUNT_FAILED =  2,  // fatal, satellite, active
  ORBIT_FAULT_MANIFEST_MISSING      =  3,  // fatal, satellite, active
  ORBIT_FAULT_MANIFEST_CORRUPT      =  4,  // fatal, satellite, active
  ORBIT_FAULT_FRAME_CHECKSUM_FAILED =  5,  // anomaly, ground, reserved
  ORBIT_FAULT_BUFFER_ALLOC_FAILED   =  6,  // fatal, satellite, active
  ORBIT_FAULT_PSRAM_FALLBACK        =  7,  // degraded, satellite, active
  ORBIT_FAULT_GRANT_UNKNOWN_ITEM    =  8,  // anomaly, satellite, active
  ORBIT_FAULT_SCORING_LATENCY_HIGH  =  9,  // degraded, satellite, active
  ORBIT_FAULT_GROUND_DOWN           = 10,  // anomaly, ground, reserved
  ORBIT_FAULT_AUTH_REJECT           = 11,  // anomaly, security, reserved
};

#define ORBIT_FAULT_COUNT  11
#define ORBIT_FAULT_MAX_ID 11

// The registry's severity name for a code. Call sites pass this to sendFault() rather than a
// literal: a hand-typed "warn" is exactly the drift this registry exists to prevent.
// Returns "" for an id this build does not know, which is never a valid severity.
static inline const char *orbit_fault_severity(int code_id) {
  switch (code_id) {
    case ORBIT_FAULT_SAT_BOOT: return "info";
    case ORBIT_FAULT_LITTLEFS_MOUNT_FAILED: return "fatal";
    case ORBIT_FAULT_MANIFEST_MISSING: return "fatal";
    case ORBIT_FAULT_MANIFEST_CORRUPT: return "fatal";
    case ORBIT_FAULT_FRAME_CHECKSUM_FAILED: return "anomaly";
    case ORBIT_FAULT_BUFFER_ALLOC_FAILED: return "fatal";
    case ORBIT_FAULT_PSRAM_FALLBACK: return "degraded";
    case ORBIT_FAULT_GRANT_UNKNOWN_ITEM: return "anomaly";
    case ORBIT_FAULT_SCORING_LATENCY_HIGH: return "degraded";
    case ORBIT_FAULT_GROUND_DOWN: return "anomaly";
    case ORBIT_FAULT_AUTH_REJECT: return "anomaly";
    default: return "";
  }
}

// The human name of a code, for the serial log. Never goes on the wire: the bus carries the
// id, and a receiver that does not know it still has severity and detail.
static inline const char *orbit_fault_slug(int code_id) {
  switch (code_id) {
    case ORBIT_FAULT_SAT_BOOT: return "sat_boot";
    case ORBIT_FAULT_LITTLEFS_MOUNT_FAILED: return "littlefs_mount_failed";
    case ORBIT_FAULT_MANIFEST_MISSING: return "manifest_missing";
    case ORBIT_FAULT_MANIFEST_CORRUPT: return "manifest_corrupt";
    case ORBIT_FAULT_FRAME_CHECKSUM_FAILED: return "frame_checksum_failed";
    case ORBIT_FAULT_BUFFER_ALLOC_FAILED: return "buffer_alloc_failed";
    case ORBIT_FAULT_PSRAM_FALLBACK: return "psram_fallback";
    case ORBIT_FAULT_GRANT_UNKNOWN_ITEM: return "grant_unknown_item";
    case ORBIT_FAULT_SCORING_LATENCY_HIGH: return "scoring_latency_high";
    case ORBIT_FAULT_GROUND_DOWN: return "ground_down";
    case ORBIT_FAULT_AUTH_REJECT: return "auth_reject";
    default: return "?";
  }
}

// True for a code THIS NODE may raise. Ground-sourced and reserved codes are in the enum so
// that their ids stay burned, but the firmware must never put one on the bus.
static inline bool orbit_fault_is_satellite(int code_id) {
  switch (code_id) {
    case ORBIT_FAULT_SAT_BOOT:
    case ORBIT_FAULT_LITTLEFS_MOUNT_FAILED:
    case ORBIT_FAULT_MANIFEST_MISSING:
    case ORBIT_FAULT_MANIFEST_CORRUPT:
    case ORBIT_FAULT_BUFFER_ALLOC_FAILED:
    case ORBIT_FAULT_PSRAM_FALLBACK:
    case ORBIT_FAULT_GRANT_UNKNOWN_ITEM:
    case ORBIT_FAULT_SCORING_LATENCY_HIGH:
      return true;
    default: return false;
  }
}
