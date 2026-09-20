// Orbit satellite node, ESP32-S3. Speaks the v1 wire protocol on the UDP multicast bus
// (orbit/protocol/messages.py) and behaves as orbit/sim/satellite.py does -- that file is
// the executable spec for this one, and the ground re-scores every frame it receives with
// the golden model, so any drift here surfaces as a mismatch on the dashboard.
//
// Autonomous by design: it captures, scores, admits, evicts, queues and bids on its own.
// The only thing the ground ever says is "you may transmit this item now".
//
// Identity is a build flag, so B and C are the same binary with -DSAT_ID:
//   arduino-cli compile --build-property build.extra_flags=-DSAT_ID='"b"' ...
//
// Frames come from LittleFS: /frames/NNN.bin, FRAME_BYTES each, 8-bit gray 128x128,
// plus /ref.bin, the reference the change term is measured against.

#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <LittleFS.h>
#include <ArduinoJson.h>
#include <mbedtls/base64.h>

#include "secrets.h"
#include "orbit_config.h"
#include "orbit_score.h"
#include "orbit_queue.h"

// Identity comes in as a bare token (-DSAT_ID=c) and is stringified here, so the build
// flag needs no nested quotes and cannot be mangled by a shell.
#ifndef SAT_ID
#define SAT_ID b
#endif
#define ORBIT_STR2(x) #x
#define ORBIT_STR(x) ORBIT_STR2(x)
#ifndef RGB_BUILTIN
#define RGB_BUILTIN 48
#endif

static const char *HOSTNAME = "esp32-satellite-" ORBIT_STR(SAT_ID);

// ---------------------------------------------------------------- state

struct Item {
  bool     used = false;
  uint16_t item_id = 0;
  uint16_t raw_score = 0;
  uint32_t captured_ms = 0;
};

static WiFiUDP      udp;
static PriorityQueue queue;
static FrameBuffer  fbuf;
static Item         items[SAT_BUFFER_SLOTS];

// The change term is measured against the frame's OWN scene reference, exactly as
// orbit/sim/satellite.py does (corpus.reference_for). /manifest.json maps each frame to
// its reference; references are stored once in /refs and cached one at a time, because
// two 16 KB buffers is all the RAM this can spare beside the WiFi stack.
static uint8_t  refFrame[FRAME_BYTES];      // currently cached reference
static int      refCached = -1;             // which /refs id is in refFrame, -1 = none
static uint8_t  scratch[FRAME_BYTES];       // one frame in flight from LittleFS

static const int MAX_FRAMES = 128;
static uint16_t  frameRef[MAX_FRAMES];      // frame index -> reference id

static uint32_t g_seq = 0;
static uint16_t g_next_item_id = 1;
static uint32_t g_next_capture_ms = 0;
static uint32_t g_next_heartbeat_ms = 0;

static uint16_t g_frame_count = 0;          // frames present on LittleFS
static uint16_t g_frame_pos = 0;            // capture cursor

static uint32_t c_captured = 0, c_evicted = 0, c_rejected = 0, c_bids = 0;
static uint32_t c_grants = 0, c_transmitted = 0, c_failed = 0, c_revoked = 0;
static uint32_t c_ack_lost = 0, c_peer_grants = 0;

// in-flight transmission
static bool     tx_active = false;
static int32_t  tx_round = -1;
static uint16_t tx_item = 0;
static float    tx_pace_bps = 0;
static uint32_t tx_total = 0;
static uint16_t tx_chunks = 0, tx_next_idx = 0;
static uint32_t tx_next_at_ms = 0;
static bool     tx_done_sent = false;
static int      tx_pass = 0;        // which full repeat pass of the chunk sequence

// The exact tx_done datagram, kept so it can be RE-SENT BYTE FOR BYTE while waiting for
// the ack. Same bytes means same seq, so a copy the ground already saw is deduped, while
// a first copy that was lost still arrives. Observed: a frame whose 19/19 chunks all
// landed was still never acked, because tx_done alone was lost -- the ground had the
// whole frame and could not know.
static uint8_t  tx_done_buf[BUS_MAX_DATAGRAM];
static size_t   tx_done_len = 0;
static uint32_t tx_done_resend_at = 0;

// awaiting tx_ack
static bool     await_ack = false;
static uint16_t await_item = 0;
static uint32_t await_since_ms = 0;
static int32_t  await_round = -1;

// ---------------------------------------------------------------- helpers

static int32_t queue_key(uint16_t raw) {
  // orbit/sim/satellite.py _queue_key: round(display(raw) * 100). No exact .5 case exists
  // for any raw (65535 is odd), so Python's banker's rounding and lround cannot diverge.
  return (int32_t)lround((double)raw * 10000.0 / 65535.0);
}

static Item *findItem(uint16_t id) {
  for (int i = 0; i < SAT_BUFFER_SLOTS; i++) if (items[i].used && items[i].item_id == id) return &items[i];
  return nullptr;
}
static Item *freeItemSlot() {
  for (int i = 0; i < SAT_BUFFER_SLOTS; i++) if (!items[i].used) return &items[i];
  return nullptr;
}

static void fillEnvelope(JsonDocument &d, const char *type) {
  d["v"] = ORBIT_PROTOCOL_VERSION;
  d["type"] = type;
  d["from"] = HOSTNAME;
  d["seq"] = ++g_seq;
  d["t_ms"] = (uint32_t)millis();
}

// Every datagram goes out BUS_TX_REPEAT times.
//
// Measured on an iPhone Personal Hotspot: 35.7% of multicast datagrams are lost (counted
// by gaps in this node's own `seq`). APs send multicast at the lowest basic rate with no
// link-layer ack or retry, and phone hotspots are especially poor at it. At that rate a
// 19-chunk frame essentially never arrives intact.
//
// Duplicates are safe by design, not by luck: the ground's bus keeps a `dedup_window` of
// (sender, seq) pairs precisely because "UDP may duplicate" (orbit/bus/base.py). Repeats
// carry the SAME seq, so the ground collapses them and the protocol is unchanged.
//
// 1 copy -> 64% delivered, 2 -> 87%, 3 -> 95%. Set to 1 on a wired or well-behaved AP.
static void sendDoc(JsonDocument &d, int repeat = BUS_TX_REPEAT) {
  uint8_t buf[BUS_MAX_DATAGRAM];
  size_t n = serializeJson(d, buf, sizeof(buf));
  if (n == 0 || n >= sizeof(buf)) {
    Serial.printf("[bus] datagram too large (%u), dropped\n", (unsigned)n);
    return;
  }
  for (int i = 0; i < repeat; i++) {
    udp.beginPacket(MCAST_GROUP, MCAST_PORT);
    udp.write(buf, n);
    udp.endPacket();
    if (i + 1 < repeat) delay(BUS_TX_REPEAT_GAP_MS);  // spread across the loss bursts
  }
}

static void addBufferStats(JsonObject o) {
  const int used = fbuf.used(), slots = fbuf.slots();
  o["slots"] = slots;
  o["capacity_bytes"] = fbuf.capacityBytes();
  o["used"] = used;
  o["free"] = slots - used;
  // one decimal, matching round(x, 1) in BufferStats
  o["occupancy_pct"] = slots ? lround(1000.0 * used / slots) / 10.0 : 0.0;
}

static float ageS(uint32_t captured_ms) { return (millis() - captured_ms) / 1000.0f; }
static float round3(float v) { return lroundf(v * 1000.0f) / 1000.0f; }

// ---------------------------------------------------------------- messages out

static void sendEviction(const Item &lost, const char *kind, const Item *by) {
  JsonDocument d;
  fillEnvelope(d, "eviction");
  d["item_id"] = lost.item_id;
  d["score"] = score_display(lost.raw_score);
  d["kind"] = kind;
  d["displaced_by"] = by ? (int)by->item_id : -1;
  d["displaced_by_score"] = by ? score_display(by->raw_score) : -1.0f;
  sendDoc(d);
  Serial.printf("[lost] item=%u kind=%s score=%.1f\n", lost.item_id, kind, score_display(lost.raw_score));
}

static void sendHeartbeat() {
  JsonDocument d;
  fillEnvelope(d, "heartbeat");
  addBufferStats(d["buffer"].to<JsonObject>());
  d["eviction_count"] = c_evicted + c_rejected;
  d["queue_len"] = queue.size();
  if (queue.hasData()) {
    Item *t = findItem(queue.at(0).item_id);
    d["top_score"] = t ? score_display(t->raw_score) : -1.0f;
    d["top_item_id"] = t ? (int)t->item_id : -1;
  } else {
    d["top_score"] = -1.0f;
    d["top_item_id"] = -1;
  }
  d["uptime_s"] = round3(millis() / 1000.0f);
  sendDoc(d);
}

static void sendBid(int32_t round_id) {
  Item *top = findItem(queue.at(0).item_id);
  if (!top) return;
  JsonDocument d;
  fillEnvelope(d, "bid");
  d["round_id"] = round_id;
  d["item_id"] = top->item_id;
  d["score"] = score_display(top->raw_score);
  d["item_age_s"] = round3(ageS(top->captured_ms));
  JsonArray w = d["window"].to<JsonArray>();
  for (int i = 1; i < queue.size() && i <= BID_WINDOW_N; i++) {
    Item *it = findItem(queue.at(i).item_id);
    if (!it) continue;
    JsonObject e = w.add<JsonObject>();
    e["item_id"] = it->item_id;
    e["score"] = score_display(it->raw_score);
    e["item_age_s"] = round3(ageS(it->captured_ms));
  }
  addBufferStats(d["buffer"].to<JsonObject>());
  d["eviction_count"] = c_evicted + c_rejected;
  d["queue_len"] = queue.size();
  sendDoc(d);
  c_bids++;
}

static void sendTxChunk(uint16_t idx) {
  const uint8_t *data = fbuf.read(tx_item);
  if (!data) return;
  const uint32_t off = (uint32_t)idx * CHUNK_BYTES;
  const size_t   len = (off + CHUNK_BYTES <= tx_total) ? CHUNK_BYTES : (tx_total - off);
  unsigned char  b64[CHUNK_BYTES * 4 / 3 + 8];
  size_t         b64len = 0;
  if (mbedtls_base64_encode(b64, sizeof(b64), &b64len, data + off, len) != 0) {
    Serial.println("[tx] base64 failed");
    return;
  }
  JsonDocument d;
  fillEnvelope(d, "tx_chunk");
  d["round_id"] = tx_round;
  d["item_id"] = tx_item;
  d["idx"] = idx;
  d["n"] = tx_chunks;
  d["data"] = (const char *)b64;     // already base64; ArduinoJson will not re-encode
  sendDoc(d, 1);                     // one copy: TX_PASSES supplies the redundancy

}

// ---------------------------------------------------------------- capture / admit

static bool loadBlob(const char *path, uint8_t *dst) {
  File f = LittleFS.open(path, "r");
  if (!f) { Serial.printf("[fs] missing %s\n", path); return false; }
  const size_t n = f.read(dst, FRAME_BYTES);
  f.close();
  if (n != FRAME_BYTES) { Serial.printf("[fs] %s short: %u\n", path, (unsigned)n); return false; }
  return true;
}

static bool loadFrame(uint16_t idx, uint8_t *dst) {
  char path[32];
  snprintf(path, sizeof(path), "/frames/%03u.bin", idx);
  return loadBlob(path, dst);
}

// Ensures refFrame holds the reference for frame `idx`. One-entry cache: the capture
// order groups scenes, so this reloads rarely.
static bool ensureRef(uint16_t idx) {
  if (idx >= MAX_FRAMES) return false;
  const int want = frameRef[idx];
  if (refCached == want) return true;
  char path[32];
  snprintf(path, sizeof(path), "/refs/%03u.bin", (unsigned)want);
  if (!loadBlob(path, refFrame)) return false;
  refCached = want;
  return true;
}

static void admit(uint16_t raw, const uint8_t *data) {
  const int32_t key = queue_key(raw);

  if (fbuf.freeSlots() > 0) {
    Item *slot = freeItemSlot();
    if (!slot) return;
    slot->used = true;
    slot->item_id = g_next_item_id++;
    slot->raw_score = raw;
    slot->captured_ms = millis();
    fbuf.store(slot->item_id, data);
    queue.insert(key, slot->item_id);
    return;
  }

  // Full. The queue tail is the worst held item; whatever is in flight is never a candidate.
  const QCell tail = queue.at(queue.size() - 1);
  const uint16_t in_flight = tx_active ? tx_item : (await_ack ? await_item : NO_FRAME);
  if (key <= tail.key || tail.item_id == in_flight) {
    Item newcomer;
    newcomer.used = true;
    newcomer.item_id = g_next_item_id++;
    newcomer.raw_score = raw;
    newcomer.captured_ms = millis();
    c_rejected++;
    sendEviction(newcomer, "rejected", nullptr);
    return;
  }

  // The newcomer beats the worst held frame: the tail leaves, permanently.
  Item *lost = findItem(tail.item_id);
  Item lostCopy;
  if (lost) { lostCopy = *lost; lost->used = false; }
  queue.removeItem(tail.item_id);
  fbuf.release(tail.item_id);

  Item *slot = freeItemSlot();
  if (!slot) return;
  slot->used = true;
  slot->item_id = g_next_item_id++;
  slot->raw_score = raw;
  slot->captured_ms = millis();
  fbuf.store(slot->item_id, data);
  queue.insert(key, slot->item_id);
  c_evicted++;
  sendEviction(lostCopy, "evicted", slot);
}

static void capture() {
  if (g_frame_count == 0) return;
  const uint16_t idx = g_frame_pos % g_frame_count;
  g_frame_pos++;
  if (!loadFrame(idx, scratch)) return;
  if (!ensureRef(idx)) { Serial.printf("[fs] no reference for frame %u, skipped\n", idx); return; }
  const uint32_t t0 = micros();
  const Scored s = orbit_score_frame(scratch, refFrame);
  const uint32_t us = micros() - t0;
  c_captured++;
  Serial.printf("[cap] frame=%u score=%u (%.1f) cloud=%u chg=%u %ums\n",
                idx, s.score, score_display(s.score), s.cloud_px, s.changed_px, us / 1000);
  admit(s.score, scratch);
}

// ---------------------------------------------------------------- messages in

static void onOffersOpen(JsonDocument &d) {
  const int32_t round_id = d["round_id"] | -1;
  if (await_ack && round_id > await_round) {
    c_ack_lost++;                       // the ack was lost and the ground has moved on
    await_ack = false;
    Serial.println("[ack] lost, ground moved on: keeping the frame");
  }
  if (tx_active || await_ack || !queue.hasData()) return;
  sendBid(round_id);
}

static void onGrant(JsonDocument &d) {
  const char *to = d["to"] | "";
  if (strcmp(to, HOSTNAME) != 0) { c_peer_grants++; return; }
  const uint16_t item_id = d["item_id"] | 0;
  Item *it = findItem(item_id);
  if (!it) { Serial.printf("[grant] unknown item %u\n", item_id); return; }
  c_grants++;
  tx_active = true;
  tx_round = d["round_id"] | -1;
  tx_item = item_id;
  tx_pace_bps = d["pace_bps"] | 65536.0f;
  tx_total = FRAME_BYTES;
  tx_chunks = (uint16_t)((tx_total + CHUNK_BYTES - 1) / CHUNK_BYTES);
  tx_next_idx = 0;
  tx_next_at_ms = millis();
  tx_done_sent = false;
  tx_pass = 0;

  JsonDocument o;
  fillEnvelope(o, "tx_begin");
  o["round_id"] = tx_round;
  o["item_id"] = tx_item;
  o["total_bytes"] = tx_total;
  o["chunks"] = tx_chunks;
  sendDoc(o);
  Serial.printf("[grant] item=%u %u chunks @ %.0f bps\n", tx_item, tx_chunks, tx_pace_bps);
}

static void onRevoke(JsonDocument &d) {
  if (strcmp(d["to"] | "", HOSTNAME) != 0) return;
  c_revoked++;
  tx_active = false;
  await_ack = false;
  Serial.printf("[revoke] item=%u reason=%s\n", (unsigned)(d["item_id"] | 0), (const char *)(d["reason"] | "?"));
}

static void onTxAck(JsonDocument &d) {
  if (strcmp(d["to"] | "", HOSTNAME) != 0) return;
  const uint16_t item_id = d["item_id"] | 0;
  if (!await_ack || item_id != await_item) return;
  await_ack = false;
  if (!(d["ok"] | false)) {
    c_failed++;                        // keep the frame: a failed transmission must not lose data
    Serial.printf("[nack] item=%u reason=%s\n", item_id, (const char *)(d["reason"] | "?"));
    return;
  }
  queue.removeItem(item_id);
  Item *it = findItem(item_id);
  if (it) it->used = false;
  fbuf.release(item_id);
  c_transmitted++;
  Serial.printf("[ack ] item=%u confirmed, popped\n", item_id);
}

// Duplicate suppression, the mirror of the ground's dedup_window (orbit/bus/base.py).
//
// Not optional once anything repeats datagrams: a redundant `grant` processed twice
// restarts the transmission from chunk 0, so the copies meant to ADD reliability instead
// corrupt the transfer. Keyed on (sender, seq) like the ground; a small ring is enough
// because only the ground talks to us and its seq advances monotonically.
static const int DEDUP_N = 64;
static uint32_t dedup_hash[DEDUP_N];
static uint32_t dedup_seq[DEDUP_N];
static int      dedup_pos = 0;

static uint32_t fnv1a(const char *s) {
  uint32_t h = 2166136261u;
  while (*s) { h ^= (uint8_t)*s++; h *= 16777619u; }
  return h;
}

// true if (sender, seq) has been seen before; records it otherwise.
static bool seenBefore(const char *sender, uint32_t seq) {
  const uint32_t h = fnv1a(sender);
  for (int i = 0; i < DEDUP_N; i++) {
    if (dedup_hash[i] == h && dedup_seq[i] == seq) return true;
  }
  dedup_hash[dedup_pos] = h;
  dedup_seq[dedup_pos] = seq;
  dedup_pos = (dedup_pos + 1) % DEDUP_N;
  return false;
}

static void pollBus() {
  int n;
  while ((n = udp.parsePacket()) > 0) {
    if (n > (int)BUS_MAX_DATAGRAM) { udp.flush(); continue; }
    uint8_t buf[BUS_MAX_DATAGRAM + 1];
    const int len = udp.read(buf, BUS_MAX_DATAGRAM);
    if (len <= 0) continue;
    buf[len] = 0;
    JsonDocument d;
    if (deserializeJson(d, buf, len) != DeserializationError::Ok) continue;
    if ((d["v"] | 0) != ORBIT_PROTOCOL_VERSION) continue;
    const char *from = d["from"] | "";
    if (strcmp(from, HOSTNAME) == 0) continue;          // our own multicast echo
    if (seenBefore(from, d["seq"] | 0)) continue;       // a repeat: act on it exactly once
    const char *type = d["type"] | "";
    if      (!strcmp(type, "offers_open")) onOffersOpen(d);
    else if (!strcmp(type, "grant"))       onGrant(d);
    else if (!strcmp(type, "revoke"))      onRevoke(d);
    else if (!strcmp(type, "tx_ack"))      onTxAck(d);
  }
}

// Chunks are repeated as WHOLE PASSES, not per-chunk.
//
// Measured: WiFi multicast loss here is bursty, not independent. A single ~500 ms outage
// took out chunks 2,3,4,5,6 together. Per-chunk redundancy cannot survive that, because
// every copy of a chunk lands inside the same outage. Repeating the whole sequence puts
// each chunk's copies seconds apart, which is longer than the bursts.
//
// The ground keys chunks by idx, so a re-sent chunk is idempotent. tx_done is sent only
// after the final pass, so the ground sees one transmission, not TX_PASSES of them.
static void pumpTx() {
  if (!tx_active) return;
  const uint32_t now = millis();
  while (tx_next_idx < tx_chunks && (int32_t)(now - tx_next_at_ms) >= 0) {
    const uint32_t off = (uint32_t)tx_next_idx * CHUNK_BYTES;
    const size_t   len = (off + CHUNK_BYTES <= tx_total) ? CHUNK_BYTES : (tx_total - off);
    sendTxChunk(tx_next_idx);
    tx_next_idx++;
    tx_next_at_ms += (uint32_t)(len * 8u * 1000.0f / tx_pace_bps);   // paced at the rate the ground quoted
  }
  if (tx_next_idx >= tx_chunks && tx_pass + 1 < TX_PASSES) {
    tx_pass++;
    tx_next_idx = 0;                   // another full pass, starting now
    Serial.printf("[tx  ] item=%u pass %d/%d\n", tx_item, tx_pass + 1, TX_PASSES);
    return;
  }
  if (tx_next_idx >= tx_chunks && !tx_done_sent) {
    tx_done_sent = true;
    Item *it = findItem(tx_item);
    JsonDocument d;
    fillEnvelope(d, "tx_done");
    d["round_id"] = tx_round;
    d["item_id"] = tx_item;
    d["total_bytes"] = tx_total;
    d["score"] = it ? score_display(it->raw_score) : 0.0f;
    tx_done_len = serializeJson(d, tx_done_buf, sizeof(tx_done_buf));
    for (int i = 0; i < BUS_TX_REPEAT; i++) {
      udp.beginPacket(MCAST_GROUP, MCAST_PORT);
      udp.write(tx_done_buf, tx_done_len);
      udp.endPacket();
      if (i + 1 < BUS_TX_REPEAT) delay(BUS_TX_REPEAT_GAP_MS);
    }
    tx_done_resend_at = millis() + TX_DONE_RESEND_MS;
    await_ack = true;
    await_item = tx_item;
    await_since_ms = millis();
    await_round = tx_round;
    tx_active = false;
    Serial.printf("[tx  ] item=%u done, %u bytes\n", tx_item, tx_total);
  }
}

// ---------------------------------------------------------------- setup / loop

static void led(uint8_t r, uint8_t g, uint8_t b) { rgbLedWrite(RGB_BUILTIN, r, g, b); }

// Joining must never block forever without saying why. The ESP32-S3 radio is 2.4 GHz
// only, so the most likely failure on a venue network is a 5 GHz-only SSID -- which
// looks identical to a wrong password unless the scan is printed. So it is printed.
static void scanAndReport() {
  Serial.println("[wifi] scanning 2.4 GHz (this radio cannot see 5 GHz at all)");
  const int n = WiFi.scanNetworks();
  if (n <= 0) { Serial.println("[wifi] no 2.4 GHz networks in range"); return; }
  bool seen = false;
  for (int i = 0; i < n; i++) {
    const bool match = WiFi.SSID(i) == WIFI_SSID;
    seen |= match;
    Serial.printf("[wifi]   %-32s ch%-3d %4d dBm%s\n", WiFi.SSID(i).c_str(), WiFi.channel(i),
                  WiFi.RSSI(i), match ? "   <-- target" : "");
  }
  if (!seen) Serial.printf("[wifi] '%s' is NOT on 2.4 GHz here: wrong SSID, out of range, or 5 GHz-only\n", WIFI_SSID);
  WiFi.scanDelete();
}

static bool connectWiFi() {
  for (int attempt = 1; ; attempt++) {
    const bool open_net = (WIFI_PASSWORD[0] == '\0');
    Serial.printf("[wifi] joining %s (attempt %d, %s)", WIFI_SSID, attempt, open_net ? "open" : "wpa");
    if (open_net) WiFi.begin(WIFI_SSID);          // open networks take no passphrase
    else          WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    const uint32_t deadline = millis() + 15000;
    while (WiFi.status() != WL_CONNECTED && (int32_t)(millis() - deadline) < 0) {
      delay(400);
      Serial.print(".");
    }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("\n[wifi] %s  ch%d  rssi %d dBm\n",
                    WiFi.localIP().toString().c_str(), WiFi.channel(), WiFi.RSSI());
      return true;
    }
    Serial.printf("\n[wifi] failed (status %d) after 15 s\n", (int)WiFi.status());
    WiFi.disconnect();
    led(32, 8, 0);
    scanAndReport();          // say WHY, then keep trying: the hotspot may not be up yet
    led(16, 0, 0);
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\n=== orbit satellite %s ===\n", HOSTNAME);
  Serial.printf("chip %s rev %d, heap %u, psram %u\n",
                ESP.getChipModel(), ESP.getChipRevision(),
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getPsramSize());
  led(16, 0, 0);

  if (!LittleFS.begin(false)) { Serial.println("FATAL: LittleFS mount failed (upload the image set)"); }
  else {
    File m = LittleFS.open("/manifest.json", "r");
    if (!m) {
      Serial.println("FATAL: /manifest.json missing -- upload the image set");
    } else {
      JsonDocument md;
      if (deserializeJson(md, m) != DeserializationError::Ok) {
        Serial.println("FATAL: /manifest.json is not valid JSON");
      } else {
        JsonArray arr = md["frames"].as<JsonArray>();
        for (JsonObject e : arr) {
          const uint16_t i = e["frame"] | 0;
          if (i < MAX_FRAMES) { frameRef[i] = e["ref"] | 0; if (i + 1 > g_frame_count) g_frame_count = i + 1; }
        }
        Serial.printf("[fs] manifest: %u frames, satellite %s\n", g_frame_count, (const char *)(md["sat"] | "?"));
      }
      m.close();
    }
  }

  if (!fbuf.begin(SAT_BUFFER_SLOTS)) { Serial.println("FATAL: frame pool allocation failed"); }
  Serial.printf("[buf] %d slots x %u B in %s\n", fbuf.slots(), FRAME_BYTES, fbuf.inPsram() ? "PSRAM" : "internal RAM");
  queue.begin(SAT_BUFFER_SLOTS);

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  // Modem sleep must be OFF. With power save on (the Arduino default) the station only
  // wakes for DTIM beacons, and the AP's buffered MULTICAST is dropped rather than
  // delivered -- the node keeps heartbeating (its own transmits are unaffected) while
  // silently missing offers_open and grant. It looks like the ground ignoring the node.
  // Costs ~40 mA; correctness on the control path is worth more than that here.
  WiFi.setSleep(false);
  connectWiFi();

  if (MDNS.begin(HOSTNAME)) Serial.printf("[mdns] %s.local\n", HOSTNAME);
  else Serial.println("[mdns] failed");

  if (udp.beginMulticast(IPAddress(239, 255, 42, 99), MCAST_PORT)) Serial.printf("[bus] joined %s:%u\n", MCAST_GROUP, MCAST_PORT);
  else Serial.println("[bus] FATAL: multicast join failed");

  g_next_capture_ms = millis() + (uint32_t)(SAT_CAPTURE_PERIOD_S * 1000);
  g_next_heartbeat_ms = millis();
  led(0, 0, 16);
  Serial.println("[ok  ] online");
}

void loop() {
  const uint32_t now = millis();
  pollBus();
  if ((int32_t)(now - g_next_capture_ms) >= 0) {
    led(0, 16, 0);
    capture();
    led(0, 0, 16);
    g_next_capture_ms += (uint32_t)(SAT_CAPTURE_PERIOD_S * 1000);
  }
  pumpTx();
  if (await_ack && tx_done_len && (int32_t)(now - tx_done_resend_at) >= 0) {
    udp.beginPacket(MCAST_GROUP, MCAST_PORT);     // identical bytes: the ground dedups
    udp.write(tx_done_buf, tx_done_len);
    udp.endPacket();
    tx_done_resend_at = now + TX_DONE_RESEND_MS;
  }
  if (await_ack && (now - await_since_ms) > SAT_ACK_TIMEOUT_MS) {
    // A lost ack must not wedge the node. We cannot know whether the ground counted the
    // frame, so we keep it (never lose data on uncertainty) and resume bidding; at worst
    // the ground receives it twice. Same rule as the simulator's _give_up_ack.
    c_ack_lost++;
    await_ack = false;
    Serial.printf("[ack ] timeout on item=%u, keeping frame\n", await_item);
  }
  if ((int32_t)(now - g_next_heartbeat_ms) >= 0) {
    sendHeartbeat();
    g_next_heartbeat_ms += SAT_HEARTBEAT_MS;
  }
  delay(2);
}
