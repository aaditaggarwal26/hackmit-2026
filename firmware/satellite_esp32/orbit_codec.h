// Frame payload compression, with no Arduino in it.
//
// Same bargain as orbit_sat.h: there is no arduino-cli in this repo's toolchain, so anything
// that lives only in the .ino is untested code. Everything here compiles under a host g++
// (firmware/test/codec_host.cpp) against the system zlib, and the ESP32 compiles the SAME
// LINES against miniz. The sketch keeps the I/O; this header owns the encoding.
//
// The mirror is exact and deliberate:
//   orbit_chunk_count   <->  orbit.protocol.codec.chunk_count
//   orbit_tx_prepare    <->  orbit.protocol.codec.compress
//
// WHAT GOES ON THE WIRE
// ---------------------
// Only the frame payload is compressed. Every control message stays plain readable JSON. The
// frame is deflated once per grant into the staging buffer below, and it is the COMPRESSED
// blob that is chunked, base64'd and paced -- which is the whole point, because the pacing is
// the airtime. tx_begin still reports the RAW 16384 as total_bytes and adds enc/enc_bytes;
// tx_done still carries the sha256 of the RAW frame. The digest contract does not move.
//
// WHY A SNAPSHOT
// --------------
// prepare() copies (or deflates) out of the pool slot into its own buffer, so the chunks are
// served from a snapshot rather than from live pool memory. TX_PASSES sends the whole
// sequence three times over several seconds, and a snapshot is what makes every pass byte
// identical -- the ground keys chunks by idx and a re-sent chunk has to be the same chunk.
//
// WHAT IS PINNED
// --------------
// level 9, window bits 15, memLevel 8, Z_DEFAULT_STRATEGY -- stated here and in
// orbit/protocol/codec.py so neither side can drift onto a library default. The two sides do
// NOT have to emit the same bytes: miniz and zlib are both conforming DEFLATE encoders and
// may make different, equally valid choices. That is fine, because the equality contract is
// the sha256 over the RAW frame, never over the compressed blob, and any conforming inflater
// recovers the exact 16384 bytes from either encoder's output.
//
// FALLING BACK IS NORMAL, NOT AN ERROR
// ------------------------------------
// If the encoder cannot allocate its workspace, or the frame does not actually shrink, the
// payload is the raw frame and enc is "raw". The ground treats an absent or "raw" encoding as
// the frame itself, so a board that never manages to compress is a slower satellite, not a
// broken one.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "orbit_config.h"

// The deflate backend. miniz exposes the zlib-compatible names (deflateInit2, deflate,
// deflateEnd, z_stream, Z_*), which is why one set of call sites serves both.
#if defined(ORBIT_CODEC_MINIZ)
#include "miniz.h"
typedef size_t orbit_zsize_t;
#elif defined(ESP32) || defined(ARDUINO)
#include "miniz.h"
#define ORBIT_CODEC_MINIZ 1
typedef size_t orbit_zsize_t;
#else
#include <zlib.h>
typedef uInt orbit_zsize_t;
#endif

#if defined(ESP32)
#include <esp_heap_caps.h>
#endif

// The encoding names as they appear in tx_begin.enc and tx_chunk.enc.
#define ORBIT_ENC_RAW  "raw"
#define ORBIT_ENC_ZLIB "zlib"
// ...and the AES-GCM-sealed variants (orbit/protocol/codec.py: compress-then-encrypt).
#define ORBIT_ENC_RAW_GCM  "raw+gcm"
#define ORBIT_ENC_ZLIB_GCM "zlib+gcm"
#define ORBIT_GCM_NONCE 12   // standard GCM nonce, matches codec.NONCE_BYTES
#define ORBIT_GCM_TAG   16   // matches codec.GCM_TAG_BYTES
#define ORBIT_GCM_AAD   "orbit-tx-v1"  // must equal codec.AAD

// The AES-GCM key as hex (32/48/64 chars = AES-128/192/256), set in the gitignored secrets.h.
// "" = confidentiality off, which is the default and correct for this public corpus. Matches the
// ground's ORBIT_PAYLOAD_KEY / Settings.payload_key.
#ifndef ORBIT_PAYLOAD_KEY
#define ORBIT_PAYLOAD_KEY ""
#endif

// Pinned encoder settings. Anything that changes one of these changes it in
// orbit/protocol/codec.py in the same commit.
static const int ORBIT_ZLIB_LEVEL    = 9;
static const int ORBIT_ZLIB_WBITS    = 15;
static const int ORBIT_ZLIB_MEMLEVEL = 8;

// deflate can expand incompressible input; zlib's own bound is srcLen + srcLen/1000 + 13, and
// this is that with room for the wrapper and a rounding margin. The buffer also has to hold a
// whole raw frame, because that is what the fallback stores.
#define ORBIT_ENC_CAP ((size_t)FRAME_BYTES + (size_t)FRAME_BYTES / 16 + 64)

// ---------------------------------------------------------------- chunk arithmetic
//
// Mirrors codec.chunk_count. The ground rejects any chunk whose idx is outside
// 0 <= idx < chunks, so the two have to agree exactly, including on the empty payload --
// which is still one (empty) chunk, never zero.
static inline uint16_t orbit_chunk_count(size_t payload_bytes, size_t chunk_bytes) {
  if (chunk_bytes == 0) return 0;
  const size_t n = (payload_bytes + chunk_bytes - 1) / chunk_bytes;
  return (uint16_t)(n < 1 ? 1 : n);
}

// ---------------------------------------------------------------- the staging buffer
//
// Function-local rather than file-scope so a translation unit that only wants
// orbit_chunk_count does not carry 17 KB of .bss it never touches.
struct OrbitTxPayload {
  uint8_t     buf[ORBIT_ENC_CAP];
  size_t      len;
  const char *enc;
};

static inline OrbitTxPayload &orbit_tx_payload() {
  static OrbitTxPayload p;
  return p;
}

// ---------------------------------------------------------------- the encoder's workspace
//
// miniz's deflate state is a few hundred KB -- far more than the ESP32's internal heap will
// give up mid-flight, and the frame pool is already in PSRAM for the same reason. z_stream's
// zalloc/zfree hooks exist exactly for this, so the workspace goes where the frames go.
// Failure here is not fatal: prepare() falls back to sending the frame raw.
static inline void *orbit_zalloc(void *opaque, orbit_zsize_t items, orbit_zsize_t size) {
  (void)opaque;
  const size_t n = (size_t)items * (size_t)size;
#if defined(ESP32)
  void *p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM);
  return p ? p : malloc(n);
#else
  return malloc(n);
#endif
}

static inline void orbit_zfree(void *opaque, void *addr) {
  (void)opaque;
  free(addr);
}

// ---------------------------------------------------------------- deflate
//
// Returns the compressed length, or 0 if the frame could not be compressed into `cap` bytes
// for any reason (no workspace, or the output did not fit). 0 is "send it raw", not an error.
static inline size_t orbit_deflate(const uint8_t *raw, size_t raw_len, uint8_t *out, size_t cap) {
  z_stream s;
  memset(&s, 0, sizeof(s));
  s.zalloc = (alloc_func)orbit_zalloc;
  s.zfree = (free_func)orbit_zfree;
  if (deflateInit2(&s, ORBIT_ZLIB_LEVEL, Z_DEFLATED, ORBIT_ZLIB_WBITS, ORBIT_ZLIB_MEMLEVEL,
                   Z_DEFAULT_STRATEGY) != Z_OK) {
    return 0;
  }
  s.next_in = (Bytef *)raw;
  s.avail_in = (unsigned)raw_len;
  s.next_out = (Bytef *)out;
  s.avail_out = (unsigned)cap;
  const int rc = deflate(&s, Z_FINISH);
  const size_t produced = (size_t)s.total_out;
  deflateEnd(&s);
  return (rc == Z_STREAM_END) ? produced : 0;  // Z_OK here means it ran out of room
}

// ---------------------------------------------------------------- one call per grant
//
// Mirrors codec.compress. Deflates the frame into the staging buffer, or copies it there
// unchanged when deflate did not help; either way the chunks are served from one pointer and
// one length, and the caller never has to know which happened.
//
// Returns the payload length to chunk and pace over (never 0 for a non-empty frame), or 0
// when there was nothing to send -- which is what a vanished pool slot looks like.
static inline size_t orbit_tx_prepare(const uint8_t *raw, size_t raw_len) {
  OrbitTxPayload &p = orbit_tx_payload();
  p.len = 0;
  p.enc = ORBIT_ENC_RAW;
  if (!raw || raw_len == 0 || raw_len > ORBIT_ENC_CAP) return 0;

  const size_t n = orbit_deflate(raw, raw_len, p.buf, ORBIT_ENC_CAP);
  if (n > 0 && n < raw_len) {
    p.len = n;
    p.enc = ORBIT_ENC_ZLIB;
    return p.len;
  }
  // No workspace, or the frame did not shrink: 16 KB plus a zlib header is not a saving.
  memcpy(p.buf, raw, raw_len);
  p.len = raw_len;
  p.enc = ORBIT_ENC_RAW;
  return p.len;
}

static inline const uint8_t *orbit_tx_data() { return orbit_tx_payload().buf; }
static inline size_t orbit_tx_size() { return orbit_tx_payload().len; }
static inline const char *orbit_tx_enc() { return orbit_tx_payload().enc; }

// ---------------------------------------------------------------- confidentiality (device only)
//
// AES-GCM the staging buffer IN PLACE, turning the compressed (or raw) payload into
// nonce || ciphertext || tag and switching enc to the "+gcm" variant. This is the reverse of
// what the ground does in orbit/protocol/codec.py::decompress, and the ground decrypts with
// cryptography's AESGCM by standard conformance -- so unlike the canonicaliser this needs no
// portable host twin: the end-to-end check is the ground re-scoring the frame and matching the
// tx_done sha256, which it already does.
//
// mbedtls only, so it is compiled only for the device; the host codec test builds the
// compression path unchanged. Call AFTER orbit_tx_prepare. Returns the new payload length, or 0
// on failure -- and a product must treat 0 as "abort the transmission", never "send it in the
// clear", so the caller drops the grant rather than downlinking unencrypted imagery.
#if defined(ESP32) || defined(ARDUINO) || defined(ORBIT_HAS_MBEDTLS)
#include <mbedtls/gcm.h>
#if defined(ESP32)
#include <esp_random.h>
#endif

static inline size_t orbit_tx_seal(const uint8_t *key, size_t keylen) {
  OrbitTxPayload &p = orbit_tx_payload();
  if (!key || (keylen != 16 && keylen != 24 && keylen != 32) || p.len == 0) return 0;
  if ((size_t)ORBIT_GCM_NONCE + p.len + ORBIT_GCM_TAG > sizeof(p.buf)) return 0;

  uint8_t nonce[ORBIT_GCM_NONCE];
#if defined(ESP32)
  esp_fill_random(nonce, sizeof(nonce));   // hardware RNG: a fresh nonce per frame
#else
  for (size_t i = 0; i < sizeof(nonce); i++) nonce[i] = (uint8_t)rand();  // host-only; device uses the TRNG
#endif

  memmove(p.buf + ORBIT_GCM_NONCE, p.buf, p.len);   // open a gap for the nonce prefix
  memcpy(p.buf, nonce, ORBIT_GCM_NONCE);
  uint8_t *pt = p.buf + ORBIT_GCM_NONCE;            // plaintext, now shifted; encrypted in place
  uint8_t tag[ORBIT_GCM_TAG];

  mbedtls_gcm_context g;
  mbedtls_gcm_init(&g);
  int rc = mbedtls_gcm_setkey(&g, MBEDTLS_CIPHER_ID_AES, key, (unsigned)(keylen * 8));
  if (rc == 0) {
    rc = mbedtls_gcm_crypt_and_tag(&g, MBEDTLS_GCM_ENCRYPT, p.len, nonce, ORBIT_GCM_NONCE,
                                   (const uint8_t *)ORBIT_GCM_AAD, sizeof(ORBIT_GCM_AAD) - 1,
                                   pt, pt, ORBIT_GCM_TAG, tag);
  }
  mbedtls_gcm_free(&g);
  if (rc != 0) return 0;

  memcpy(pt + p.len, tag, ORBIT_GCM_TAG);           // wire = nonce || ciphertext || tag
  p.len = (size_t)ORBIT_GCM_NONCE + p.len + ORBIT_GCM_TAG;
  p.enc = (strcmp(p.enc, ORBIT_ENC_ZLIB) == 0) ? ORBIT_ENC_ZLIB_GCM : ORBIT_ENC_RAW_GCM;
  return p.len;
}
#endif
