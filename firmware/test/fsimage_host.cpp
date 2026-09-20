// Host harness for the flash image's integrity check, the way sat_host.cpp is the harness for
// the satellite's decisions: the SAME header the ESP32 builds, compiled with no Arduino present,
// so tests/test_firmware_fsimage.py can hold its CRC against Python's zlib over the real corpus.
//
// A CRC32 with the wrong polynomial, init, or reflection is the classic silent mismatch -- it
// still looks like a checksum, it just never agrees with the builder. That is what this exists
// to make impossible.
//
//   g++ -O2 -std=c++17 -I../satellite_esp32 fsimage_host.cpp -o fsimage_host
//   ./fsimage_host selftest           # canonical CRC vectors + the manifest gate, exit 0 on ok
//   ./fsimage_host crc                # stdin: raw bytes -> one line, 8 lowercase hex digits
//   ./fsimage_host verify <dir>       # stdin: "<fmt> <n>" then n lines "<ref> <crc> <refcrc>"
//                                     #   (hex), blobs read from <dir>/frames, <dir>/refs.
//                                     #   prints "<verdict>|<path>|<want>|<got>" per failure,
//                                     #   then "bad=<k>".
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "orbit_fsimage.h"

static int failures = 0;

static void check(bool cond, const char *what) {
  if (!cond) { printf("FAIL %s\n", what); failures++; }
}

static uint32_t crc_str(const char *s) {
  return orbit_crc32((const uint8_t *)s, strlen(s));
}

static void selftest() {
  // The one constant that pins all four parameters at once. CRC-32/ISO-HDLC over "123456789"
  // is 0xCBF43926; get the polynomial, the init, the reflection or the final xor wrong and this
  // is some other number. Python: zlib.crc32(b"123456789") == 0xCBF43926.
  check(crc_str("123456789") == 0xCBF43926u, "crc32(\"123456789\") == 0xCBF43926");
  check(orbit_crc32(nullptr, 0) == 0u, "crc32(empty) == 0");
  // A few more zlib.crc32 values, including a byte whose high bit is set (reflection) and an
  // input whose CRC exceeds 2^31 (the range a signed int would mangle).
  check(crc_str("a") == 0xE8B7BE43u, "crc32(\"a\")");
  check(crc_str("orbit") == 0xB4720513u, "crc32(\"orbit\")");
  check(orbit_crc32((const uint8_t *)"\xff", 1) == 0xFF000000u, "crc32(0xff) is above 2^31");
  // Streaming in pieces must equal one shot: the boot sweep reads whole blobs today, but the
  // update form is the part that would change if it ever chunked.
  const char *msg = "123456789";
  uint32_t c = orbit_crc32_update(0u, (const uint8_t *)msg, 4);
  c = orbit_crc32_update(c, (const uint8_t *)msg + 4, 5);
  check(c == 0xCBF43926u, "crc32 streams in pieces");
  // 16384 zero bytes: a blank or erased frame is not crc 0, which is why "all zeros" is caught.
  std::vector<uint8_t> zeros(FRAME_BYTES, 0);
  check(orbit_crc32(zeros.data(), zeros.size()) != 0u, "an erased frame does not check out");

  // The manifest gate: an older image cannot be verified and must say so rather than report
  // every blob as corrupt.
  check(ORBIT_MANIFEST_FMT == 2, "manifest format is 2");
  check(strcmp(orbit_fs_verdict_name(FS_MANIFEST_OLD), "manifest predates per-frame checksums") == 0,
        "manifest verdict text");

  if (failures == 0) printf("OK: 10 checksum and manifest checks\n");
}

// ---------------------------------------------------------------- verify mode

static const char *g_root = ".";

// Stands in for the .ino's loadBlob(): same signature, same contract (false unless exactly
// FRAME_BYTES came back, which is how a truncated write reads).
static bool host_read_blob(const char *path, uint8_t *dst) {
  char full[512];
  snprintf(full, sizeof(full), "%s%s", g_root, path);
  FILE *f = fopen(full, "rb");
  if (!f) return false;
  const size_t n = fread(dst, 1, FRAME_BYTES, f);
  fclose(f);
  return n == FRAME_BYTES;
}

static void host_report(FsVerdict v, const char *path, uint32_t want, uint32_t got) {
  printf("%s|%s|%08x|%08x\n", orbit_fs_verdict_name(v), path, want, got);
}

static int verify_mode() {
  unsigned fmt = 0, n = 0;
  if (scanf("%u %u", &fmt, &n) != 2) { fprintf(stderr, "bad header\n"); return 2; }
  std::vector<uint16_t> refs(n);
  std::vector<uint32_t> fcrc(n), rcrc(n);
  for (unsigned i = 0; i < n; i++) {
    unsigned r = 0, a = 0, b = 0;
    if (scanf("%u %x %x", &r, &a, &b) != 3) { fprintf(stderr, "bad row %u\n", i); return 2; }
    refs[i] = (uint16_t)r;
    fcrc[i] = a;
    rcrc[i] = b;
  }
  std::vector<uint8_t> scratch(FRAME_BYTES);
  const int bad = orbit_fs_verify(fmt, refs.data(), fcrc.data(), rcrc.data(), (uint16_t)n,
                                  scratch.data(), host_read_blob, host_report);
  printf("bad=%d\n", bad);
  return 0;
}

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "selftest";

  if (!strcmp(mode, "selftest")) {
    selftest();
    return failures ? 1 : 0;
  }
  if (!strcmp(mode, "crc")) {
    uint32_t crc = 0;
    uint8_t buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), stdin)) > 0) crc = orbit_crc32_update(crc, buf, n);
    printf("%08x\n", crc);
    return 0;
  }
  if (!strcmp(mode, "verify")) {
    if (argc < 3) { fprintf(stderr, "verify needs a directory\n"); return 2; }
    g_root = argv[2];
    return verify_mode();
  }
  fprintf(stderr, "usage: %s selftest|crc|verify <dir>\n", argv[0]);
  return 2;
}
