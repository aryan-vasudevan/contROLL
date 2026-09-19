#include "leds.h"
#include "badge_pins.h"
#include <Adafruit_NeoPixel.h>

namespace leds {
namespace {

Adafruit_NeoPixel gStrip(LED_COUNT, PIN_LED_DIN, NEO_GRB + NEO_KHZ800);

Status   gStatus   = Status::Booting;
Status   gOverlay  = Status::Booting;
bool     gHasOverlay = false;
uint32_t gOverlayUntil = 0;
uint32_t gLastFrame = 0;
uint16_t gPhase = 0;

constexpr uint8_t kBright = 40;   // keep current draw modest on battery

uint32_t rgb(uint8_t r, uint8_t g, uint8_t b) {
  return gStrip.Color(r, g, b);
}

void fill(uint32_t c) {
  for (int i = 0; i < LED_COUNT; i++) gStrip.setPixelColor(i, c);
}

void chase(uint8_t r, uint8_t g, uint8_t b, uint16_t phase) {
  const int head = (phase / 6) % LED_COUNT;
  for (int i = 0; i < LED_COUNT; i++) {
    const int d = (i - head + LED_COUNT) % LED_COUNT;
    const uint8_t k = d == 0 ? 255 : (d == 1 ? 90 : (d == 2 ? 25 : 0));
    gStrip.setPixelColor(i, rgb((uint16_t)r * k / 255, (uint16_t)g * k / 255,
                                (uint16_t)b * k / 255));
  }
}

void breathe(uint8_t r, uint8_t g, uint8_t b, uint16_t phase) {
  // Triangle wave is smooth enough here and avoids a sin call every frame.
  const uint16_t t = phase % 160;
  const uint8_t k = t < 80 ? (uint8_t)(t * 255 / 80)
                           : (uint8_t)((160 - t) * 255 / 80);
  fill(rgb((uint16_t)r * k / 255, (uint16_t)g * k / 255, (uint16_t)b * k / 255));
}

void blink(uint8_t r, uint8_t g, uint8_t b, uint16_t phase, uint16_t period) {
  fill((phase % period) < period / 2 ? rgb(r, g, b) : 0);
}

void render(Status s, uint16_t phase) {
  switch (s) {
    case Status::Booting:        chase(120, 120, 120, phase); break;
    case Status::WifiConnecting: chase(0, 40, 255, phase);    break;
    case Status::LinkWaiting:    breathe(0, 40, 255, phase);  break;
    case Status::LinkLost:       blink(255, 0, 0, phase, 20); break;
    case Status::Disarmed:       fill(rgb(0, 60, 0));         break;
    case Status::Armed:          fill(rgb(255, 0, 0));        break;
    case Status::Flying:         chase(0, 255, 60, phase);    break;
    case Status::ComePending:    blink(255, 140, 0, phase, 10); break;
    case Status::ComeActive:     chase(255, 0, 200, phase);   break;
    case Status::BeaconSet:      fill(rgb(0, 220, 220));      break;
    case Status::Rejected:       blink(255, 0, 0, phase, 8);  break;
  }
}

}  // namespace

void begin() {
  gStrip.begin();
  gStrip.setBrightness(kBright);
  gStrip.clear();
  gStrip.show();
}

void setStatus(Status s) { gStatus = s; }

void flash(Status s, uint32_t ms) {
  gOverlay      = s;
  gHasOverlay   = true;
  gOverlayUntil = millis() + ms;
}

void poll() {
  const uint32_t now = millis();
  if (now - gLastFrame < 25) return;        // ~40 fps
  gLastFrame = now;
  gPhase++;

  if (gHasOverlay && (int32_t)(now - gOverlayUntil) >= 0) gHasOverlay = false;

  render(gHasOverlay ? gOverlay : gStatus, gPhase);
  gStrip.show();
}

void allOff() {
  gStrip.clear();
  gStrip.show();
}

}  // namespace leds
