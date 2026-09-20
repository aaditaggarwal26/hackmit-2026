// Host harness: compiles the FIRMWARE's scoring kernel with no Arduino dependency and
// scores frame/ref pairs from a flat file, so tools/check_score_parity.py can hold it to
// the Python golden model bit for bit. Same header the ESP32 builds -- not a transcription.
//
//   g++ -O2 -I../satellite_esp32 score_host.cpp -o score_host
//   ./score_host pairs.bin        # N x (16384 frame + 16384 ref), prints one score per line
#include <cstdio>
#include <cstdlib>
#include <vector>
#include "orbit_score.h"

int main(int argc, char **argv) {
  if (argc < 2) { fprintf(stderr, "usage: %s pairs.bin\n", argv[0]); return 2; }
  FILE *f = fopen(argv[1], "rb");
  if (!f) { perror("open"); return 2; }
  std::vector<uint8_t> frame(FRAME_BYTES), ref(FRAME_BYTES);
  while (fread(frame.data(), 1, FRAME_BYTES, f) == FRAME_BYTES &&
         fread(ref.data(),   1, FRAME_BYTES, f) == FRAME_BYTES) {
    const Scored s = orbit_score_frame(frame.data(), ref.data());
    // every intermediate, so a mismatch says which term drifted
    printf("%u %u %llu %u %u %u %u\n", s.cloud_px, s.changed_px,
           (unsigned long long)s.sobel_sum, s.clear, s.sharp, s.change, s.score);
  }
  fclose(f);
  return 0;
}
