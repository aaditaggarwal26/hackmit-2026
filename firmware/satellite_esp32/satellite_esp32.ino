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
#include <mbedtls/sha256.h>

#include "secrets.h"
#include "orbit_config.h"
#include "orbit_faults.h"
#include "orbit_score.h"
#include "orbit_queue.h"
#include "orbit_sat.h"
#include "orbit_crypto.h"   // control-bus auth: sender pinning + HMAC-SHA256, host-tested
#include "orbit_codec.h"
#include "orbit_fsimage.h"

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

// `Item` is in orbit_sat.h with the rest of the logic a host g++ can check: it had to widen
// anyway (the `scored` and `tx_done` messages report the cloud fraction and the three score
// parts, which capture() used to compute and throw away) and it is what the decision functions
// there are about.

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
// The manifest's CRC-32 of each blob's exact bytes, checked once at boot (orbit_fsimage.h).
static uint32_t  frameCrc[MAX_FRAMES];      // frame index -> crc32 of /frames/NNN.bin
static uint32_t  refCrc[MAX_FRAMES];        // frame index -> crc32 of ITS /refs/RRR.bin
static uint32_t  manifestFmt = 0;           // 0 = nothing read

static uint32_t g_seq = 0;
static uint16_t g_next_item_id = 1;
static uint32_t g_next_capture_ms = 0;
static uint32_t g_next_heartbeat_ms = 0;

static uint16_t g_frame_count = 0;          // frames present on LittleFS
static uint16_t g_frame_pos = 0;            // capture cursor

// Faults. The registry (orbit/protocol/registry.py) names every code and owns its number;
// orbit_faults.h is generated from it, and orbit_sat.h holds the two pieces of behaviour that
// are testable off-board: the boot latch and the edge trigger.
static PendingFaults g_boot_faults;   // raised before the radio exists; drained after the join
static FaultOnce     g_fault_once;    // conditions that recur every capture period

// What counts as an abnormal kernel pass. The scoring kernel runs once per capture and the
// cadence is SAT_CAPTURE_PERIOD_S; a pass that eats a third of the period is not keeping up with
// its own camera, whatever the absolute number turns out to be on this silicon. NEEDS CALIBRATION
// against a real board: it is a ceiling chosen from the cadence, not from a measurement.
static const uint32_t SCORING_LATENCY_WARN_US = (uint32_t)(SAT_CAPTURE_PERIOD_S * 1000000.0f / 3.0f);

// The cadence is nominal, not a metronome. Two boards each ticking at exactly
// SAT_CAPTURE_PERIOD_S hold their relative phase for the whole window: every round finds them
// in the same relative state it found them in last time, one board tends to lead throughout,
// and the arbitration that is the point of the demo plays out the same way every run. Real
// satellites do not share a clock. orbit/sim/satellite.py jitters its captures by this same
// fraction (SatelliteProfile.capture_jitter); this is the firmware saying the same thing.
// It is deliberately NOT in orbit_config.h: nothing on the ground needs to know, and that
// header restates only facts owned by orbit/config.py.
static const float CAPTURE_JITTER = 0.2f;   // +/- this fraction of the period

static uint32_t captureDelayMs() {
  const float base = SAT_CAPTURE_PERIOD_S * 1000.0f;
  const float off = (float)(esp_random() % 2001) / 1000.0f - 1.0f;   // -1.0 .. +1.0
  return (uint32_t)(base * (1.0f + CAPTURE_JITTER * off));
}

static uint32_t c_captured = 0, c_evicted = 0, c_rejected = 0, c_bids = 0;
static uint32_t c_grants = 0, c_transmitted = 0, c_failed = 0, c_revoked = 0;
static uint32_t c_ack_lost = 0, c_peer_grants = 0, c_late_acks = 0;

// Payload confidentiality: the AES-GCM key, decoded once at boot from the ORBIT_PAYLOAD_KEY hex
// in secrets.h. Length 0 means the layer is off and frames go out compressed-but-clear, exactly
// as before -- which is correct for this public MODIS corpus.
static uint8_t g_payload_key[32];
static size_t  g_payload_key_len = 0;

// The ground's Ed25519 public key, decoded once at boot from ORBIT_GROUND_PUBKEY. When present,
// a grant/revoke/tx_ack must carry a valid signature from the ground -- not merely a valid HMAC
// tag, which a holder of the shared key could also produce.
static uint8_t       g_ground_pk[32];
static const uint8_t *g_ground_pkp = nullptr;

// TweetNaCl (orbit_crypto.h) references randombytes() in its keypair code, which we never call --
// we only verify. The symbol must still link, so back it with the hardware RNG.
extern "C" void randombytes(unsigned char *p, unsigned long long n) { esp_fill_random(p, (size_t)n); }

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
  // Authenticate the SERIALISED bytes, so what is MACed is exactly what goes on the wire and
  // ArduinoJson's number formatting never has to agree with Python's. Adds 42 bytes
  // (,"auth":"<32 hex>"); returns 0 if they do not fit or the bytes will not canonicalise, and
  // an unsigned datagram on an authenticated bus is one nobody would act on anyway.
  n = orbit_auth_sign(buf, n, sizeof(buf), ORBIT_AUTH_KEY);
  if (n == 0) { Serial.println("[bus] datagram could not be signed, dropped"); return; }
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

// One edge-triggered onboard fault. `code_id` indexes the fault-code registry
// (orbit/protocol/registry.py); the ground looks the number up there, which is why a code can be
// added or reclassified without touching the wire schema.
//
// `severity` is a parameter rather than something this function looks up, but every call site
// passes orbit_fault_severity(code) out of the generated header. A hand-typed severity string is
// precisely the drift the registry exists to prevent: it would be the one fact about a fault that
// the board and the ground could disagree about while both looked correct.
static void sendFault(int code_id, const char *severity, const char *detail) {
  JsonDocument d;
  fillEnvelope(d, "fault");
  d["code_id"] = code_id;
  d["severity"] = severity;
  d["detail"] = detail;
  sendDoc(d);
  Serial.printf("[falt] %d %s (%s): %s\n", code_id, orbit_fault_slug(code_id), severity, detail);
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
  d["frames_scored"] = c_captured;
  d["frames_sent"] = c_transmitted;
  // Health, LEVEL-triggered: every heartbeat states the condition now, so nothing has to
  // announce recovery and a lost heartbeat costs one period of staleness rather than a missed
  // edge. All three are optional on the wire -- a value we cannot measure is left OUT, never
  // sent as a zero the ground would read as data. RSSI off a disconnected radio is exactly that
  // case, which is why it is conditional and the other two are not.
  if (WiFi.status() == WL_CONNECTED) d["rssi_dbm"] = (int)WiFi.RSSI();
  d["free_heap_bytes"] = (uint32_t)ESP.getFreeHeap();
  d["psram_ok"] = fbuf.inPsram();
  sendDoc(d);
}

// One frame went through the kernel. The ground does not arbitrate on this, but it is what
// feeds the display's frame_scored, the no-scoring FIFO baseline and the `usable` verdict, and
// it is the only place the score PARTS and the cloud fraction are reported for a frame that was
// rejected on arrival and will never be transmitted. Sent for every capture, kept or not.
static void sendScored(const Item &it, bool queued, int32_t evicted_item_id) {
  JsonDocument d;
  fillEnvelope(d, "scored");
  d["item_id"] = it.item_id;
  d["score"] = score_display(it.raw_score);
  JsonObject p = d["parts"].to<JsonObject>();
  p["clear"]  = orbit_part(it.clear);
  p["sharp"]  = orbit_part(it.sharp);
  p["change"] = orbit_part(it.change);
  d["cloud_frac"] = orbit_cloud_frac(it.cloud_px);
  d["queued"] = queued;
  d["evicted_item_id"] = evicted_item_id;
  d["queue_depth"] = queue.size();
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
  const uint8_t *data = orbit_tx_data();
  if (tx_total == 0) return;
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
  d["enc"] = orbit_tx_enc();         // "zlib" or "raw": how the ground must decode this payload
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

// Printed for every blob that fails the boot sweep. Serial only: the per-blob detail is for the
// operator holding the USB cable, and 54 fault datagrams is not a thing to put on the bus.
static void fsReport(FsVerdict v, const char *path, uint32_t want, uint32_t got) {
  Serial.printf("FATAL: %s %s (manifest %08x, flash %08x)\n",
                orbit_fs_verdict_name(v), path, (unsigned)want, (unsigned)got);
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

// The newcomer, carrying everything the kernel already worked out about it. Scoring a frame
// costs ~16 K pixel operations; re-deriving cloud_frac or the parts later would mean paying it
// twice (and re-reading the frame out of the pool), so they are kept with the item.
static Item makeItem(const Scored &sc) {
  Item it;
  it.used        = true;
  it.item_id     = g_next_item_id++;
  it.raw_score   = sc.score;
  it.captured_ms = millis();
  it.cloud_px    = sc.cloud_px;
  it.clear       = sc.clear;
  it.sharp       = sc.sharp;
  it.change      = sc.change;
  return it;
}

struct AdmitResult {
  Item    item;
  bool    queued = false;           // false = rejected on arrival, never held
  int32_t evicted_item_id = -1;     // what this frame displaced, -1 if nothing
};

// The decision itself is admit_decide() in orbit_sat.h, mirroring FakeSatellite._admit and
// tested against it on the host; everything here is the storage that follows from it.
static AdmitResult admit(const Scored &sc, const uint8_t *data) {
  AdmitResult r;
  r.item = makeItem(sc);
  const int32_t key = queue_key(r.item.raw_score);
  const uint16_t in_flight = tx_active ? tx_item : (await_ack ? await_item : NO_FRAME);
  const QCell tail = queue.hasData() ? queue.at(queue.size() - 1) : QCell{0, NO_FRAME};

  switch (admit_decide(key, fbuf.freeSlots(), tail.key, tail.item_id, in_flight)) {
    case ADMIT_REJECT:
      c_rejected++;
      sendEviction(r.item, "rejected", nullptr);
      return r;                     // queued stays false: the frame is gone

    case ADMIT_EVICT_TAIL: {
      // The newcomer beats the worst held frame: the tail leaves, permanently.
      Item *lost = findItem(tail.item_id);
      Item lostCopy;
      if (lost) { lostCopy = *lost; lost->used = false; }
      queue.removeItem(tail.item_id);
      fbuf.release(tail.item_id);
      Item *slot = freeItemSlot();
      if (!slot) return r;          // cannot happen: the release above freed one
      *slot = r.item;
      fbuf.store(slot->item_id, data);
      queue.insert(key, slot->item_id);
      c_evicted++;
      r.queued = true;
      r.evicted_item_id = (int32_t)lostCopy.item_id;
      sendEviction(lostCopy, "evicted", slot);
      return r;
    }

    default: {                      // ADMIT_STORE: a slot was free
      Item *slot = freeItemSlot();
      if (!slot) return r;
      *slot = r.item;
      fbuf.store(slot->item_id, data);
      queue.insert(key, slot->item_id);
      r.queued = true;
      return r;
    }
  }
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
  // Degraded, not fatal: the frame was still scored. But a kernel that cannot finish inside the
  // capture cadence means the queue is being fed slower than the camera runs, and that shows up
  // on the dashboard as a satellite that simply bids less often, with no reason given. Once per
  // boot (FaultOnce): every capture after the first would report the same condition.
  if (us > SCORING_LATENCY_WARN_US && g_fault_once.first(ORBIT_FAULT_SCORING_LATENCY_HIGH)) {
    char detail[48];
    snprintf(detail, sizeof(detail), "frame %u scored in %u ms", idx, us / 1000);
    sendFault(ORBIT_FAULT_SCORING_LATENCY_HIGH, orbit_fault_severity(ORBIT_FAULT_SCORING_LATENCY_HIGH), detail);
  }
  // eviction first, then scored -- the same order orbit/sim/satellite.py::capture emits them,
  // so a reader of the bus log sees the loss before the frame that caused it is announced.
  const AdmitResult r = admit(s, scratch);
  sendScored(r.item, r.queued, r.evicted_item_id);
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
  if (!it) {
    // The ground's view of this node's queue and the node's own have diverged: it granted a frame
    // we do not hold. Not once-only — each grant is its own event, and how often it happens is the
    // measurement. The slot is lost either way (the ground times the grant out and re-arbitrates),
    // so the fault is the only record that it was a disagreement rather than a dead board.
    Serial.printf("[grant] unknown item %u\n", item_id);
    char detail[48];
    snprintf(detail, sizeof(detail), "granted item %u, not in the pool", item_id);
    sendFault(ORBIT_FAULT_GRANT_UNKNOWN_ITEM, orbit_fault_severity(ORBIT_FAULT_GRANT_UNKNOWN_ITEM), detail);
    return;
  }
  c_grants++;
  tx_active = true;
  tx_round = d["round_id"] | -1;
  tx_item = item_id;
  tx_pace_bps = d["pace_bps"] | 65536.0f;
  tx_total = orbit_tx_prepare(fbuf.read(item_id), FRAME_BYTES);   // compressed payload, or the raw frame
  if (tx_total == 0) { tx_active = false; Serial.println("[grant] nothing to send"); return; }
  if (g_payload_key_len) {
    // Sensitive imagery: seal the payload. A failure to seal must NOT fall back to plaintext --
    // abort the grant instead. The ground times it out and re-arbitrates; nothing goes out clear.
    const size_t sealed = orbit_tx_seal(g_payload_key, g_payload_key_len);
    if (sealed == 0) { tx_active = false; Serial.println("[grant] seal failed; not sending in the clear"); return; }
    tx_total = sealed;
  }
  tx_chunks = orbit_chunk_count(tx_total, CHUNK_BYTES);
  tx_next_idx = 0;
  tx_next_at_ms = millis();
  tx_done_sent = false;
  tx_pass = 0;

  JsonDocument o;
  fillEnvelope(o, "tx_begin");
  o["round_id"] = tx_round;
  o["item_id"] = tx_item;
  o["total_bytes"] = FRAME_BYTES;    // RAW: the frame the ground must end up holding
  o["chunks"] = tx_chunks;
  o["enc"] = orbit_tx_enc();
  o["enc_bytes"] = tx_total;         // what the chunks actually carry
  sendDoc(o);
  Serial.printf("[grant] item=%u %u chunks %s %u B @ %.0f bps\n", tx_item, tx_chunks,
                orbit_tx_enc(), (unsigned)tx_total, tx_pace_bps);
}

static void onRevoke(JsonDocument &d) {
  if (strcmp(d["to"] | "", HOSTNAME) != 0) return;
  c_revoked++;
  tx_active = false;
  await_ack = false;
  Serial.printf("[revoke] item=%u reason=%s\n", (unsigned)(d["item_id"] | 0), (const char *)(d["reason"] | "?"));
}

static void popItem(uint16_t item_id) {
  queue.removeItem(item_id);
  Item *it = findItem(item_id);
  if (it) it->used = false;
  fbuf.release(item_id);
  c_transmitted++;
}

// The decision is ack_decide() in orbit_sat.h, mirroring FakeSatellite._acked. This used to
// return early whenever the ack was not the one being waited for, which stranded a frame the
// ground already had: after a timeout or a "ground moved on" give-up we re-offer the item, and
// the ground answers a re-offer of a frame it holds with tx_ack{ok} rather than a grant. The
// item then sat in the pool winning rounds that would never be granted, and its slot never came
// back. Protocol rule 6: an ok-ack for an item we still hold means pop it, never resend it.
static void onTxAck(JsonDocument &d) {
  if (strcmp(d["to"] | "", HOSTNAME) != 0) return;
  const uint16_t item_id = d["item_id"] | 0;
  const bool ok = d["ok"] | false;
  const bool held = findItem(item_id) != nullptr;

  switch (ack_decide(await_ack, await_item, item_id, ok, held, tx_active, tx_item)) {
    case ACK_POP:
      await_ack = false;
      popItem(item_id);
      Serial.printf("[ack ] item=%u confirmed, popped\n", item_id);
      return;
    case ACK_NACK_KEEP:
      await_ack = false;
      c_failed++;                      // keep the frame: a failed transmission must not lose data
      Serial.printf("[nack] item=%u reason=%s\n", item_id, (const char *)(d["reason"] | "?"));
      return;
    case ACK_POP_LATE:
      c_late_acks++;
      popItem(item_id);
      Serial.printf("[ack ] item=%u late ok, popped without resending\n", item_id);
      return;
    default:
      return;
  }
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

// Anti-replay: the highest seq VERIFIED from each sender. Cleared for a sender whose uptime
// jumps backwards past ORBIT_RESTART_SLACK_MS, so an operator restarting the ground mid-demo
// is not fatal. Zero-initialised as a static: no peers known at boot.
static OrbitAuthState g_auth;

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
    // Authenticity BEFORE the parser and BEFORE seenBefore(): nothing a stranger sends should
    // reach ArduinoJson (zero-copy deserialisation mutates this buffer), and an unverified
    // datagram must never move this node's sequence state -- one forgery with a huge seq would
    // otherwise mute the real ground for the rest of the window. Sender pinning, the MAC and
    // the anti-replay watermark, in one call, on the raw bytes.
    if (orbit_auth_accept(buf, (size_t)len, ORBIT_AUTH_KEY, ORBIT_GROUND_NAME, g_ground_pkp, &g_auth) != ORBIT_OK)
      continue;
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
    Item *it = findItem(tx_item);
    const uint8_t *data = fbuf.read(tx_item);
    if (!it || !data) {
      // The frame left the pool mid-transmission. tx_done without a digest over the real bytes
      // would be a claim we cannot back, so nothing is sent: the ground times the grant out and
      // re-arbitrates, which is a failure it already knows how to handle.
      Serial.printf("[tx  ] item=%u vanished mid-transmission, no tx_done\n", tx_item);
      tx_done_sent = true;
      tx_active = false;
      return;
    }
    tx_done_sent = true;
    // The ground reassembles the chunks and compares this digest before it counts the frame, so
    // it has to be a real sha256 over exactly the bytes that were sent -- one-shot mbedtls over
    // the pool slot, hex-formatted by orbit_hex64 (orbit_sat.h, tested on the host).
    uint8_t digest[32];
    char    sha_hex[65];
    mbedtls_sha256(data, FRAME_BYTES, digest, 0);   // the RAW frame, never the compressed payload
    orbit_hex64(digest, sha_hex);
    JsonDocument d;
    fillEnvelope(d, "tx_done");
    d["round_id"] = tx_round;
    d["item_id"] = tx_item;
    d["total_bytes"] = FRAME_BYTES;
    d["score"] = score_display(it->raw_score);
    // Repeated from `scored` on purpose: the ground can still judge the frame usable when the
    // earlier scored datagram was one of the ones multicast lost.
    d["cloud_frac"] = orbit_cloud_frac(it->cloud_px);
    d["sha256"] = sha_hex;
    tx_done_len = serializeJson(d, tx_done_buf, sizeof(tx_done_buf));
    // tx_done keeps its bytes for the resend path, so it needs its own sign call. The resends
    // are the same signed bytes with the same seq: the ground dedups them, exactly as before.
    tx_done_len = orbit_auth_sign(tx_done_buf, tx_done_len, sizeof(tx_done_buf), ORBIT_AUTH_KEY);
    if (tx_done_len == 0) {
      Serial.println("[tx  ] tx_done could not be signed, dropped");
      tx_active = false;      // no tx_done: the ground times the grant out and re-arbitrates
      return;                 // await_ack stays false, so the frame is KEPT
    }
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
    Serial.printf("[tx  ] item=%u done, %u B %s (%u raw)\n", tx_item, (unsigned)tx_total,
                  orbit_tx_enc(), (unsigned)FRAME_BYTES);
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

// Decode ORBIT_PAYLOAD_KEY (hex) into g_payload_key. "" leaves confidentiality off; a malformed
// or wrong-length key is a loud boot failure rather than a board that quietly ships plaintext.
static void loadPayloadKey() {
  const char *hex = ORBIT_PAYLOAD_KEY;
  const size_t n = strlen(hex);
  if (n == 0) { g_payload_key_len = 0; return; }
  if (n % 2 || n / 2 > sizeof(g_payload_key) || (n / 2 != 16 && n / 2 != 24 && n / 2 != 32)) {
    Serial.printf("[crypto] ORBIT_PAYLOAD_KEY must be 32/48/64 hex chars; ignoring\n");
    g_payload_key_len = 0;
    return;
  }
  for (size_t i = 0; i < n / 2; i++) {
    const int hi = orbit_unhex((uint8_t)hex[i * 2]), lo = orbit_unhex((uint8_t)hex[i * 2 + 1]);
    if (hi < 0 || lo < 0) { Serial.println("[crypto] ORBIT_PAYLOAD_KEY is not hex; ignoring"); g_payload_key_len = 0; return; }
    g_payload_key[i] = (uint8_t)((hi << 4) | lo);
  }
  g_payload_key_len = n / 2;
  Serial.printf("[crypto] payload encryption ON: AES-%u-GCM\n", (unsigned)(g_payload_key_len * 8));
}

// Decode ORBIT_GROUND_PUBKEY (64 hex chars) into g_ground_pk. "" leaves the signature check off
// (HMAC + pin still apply); a malformed key is a loud boot message, never a silent downgrade.
static void loadGroundPubkey() {
  const char *hex = ORBIT_GROUND_PUBKEY;
  const size_t n = strlen(hex);
  if (n == 0) { g_ground_pkp = nullptr; return; }
  if (n != 64) { Serial.println("[crypto] ORBIT_GROUND_PUBKEY must be 64 hex chars; ignoring"); return; }
  for (size_t i = 0; i < 32; i++) {
    const int hi = orbit_unhex((uint8_t)hex[i * 2]), lo = orbit_unhex((uint8_t)hex[i * 2 + 1]);
    if (hi < 0 || lo < 0) { Serial.println("[crypto] ORBIT_GROUND_PUBKEY is not hex; ignoring"); return; }
    g_ground_pk[i] = (uint8_t)((hi << 4) | lo);
  }
  g_ground_pkp = g_ground_pk;
  Serial.println("[crypto] ground command signatures: VERIFIED (Ed25519)");
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\n=== orbit satellite %s ===\n", HOSTNAME);
  loadPayloadKey();
  loadGroundPubkey();
  Serial.printf("chip %s rev %d, heap %u, psram %u\n",
                ESP.getChipModel(), ESP.getChipRevision(),
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getPsramSize());
  led(16, 0, 0);

  // Everything from here to the multicast join can fail fatally, and none of it can say so on the
  // bus yet: there is no socket. Each failure is latched into g_boot_faults and drained the moment
  // the join succeeds, so a board that comes up crippled reports it over the air instead of only
  // down a serial cable nobody has plugged in.
  if (!LittleFS.begin(false)) {
    Serial.println("FATAL: LittleFS mount failed (upload the image set)");
    g_boot_faults.push(ORBIT_FAULT_LITTLEFS_MOUNT_FAILED, "LittleFS.begin(false) failed");
  }
  else {
    File m = LittleFS.open("/manifest.json", "r");
    if (!m) {
      Serial.println("FATAL: /manifest.json missing -- upload the image set");
      g_boot_faults.push(ORBIT_FAULT_MANIFEST_MISSING, "/manifest.json not on LittleFS");
    } else {
      JsonDocument md;
      if (deserializeJson(md, m) != DeserializationError::Ok) {
        Serial.println("FATAL: /manifest.json is not valid JSON");
        g_boot_faults.push(ORBIT_FAULT_MANIFEST_CORRUPT, "/manifest.json is not valid JSON");
      } else {
        JsonArray arr = md["frames"].as<JsonArray>();
        manifestFmt = md["fmt"] | 0u;
        for (JsonObject e : arr) {
          const uint16_t i = e["frame"] | 0;
          if (i < MAX_FRAMES) {
            frameRef[i] = e["ref"] | 0;
            // .as<uint32_t>(), NOT `| 0`: ArduinoJson's default there is a SIGNED int, and half
            // of all CRCs are above 2^31 -- this image has 2834247424 at frame 1. A sign-mangled
            // expectation would fail every one of those frames at boot.
            frameCrc[i] = e["crc32"].as<uint32_t>();
            refCrc[i]   = e["ref_crc32"].as<uint32_t>();
            if (i + 1 > g_frame_count) g_frame_count = i + 1;
          }
        }
        Serial.printf("[fs] manifest: %u frames, satellite %s\n", g_frame_count, (const char *)(md["sat"] | "?"));
        // Verify the flashed image before anything is captured from it. orbit_fsimage.h has the
        // reasoning: why at boot rather than lazily at load, and what it costs.
        const int bad = orbit_fs_verify(manifestFmt, frameRef, frameCrc, refCrc, g_frame_count,
                                        scratch, loadBlob, fsReport);
        if (bad != 0) {
          // Loud, and then inert. g_frame_count = 0 makes capture() return immediately, so no
          // frame from this image is ever scored, bid on or transmitted -- the whole point, since
          // a silently scored bad frame reaches the ground as a re-score mismatch nobody can read.
          // Nothing blocks or reboots: the node still joins the bus and still heartbeats, so it
          // appears on the dashboard at captured=0 with this fault beside it.
          char detail[PendingFaults::DETAIL_LEN];   // push() truncates past this; both fit
          if (bad < 0) snprintf(detail, sizeof(detail), "manifest fmt %u, firmware needs %u: reflash",
                                (unsigned)manifestFmt, (unsigned)ORBIT_MANIFEST_FMT);
          else         snprintf(detail, sizeof(detail), "%d blob(s) failed CRC: reflash the image", bad);
          Serial.printf("FATAL: flash image integrity: %s\n", detail);
          g_boot_faults.push(ORBIT_FAULT_FLASH_IMAGE_CORRUPT, detail);
          g_frame_count = 0;
        } else {
          Serial.printf("[fs] integrity: %u frames + references verified (crc32)\n", g_frame_count);
        }
      }
      m.close();
    }
  }

  if (!fbuf.begin(SAT_BUFFER_SLOTS)) {
    Serial.println("FATAL: frame pool allocation failed");
    g_boot_faults.push(ORBIT_FAULT_BUFFER_ALLOC_FAILED, "frame pool allocation failed");
  } else if (!fbuf.inPsram()) {
    // Degraded, not fatal: FrameBuffer::begin falls back to internal SRAM (orbit_queue.h) and the
    // node works. It is reported because 8 x 16 KB beside the WiFi stack is the configuration that
    // starts failing allocations later, under load, for reasons nobody will connect back to boot.
    g_boot_faults.push(ORBIT_FAULT_PSRAM_FALLBACK, "frame pool in internal SRAM");
  }
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

  // There is a socket now, so everything latched above can finally be said out loud. sat_boot goes
  // first and carries the overflow count: it is this node announcing itself, and "I am here, and N
  // problems did not fit in the list" is the one thing that must never be the part that got
  // dropped. The rest follow in the order they happened, which is the order that explains them.
  {
    char boot[48];
    snprintf(boot, sizeof(boot), "%s up, %d boot faults", HOSTNAME, g_boot_faults.size() + g_boot_faults.dropped());
    sendFault(ORBIT_FAULT_SAT_BOOT, orbit_fault_severity(ORBIT_FAULT_SAT_BOOT), boot);
  }
  for (int i = 0; i < g_boot_faults.size(); i++) {
    const int code_id = g_boot_faults.code(i);
    sendFault(code_id, orbit_fault_severity(code_id), g_boot_faults.detail(i));
  }

  // Where in the roll this board starts. At a fixed 0, two boards flashed from the same corpus
  // walk their sequences from the same place on every power-up, so the same photograph meets the
  // same rival photograph in the same round, run after run. The hardware RNG is seeded per boot,
  // so each start enters the ring somewhere else and the contest is a different one.
  if (g_frame_count) {
    g_frame_pos = (uint16_t)(esp_random() % g_frame_count);
    Serial.printf("[cap] starting at frame %u of %u, cadence %.1fs +/-%.0f%%\n",
                  g_frame_pos, g_frame_count, SAT_CAPTURE_PERIOD_S, CAPTURE_JITTER * 100.0f);
  }
  g_next_capture_ms = millis() + captureDelayMs();
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
    g_next_capture_ms += captureDelayMs();
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
