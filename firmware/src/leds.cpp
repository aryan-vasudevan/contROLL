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

// The Solana palette, one colour per LED, crawling around the ring. This is
// the badge's ambient look for the Best Use of Badge track -- the ring spins
// these five continuously whenever nothing needs urgent attention.
constexpr uint8_t kSolana[][3] = {
    {153,  69, 255},   // purple  #9945FF
    { 20, 241, 149},   // light green #14F195
    {  0, 200, 200},   // teal
    { 60, 120, 255},   // blue
    {255,  80, 200},   // pink
};
constexpr int kSolanaN = sizeof(kSolana) / sizeof(kSolana[0]);

void solanaSnake(uint16_t phase) {
  // The palette walks the ring: each LED shows a colour one step behind its
  // neighbour, and the whole pattern rotates one LED every few frames.
  const int shift = (phase / 5) % LED_COUNT;
  for (int i = 0; i < LED_COUNT; i++) {
    const uint8_t *c = kSolana[(i + shift) % kSolanaN];
    // dim the tail so it reads as a snake with a head, not a static rainbow
    const int d = (i + LED_COUNT - shift) % LED_COUNT;
    const uint8_t k = d == 0 ? 255 : (d < 3 ? 140 : 70);
    gStrip.setPixelColor(i, rgb((uint16_t)c[0] * k / 255,
                                (uint16_t)c[1] * k / 255,
                                (uint16_t)c[2] * k / 255));
  }
}

void render(Status s, uint16_t phase) {
  switch (s) {
    // Only genuinely urgent states may interrupt the snake.
    case Status::LinkLost:       blink(255, 0, 0, phase, 20); break;
    case Status::Rejected:       blink(255, 0, 0, phase, 8);  break;
    case Status::Armed:          fill(rgb(255, 0, 0));        break;
    default:                     solanaSnake(phase);          break;
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
