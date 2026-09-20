// Priority queue and frame buffer, mirroring orbit/golden/queue.py and the FrameBuffer
// in orbit/sim/satellite.py.
//
// The two are deliberately separate, and on this chip the reason is physical:
//   FrameBuffer  a fixed pool allocated once at boot. Large, never moves, never grows.
//                On an ESP32-S3 there is no growth path -- full is full.
//   PriorityQueue small (key, item_id) entries that reorder constantly. It points INTO
//                the buffer; it never contains frame bytes.
//
// Ordering: key descending, and among equal keys the earlier insert stays first. That
// tie rule is load-bearing -- it is what makes the ground's mirror of this queue match.
#pragma once
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include "orbit_config.h"

static const uint16_t NO_FRAME = 0xFFFF;

struct QCell { int32_t key; uint16_t item_id; };

class PriorityQueue {
 public:
  void begin(int limit) { limit_ = limit; n_ = 0; evicted_ = 0; }

  int  size()     const { return n_; }
  bool hasData()  const { return n_ > 0; }
  uint16_t evicted() const { return evicted_; }
  const QCell &at(int i) const { return cells_[i]; }

  // Returns the item_id lost by this insert, or NO_FRAME. Mirrors queue.py insert().
  uint16_t insert(int32_t key, uint16_t item_id) {
    uint16_t lost = NO_FRAME;
    if (n_ >= limit_) {
      evicted_++;
      if (key <= cells_[n_ - 1].key) return item_id;   // not better than the tail: newcomer lost
      lost = cells_[n_ - 1].item_id;
      n_--;
    }
    int i = 0;
    while (i < n_ && cells_[i].key >= key) i++;        // ties: insert after equals
    memmove(&cells_[i + 1], &cells_[i], (size_t)(n_ - i) * sizeof(QCell));
    cells_[i].key = key;
    cells_[i].item_id = item_id;
    n_++;
    return lost;
  }

  void removeItem(uint16_t item_id) {
    for (int i = 0; i < n_; i++) {
      if (cells_[i].item_id == item_id) {
        memmove(&cells_[i], &cells_[i + 1], (size_t)(n_ - i - 1) * sizeof(QCell));
        n_--;
        return;
      }
    }
  }

 private:
  QCell cells_[SAT_BUFFER_SLOTS];
  int   limit_ = SAT_BUFFER_SLOTS;
  int   n_ = 0;
  uint16_t evicted_ = 0;
};

// Fixed pool of `slots` frames. store() requires a free slot: admission is the caller's
// decision, exactly as in the simulator.
class FrameBuffer {
 public:
  bool begin(int slots) {
    slots_ = slots;
    // PSRAM first when the module has it -- 8 x 16 KB does not fit comfortably beside
    // the WiFi stack in internal SRAM. Falls back, and the caller reports which happened.
    pool_ = (uint8_t *)ps_malloc((size_t)slots_ * FRAME_BYTES);
    inPsram_ = pool_ != nullptr;
    if (!pool_) pool_ = (uint8_t *)malloc((size_t)slots_ * FRAME_BYTES);
    if (!pool_) return false;
    for (int i = 0; i < slots_; i++) { slotOf_[i] = NO_FRAME; }
    return true;
  }

  bool inPsram() const { return inPsram_; }
  int  slots()   const { return slots_; }
  int  used()    const { int u = 0; for (int i = 0; i < slots_; i++) if (slotOf_[i] != NO_FRAME) u++; return u; }
  int  freeSlots() const { return slots_ - used(); }
  uint32_t capacityBytes() const { return (uint32_t)slots_ * FRAME_BYTES; }

  bool store(uint16_t item_id, const uint8_t *data) {
    for (int i = 0; i < slots_; i++) {
      if (slotOf_[i] == NO_FRAME) {
        memcpy(pool_ + (size_t)i * FRAME_BYTES, data, FRAME_BYTES);
        slotOf_[i] = item_id;
        return true;
      }
    }
    return false;                    // caller must check freeSlots() first
  }

  void release(uint16_t item_id) {
    for (int i = 0; i < slots_; i++) if (slotOf_[i] == item_id) { slotOf_[i] = NO_FRAME; return; }
  }

  const uint8_t *read(uint16_t item_id) const {
    for (int i = 0; i < slots_; i++) if (slotOf_[i] == item_id) return pool_ + (size_t)i * FRAME_BYTES;
    return nullptr;
  }

 private:
  uint8_t *pool_ = nullptr;
  uint16_t slotOf_[SAT_BUFFER_SLOTS];
  int      slots_ = 0;
  bool     inPsram_ = false;
};
