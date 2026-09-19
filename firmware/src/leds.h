#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// The six WS2812B pixels are the only status display this firmware drives, so
// they carry all the state you need while flying: link, arm state, and what
// the badge is about to do.
//
// They are powered from the +5V boost (MT3608, U4), which slide switch SW1
// gates. If the LEDs stay dark but the badge is clearly running, check SW1
// before suspecting the firmware.
// ---------------------------------------------------------------------------

namespace leds {

enum class Status : uint8_t {
  Booting,        // white chase
  WifiConnecting, // blue chase
  LinkWaiting,    // blue breathing, Wi-Fi up but no MAVLink yet
  LinkLost,       // fast red blink
  Disarmed,       // steady dim green
  Armed,          // steady red
  Flying,         // slow green sweep
  ComePending,    // amber flash, cancel window open
  ComeActive,     // magenta sweep, flying home
  BeaconSet,      // brief cyan flash
  Rejected,       // brief red double flash
};

void begin();
void setStatus(Status s);
void flash(Status s, uint32_t ms);   // temporary overlay, then back to status
void poll();                          // call every loop; drives animation
void allOff();

}  // namespace leds
