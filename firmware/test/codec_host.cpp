// Host harness for the satellite's payload codec, the way sat_host.cpp is the harness for its
// decision logic: the SAME header the ESP32 builds, compiled with no Arduino present, so
// tests/test_firmware_codec.py can hold it against orbit/protocol/codec.py and against real
// corpus frames.
//
//   g++ -O2 -std=c++17 -I../satellite_esp32 codec_host.cpp -o codec_host -lz
//   ./codec_host selftest        # chunk arithmetic, round trip, raw fallback; exit 0 on success
//   ./codec_host chunks          # stdin: "<payload_bytes> <chunk_bytes>" per line -> the count
//   ./codec_host deflate         # stdin: raw bytes -> stdout: what the firmware would transmit
//   ./codec_host inflate         # stdin: a zlib stream -> stdout: the raw bytes
//   ./codec_host enc             # stdin: raw bytes -> stdout: "zlib" or "raw" (what it chose)
//
// `deflate` and `inflate` are the interop pair: Python deflates and this inflates, this
// deflates and Python inflates. The firmware itself never inflates -- only the ground does --
// so inflate lives here in the harness rather than in the header the board compiles.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include "orbit_codec.h"

static int failures = 0;

static void check(bool cond, const char *what) {
  if (!cond) { printf("FAIL %s\n", what); failures++; }
}

static std::vector<uint8_t> read_stdin() {
  std::vector<uint8_t> out;
  uint8_t buf[8192];
  size_t n;
  while ((n = fread(buf, 1, sizeof(buf), stdin)) > 0) out.insert(out.end(), buf, buf + n);
  return out;
}

// The ground's side of the contract, so the harness can prove the round trip on its own.
static bool inflate_all(const uint8_t *src, size_t src_len, std::vector<uint8_t> &out) {
  z_stream s;
  memset(&s, 0, sizeof(s));
  if (inflateInit2(&s, ORBIT_ZLIB_WBITS) != Z_OK) return false;
  s.next_in = (Bytef *)src;
  s.avail_in = (unsigned)src_len;
  uint8_t buf[8192];
  int rc = Z_OK;
  do {
    s.next_out = (Bytef *)buf;
    s.avail_out = (unsigned)sizeof(buf);
    rc = inflate(&s, Z_NO_FLUSH);
    if (rc != Z_OK && rc != Z_STREAM_END) { inflateEnd(&s); return false; }
    out.insert(out.end(), buf, buf + (sizeof(buf) - s.avail_out));
  } while (rc != Z_STREAM_END);
  inflateEnd(&s);
  return s.avail_in == 0;  // trailing bytes after the stream are a failure, not a curiosity
}

static void selftest() {
  // --- chunk arithmetic, mirroring codec.chunk_count --------------------------------
  check(orbit_chunk_count(0, 900) == 1, "empty payload is still one chunk");
  check(orbit_chunk_count(1, 900) == 1, "one byte, one chunk");
  check(orbit_chunk_count(900, 900) == 1, "exactly one chunk");
  check(orbit_chunk_count(901, 900) == 2, "one byte over");
  check(orbit_chunk_count(FRAME_BYTES, 900) == 19, "a whole raw frame");
  check(orbit_chunk_count(11245, 900) == 13, "a typical compressed frame");

  // --- a compressible frame: deflated, and it inflates back to exactly what went in ---
  std::vector<uint8_t> frame(FRAME_BYTES);
  for (size_t i = 0; i < frame.size(); i++) frame[i] = (uint8_t)((i / 64) & 0x3F);  // banded, like sky
  const size_t n = orbit_tx_prepare(frame.data(), frame.size());
  check(n > 0 && n < frame.size(), "a compressible frame shrinks");
  check(strcmp(orbit_tx_enc(), ORBIT_ENC_ZLIB) == 0, "and is labelled zlib");
  std::vector<uint8_t> back;
  check(inflate_all(orbit_tx_data(), n, back), "the stream inflates");
  check(back.size() == frame.size() && memcmp(back.data(), frame.data(), frame.size()) == 0,
        "lossless: exactly the bytes that went in");

  // --- preparing twice is idempotent: TX_PASSES re-sends chunks and they must match ---
  std::vector<uint8_t> first(orbit_tx_data(), orbit_tx_data() + n);
  const size_t n2 = orbit_tx_prepare(frame.data(), frame.size());
  check(n2 == n && memcmp(first.data(), orbit_tx_data(), n) == 0, "same frame, same bytes");

  // --- an incompressible frame falls back to raw rather than paying for a wrapper -----
  std::vector<uint8_t> noise(FRAME_BYTES);
  uint32_t x = 123456789u;
  for (size_t i = 0; i < noise.size(); i++) {  // xorshift32: no structure for deflate to find
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    noise[i] = (uint8_t)x;
  }
  const size_t m = orbit_tx_prepare(noise.data(), noise.size());
  check(m == noise.size(), "incompressible: the payload is the frame");
  check(strcmp(orbit_tx_enc(), ORBIT_ENC_RAW) == 0, "and is labelled raw");
  check(memcmp(orbit_tx_data(), noise.data(), noise.size()) == 0, "and it is the frame, byte for byte");

  // --- nothing to send ---------------------------------------------------------------
  check(orbit_tx_prepare(nullptr, FRAME_BYTES) == 0, "no frame -> nothing to send");
  check(orbit_tx_prepare(frame.data(), 0) == 0, "empty frame -> nothing to send");

  if (failures == 0) printf("OK: 16 codec checks\n");
}

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "selftest";

  if (!strcmp(mode, "selftest")) {
    selftest();
    return failures ? 1 : 0;
  }
  if (!strcmp(mode, "chunks")) {
    long payload, chunk;
    while (scanf("%ld %ld", &payload, &chunk) == 2)
      printf("%u\n", (unsigned)orbit_chunk_count((size_t)payload, (size_t)chunk));
    return 0;
  }
  if (!strcmp(mode, "deflate") || !strcmp(mode, "enc")) {
    const std::vector<uint8_t> raw = read_stdin();
    const size_t n = orbit_tx_prepare(raw.data(), raw.size());
    if (!strcmp(mode, "enc")) { printf("%s\n", orbit_tx_enc()); return 0; }
    if (n == 0) { fprintf(stderr, "nothing to send\n"); return 1; }
    return fwrite(orbit_tx_data(), 1, n, stdout) == n ? 0 : 1;
  }
  if (!strcmp(mode, "inflate")) {
    const std::vector<uint8_t> src = read_stdin();
    std::vector<uint8_t> out;
    if (!inflate_all(src.data(), src.size(), out)) { fprintf(stderr, "inflate failed\n"); return 1; }
    return fwrite(out.data(), 1, out.size(), stdout) == out.size() ? 0 : 1;
  }
  fprintf(stderr, "usage: %s selftest|chunks|deflate|inflate|enc\n", argv[0]);
  return 2;
}
