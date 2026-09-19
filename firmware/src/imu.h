#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// SC7A20 accelerometer (U2) and the shake detector built on it.
//
// The SC7A20 is register-compatible with the ST LIS2DH family. On this badge
// its CS pin is pulled high through R5, which selects the I2C interface, and
// its SDO pin is pulled high through R4, which sets the 7-bit address to 0x19.
// Both resistors were read off the board rather than assumed.
//
// Shake detection removes gravity with a slow exponential average, then counts
// how many times the leftover magnitude crosses a threshold. Requiring several
// crossings inside a short window is what separates a deliberate shake from a
// single knock or from the badge swinging on a lanyard while you walk.
// ---------------------------------------------------------------------------

namespace imu {

bool begin();          // false if the chip does not answer on I2C
bool present();

// Reads a fresh sample and advances the shake detector. Call every loop.
void poll();

// True exactly once per detected shake, subject to the cooldown in config.h.
bool shakeDetected();

// Latest reading in g, board axes.
void accelG(float &x, float &y, float &z);

// Gravity-removed magnitude in g, which is what the threshold is compared to.
float jolt();

// WHO_AM_I value read at startup, for diagnostics.
uint8_t whoAmI();

namespace detail {
// The detector, split out from the I2C read so it can be driven with recorded
// or synthetic motion on a host. poll() feeds it real samples; the tests feed
// it a walk, a knock and a shake to check the thresholds actually tell them
// apart. Returns true on the sample where a shake completes.
bool feedSample(float x, float y, float z, uint32_t nowMs);
void reset(uint32_t nowMs);
float baseline();
}  // namespace detail

}  // namespace imu
