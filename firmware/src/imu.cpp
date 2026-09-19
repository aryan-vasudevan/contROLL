#include "imu.h"
#include "badge_pins.h"
#include "config.h"
#include <Wire.h>
#include <math.h>

namespace imu {

// Detector state. Not in the anonymous namespace because imu::detail reaches
// into it, and the host tests drive imu::detail directly.
namespace {
bool     gPresent = false;
uint8_t  gWhoAmI  = 0;
bool     gShakeLatched = false;
uint32_t gLastSampleMs = 0;
}  // namespace

// Shakes are ignored for this long after startup, while the gravity baseline
// is still converging and the badge is most likely being picked up.
constexpr uint32_t kStartupGraceMs = 1500;

float    gX = 0, gY = 0, gZ = 0;
float    gBaseline = 1.0f;     // slow average of |a|, tracks gravity
float    gJolt = 0;
bool     gArmedAboveThreshold = false;
uint8_t  gCrossings = 0;
uint32_t gFirstCrossingMs = 0;
uint32_t gLastFireMs = 0;
bool     gHasFired = false;    // cooldown only applies once something has fired
uint32_t gStartMs = 0;

namespace {

// LIS2DH-compatible register map.
constexpr uint8_t REG_WHO_AM_I  = 0x0F;
constexpr uint8_t REG_CTRL1     = 0x20;
constexpr uint8_t REG_CTRL4     = 0x23;
constexpr uint8_t REG_OUT_X_L   = 0x28;
constexpr uint8_t AUTO_INCREMENT= 0x80;

// The SC7A20 answers 0x11. Genuine ST parts answer 0x33. Accept either so the
// same firmware runs on a badge that was built with a substitute part.
constexpr uint8_t WHOAMI_SC7A20 = 0x11;
constexpr uint8_t WHOAMI_LIS2DH = 0x33;

// CTRL_REG4 = BDU | FS=+/-4g | high resolution.
constexpr uint8_t CTRL4_VALUE   = 0x80 | 0x10 | 0x08;
// High-resolution mode at +/-4g is 2 mg per count on a 12-bit left-justified
// value, so shift right by 4 and scale.
constexpr float   MG_PER_COUNT  = 2.0f;

constexpr uint32_t kSamplePeriodMs = 10;   // 100 Hz

bool writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(ACCEL_I2C_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool readRegs(uint8_t reg, uint8_t *buf, size_t n) {
  Wire.beginTransmission(ACCEL_I2C_ADDR);
  Wire.write((uint8_t)(n > 1 ? (reg | AUTO_INCREMENT) : reg));
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)ACCEL_I2C_ADDR, (int)n) != (int)n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

}  // namespace

bool begin() {
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 400000);

  if (!readRegs(REG_WHO_AM_I, &gWhoAmI, 1)) return false;
  if (gWhoAmI != WHOAMI_SC7A20 && gWhoAmI != WHOAMI_LIS2DH) return false;

  // 100 Hz, normal power, all three axes enabled.
  if (!writeReg(REG_CTRL1, 0x57)) return false;
  if (!writeReg(REG_CTRL4, CTRL4_VALUE)) return false;

  delay(20);
  gPresent = true;
  detail::reset(millis());
  return true;
}

bool present() { return gPresent; }
uint8_t whoAmI() { return gWhoAmI; }

void accelG(float &x, float &y, float &z) { x = gX; y = gY; z = gZ; }
float jolt() { return gJolt; }

void poll() {
  if (!gPresent) return;

  const uint32_t now = millis();
  if (now - gLastSampleMs < kSamplePeriodMs) return;
  gLastSampleMs = now;

  uint8_t b[6];
  if (!readRegs(REG_OUT_X_L, b, sizeof(b))) return;

  const int16_t rawX = (int16_t)((uint16_t)b[0] | ((uint16_t)b[1] << 8));
  const int16_t rawY = (int16_t)((uint16_t)b[2] | ((uint16_t)b[3] << 8));
  const int16_t rawZ = (int16_t)((uint16_t)b[4] | ((uint16_t)b[5] << 8));

  const float x = (rawX >> 4) * MG_PER_COUNT / 1000.0f;
  const float y = (rawY >> 4) * MG_PER_COUNT / 1000.0f;
  const float z = (rawZ >> 4) * MG_PER_COUNT / 1000.0f;

  if (detail::feedSample(x, y, z, now)) gShakeLatched = true;
}

namespace detail {

bool feedSample(float x, float y, float z, uint32_t nowMs) {
  gX = x; gY = y; gZ = z;

  const float mag = sqrtf(x * x + y * y + z * z);

  // Slow average tracks gravity and any steady tilt, so whatever is left over
  // is motion. The coefficient gives a time constant near half a second at
  // 100 Hz, which is slow enough to ignore a shake and fast enough that
  // turning the badge over does not register as one.
  gBaseline += (mag - gBaseline) * 0.02f;
  gJolt = fabsf(mag - gBaseline);

  // Count threshold crossings with hysteresis, so a single sharp movement that
  // rattles around the threshold cannot be counted as several.
  const float upper = SHAKE_THRESHOLD_G;
  const float lower = SHAKE_THRESHOLD_G * 0.5f;

  if (!gArmedAboveThreshold && gJolt > upper) {
    gArmedAboveThreshold = true;
    if (gCrossings == 0) gFirstCrossingMs = nowMs;
    gCrossings++;
  } else if (gArmedAboveThreshold && gJolt < lower) {
    gArmedAboveThreshold = false;
  }

  // Crossings have to cluster inside the window, otherwise start counting over.
  // This is what separates a shake from a knock, and from the badge swinging
  // on a lanyard while you walk.
  if (gCrossings > 0 && (nowMs - gFirstCrossingMs) > SHAKE_WINDOW_MS) {
    gCrossings = 0;
    gArmedAboveThreshold = false;
  }

  if (gCrossings >= SHAKE_CROSSINGS) {
    gCrossings = 0;
    gArmedAboveThreshold = false;

    // Ignore anything in the first moment after startup, while the baseline is
    // still converging and the badge is probably being picked up.
    if (nowMs - gStartMs < kStartupGraceMs) return false;

    // The cooldown stops one long shake from firing over and over. It must not
    // apply before the first shake, or gLastFireMs sitting at zero would eat
    // every shake for SHAKE_COOLDOWN_MS after boot.
    if (gHasFired && (nowMs - gLastFireMs) < SHAKE_COOLDOWN_MS) return false;

    gHasFired   = true;
    gLastFireMs = nowMs;
    return true;
  }
  return false;
}

void reset(uint32_t nowMs) {
  gBaseline = 1.0f;
  gJolt = 0;
  gCrossings = 0;
  gArmedAboveThreshold = false;
  gFirstCrossingMs = 0;
  gLastFireMs = 0;
  gHasFired = false;
  gStartMs = nowMs;
}

float baseline() { return gBaseline; }

}  // namespace detail

bool shakeDetected() {
  if (!gShakeLatched) return false;
  gShakeLatched = false;
  return true;
}

}  // namespace imu
