#include "buttons.h"
#include "badge_pins.h"

namespace btn {
namespace {

// Bit position within the byte clocked out of the shift register, MSB first.
//
// VERIFIED ON A REAL BADGE. An earlier version of this had the low nibble
// reversed, from assuming the 74HC165's pins 11/12/13/14 are D3/D2/D1/D0. On
// this part they are D0/D1/D2/D3, so the four D-pad bits came out mirrored:
// pressing UP reported RIGHT, RIGHT reported UP, and LEFT reported SLIDE.
// DOWN, A, B and SELECT were unaffected, which is why it looked half-working.
//
// The corrected order is the obvious one the net names were hinting at all
// along: BTN_1..BTN_7 sit on bits 0..6, and SW_HPM on bit 7.
constexpr uint8_t BIT_A      = 7;  // SW_HPM, SW6   silkscreen A
constexpr uint8_t BIT_B      = 6;  // BTN_7,  SW5   silkscreen B
constexpr uint8_t BIT_SELECT = 5;  // BTN_6,  SW7   silkscreen HOME
constexpr uint8_t BIT_DOWN   = 4;  // BTN_5,  SW8   silkscreen DOWN
constexpr uint8_t BIT_LEFT   = 3;  // BTN_4,  SW4   silkscreen LEFT
constexpr uint8_t BIT_RIGHT  = 2;  // BTN_3,  SW3   silkscreen RIGHT
constexpr uint8_t BIT_UP     = 1;  // BTN_2,  SW2   silkscreen UP
constexpr uint8_t BIT_SLIDE  = 0;  // BTN_1,  SW11  unlabelled slide switch

constexpr uint8_t kBit[COUNT] = {
  BIT_UP, BIT_DOWN, BIT_LEFT, BIT_RIGHT,
  BIT_A,  BIT_B,    BIT_SELECT, BIT_SLIDE,
  0xFF,   // BOOT is a real GPIO, not a register bit
};

const char *kName[COUNT] = {
  "UP", "DOWN", "LEFT", "RIGHT", "A", "B", "SELECT", "SLIDE", "BOOT"
};

// Debounce: a reading has to agree with itself for this long to be accepted.
constexpr uint32_t kDebounceMs = 12;

uint8_t  gRaw = 0;                 // last register byte, already inverted
bool     gStable[COUNT]   = {false};
bool     gCandidate[COUNT]= {false};
uint32_t gChangedAt[COUNT]= {0};
bool     gPrev[COUNT]     = {false};
uint32_t gDownSince[COUNT]= {0};
bool     gPressed[COUNT]  = {false};
bool     gReleased[COUNT] = {false};

uint8_t shiftIn8() {
  // Asynchronous parallel load while SH/LD is low. CE (pin 15) is tied to GND
  // on this board, so the register always clocks.
  digitalWrite(PIN_SR_SHLD, LOW);
  delayMicroseconds(5);
  digitalWrite(PIN_SR_SHLD, HIGH);
  delayMicroseconds(5);

  uint8_t v = 0;
  for (int i = 0; i < 8; i++) {
    // QH already holds the next bit; read before clocking, not after.
    v = (uint8_t)((v << 1) | (digitalRead(PIN_SR_QH) ? 1 : 0));
    digitalWrite(PIN_SR_CLK, HIGH);
    delayMicroseconds(3);
    digitalWrite(PIN_SR_CLK, LOW);
    delayMicroseconds(3);
  }
  return v;
}

}  // namespace

void begin() {
  pinMode(PIN_SR_QH, INPUT);
  pinMode(PIN_SR_SHLD, OUTPUT);
  pinMode(PIN_SR_CLK, OUTPUT);
  digitalWrite(PIN_SR_SHLD, HIGH);
  digitalWrite(PIN_SR_CLK, LOW);

  // GPIO9 has an external 10k pull-up (R31); INPUT_PULLUP is harmless and
  // keeps the pin defined if that resistor is ever depopulated.
  pinMode(PIN_BOOT_BTN, INPUT_PULLUP);

  const uint32_t now = millis();
  for (int i = 0; i < COUNT; i++) gChangedAt[i] = now;
}

void poll() {
  const uint32_t now = millis();

  // Switches pull their net to GND, with 10k pull-ups to 3V3, so a pressed
  // button reads 0. Invert once here and treat 1 as "down" everywhere else.
  gRaw = (uint8_t)~shiftIn8();

  for (int i = 0; i < COUNT; i++) {
    const bool sample = (i == BOOT) ? (digitalRead(PIN_BOOT_BTN) == LOW)
                                    : ((gRaw >> kBit[i]) & 0x01) != 0;

    gPrev[i]     = gStable[i];
    gPressed[i]  = false;
    gReleased[i] = false;

    if (sample != gCandidate[i]) {
      gCandidate[i]  = sample;
      gChangedAt[i]  = now;
    } else if (sample != gStable[i] && (now - gChangedAt[i]) >= kDebounceMs) {
      gStable[i] = sample;
      if (sample) {
        gDownSince[i] = now;
        gPressed[i]   = true;
      } else {
        gDownSince[i] = 0;
        gReleased[i]  = true;
      }
    }
  }
}

bool down(Id b)     { return gStable[b]; }
bool pressed(Id b)  { return gPressed[b]; }
bool released(Id b) { return gReleased[b]; }

uint32_t heldMs(Id b) {
  if (!gStable[b] || gDownSince[b] == 0) return 0;
  return millis() - gDownSince[b];
}

bool anyDown() {
  return gStable[UP] || gStable[DOWN] || gStable[LEFT] || gStable[RIGHT] ||
         gStable[A]  || gStable[B]    || gStable[SELECT];
}

uint8_t rawRegister() { return gRaw; }

const char *name(Id b) { return kName[b]; }

}  // namespace btn
