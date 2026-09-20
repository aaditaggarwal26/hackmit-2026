// Toolchain + board bring-up check. Not part of the satellite firmware.
//
// The DevKitC's onboard LED is a single addressable WS2812 on GPIO48, not a plain
// GPIO LED: digitalWrite() does nothing visible and looks like a flashing failure.
// rgbLedWrite() is the core's built-in driver for it, so this needs no library.
//
// Prints a line per cycle so success is confirmed from serial output, not from
// looking at the board.

#include <WiFi.h>   // for WiFi.macAddress() in the banner

#ifndef RGB_BUILTIN
#define RGB_BUILTIN 48
#endif

static uint32_t tick = 0;

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("=== orbit blink_rgb: toolchain and board check ===");
  Serial.printf("chip:  %s rev %d, %d core(s)\n", ESP.getChipModel(), ESP.getChipRevision(), ESP.getChipCores());
  Serial.printf("flash: %u bytes\n", (unsigned)ESP.getFlashChipSize());
  Serial.printf("heap:  %u bytes free\n", (unsigned)ESP.getFreeHeap());
  Serial.printf("psram: %u bytes\n", (unsigned)ESP.getPsramSize());
  Serial.printf("mac:   %s\n", WiFi.macAddress().c_str());
  Serial.printf("led:   GPIO%d (addressable RGB)\n", RGB_BUILTIN);
  Serial.println("if you can read this, the sketch is ours and not factory firmware");
}

void loop() {
  const char *name[3] = {"red", "green", "blue"};
  uint8_t r = 0, g = 0, b = 0;
  switch (tick % 3) {
    case 0: r = 32; break;
    case 1: g = 32; break;
    case 2: b = 32; break;
  }
  rgbLedWrite(RGB_BUILTIN, r, g, b);
  Serial.printf("[%lu] led=%s uptime=%lus heap=%u\n",
                (unsigned long)tick, name[tick % 3],
                (unsigned long)(millis() / 1000), (unsigned)ESP.getFreeHeap());
  tick++;
  delay(1000);
}
