#pragma once
// ---------------------------------------------------------------------------
// Everything you are expected to edit lives in this file.
// ---------------------------------------------------------------------------

// === Wi-Fi ==================================================================
// The badge joins the drone's telemetry Wi-Fi as a station. Whatever is doing
// the bridging on the aircraft, it needs to present MAVLink over UDP.
//
// These defaults match pi/setup.sh, which puts a Raspberry Pi access point on
// 192.168.4.1. Change WIFI_PASS: setup.sh refuses to run without a password
// you chose, and this placeholder is not one.
//
// If you are using one of the small bridges instead, their stock settings are:
//
//   MAVESP8266            SSID "ArduPilot",             pass "ardupilot",
//                         192.168.4.1
//   DroneBridge for ESP32 SSID "DroneBridge for ESP32", pass "dronebridge",
//                         192.168.2.1
//
// Wi-Fi names are case sensitive, so "ardupilot" will not find "ArduPilot".
//
// REAL CREDENTIALS DO NOT GO IN THIS FILE -- it is tracked by git, and a
// password committed once stays in the history even after it is deleted.
// Put them in include/secrets.h, which is gitignored:
//
//     #define WIFI_SSID "my-network"
//     #define WIFI_PASS "my-password"
//     #define PI_IP     "192.168.4.5"
//
// The placeholders below apply only when that file is absent.
#if __has_include("secrets.h")
  #include "secrets.h"
#endif

#ifndef WIFI_SSID
  #define WIFI_SSID      "f450-badge"
#endif
#ifndef WIFI_PASS
  #define WIFI_PASS      "change-me-please"
#endif

// UDP endpoints. DRONE_IP is where we send before anything has been heard back.
// Leave AUTO_DISCOVER_PEER on and the badge re-targets whatever address MAVLink
// actually arrives from, which covers bridges that broadcast, DHCP surprises,
// and setups behind a router.
#define DRONE_IP         "192.168.4.1"
#define DRONE_UDP_PORT   14550
#define LOCAL_UDP_PORT   14550
#define AUTO_DISCOVER_PEER 1

// === Badge -> Pi button link (env:pilink only) ==============================
// Used by src/pilink_main.cpp, which sends button state to the Raspberry Pi as
// plain text and lets the Pi decide what the drone should do. None of this
// affects the MAVLink firmware in main.cpp.
//
// PI_IP defaults to the same address as the bridge, because pi/setup.sh puts
// the access point on 192.168.4.1 either way. The port is deliberately NOT
// 14550: that one belongs to MAVLink, and keeping them apart means you can run
// both on the Pi at once without them eating each other's packets.
                                        // set back to DRONE_IP for the Pi AP
#ifndef PI_IP
  #define PI_IP           DRONE_IP
#endif
#define PI_UDP_PORT       14555   // where the Pi listens for button state
#define PI_UDP_LOCAL_PORT 14556   // where the badge listens for the reply

// Wi-Fi transmit power. Lower means smaller current spikes, which matters a
// lot on two AA cells through a boost converter: a brownout there looks like
// a firmware crash and is not one. WIFI_POWER_11dBm still leaves tens of dB
// of margin against a Pi in the same room. Raise it if the link gets flaky at
// distance, and expect to pay for it in battery life.
#define WIFI_TX_POWER     WIFI_POWER_19_5dBm

// Send rates. Fast while a button is held so a press lands promptly, slow when
// idle so the Pi still has a liveness signal without pointless traffic.
#define BADGE_SEND_HZ     20
#define BADGE_IDLE_HZ     2

// === MAVLink identity =======================================================
// 255 is the conventional ground-station system id. Component 190 marks us as a
// manual-control station rather than a mission planner.
#define GCS_SYSID        255
#define GCS_COMPID       190

// === Flight stack ===========================================================
// 1 = ArduPilot / ArduCopter (the usual F450 setup, and what this is tuned for)
// 0 = PX4. See README for the differences that are not handled automatically.
#define STACK_ARDUPILOT  1

// ArduCopter custom_mode numbers used by the mode commands below.
#define COPTER_MODE_STABILIZE 0
#define COPTER_MODE_ALT_HOLD  2
#define COPTER_MODE_LOITER    5
#define COPTER_MODE_RTL       6
#define COPTER_MODE_GUIDED    4
#define COPTER_MODE_LAND      9

// Mode the badge puts the aircraft in for stick flying. LOITER holds position
// when you let go of the buttons, which is the forgiving choice outdoors with a
// GPS lock. ALT_HOLD only holds height and will drift. Do not use STABILIZE
// with this firmware: it has no throttle stick, so it would drop out of the sky.
#define FLIGHT_MODE      COPTER_MODE_LOITER

// === Stick feel =============================================================
// MANUAL_CONTROL axes run -1000..+1000. These are the values a held button
// produces. Start gentle; 300 is roughly a third stick and is plenty indoors.
#define STICK_PITCH      300   // A / B, forward and back
#define STICK_ROLL       300   // left / right when strafing
#define STICK_YAW        350   // left / right when turning
#define STICK_THROTTLE   250   // up / down, offset either side of neutral

// Throttle centre for the z axis. ArduPilot reads MANUAL_CONTROL z as 0..1000
// with 500 meaning "hold what you have". PX4 wants -1000..1000 centred on 0.
#if STACK_ARDUPILOT
  #define THROTTLE_NEUTRAL 500
#else
  #define THROTTLE_NEUTRAL 0
#endif

// Milliseconds for a held button to ramp from nothing to full deflection.
// Ramping stops a button press from being a step input into the controller.
#define STICK_RAMP_MS    350

// What left and right do when the SW11 slide switch is in its default position.
// 0 = yaw (spin on the spot), 1 = roll (strafe sideways). Flicking the slide
// switch swaps to the other one in flight, so you get both without a combo.
#define DPAD_LR_IS_ROLL  0

// === Rates ==================================================================
#define HEARTBEAT_HZ     1
#define CONTROL_HZ       20    // MANUAL_CONTROL send rate
#define GUIDED_HZ        5     // position target rate while flying home

// === Failsafes ==============================================================
// No MAVLink from the aircraft for this long and the badge declares the link
// dead, stops sending sticks and shows red.
#define LINK_TIMEOUT_MS  2000

// Refuse to arm, and abort a flight-home, if the GPS is not good enough.
#define MIN_GPS_FIX      3     // 3 = 3D fix
#define MIN_SATELLITES   6

// === Come to me =============================================================
// How close to the beacon the drone is asked to stop. It flies to a point this
// far from the beacon on the side it is already approaching from, so it parks
// near you instead of on top of you. Do not set this below about 3 metres.
#define COME_STANDOFF_M     4.0f

// Altitude band held during the flight home, in metres above the launch point.
#define COME_MIN_ALT_M      3.0f
#define COME_MAX_ALT_M      10.0f

// After a shake is detected the badge flashes amber for this long before it
// actually commands anything. Any button press during the window cancels.
#define COME_CONFIRM_MS     1500

// Ignore further shakes for this long after one fires.
#define SHAKE_COOLDOWN_MS   4000

// === Shake detection ========================================================
// A shake is counted when the accelerometer's gravity-removed magnitude swings
// past this threshold, in g. Raise it if the drone gets called by walking.
#define SHAKE_THRESHOLD_G   1.1f

// Number of threshold crossings, and the window they must land in, for the
// motion to count as a deliberate shake rather than a knock.
#define SHAKE_CROSSINGS     4
#define SHAKE_WINDOW_MS     1200

// === Optional fixed beacon ==================================================
// Leave both at 0 to use the "mark my spot" gesture instead, which captures the
// drone's own GPS while it sits next to you. If you know your coordinates, put
// them here and they load at boot.
#define BEACON_FIXED_LAT    0.0
#define BEACON_FIXED_LON    0.0

// === Takeoff ================================================================
// Altitude the guided-takeoff combo climbs to, in metres above launch.
#define TAKEOFF_ALT_M       3.0f

// How long a SELECT combo must be held before it fires, in milliseconds.
// Long enough that nothing happens by accident, short enough to feel responsive.
#define COMBO_HOLD_MS       1500
#define COMBO_HOLD_LONG_MS  2000
