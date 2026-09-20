// The scoring kernel, integer-for-integer with orbit/golden/score.py (protocol.md §5.2).
//
// Must stay bit-exact: the ground re-scores every frame it receives with the golden
// model and compares. Any drift here shows up as a mismatch on the dashboard.
//
// Three passes fused into one walk over the frame:
//   cloud_px   pixels brighter than CLOUD_THRESHOLD
//   changed_px pixels differing from the reference by more than CHANGE_THRESHOLD
//   sobel_sum  sum of |Gx|+|Gy| over the interior, 3x3, taps +-1/+-2 (no multiplies)
//
// Overflow notes, because these bounds are the reason the types are what they are:
//   sobel_sum  <= 126*126*4*255*2 = 32.4e6, fits u32; u64 used anyway, it is free here
//   weighted   21845*65535*3 = 4,294,899,225 -- 68k short of UINT32_MAX. u64 is not
//              optional: the u32 form is correct only by luck and breaks if a weight moves.
#pragma once
#include <stdint.h>
#include <stdlib.h>
#include "orbit_config.h"

struct Scored {
  uint32_t cloud_px;
  uint32_t changed_px;
  uint64_t sobel_sum;
  uint16_t clear;
  uint16_t sharp;
  uint16_t change;
  uint16_t score;      // 0..65535, what goes in the queue
};

static inline uint16_t sat16(uint64_t x) {
  return x > (uint64_t)U16_MAX_ ? U16_MAX_ : (uint16_t)x;
}

// 0..65535 -> 0..100 for humans. golden/score.py display(); never on the wire as u16.
static inline float score_display(uint16_t s) {
  return (float)s * 100.0f / (float)U16_MAX_;
}

static inline Scored orbit_composite(uint32_t cloud_px, uint32_t changed_px, uint64_t ssum) {
  Scored o;
  o.cloud_px   = cloud_px;
  o.changed_px = changed_px;
  o.sobel_sum  = ssum;
  o.clear  = sat16((uint64_t)(FRAME_BYTES - cloud_px) << 2);
  o.sharp  = sat16(ssum >> SHARP_SHIFT);
  o.change = sat16((uint64_t)changed_px << 2);
  const uint64_t w = (uint64_t)W_CLEAR  * o.clear
                   + (uint64_t)W_SHARP  * o.sharp
                   + (uint64_t)W_CHANGE * o.change;
  o.score = sat16(w >> 16);
  return o;
}

// frame and ref are FRAME_BYTES each, row-major, 8-bit gray.
static inline Scored orbit_score_frame(const uint8_t *frame, const uint8_t *ref) {
  uint32_t cloud_px = 0, changed_px = 0;
  uint64_t ssum = 0;

  for (uint32_t i = 0; i < FRAME_BYTES; i++) {
    if (frame[i] > CLOUD_THRESHOLD) cloud_px++;
    const int d = (int)frame[i] - (int)ref[i];
    if (abs(d) > (int)CHANGE_THRESHOLD) changed_px++;
  }

  // Interior only; the border is masked, exactly as the RTL and the golden model do.
  for (int y = 1; y < FRAME_H - 1; y++) {
    const uint8_t *rm = frame + (size_t)(y - 1) * FRAME_W;   // row above
    const uint8_t *r0 = frame + (size_t)(y)     * FRAME_W;   // row
    const uint8_t *rp = frame + (size_t)(y + 1) * FRAME_W;   // row below
    for (int x = 1; x < FRAME_W - 1; x++) {
      const int gx = ((int)rm[x + 1] - (int)rm[x - 1])
                   + (((int)r0[x + 1] - (int)r0[x - 1]) << 1)
                   + ((int)rp[x + 1] - (int)rp[x - 1]);
      const int gy = ((int)rp[x - 1] - (int)rm[x - 1])
                   + (((int)rp[x]     - (int)rm[x])     << 1)
                   + ((int)rp[x + 1] - (int)rm[x + 1]);
      ssum += (uint64_t)(abs(gx) + abs(gy));
    }
  }
  return orbit_composite(cloud_px, changed_px, ssum);
}
