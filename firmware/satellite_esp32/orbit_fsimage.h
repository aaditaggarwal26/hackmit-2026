// Integrity of the flashed LittleFS image, with no Arduino in it.
//
// The board is flashed with `esptool write-flash`, over USB, minutes before a demo. That write
// can come back "OK" and still leave a short or corrupted image: a cable that browned out, a
// partition offset that overlapped, a reset mid-erase. Nothing downstream notices. The frames
// still load, the kernel still scores them, and the satellite bids on garbage — which surfaces
// on the ground as a re-score mismatch, the single hardest failure to read off a dashboard.
//
// So the image carries a checksum per blob (tools/build_fs_image.py writes them into
// /manifest.json) and the firmware checks them before it captures anything.
//
// Like orbit_sat.h, this file is Arduino-free on purpose: the ESP32 builds it and a host g++
// builds the same bytes (firmware/test/fsimage_host.cpp), which is what lets
// tests/test_firmware_fsimage.py prove the CRC agrees with Python over the real corpus. I/O
// stays in the .ino and arrives here as two function pointers.
//
// --------------------------------------------------------------------------- CRC32, not sha256
//
// CRC-32/ISO-HDLC (zlib's, PNG's) detects FLASH CORRUPTION AND TRUNCATED WRITES. It does NOT
// detect TAMPERING: anyone who can rewrite a frame can rewrite its checksum beside it. That is
// the right trade here, because the threat is a bad cable, not an adversary:
//   * sha256 would need mbedtls, which does not exist on the host, so the one thing this file
//     is for -- being compiled and checked by g++ against Python -- would be lost;
//   * 4 bytes per blob instead of 32 keeps the manifest a single small read at boot;
//   * end-to-end integrity against the ground is ALREADY sha256, on the wire, per frame
//     (tx_done, see orbit_hex64 in orbit_sat.h). This layer only has to catch a bad flash.
// A single-bit flip, any burst up to 32 bits, and any truncation all fail a CRC32; the residual
// probability for random corruption is 2^-32.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include "orbit_config.h"

// Manifest layout version. Bumped when /manifest.json gains a field the firmware requires, so a
// board still carrying an older image says so instead of failing 40 checksums in a row. 2 added
// the per-blob CRCs; keep in step with MANIFEST_FMT in tools/build_fs_image.py (checked by
// tests/test_firmware_fsimage.py).
#define ORBIT_MANIFEST_FMT 2

// ---------------------------------------------------------------- CRC-32/ISO-HDLC
//
// Reflected polynomial 0xEDB88320, init 0xFFFFFFFF, final xor 0xFFFFFFFF -- byte for byte what
// Python's zlib.crc32() produces. Those four choices are exactly where a CRC silently disagrees
// across languages, so they are not asserted here, they are checked: the canonical vector
// crc32("123456789") == 0xCBF43926 in the host selftest, and every real frame of the corpus in
// tests/test_firmware_fsimage.py.
//
// Bitwise, with no lookup table on purpose. At 240 MHz this is roughly 1 ms per 16 KB frame, so
// a whole 40-frame image costs tens of milliseconds of CPU -- far below the LittleFS reads it
// runs alongside. A 1 KB table would buy back time that is not the bottleneck.
static inline uint32_t orbit_crc32_update(uint32_t crc, const uint8_t *buf, size_t n) {
  crc = ~crc;
  while (n--) {
    crc ^= (uint32_t)*buf++;
    for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(0u - (crc & 1u)));
  }
  return ~crc;
}

static inline uint32_t orbit_crc32(const uint8_t *buf, size_t n) {
  return orbit_crc32_update(0u, buf, n);
}

// ---------------------------------------------------------------- verdicts
//
// Deliberately unnumbered, and this file does not include orbit_faults.h. The registry
// (orbit/protocol/registry.py) owns every fault id; a second numbering living here is exactly
// the drift it exists to prevent. The mapping belongs at the one call site in the .ino:
//
//   FS_MANIFEST_OLD              -> ORBIT_FAULT_MANIFEST_CORRUPT (4, fatal, satellite)
//   FS_BLOB_MISSING / FS_BLOB_CRC-> ORBIT_FAULT_MANIFEST_CORRUPT (4) FOR NOW.
//
// The registry's ORBIT_FAULT_FRAME_CHECKSUM_FAILED (5) is the natural home for the second one,
// but as generated it is source=ground, status=reserved, and orbit_fault_is_satellite(5) is
// false -- the firmware is forbidden to raise it. It describes the GROUND re-scoring a received
// frame and disagreeing, which is a different event from this one (flash does not match what was
// built). Either code 5 becomes satellite-sourced and active, or the registry gains a satellite
// `flash_image_corrupt`; until then the fatal manifest code carries it, which is at least true:
// the image the manifest describes is not the image on flash.
enum FsVerdict {
  FS_OK,             // blob read and matched
  FS_MANIFEST_OLD,   // the manifest predates per-blob checksums: cannot verify anything (fatal)
  FS_BLOB_MISSING,   // could not read FRAME_BYTES from the blob: absent, or the write was short
  FS_BLOB_CRC,       // read fine, checksum disagrees: the bytes on flash are not the bytes built
};

static inline const char *orbit_fs_verdict_name(FsVerdict v) {
  switch (v) {
    case FS_MANIFEST_OLD:  return "manifest predates per-frame checksums";
    case FS_BLOB_MISSING:  return "missing or short";
    case FS_BLOB_CRC:      return "CHECKSUM MISMATCH";
    default:               return "ok";
  }
}

// Reads FRAME_BYTES from `path` into `dst`, false if it could not. The .ino's loadBlob() has
// exactly this signature and is passed straight in.
typedef bool (*OrbitBlobReader)(const char *path, uint8_t *dst);

// Called once per failure. The device prints it; the host test collects it.
typedef void (*OrbitFsReport)(FsVerdict v, const char *path, uint32_t want, uint32_t got);

// ---------------------------------------------------------------- the boot sweep
//
// Verified AT BOOT, in one pass, rather than lazily as each frame is loaded.
//
// Lazy costs nothing extra -- capture() already has the frame in RAM, so the CRC is free -- and
// that is precisely the problem: with SAT_CAPTURE_PERIOD_S = 3, a corrupt frame 17 is discovered
// 51 s into the run, on a console nobody is watching, with the demo already live. The whole
// point of this file is to catch a bad `esptool write-flash` while the operator is still holding
// the USB cable, so the check has to happen before "[ok  ] online" is printed.
//
// What that costs: one extra read of the whole image. Satellite B's current 40-frame set is 54
// blobs and 867 KiB (tools/build_fs_image.py reports it). The CRC itself is ~2 ms per 16 KB blob
// at 240 MHz, so ~100 ms of CPU; the LittleFS reads alongside it run on the order of 1-4 MB/s,
// putting the total in the few-hundred-millisecond range. That figure is UNMEASURED -- this repo
// has no board -- but it does not need measuring to be affordable, because it is bounded above by
// two things already in the boot path: it is less than one SAT_CAPTURE_PERIOD_S (3 s), and it is
// a small fraction of the WiFi association boot already blocks on for seconds (and retries for up
// to 15). Boot spends no extra RAM either: the caller hands in the .ino's existing `scratch`,
// which is static and idle until the first capture.
//
// Returns the number of bad blobs, or -1 if the manifest itself cannot be trusted. Every blob is
// checked even after the first failure, so one boot tells the operator the whole story rather
// than one line of it.
static inline int orbit_fs_verify(uint32_t fmt, const uint16_t *frame_ref, const uint32_t *frame_crc,
                                  const uint32_t *ref_crc, uint16_t n, uint8_t *scratch,
                                  OrbitBlobReader read_blob, OrbitFsReport report) {
  if (fmt < ORBIT_MANIFEST_FMT) {
    report(FS_MANIFEST_OLD, "/manifest.json", (uint32_t)ORBIT_MANIFEST_FMT, fmt);
    return -1;
  }

  int bad = 0;
  char path[32];
  for (uint16_t i = 0; i < n; i++) {
    snprintf(path, sizeof(path), "/frames/%03u.bin", (unsigned)i);
    if (!read_blob(path, scratch)) {
      report(FS_BLOB_MISSING, path, frame_crc[i], 0u);
      bad++;
    } else {
      const uint32_t got = orbit_crc32(scratch, FRAME_BYTES);
      if (got != frame_crc[i]) { report(FS_BLOB_CRC, path, frame_crc[i], got); bad++; }
    }

    // A reference is shared by every frame of its scene and is read once here. It is NOT
    // optional to check: the change term of every frame in the scene is measured against it, so
    // one rotten reference mis-scores a whole run just as thoroughly as a rotten frame does.
    bool seen = false;
    for (uint16_t j = 0; j < i; j++) if (frame_ref[j] == frame_ref[i]) { seen = true; break; }
    if (seen) continue;
    snprintf(path, sizeof(path), "/refs/%03u.bin", (unsigned)frame_ref[i]);
    if (!read_blob(path, scratch)) {
      report(FS_BLOB_MISSING, path, ref_crc[i], 0u);
      bad++;
    } else {
      const uint32_t got = orbit_crc32(scratch, FRAME_BYTES);
      if (got != ref_crc[i]) { report(FS_BLOB_CRC, path, ref_crc[i], got); bad++; }
    }
  }
  return bad;
}
