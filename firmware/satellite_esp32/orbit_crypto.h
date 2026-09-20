// Control-bus AUTHENTICITY: sender pinning + HMAC-SHA256 over a canonical byte string.
//
// Threat model: local-link UDP multicast on shared WiFi carrying public NASA imagery. What
// must not be forgeable is the CONTROL path -- a stranger on the AP must not be able to say
// "grant", "revoke" or "tx_ack" and steer the demo. Confidentiality is not a goal and nothing
// here encrypts: the JSON stays readable in the bus log, which is the point of the format.
//
// Like orbit_sat.h and orbit_score.h this file is Arduino-free and compiles under a host g++
// (firmware/test/crypto_host.cpp), because there is no arduino-cli in this toolchain and
// anything that lives only in the .ino is untested code. The SHA-256 here is a portable
// implementation rather than mbedtls for exactly that reason: the ESP32 runs the same bytes
// tests/test_crypto_parity.py checks against Python's hashlib. (The tx_done frame digest in
// the sketch still uses mbedtls_sha256 -- that one is checked by the ground re-hashing the
// reassembled frame, so it needs no host twin.)
//
// ================================ THE CANONICAL FORM ================================
//
// ArduinoJson and Python's json do NOT serialise the same bytes for the same values, so the
// raw datagram cannot be MACed directly. The MAC input is built instead, in a fixed order:
//
//     v|type|from|seq|t_ms|<body>
//
// where the five envelope values are emitted in that order (`type` and `from` as their JSON
// *string literals*, quotes included, so a `|` or a `"` inside a hostname cannot forge a field
// boundary), and <body> is every other top-level member as a JSON object with keys sorted.
// The top-level `auth` member is EXCLUDED -- it is the field that carries the tag.
//
// The one idea that makes this checkable: SCALAR TOKENS ARE COPIED VERBATIM. Numbers, string
// literals (escapes and all), `true`, `false` and `null` are copied byte for byte out of the
// datagram. Nothing is re-serialised, so none of the places the two languages disagree can be
// reached. Concretely, the ambiguities this pins down:
//
//   float formatting     not normalised. 61.04 MACs as `61.04`; the float-narrowed
//                        61.040000916 (orbit_sat.h's note) MACs as `61.040000916` and is a
//                        DIFFERENT message. A sender MACs the digits it actually transmits.
//   whole numbers        `91` and `91.0` are different canonical strings, hence different
//                        tags. No int/float unification is attempted or needed.
//   exponents            `1e2`, `1E2` and `100` are all distinct. Copied as written.
//   negative zero        `-0` and `0` are distinct.
//   bool spelling        `true`/`false` only; `True`, `1` or `"true"` are other things.
//   null vs absent       distinct. encode() omits None, so a null on the wire is a different
//                        message from an absent field and gets a different tag.
//   string escaping      not normalised. `"\u0041"`, `"A"` and `"A "` are three
//                        different literals. `"\/"` differs from `"/"`.
//   non-ASCII            the canonicaliser is BYTE-oriented and never decodes UTF-8, so raw
//                        UTF-8 (a hostname with U+2019) and its `\uXXXX` spelling are simply
//                        two different literals, and no transcoding can diverge.
//   key order            every object's members are sorted by their raw key LITERAL bytes
//                        (memcmp, shorter first on a prefix tie), at every depth.
//   arrays               order preserved -- it is data.
//   whitespace           the only thing normalised: whitespace between tokens is dropped, so
//                        a pretty-printed datagram and a compact one agree.
//   duplicate keys       REJECTED. Python's json keeps the last, ArduinoJson the first; rather
//                        than pick, a datagram with two identical key literals in one object
//                        cannot be signed or verified at all.
//   trailing bytes       REJECTED. Anything after the closing `}` fails.
//
// What is NOT covered, stated plainly: two different spellings of the SAME key (`"a"` and
// `"\u0061"`) are treated as distinct members and both survive into the canonical string. A
// holder of the key could therefore build a datagram that a Python receiver and an ArduinoJson
// receiver read differently. That needs the pre-shared key, so it is not a forgery path.
//
// ================================ THE TAG =====================================
//
// tag = first 16 bytes of HMAC-SHA256(key, canonical), 32 lowercase hex characters, carried as
// `"auth"` and spliced in as the last member: `,"auth":"<32 hex>"` before the closing brace.
//
// 128 bits, not 256, and the reason is a measurement rather than a preference: the worst-case
// tx_chunk encodes to 1349 bytes and BUS_MAX_DATAGRAM is 1400. A 32-byte tag adds 74 bytes
// (1423) and every frame chunk would be dropped as oversize by sendDoc's own `n >= sizeof(buf)`
// guard. A 16-byte tag adds 42 (1391) and fits with room. Truncated HMAC is the ordinary
// construction (RFC 4868 specifies HMAC-SHA-256-128 for exactly this reason) and 2^-128 forgery
// odds are not the weak point of anything on this link.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// ---------------------------------------------------------------- build-time configuration
//
// These two live HERE rather than in orbit_config.h deliberately: orbit_config.h is the file
// tools/check_firmware_sync.py holds against orbit/config.py field for field, and neither of
// these is a shared protocol fact -- the ground's hostname is site-local and the key is a
// secret. Override either in secrets.h (which is #included before this header) or with a
// -D build flag; the defaults here are what an unconfigured board uses.

// The ONLY sender whose offers_open / grant / revoke / tx_ack this node will act on. The real
// ground announces itself with socket.gethostname().split(".")[0] unless Settings.hostname is
// set, so this has to match whatever that box is called on the day -- and the default is ""
// rather than a plausible-looking guess on purpose: a board that pinned to the WRONG name would
// be silently deaf to every grant, which looks exactly like a WiFi problem and is the worst
// failure this file could introduce. "" means no pin, which is how the bus behaved before, and
// secrets.h.example carries the line to uncomment. The ground's own Settings.ground_name and
// Settings.pin_senders default off to match.
#ifndef ORBIT_GROUND_NAME
#define ORBIT_GROUND_NAME ""
#endif

// The pre-shared key, as raw bytes: whatever characters the macro's string literal contains are
// the HMAC key, so C and Python agree without any decoding step. NEVER commit a real one --
// secrets.h is gitignored and secrets.h.example carries only the placeholder below.
#ifndef ORBIT_AUTH_KEY
#define ORBIT_AUTH_KEY ""
#endif

// "" disables the MAC entirely (sign and accept both become pass-throughs). That is the state a
// board flashed from secrets.h.example is in, and it is how the bus behaved before this file
// existed, so an unconfigured node is not silently deaf -- it is unauthenticated, loudly.
#define ORBIT_AUTH_ENABLED (sizeof(ORBIT_AUTH_KEY) > 1)

#define ORBIT_TAG_BYTES    16                       // truncated HMAC-SHA256, see above
#define ORBIT_TAG_HEX      (ORBIT_TAG_BYTES * 2)    // 32 characters on the wire
#define ORBIT_AUTH_OVERHEAD (ORBIT_TAG_HEX + 10)    // ,"auth":"..."  -> 42 bytes

#ifndef ORBIT_CANON_MAX
#define ORBIT_CANON_MAX 1600      // >= BUS_MAX_DATAGRAM; the canonical form is never longer
#endif                            // than the datagram (whitespace only ever comes out)
#ifndef ORBIT_JSON_MAX_DEPTH
#define ORBIT_JSON_MAX_DEPTH 5    // the protocol nests 3 deep (doc > window[] > entry)
#endif
#ifndef ORBIT_JSON_MAX_MEMBERS
#define ORBIT_JSON_MAX_MEMBERS 24 // heartbeat, the widest message, has 16
#endif
#ifndef ORBIT_PEERS_N
#define ORBIT_PEERS_N 8           // senders tracked: a ground, both boards, our own echo, slack
#endif
#ifndef ORBIT_PEER_NAME_MAX
#define ORBIT_PEER_NAME_MAX 32
#endif

// =============================================================== SHA-256
//
// FIPS 180-4. Portable, no intrinsics, no mbedtls: the host test runs this exact code against
// Python's hashlib, which is the only reason it can be trusted at all.

typedef struct {
  uint32_t state[8];
  uint64_t bits;
  uint8_t  buf[64];
  size_t   have;
} OrbitSha256;

static const uint32_t ORBIT_SHA_K[64] = {
  0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
  0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
  0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
  0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
  0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
  0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
  0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
  0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u};

static inline uint32_t orbit_ror32(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

static inline void orbit_sha256_block(OrbitSha256 *c, const uint8_t *p) {
  uint32_t w[64], a, b, d, e, f, g, h, t1, t2, cc;
  int i;
  for (i = 0; i < 16; i++)
    w[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) |
           ((uint32_t)p[i * 4 + 2] << 8) | (uint32_t)p[i * 4 + 3];
  for (i = 16; i < 64; i++) {
    const uint32_t s0 = orbit_ror32(w[i - 15], 7) ^ orbit_ror32(w[i - 15], 18) ^ (w[i - 15] >> 3);
    const uint32_t s1 = orbit_ror32(w[i - 2], 17) ^ orbit_ror32(w[i - 2], 19) ^ (w[i - 2] >> 10);
    w[i] = w[i - 16] + s0 + w[i - 7] + s1;
  }
  a = c->state[0]; b = c->state[1]; cc = c->state[2]; d = c->state[3];
  e = c->state[4]; f = c->state[5]; g = c->state[6]; h = c->state[7];
  for (i = 0; i < 64; i++) {
    const uint32_t S1 = orbit_ror32(e, 6) ^ orbit_ror32(e, 11) ^ orbit_ror32(e, 25);
    const uint32_t ch = (e & f) ^ ((~e) & g);
    t1 = h + S1 + ch + ORBIT_SHA_K[i] + w[i];
    const uint32_t S0 = orbit_ror32(a, 2) ^ orbit_ror32(a, 13) ^ orbit_ror32(a, 22);
    const uint32_t mj = (a & b) ^ (a & cc) ^ (b & cc);
    t2 = S0 + mj;
    h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2;
  }
  c->state[0] += a; c->state[1] += b; c->state[2] += cc; c->state[3] += d;
  c->state[4] += e; c->state[5] += f; c->state[6] += g; c->state[7] += h;
}

static inline void orbit_sha256_init(OrbitSha256 *c) {
  c->state[0] = 0x6a09e667u; c->state[1] = 0xbb67ae85u; c->state[2] = 0x3c6ef372u;
  c->state[3] = 0xa54ff53au; c->state[4] = 0x510e527fu; c->state[5] = 0x9b05688cu;
  c->state[6] = 0x1f83d9abu; c->state[7] = 0x5be0cd19u;
  c->bits = 0; c->have = 0;
}

static inline void orbit_sha256_update(OrbitSha256 *c, const void *data, size_t n) {
  const uint8_t *p = (const uint8_t *)data;
  c->bits += (uint64_t)n * 8u;
  while (n) {
    const size_t take = (64 - c->have < n) ? 64 - c->have : n;
    memcpy(c->buf + c->have, p, take);
    c->have += take; p += take; n -= take;
    if (c->have == 64) { orbit_sha256_block(c, c->buf); c->have = 0; }
  }
}

static inline void orbit_sha256_final(OrbitSha256 *c, uint8_t out[32]) {
  const uint64_t bits = c->bits;
  uint8_t pad = 0x80;
  int i;
  orbit_sha256_update(c, &pad, 1);
  pad = 0;
  while (c->have != 56) orbit_sha256_update(c, &pad, 1);
  for (i = 7; i >= 0; i--) { const uint8_t b = (uint8_t)(bits >> (i * 8)); orbit_sha256_update(c, &b, 1); }
  for (i = 0; i < 8; i++) {
    out[i * 4]     = (uint8_t)(c->state[i] >> 24);
    out[i * 4 + 1] = (uint8_t)(c->state[i] >> 16);
    out[i * 4 + 2] = (uint8_t)(c->state[i] >> 8);
    out[i * 4 + 3] = (uint8_t)(c->state[i]);
  }
}

// RFC 2104. A key longer than the 64-byte block is hashed first; a shorter one is zero-padded.
static inline void orbit_hmac_sha256(const uint8_t *key, size_t klen,
                                     const uint8_t *msg, size_t mlen, uint8_t out[32]) {
  uint8_t k[64], pad[64], inner[32];
  OrbitSha256 c;
  size_t i;
  memset(k, 0, sizeof(k));
  if (klen > 64) { orbit_sha256_init(&c); orbit_sha256_update(&c, key, klen); orbit_sha256_final(&c, k); }
  else if (klen)  memcpy(k, key, klen);
  for (i = 0; i < 64; i++) pad[i] = (uint8_t)(k[i] ^ 0x36);
  orbit_sha256_init(&c);
  orbit_sha256_update(&c, pad, 64);
  orbit_sha256_update(&c, msg, mlen);
  orbit_sha256_final(&c, inner);
  for (i = 0; i < 64; i++) pad[i] = (uint8_t)(k[i] ^ 0x5c);
  orbit_sha256_init(&c);
  orbit_sha256_update(&c, pad, 64);
  orbit_sha256_update(&c, inner, 32);
  orbit_sha256_final(&c, out);
}

// =============================================================== canonicaliser
//
// A JSON scanner strict enough that both languages accept and reject the same bytes. It never
// decodes anything: it finds token boundaries and copies the tokens through.

typedef struct { uint8_t *buf; size_t cap; size_t len; int ovf; } OrbitOut;

static inline void orbit_out_put(OrbitOut *o, const void *d, size_t n) {
  if (o->ovf || o->len + n > o->cap) { o->ovf = 1; return; }
  memcpy(o->buf + o->len, d, n);
  o->len += n;
}
static inline void orbit_out_ch(OrbitOut *o, char ch) { orbit_out_put(o, &ch, 1); }

typedef struct { const uint8_t *ks, *ke, *vs, *ve; } OrbitMember;

static inline const uint8_t *orbit_ws(const uint8_t *p, const uint8_t *e) {
  while (p < e && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
  return p;
}

static inline int orbit_isdigit(uint8_t c) { return c >= '0' && c <= '9'; }
static inline int orbit_ishex(uint8_t c) {
  return orbit_isdigit(c) || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

// A JSON string literal, quotes included. Rejects an unescaped control byte and any escape the
// grammar does not define, so a datagram either scans the same way on both tiers or on neither.
// Bytes >= 0x80 pass through untouched: UTF-8 is never decoded, so it cannot be transcoded wrong.
static inline const uint8_t *orbit_scan_string(const uint8_t *p, const uint8_t *e) {
  if (p >= e || *p != '"') return NULL;
  p++;
  while (p < e) {
    const uint8_t c = *p;
    if (c == '"') return p + 1;
    if (c == '\\') {
      p++;
      if (p >= e) return NULL;
      if (*p == 'u') {
        if (e - p < 5) return NULL;
        for (int i = 1; i <= 4; i++) if (!orbit_ishex(p[i])) return NULL;
        p += 5;
      } else if (*p == '"' || *p == '\\' || *p == '/' || *p == 'b' || *p == 'f' ||
                 *p == 'n' || *p == 'r' || *p == 't') {
        p++;
      } else {
        return NULL;
      }
    } else if (c < 0x20) {
      return NULL;   // a raw control byte inside a string is not JSON
    } else {
      p++;
    }
  }
  return NULL;
}

// -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?  -- no leading +, no leading zeros, no bare
// ".5", no trailing ".", and no nan/inf (which is what ArduinoJson emits for a non-finite
// double: such a datagram fails to sign rather than going out unauthenticated).
static inline const uint8_t *orbit_scan_number(const uint8_t *p, const uint8_t *e) {
  if (p < e && *p == '-') p++;
  if (p >= e) return NULL;
  if (*p == '0') p++;
  else if (*p >= '1' && *p <= '9') { while (p < e && orbit_isdigit(*p)) p++; }
  else return NULL;
  if (p < e && *p == '.') {
    p++;
    if (p >= e || !orbit_isdigit(*p)) return NULL;
    while (p < e && orbit_isdigit(*p)) p++;
  }
  if (p < e && (*p == 'e' || *p == 'E')) {
    p++;
    if (p < e && (*p == '+' || *p == '-')) p++;
    if (p >= e || !orbit_isdigit(*p)) return NULL;
    while (p < e && orbit_isdigit(*p)) p++;
  }
  return p;
}

// Raw key-literal order: memcmp over the shared prefix, shorter first on a tie. Both tiers sort
// the same bytes with the same rule, so no locale or codec can get between them.
static inline int orbit_key_cmp(const OrbitMember *a, const OrbitMember *b) {
  const size_t la = (size_t)(a->ke - a->ks), lb = (size_t)(b->ke - b->ks);
  const size_t n = la < lb ? la : lb;
  const int r = n ? memcmp(a->ks, b->ks, n) : 0;
  if (r) return r;
  return la == lb ? 0 : (la < lb ? -1 : 1);
}

// Find the end of one value without emitting anything and without touching the stack for
// member tables. The emit pass below needs each member's extent before it can sort them, and
// doing that with the emitter itself would double the recursion depth on an 8 KB task stack.
static inline const uint8_t *orbit_scan_value(const uint8_t *p, const uint8_t *e, int depth) {
  if (depth > ORBIT_JSON_MAX_DEPTH) return NULL;
  p = orbit_ws(p, e);
  if (p >= e) return NULL;
  if (*p == '{') {
    p = orbit_ws(p + 1, e);
    if (p < e && *p == '}') return p + 1;
    for (;;) {
      p = orbit_ws(p, e);
      const uint8_t *ke = orbit_scan_string(p, e);
      if (!ke) return NULL;
      p = orbit_ws(ke, e);
      if (p >= e || *p != ':') return NULL;
      p = orbit_scan_value(p + 1, e, depth + 1);
      if (!p) return NULL;
      p = orbit_ws(p, e);
      if (p < e && *p == ',') { p++; continue; }
      if (p < e && *p == '}') return p + 1;
      return NULL;
    }
  }
  if (*p == '[') {
    p = orbit_ws(p + 1, e);
    if (p < e && *p == ']') return p + 1;
    for (;;) {
      p = orbit_scan_value(p, e, depth + 1);
      if (!p) return NULL;
      p = orbit_ws(p, e);
      if (p < e && *p == ',') { p++; continue; }
      if (p < e && *p == ']') return p + 1;
      return NULL;
    }
  }
  if (*p == '"') return orbit_scan_string(p, e);
  if ((size_t)(e - p) >= 4 && !memcmp(p, "true", 4))  return p + 4;
  if ((size_t)(e - p) >= 5 && !memcmp(p, "false", 5)) return p + 5;
  if ((size_t)(e - p) >= 4 && !memcmp(p, "null", 4))  return p + 4;
  return orbit_scan_number(p, e);
}

// Scan `{...}` into `m`. Returns the byte after `}`, or NULL. Sorts and rejects duplicate keys.
static inline const uint8_t *orbit_scan_object(const uint8_t *p, const uint8_t *e, int depth,
                                               OrbitMember *m, int *count) {
  int n = 0;
  if (depth > ORBIT_JSON_MAX_DEPTH) return NULL;
  if (p >= e || *p != '{') return NULL;
  p = orbit_ws(p + 1, e);
  if (p < e && *p == '}') { *count = 0; return p + 1; }
  for (;;) {
    if (n >= ORBIT_JSON_MAX_MEMBERS) return NULL;
    p = orbit_ws(p, e);
    const uint8_t *ks = p, *ke = orbit_scan_string(p, e);
    if (!ke) return NULL;
    p = orbit_ws(ke, e);
    if (p >= e || *p != ':') return NULL;
    p = orbit_ws(p + 1, e);
    const uint8_t *vs = p;
    const uint8_t *ve = orbit_scan_value(vs, e, depth + 1);
    if (!ve) return NULL;
    m[n].ks = ks; m[n].ke = ke; m[n].vs = vs; m[n].ve = ve;
    n++;
    p = orbit_ws(ve, e);
    if (p < e && *p == ',') { p++; continue; }
    if (p < e && *p == '}') { p++; break; }
    return NULL;
  }
  for (int i = 1; i < n; i++) {                 // insertion sort: n is at most 32
    const OrbitMember cur = m[i];
    int j = i - 1;
    while (j >= 0 && orbit_key_cmp(&m[j], &cur) > 0) { m[j + 1] = m[j]; j--; }
    m[j + 1] = cur;
  }
  for (int i = 1; i < n; i++) if (orbit_key_cmp(&m[i - 1], &m[i]) == 0) return NULL;  // duplicate key
  *count = n;
  return p;
}

// One value, canonical, appended to `o`. Returns the byte after it, or NULL.
static inline const uint8_t *orbit_emit_value(const uint8_t *p, const uint8_t *e, int depth, OrbitOut *o) {
  if (depth > ORBIT_JSON_MAX_DEPTH) return NULL;
  p = orbit_ws(p, e);
  if (p >= e) return NULL;
  if (*p == '{') {
    OrbitMember m[ORBIT_JSON_MAX_MEMBERS];
    int n = 0;
    const uint8_t *end = orbit_scan_object(p, e, depth, m, &n);
    if (!end) return NULL;
    orbit_out_ch(o, '{');
    for (int i = 0; i < n; i++) {
      if (i) orbit_out_ch(o, ',');
      orbit_out_put(o, m[i].ks, (size_t)(m[i].ke - m[i].ks));
      orbit_out_ch(o, ':');
      if (!orbit_emit_value(m[i].vs, m[i].ve, depth + 1, o)) return NULL;
    }
    orbit_out_ch(o, '}');
    return end;
  }
  if (*p == '[') {
    p = orbit_ws(p + 1, e);
    orbit_out_ch(o, '[');
    if (p < e && *p == ']') { orbit_out_ch(o, ']'); return p + 1; }
    for (int i = 0;; i++) {
      if (i) orbit_out_ch(o, ',');
      const uint8_t *ve = orbit_emit_value(p, e, depth + 1, o);
      if (!ve) return NULL;
      p = orbit_ws(ve, e);
      if (p < e && *p == ',') { p = orbit_ws(p + 1, e); continue; }
      if (p < e && *p == ']') { orbit_out_ch(o, ']'); return p + 1; }
      return NULL;
    }
  }
  if (*p == '"') {
    const uint8_t *end = orbit_scan_string(p, e);
    if (!end) return NULL;
    orbit_out_put(o, p, (size_t)(end - p));      // verbatim, escapes and all
    return end;
  }
  if ((size_t)(e - p) >= 4 && !memcmp(p, "true", 4))  { orbit_out_put(o, "true", 4);  return p + 4; }
  if ((size_t)(e - p) >= 5 && !memcmp(p, "false", 5)) { orbit_out_put(o, "false", 5); return p + 5; }
  if ((size_t)(e - p) >= 4 && !memcmp(p, "null", 4))  { orbit_out_put(o, "null", 4);  return p + 4; }
  {
    const uint8_t *end = orbit_scan_number(p, e);
    if (!end) return NULL;
    orbit_out_put(o, p, (size_t)(end - p));      // verbatim: the digits that were transmitted
    return end;
  }
}

// What the envelope scan hands back, so callers need not re-parse for `from`/`seq`/`auth`.
typedef struct {
  const uint8_t *type_s, *type_e;   // JSON string literals, quotes included
  const uint8_t *from_s, *from_e;
  const uint8_t *auth_s, *auth_e;   // the tag literal, or NULL when there is no `auth` member
  uint32_t       seq;
  uint32_t       t_ms;
  int            members;           // top-level members, so sign() can see whether a 25th fits
} OrbitEnvelope;

static inline int orbit_u32(const uint8_t *p, const uint8_t *e, uint32_t *out) {
  uint64_t v = 0;
  if (p >= e) return 0;
  for (; p < e; p++) {
    if (!orbit_isdigit(*p)) return 0;            // negative or fractional: not a seq/t_ms
    v = v * 10u + (uint64_t)(*p - '0');
    if (v > 0xFFFFFFFFull) return 0;
  }
  *out = (uint32_t)v;
  return 1;
}

// datagram bytes -> canonical bytes. Returns the canonical length, or 0 if the datagram cannot
// be canonicalised at all (bad JSON, missing envelope field, duplicate key, trailing bytes,
// too deep, too many members, output too long). `env` may be NULL.
static inline size_t orbit_canonical(const uint8_t *src, size_t len,
                                     uint8_t *out, size_t outcap, OrbitEnvelope *env) {
  OrbitMember m[ORBIT_JSON_MAX_MEMBERS];
  OrbitOut o;
  OrbitEnvelope ev;
  const uint8_t *e = src + len;
  const uint8_t *p = orbit_ws(src, e);
  int n = 0, i;
  const OrbitMember *mv = NULL, *mt = NULL, *mf = NULL, *ms = NULL, *mu = NULL, *ma = NULL;

  p = orbit_scan_object(p, e, 1, m, &n);
  if (!p) return 0;
  if (orbit_ws(p, e) != e) return 0;             // trailing bytes after the object

  for (i = 0; i < n; i++) {
    const size_t kl = (size_t)(m[i].ke - m[i].ks);
    const uint8_t *k = m[i].ks;
    if      (kl == 3 && !memcmp(k, "\"v\"", 3))       mv = &m[i];
    else if (kl == 6 && !memcmp(k, "\"type\"", 6))    mt = &m[i];
    else if (kl == 6 && !memcmp(k, "\"from\"", 6))    mf = &m[i];
    else if (kl == 5 && !memcmp(k, "\"seq\"", 5))     ms = &m[i];
    else if (kl == 6 && !memcmp(k, "\"t_ms\"", 6))    mu = &m[i];
    else if (kl == 6 && !memcmp(k, "\"auth\"", 6))    ma = &m[i];
  }
  if (!mv || !mt || !mf || !ms || !mu) return 0;
  if (*mt->vs != '"' || *mf->vs != '"') return 0;            // type/from must be strings
  memset(&ev, 0, sizeof(ev));
  ev.type_s = mt->vs; ev.type_e = mt->ve;
  ev.from_s = mf->vs; ev.from_e = mf->ve;
  ev.auth_s = ma ? ma->vs : NULL;
  ev.auth_e = ma ? ma->ve : NULL;
  ev.members = n;
  if (!orbit_u32(ms->vs, ms->ve, &ev.seq)) return 0;
  if (!orbit_u32(mu->vs, mu->ve, &ev.t_ms)) return 0;

  o.buf = out; o.cap = outcap; o.len = 0; o.ovf = 0;
  if (!orbit_emit_value(mv->vs, mv->ve, 2, &o)) return 0;    // v
  orbit_out_ch(&o, '|');
  orbit_out_put(&o, mt->vs, (size_t)(mt->ve - mt->vs));      // "type", quoted
  orbit_out_ch(&o, '|');
  orbit_out_put(&o, mf->vs, (size_t)(mf->ve - mf->vs));      // "from", quoted
  orbit_out_ch(&o, '|');
  orbit_out_put(&o, ms->vs, (size_t)(ms->ve - ms->vs));      // seq
  orbit_out_ch(&o, '|');
  orbit_out_put(&o, mu->vs, (size_t)(mu->ve - mu->vs));      // t_ms
  orbit_out_ch(&o, '|');
  orbit_out_ch(&o, '{');                                     // the body, already key-sorted
  {
    int first = 1;
    for (i = 0; i < n; i++) {
      if (&m[i] == mv || &m[i] == mt || &m[i] == mf || &m[i] == ms || &m[i] == mu) continue;
      if (&m[i] == ma) continue;                             // the tag is never its own input
      if (!first) orbit_out_ch(&o, ',');
      first = 0;
      orbit_out_put(&o, m[i].ks, (size_t)(m[i].ke - m[i].ks));
      orbit_out_ch(&o, ':');
      if (!orbit_emit_value(m[i].vs, m[i].ve, 2, &o)) return 0;
    }
  }
  orbit_out_ch(&o, '}');
  if (o.ovf) return 0;
  if (env) *env = ev;
  return o.len;
}

// =============================================================== tag

static inline void orbit_tag_hex(const char *key, const uint8_t *canon, size_t clen, char out[ORBIT_TAG_HEX + 1]) {
  static const char HEX[] = "0123456789abcdef";
  uint8_t mac[32];
  orbit_hmac_sha256((const uint8_t *)key, strlen(key), canon, clen, mac);
  for (int i = 0; i < ORBIT_TAG_BYTES; i++) {
    out[i * 2]     = HEX[mac[i] >> 4];
    out[i * 2 + 1] = HEX[mac[i] & 0x0F];
  }
  out[ORBIT_TAG_HEX] = '\0';
}

// Comparison that does not leak where two tags first differ. On this link a timing oracle is
// not the realistic attack, but a tag compare is the one place the habit costs nothing.
static inline int orbit_ct_eq(const uint8_t *a, const uint8_t *b, size_t n) {
  uint8_t d = 0;
  for (size_t i = 0; i < n; i++) d = (uint8_t)(d | (a[i] ^ b[i]));
  return d == 0;
}

// =============================================================== send side
//
// Signs a SERIALISED datagram in place: canonicalises `buf[0..len)`, computes the tag and
// splices `,"auth":"<32 hex>"` in before the closing brace. Returns the new length, or 0 if it
// does not fit in `cap` or the bytes cannot be canonicalised -- in both cases the caller must
// DROP the datagram rather than send it unsigned.
//
// With the key disabled this returns `len` unchanged, which is what keeps an unconfigured board
// behaving exactly as it did before this header existed.
static inline size_t orbit_auth_sign(uint8_t *buf, size_t len, size_t cap, const char *key) {
  static uint8_t canon[ORBIT_CANON_MAX];
  OrbitEnvelope env;
  char tag[ORBIT_TAG_HEX + 1];
  size_t clen;
  if (!key || !*key) return len;
  if (len < 2 || buf[len - 1] != '}') return 0;
  if (len + ORBIT_AUTH_OVERHEAD > cap) return 0;
  clen = orbit_canonical(buf, len, canon, sizeof(canon), &env);
  if (clen == 0) return 0;
  // The tag is a top-level member like any other, so it has to fit under the same limit, and it
  // may not be a SECOND one -- re-signing would put two `auth` keys in the object and a
  // duplicate key is exactly what neither tier will canonicalise. Both would produce a datagram
  // that signs cleanly and then verifies nowhere, so both fail here instead.
  if (env.members >= ORBIT_JSON_MAX_MEMBERS || env.auth_s) return 0;
  orbit_tag_hex(key, canon, clen, tag);
  len -= 1;                                     // drop the closing brace, put it back at the end
  memcpy(buf + len, ",\"auth\":\"", 9);   len += 9;
  memcpy(buf + len, tag, ORBIT_TAG_HEX); len += ORBIT_TAG_HEX;
  buf[len++] = '"';
  buf[len++] = '}';
  return len;
}

// =============================================================== receive side

typedef struct {
  char     name[ORBIT_PEER_NAME_MAX];
  uint32_t max_seq;         // the highest seq VERIFIED from this sender
  uint32_t last_t_ms;
  uint8_t  used;
} OrbitPeer;

typedef struct {
  OrbitPeer peers[ORBIT_PEERS_N];
  uint32_t  restart_slack_ms;   // 0 -> ORBIT_RESTART_SLACK_MS
  uint32_t  accepted, rejected;
} OrbitAuthState;

#ifndef ORBIT_RESTART_SLACK_MS
#define ORBIT_RESTART_SLACK_MS 5000   // matches Settings.restart_slack_ms
#endif

typedef enum {
  ORBIT_OK = 0,
  ORBIT_ERR_CANON,     // not canonicalisable: bad JSON, dup key, missing envelope field, ...
  ORBIT_ERR_NO_TAG,    // no `auth` member, or not 32 hex characters
  ORBIT_ERR_BAD_TAG,   // the tag does not verify under the key
  ORBIT_ERR_PIN,       // a control message from someone who is not the ground
  ORBIT_ERR_REPLAY,    // seq <= the highest already verified from this sender
  ORBIT_ERR_PEERS      // the peer table is full
} OrbitAuthResult;

static inline int orbit_is_control(const uint8_t *ts, const uint8_t *te) {
  const size_t n = (size_t)(te - ts);
  return (n == 13 && !memcmp(ts, "\"offers_open\"", 13)) ||
         (n ==  7 && !memcmp(ts, "\"grant\"", 7))        ||
         (n ==  8 && !memcmp(ts, "\"revoke\"", 8))       ||
         (n ==  8 && !memcmp(ts, "\"tx_ack\"", 8));
}

// `name` compared against the raw `from` LITERAL, so a sender whose hostname arrives
// JSON-escaped does not match. Node names are plain ASCII by construction (the firmware builds
// "esp32-satellite-" + SAT_ID, the ground uses its short hostname), and an escaped spelling of
// one is exactly the confusion this refuses to resolve.
static inline int orbit_name_is(const uint8_t *s, const uint8_t *e, const char *name) {
  const size_t n = strlen(name);
  return (size_t)(e - s) == n + 2 && s[0] == '"' && e[-1] == '"' && !memcmp(s + 1, name, n);
}

static inline OrbitPeer *orbit_peer(OrbitAuthState *st, const uint8_t *s, const uint8_t *e) {
  size_t n = (size_t)(e - s);
  char name[ORBIT_PEER_NAME_MAX];
  if (n < 2) return NULL;
  s++; n -= 2;                                   // strip the quotes
  if (n >= sizeof(name)) return NULL;
  memcpy(name, s, n);
  name[n] = '\0';
  for (int i = 0; i < ORBIT_PEERS_N; i++)
    if (st->peers[i].used && !strcmp(st->peers[i].name, name)) return &st->peers[i];
  for (int i = 0; i < ORBIT_PEERS_N; i++)
    if (!st->peers[i].used) {
      st->peers[i].used = 1;
      memcpy(st->peers[i].name, name, n + 1);
      st->peers[i].max_seq = 0;
      st->peers[i].last_t_ms = 0;
      return &st->peers[i];
    }
  return NULL;
}

static inline int orbit_unhex(uint8_t c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;                                     // lowercase only: the tag we emit is lowercase
}

// THE ONE CALL THE SKETCH MAKES ON RECEIVE. Verifies the tag, applies sender pinning and
// rejects a replayed sequence number, all on the RAW datagram -- before ArduinoJson sees it, so
// nothing an attacker sends reaches the parser, and before the sketch's own (sender, seq) dedup
// table, so a forgery cannot poison it.
//
// ANTI-REPLAY: a sender's seq must strictly exceed the highest one VERIFIED from it. Only
// verified messages move that watermark, so an attacker cannot push it forward and mute a real
// node. A sender whose uptime jumps BACKWARDS by more than restart_slack_ms has rebooted and
// starts again at seq 1 (the ground being restarted mid-demo is the case this exists for), so
// its watermark is cleared -- which is also the residual weakness, stated plainly: a captured
// boot-era datagram (low seq AND low t_ms) satisfies that test and can be replayed once.
//
// A repeat of a datagram already accepted (BUS_TX_REPEAT sends four copies of each) fails with
// ORBIT_ERR_REPLAY because its seq is no longer greater -- which is the correct outcome, the
// copies exist to survive loss and must be acted on exactly once.
static inline OrbitAuthResult orbit_auth_accept(const uint8_t *buf, size_t len, const char *key,
                                                const char *ground, OrbitAuthState *st) {
  static uint8_t canon[ORBIT_CANON_MAX];
  uint8_t want[ORBIT_TAG_BYTES], got[ORBIT_TAG_BYTES], mac[32];
  OrbitEnvelope env;
  OrbitPeer *pe;
  size_t clen;
  // An all-off policy is a pass-through and does not even parse, exactly as Policy.check does on
  // the ground. Not an optimisation: it is the guarantee that a board flashed from
  // secrets.h.example behaves precisely as it did before this header existed, right down to a
  // malformed datagram reaching ArduinoJson and being dropped there rather than here.
  if ((!key || !*key) && (!ground || !*ground)) return ORBIT_OK;
  clen = orbit_canonical(buf, len, canon, sizeof(canon), &env);
  if (clen == 0) return ORBIT_ERR_CANON;

  if (ground && *ground && orbit_is_control(env.type_s, env.type_e) &&
      !orbit_name_is(env.from_s, env.from_e, ground))
    return ORBIT_ERR_PIN;

  if (key && *key) {
    if (!env.auth_s || (size_t)(env.auth_e - env.auth_s) != ORBIT_TAG_HEX + 2) return ORBIT_ERR_NO_TAG;
    for (int i = 0; i < ORBIT_TAG_BYTES; i++) {
      const int hi = orbit_unhex(env.auth_s[1 + i * 2]), lo = orbit_unhex(env.auth_s[2 + i * 2]);
      if (hi < 0 || lo < 0) return ORBIT_ERR_NO_TAG;
      got[i] = (uint8_t)((hi << 4) | lo);
    }
    orbit_hmac_sha256((const uint8_t *)key, strlen(key), canon, clen, mac);
    memcpy(want, mac, ORBIT_TAG_BYTES);
    if (!orbit_ct_eq(want, got, ORBIT_TAG_BYTES)) return ORBIT_ERR_BAD_TAG;
  }

  // The watermark needs the MAC. Without one, "seq must increase" is not anti-replay at all --
  // a sender name is free to spoof, so an attacker would simply send one datagram with a huge
  // seq and mute the real node for the rest of the window, and honest reordering would be lost
  // for nothing. Pinning alone leaves the sketch's own seenBefore() doing what it always did.
  if (!st || !key || !*key) return ORBIT_OK;
  pe = orbit_peer(st, env.from_s, env.from_e);
  if (!pe) { st->rejected++; return ORBIT_ERR_PEERS; }
  {
    const uint32_t slack = st->restart_slack_ms ? st->restart_slack_ms : ORBIT_RESTART_SLACK_MS;
    if (pe->last_t_ms > env.t_ms && pe->last_t_ms - env.t_ms > slack) {
      pe->max_seq = 0;                           // that node rebooted: re-learn its sequence
      pe->last_t_ms = 0;
    }
  }
  if (env.seq <= pe->max_seq) { st->rejected++; return ORBIT_ERR_REPLAY; }
  pe->max_seq = env.seq;
  if (env.t_ms > pe->last_t_ms) pe->last_t_ms = env.t_ms;
  st->accepted++;
  return ORBIT_OK;
}
