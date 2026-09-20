// Host harness for the control-bus authenticator, the way sat_host.cpp is the harness for the
// satellite's decisions and score_host.cpp for its scoring kernel: the SAME header the ESP32
// builds, compiled with no Arduino present, so tests/test_crypto_parity.py can hold it against
// orbit/protocol/auth.py byte for byte.
//
//   g++ -O2 -std=c++17 -Wall -Wextra -Werror -I../satellite_esp32 crypto_host.cpp -o crypto_host
//   ./crypto_host selftest                 # SHA-256/HMAC vectors + canonical + sign/verify
//   ./crypto_host canon                    # stdin: <datagram hex> per line -> <canonical hex> | ERR
//   ./crypto_host hmac <key hex>           # stdin: <message hex> per line  -> 64 hex (full HMAC)
//   ./crypto_host tag <key hex>            # stdin: <datagram hex> per line -> 32 hex tag | ERR
//   ./crypto_host sign <key hex>           # stdin: <datagram hex> per line -> <signed hex> | ERR
//   ./crypto_host accept <key hex> <ground># stdin: <datagram hex> per line -> OK|PIN|REPLAY|...
//
// EVERYTHING crosses the process boundary HEX-ENCODED. The adversarial cases are the whole
// point of this binary and several of them contain raw newlines, NUL bytes and invalid UTF-8
// inside JSON strings; a line-oriented plain-text protocol would quietly eat exactly those.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include "orbit_crypto.h"

// TweetNaCl references randombytes() in its keypair functions (which we never call -- we only
// verify). The symbol still has to link, so provide one; on the device the sketch provides a real
// one from the hardware RNG. Verification is deterministic and never touches this.
extern "C" void randombytes(unsigned char *p, unsigned long long n) {
  for (unsigned long long i = 0; i < n; i++) p[i] = (unsigned char)rand();
}

static int failures = 0;

static void check(bool cond, const char *what) {
  if (!cond) { printf("FAIL %s\n", what); failures++; }
}

static std::string to_hex(const uint8_t *p, size_t n) {
  static const char H[] = "0123456789abcdef";
  std::string s;
  s.reserve(n * 2);
  for (size_t i = 0; i < n; i++) { s += H[p[i] >> 4]; s += H[p[i] & 0xF]; }
  return s;
}

static bool from_hex(const std::string &in, std::vector<uint8_t> &out) {
  if (in.size() % 2) return false;
  out.clear();
  out.reserve(in.size() / 2);
  for (size_t i = 0; i < in.size(); i += 2) {
    int hi = orbit_unhex((uint8_t)in[i]), lo = orbit_unhex((uint8_t)in[i + 1]);
    if (hi < 0 || lo < 0) return false;
    out.push_back((uint8_t)((hi << 4) | lo));
  }
  return true;
}

static bool read_line(std::string &s) {
  s.clear();
  int c;
  while ((c = getchar()) != EOF && c != '\n') if (c != '\r') s += (char)c;
  return !(s.empty() && c == EOF);
}

// The key reaches orbit_auth_sign/accept as a C string, so it cannot contain a NUL byte --
// orbit/protocol/auth.py enforces the same rule on its side rather than letting a key be
// silently truncated at a zero.
static std::string key_from_hex(const char *hex) {
  std::vector<uint8_t> k;
  if (!from_hex(hex, k)) { fprintf(stderr, "bad key hex\n"); exit(2); }
  for (uint8_t b : k) if (b == 0) { fprintf(stderr, "key contains NUL\n"); exit(2); }
  return std::string((const char *)k.data(), k.size());
}

static const char *result_name(OrbitAuthResult r) {
  switch (r) {
    case ORBIT_OK:           return "OK";
    case ORBIT_ERR_CANON:    return "CANON";
    case ORBIT_ERR_NO_TAG:   return "NO_TAG";
    case ORBIT_ERR_BAD_TAG:  return "BAD_TAG";
    case ORBIT_ERR_PIN:      return "PIN";
    case ORBIT_ERR_REPLAY:   return "REPLAY";
    case ORBIT_ERR_NO_SIG:   return "NO_SIG";
    case ORBIT_ERR_BAD_SIG:  return "BAD_SIG";
    default:                 return "PEERS";
  }
}

static std::string canon_of(const std::vector<uint8_t> &d) {
  uint8_t out[ORBIT_CANON_MAX];
  const size_t n = orbit_canonical(d.data(), d.size(), out, sizeof(out), NULL);
  return n ? to_hex(out, n) : std::string("ERR");
}

// A handful of cases stated here rather than only in Python, so a broken header fails its own
// selftest before the parity test has to explain why every vector moved at once.
static void selftest() {
  uint8_t mac[32];
  OrbitSha256 c;

  // --- SHA-256, FIPS 180-4 examples ---------------------------------------------------
  orbit_sha256_init(&c);
  orbit_sha256_final(&c, mac);
  check(to_hex(mac, 32) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "sha256('')");
  orbit_sha256_init(&c);
  orbit_sha256_update(&c, "abc", 3);
  orbit_sha256_final(&c, mac);
  check(to_hex(mac, 32) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "sha256('abc')");
  {   // a multi-block message, to exercise the buffering path
    std::string s(1000, 'a');
    orbit_sha256_init(&c);
    for (int i = 0; i < 1000; i++) orbit_sha256_update(&c, s.data(), 1000);
    orbit_sha256_final(&c, mac);
    check(to_hex(mac, 32) == "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0", "sha256(a*1e6)");
  }

  // --- HMAC-SHA-256, RFC 4231 test case 1 and 2 ---------------------------------------
  {
    const uint8_t k[20] = {0x0b,0x0b,0x0b,0x0b,0x0b,0x0b,0x0b,0x0b,0x0b,0x0b,
                           0x0b,0x0b,0x0b,0x0b,0x0b,0x0b,0x0b,0x0b,0x0b,0x0b};
    orbit_hmac_sha256(k, 20, (const uint8_t *)"Hi There", 8, mac);
    check(to_hex(mac, 32) == "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7", "hmac rfc4231 #1");
    orbit_hmac_sha256((const uint8_t *)"Jefe", 4,
                      (const uint8_t *)"what do ya want for nothing?", 28, mac);
    check(to_hex(mac, 32) == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843", "hmac rfc4231 #2");
  }

  // --- the canonical form --------------------------------------------------------------
  {
    uint8_t out[ORBIT_CANON_MAX];
    const char *doc = "{\"v\":1,\"type\":\"grant\",\"from\":\"g\",\"seq\":2,\"t_ms\":3,\"b\":2,\"a\":1}";
    size_t n = orbit_canonical((const uint8_t *)doc, strlen(doc), out, sizeof(out), NULL);
    check(n && std::string((char *)out, n) == "1|\"grant\"|\"g\"|2|3|{\"a\":1,\"b\":2}", "envelope + sorted body");

    // whitespace is the only thing normalised away
    const char *sp = "{ \"v\" : 1 , \"type\" : \"grant\" , \"from\" : \"g\" , \"seq\" : 2 ,"
                     " \"t_ms\" : 3 , \"b\" : 2 , \"a\" : 1 }";
    size_t m = orbit_canonical((const uint8_t *)sp, strlen(sp), out, sizeof(out), NULL);
    check(m == n, "whitespace does not change the canonical form");

    // `auth` is excluded, and only at the top level
    const char *au = "{\"v\":1,\"type\":\"grant\",\"from\":\"g\",\"seq\":2,\"t_ms\":3,\"b\":2,\"a\":1,"
                     "\"auth\":\"ffffffffffffffffffffffffffffffff\"}";
    check(orbit_canonical((const uint8_t *)au, strlen(au), out, sizeof(out), NULL) == n, "auth excluded");
    const char *nested = "{\"v\":1,\"type\":\"grant\",\"from\":\"g\",\"seq\":2,\"t_ms\":3,\"x\":{\"auth\":1}}";
    size_t q = orbit_canonical((const uint8_t *)nested, strlen(nested), out, sizeof(out), NULL);
    check(q && std::string((char *)out, q).find("\"auth\":1") != std::string::npos, "nested auth is MACed");

    // things that must not canonicalise at all
    const char *bad[] = {
      "{\"v\":1,\"type\":\"a\",\"from\":\"g\",\"seq\":1,\"t_ms\":1,\"a\":1,\"a\":2}",  // duplicate key
      "{\"v\":1,\"type\":\"a\",\"from\":\"g\",\"seq\":1,\"t_ms\":1} ",                  // ok actually (ws)
      "{\"v\":1,\"type\":\"a\",\"from\":\"g\",\"seq\":1,\"t_ms\":1}x",                  // trailing bytes
      "{\"v\":1,\"type\":\"a\",\"from\":\"g\",\"seq\":1}",                              // no t_ms
      "{\"v\":1,\"type\":\"a\",\"from\":7,\"seq\":1,\"t_ms\":1}",                       // from not a string
      "{\"v\":1,\"type\":\"a\",\"from\":\"g\",\"seq\":-1,\"t_ms\":1}",                  // seq not a u32
      "{\"v\":1,\"type\":\"a\",\"from\":\"g\",\"seq\":1,\"t_ms\":1,\"x\":nan}",         // ArduinoJson's nan
      "{\"v\":1,\"type\":\"a\",\"from\":\"g\",\"seq\":1,\"t_ms\":1,\"x\":01}",          // leading zero
      "{\"v\":1,\"type\":\"a\",\"from\":\"g\",\"seq\":1,\"t_ms\":1,\"x\":.5}",          // bare fraction
      "{\"v\":1,\"type\":\"a\",\"from\":\"g\",\"seq\":1,\"t_ms\":1,\"x\":\"\\q\"}",     // bad escape
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(*bad); i++) {
      const size_t r = orbit_canonical((const uint8_t *)bad[i], strlen(bad[i]), out, sizeof(out), NULL);
      check((i == 1) ? r != 0 : r == 0, bad[i]);
    }
  }

  // --- sign / accept round trip ---------------------------------------------------------
  {
    const char *key = "not-a-real-key-0000000000000000";
    uint8_t buf[256];
    const char *doc = "{\"v\":1,\"type\":\"grant\",\"from\":\"ground-x\",\"seq\":5,\"t_ms\":900,\"to\":\"b\"}";
    size_t n = strlen(doc);
    memcpy(buf, doc, n);
    const size_t signed_len = orbit_auth_sign(buf, n, sizeof(buf), key);
    check(signed_len == n + ORBIT_AUTH_OVERHEAD, "sign adds exactly 42 bytes");
    check(buf[signed_len - 1] == '}', "signed datagram still ends in }");

    OrbitAuthState st;
    memset(&st, 0, sizeof(st));
    check(orbit_auth_accept(buf, signed_len, key, "ground-x", nullptr, &st) == ORBIT_OK, "accept own signature");
    // the same datagram again: seq is no longer ahead of the watermark
    check(orbit_auth_accept(buf, signed_len, key, "ground-x", nullptr, &st) == ORBIT_ERR_REPLAY, "replay rejected");
    // pinned to the wrong ground
    memset(&st, 0, sizeof(st));
    check(orbit_auth_accept(buf, signed_len, key, "someone-else", nullptr, &st) == ORBIT_ERR_PIN, "sender pinning");
    // one flipped bit anywhere in the body
    memset(&st, 0, sizeof(st));
    buf[30] ^= 0x01;
    check(orbit_auth_accept(buf, signed_len, key, "ground-x", nullptr, &st) != ORBIT_OK, "tampered body rejected");
    buf[30] ^= 0x01;
    // the wrong key
    memset(&st, 0, sizeof(st));
    check(orbit_auth_accept(buf, signed_len, "another-key", "ground-x", nullptr, &st) == ORBIT_ERR_BAD_TAG, "wrong key");
    // no tag at all
    memset(&st, 0, sizeof(st));
    check(orbit_auth_accept((const uint8_t *)doc, n, key, "ground-x", nullptr, &st) == ORBIT_ERR_NO_TAG, "untagged");
    // a satellite type is not pinned to the ground name
    memset(&st, 0, sizeof(st));
    uint8_t sb[256];
    const char *sd = "{\"v\":1,\"type\":\"heartbeat\",\"from\":\"esp32-satellite-b\",\"seq\":1,\"t_ms\":5}";
    size_t sn = strlen(sd);
    memcpy(sb, sd, sn);
    sn = orbit_auth_sign(sb, sn, sizeof(sb), key);
    check(orbit_auth_accept(sb, sn, key, "ground-x", nullptr, &st) == ORBIT_OK, "peer traffic not pinned");

    // --- an unconfigured board: no key, no ground name --------------------------------
    // A pass-through. No watermark (that needs the MAC), no canonical check, no pin.
    memset(&st, 0, sizeof(st));
    check(orbit_auth_accept((const uint8_t *)doc, n, "", "", nullptr, &st) == ORBIT_OK, "no key, no pin: pass 1");
    check(orbit_auth_accept((const uint8_t *)doc, n, "", "", nullptr, &st) == ORBIT_OK, "no key, no pin: pass 2");
    check(orbit_auth_accept((const uint8_t *)"{not json", 9, "", "", nullptr, &st) == ORBIT_OK, "no key, no pin: garbage");
    // Pinning alone, still no key: the pin bites, the watermark does not.
    memset(&st, 0, sizeof(st));
    check(orbit_auth_accept((const uint8_t *)doc, n, "", "ground-x", nullptr, &st) == ORBIT_OK, "pin only: pass 1");
    check(orbit_auth_accept((const uint8_t *)doc, n, "", "ground-x", nullptr, &st) == ORBIT_OK, "pin only: repeat is fine");
    check(orbit_auth_accept((const uint8_t *)doc, n, "", "someone-else", nullptr, &st) == ORBIT_ERR_PIN, "pin only: pinned");
  }

  // --- Ed25519 (TweetNaCl), held to the RFC 8032 vectors through the detached-verify wrapper ---
  {
    std::vector<uint8_t> pk, sig, m3, pk3, sig3;
    from_hex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", pk);      // Test 1 pk
    from_hex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a3"
             "3bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b", sig);                 // Test 1 sig, empty msg
    check(orbit_ed25519_verify(pk.data(), (const uint8_t *)"", 0, sig.data()) == 1, "ed25519 RFC8032 vector 1");
    from_hex("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", pk3);     // Test 3 pk
    from_hex("6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f"
             "290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a", sig3);                 // Test 3 sig
    from_hex("af82", m3);
    check(orbit_ed25519_verify(pk3.data(), m3.data(), m3.size(), sig3.data()) == 1, "ed25519 RFC8032 vector 3");
    sig3[0] ^= 0x01;
    check(orbit_ed25519_verify(pk3.data(), m3.data(), m3.size(), sig3.data()) == 0, "ed25519 tampered sig rejected");
    check(orbit_ed25519_verify(pk.data(), (const uint8_t *)"x", 1, sig.data()) == 0, "ed25519 wrong message rejected");
  }

  if (failures == 0) printf("OK: crypto, canonical, policy and Ed25519 checks\n");
}

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "selftest";
  std::string line;
  std::vector<uint8_t> d;

  if (!strcmp(mode, "selftest")) {
    selftest();
    return failures ? 1 : 0;
  }

  if (!strcmp(mode, "canon")) {
    while (read_line(line)) {
      if (!from_hex(line, d)) { printf("ERR\n"); continue; }
      printf("%s\n", canon_of(d).c_str());
    }
    return 0;
  }

  if (!strcmp(mode, "hmac") && argc > 2) {
    const std::string key = key_from_hex(argv[2]);
    uint8_t mac[32];
    while (read_line(line)) {
      if (!from_hex(line, d)) { printf("ERR\n"); continue; }
      orbit_hmac_sha256((const uint8_t *)key.data(), key.size(), d.data(), d.size(), mac);
      printf("%s\n", to_hex(mac, 32).c_str());
    }
    return 0;
  }

  if (!strcmp(mode, "tag") && argc > 2) {
    const std::string key = key_from_hex(argv[2]);
    uint8_t out[ORBIT_CANON_MAX];
    char tag[ORBIT_TAG_HEX + 1];
    while (read_line(line)) {
      if (!from_hex(line, d)) { printf("ERR\n"); continue; }
      const size_t n = orbit_canonical(d.data(), d.size(), out, sizeof(out), NULL);
      if (!n) { printf("ERR\n"); continue; }
      orbit_tag_hex(key.c_str(), out, n, tag);
      printf("%s\n", tag);
    }
    return 0;
  }

  if (!strcmp(mode, "sign") && argc > 2) {
    const std::string key = key_from_hex(argv[2]);
    std::vector<uint8_t> buf(2048);
    while (read_line(line)) {
      if (!from_hex(line, d) || d.size() + ORBIT_AUTH_OVERHEAD > buf.size()) { printf("ERR\n"); continue; }
      memcpy(buf.data(), d.data(), d.size());
      const size_t n = orbit_auth_sign(buf.data(), d.size(), buf.size(), key.c_str());
      printf("%s\n", n ? to_hex(buf.data(), n).c_str() : "ERR");
    }
    return 0;
  }

  if (!strcmp(mode, "accept") && argc > 3) {
    const std::string key = key_from_hex(argv[2]);
    std::vector<uint8_t> pk;                 // optional 4th arg: the ground's Ed25519 public key (hex)
    const uint8_t *pkp = nullptr;
    if (argc > 4 && *argv[4]) {
      if (!from_hex(argv[4], pk) || pk.size() != 32) { fprintf(stderr, "bad ground pubkey hex\n"); return 2; }
      pkp = pk.data();
    }
    OrbitAuthState st;
    memset(&st, 0, sizeof(st));
    while (read_line(line)) {
      if (!from_hex(line, d)) { printf("ERR\n"); continue; }
      printf("%s\n", result_name(orbit_auth_accept(d.data(), d.size(), key.c_str(), argv[3], pkp, &st)));
    }
    return 0;
  }

  fprintf(stderr, "usage: %s selftest|canon|hmac <keyhex>|tag <keyhex>|sign <keyhex>|accept <keyhex> <ground>\n",
          argv[0]);
  return 2;
}
