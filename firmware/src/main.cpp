// ---------------------------------------------------------------------------
// Hack the North badge -> F450 quadcopter controller.
//
// The badge joins the drone's telemetry Wi-Fi and speaks MAVLink over UDP.
// The D-pad and the A/B buttons drive the aircraft through MANUAL_CONTROL;
// shaking the badge sends it to a position you marked earlier.
//
//   UP / DOWN     climb and descend
//   LEFT / RIGHT  yaw, or strafe, depending on the SW11 slide switch
//   A             fly forward
//   B             fly backward
//   shake         fly to the marked spot and hold station near it
//
// Hold SELECT and press a direction for the commands:
//
//   SELECT + A       arm        (1.5 s, only while landed)
//   SELECT + B       disarm     (1.5 s, only while landed)
//   SELECT + UP      take off to TAKEOFF_ALT_M
//   SELECT + DOWN    land here
//   SELECT + LEFT    return to launch
//   SELECT + RIGHT   mark this spot as the beacon (2 s, drone must be landed)
//
// Safety, because this drives real propellers:
//   - nothing is armed or commanded at boot
//   - sticks are only sent while the MAVLink link is alive
//   - arming and every command needs a deliberate two-button hold
//   - a shake opens a cancel window before anything is sent, and any button
//     press aborts both the window and the flight home
//   - the aircraft is asked to stop COME_STANDOFF_M short of the beacon rather
//     than fly onto the person holding the badge
// ---------------------------------------------------------------------------

#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <Wire.h>
#include <math.h>
#include <stdarg.h>
#include <string.h>

#include "badge_pins.h"
#include "config.h"
#include "buttons.h"
#include "imu.h"
#include "leds.h"
#include "dronelink.h"

namespace {

enum class Mode : uint8_t {
  Boot,
  WifiConnecting,
  LinkWaiting,
  Ready,          // link alive, aircraft disarmed
  Flying,         // armed, sticks live
  ComePending,    // shake seen, cancel window open
  ComeActive,     // streaming a guided position target
  Diagnostics,    // button and accelerometer dump, nothing is transmitted
};

Mode        gMode = Mode::Boot;
Preferences gPrefs;

// --- beacon ----------------------------------------------------------------
struct Beacon {
  bool    set   = false;
  int32_t latE7 = 0;
  int32_t lonE7 = 0;
};
Beacon gBeacon;

void beaconLoad() {
  gPrefs.begin("badgedrone", true);
  gBeacon.latE7 = gPrefs.getInt("blat", 0);
  gBeacon.lonE7 = gPrefs.getInt("blon", 0);
  gPrefs.end();
  gBeacon.set = (gBeacon.latE7 != 0 || gBeacon.lonE7 != 0);

  if (!gBeacon.set && (BEACON_FIXED_LAT != 0.0 || BEACON_FIXED_LON != 0.0)) {
    gBeacon.latE7 = (int32_t)lround(BEACON_FIXED_LAT * 1e7);
    gBeacon.lonE7 = (int32_t)lround(BEACON_FIXED_LON * 1e7);
    gBeacon.set   = true;
  }
}

void beaconSave(int32_t latE7, int32_t lonE7) {
  gBeacon.latE7 = latE7;
  gBeacon.lonE7 = lonE7;
  gBeacon.set   = true;
  gPrefs.begin("badgedrone", false);
  gPrefs.putInt("blat", latE7);
  gPrefs.putInt("blon", lonE7);
  gPrefs.end();
}

// --- geometry --------------------------------------------------------------
constexpr double kEarthR = 6371000.0;

// Metres between two lat/lon pairs. Equirectangular is accurate to well under a
// metre over the few hundred metres a badge-controlled drone should ever be at.
double distanceM(int32_t aLatE7, int32_t aLonE7, int32_t bLatE7, int32_t bLonE7) {
  const double aLat = aLatE7 / 1e7 * DEG_TO_RAD;
  const double bLat = bLatE7 / 1e7 * DEG_TO_RAD;
  const double dLat = bLat - aLat;
  const double dLon = (bLonE7 - aLonE7) / 1e7 * DEG_TO_RAD * cos((aLat + bLat) * 0.5);
  return kEarthR * sqrt(dLat * dLat + dLon * dLon);
}

// The point COME_STANDOFF_M from the beacon, on the side the drone is already
// on. Flying to the beacon itself would mean flying at the person holding it.
void standoffTarget(int32_t droneLatE7, int32_t droneLonE7,
                    int32_t &outLatE7, int32_t &outLonE7) {
  const double midLat = (gBeacon.latE7 + droneLatE7) / 2e7 * DEG_TO_RAD;
  const double cosLat = cos(midLat);

  // Offsets from beacon to drone, in metres.
  const double dNorth = (droneLatE7 - gBeacon.latE7) / 1e7 * DEG_TO_RAD * kEarthR;
  const double dEast  = (droneLonE7 - gBeacon.lonE7) / 1e7 * DEG_TO_RAD * kEarthR * cosLat;
  const double dist   = sqrt(dNorth * dNorth + dEast * dEast);

  if (dist < 0.5) {
    // Already on top of the beacon, and there is no meaningful direction to
    // back off along. Hold where it is rather than picking one at random.
    outLatE7 = droneLatE7;
    outLonE7 = droneLonE7;
    return;
  }

  const double k = COME_STANDOFF_M / dist;
  const double tNorth = dNorth * k;
  const double tEast  = dEast  * k;

  outLatE7 = gBeacon.latE7 + (int32_t)lround(tNorth / kEarthR * RAD_TO_DEG * 1e7);
  outLonE7 = gBeacon.lonE7 + (int32_t)lround(tEast / (kEarthR * cosLat) * RAD_TO_DEG * 1e7);
}

// --- stick shaping ---------------------------------------------------------
struct Axis {
  float value = 0;
  void step(float target, uint32_t dtMs) {
    // Ramp up gently, fall back to centre about twice as fast. Letting go
    // should always settle the aircraft quicker than pushing moves it.
    const float rampMs = (fabsf(target) > fabsf(value)) ? STICK_RAMP_MS
                                                        : STICK_RAMP_MS * 0.5f;
    const float maxStep = 1000.0f * (float)dtMs / rampMs;
    const float delta = target - value;
    if (fabsf(delta) <= maxStep) value = target;
    else                          value += (delta > 0 ? maxStep : -maxStep);
  }
  int16_t out() const { return (int16_t)lroundf(value); }
};

Axis gPitch, gRoll, gYaw, gThrottle;

void centreSticks() {
  gPitch.value = gRoll.value = gYaw.value = 0;
  gThrottle.value = 0;
}

// --- helpers ---------------------------------------------------------------
bool gpsGoodEnough() {
  const auto &s = dronelink::state();
  return s.gpsFixType >= MIN_GPS_FIX && s.satellites >= MIN_SATELLITES && s.posValid;
}

bool airborne() {
  const auto &s = dronelink::state();
  return s.armed && s.relAltM > 1.0f;
}

void say(const char *fmt, ...) {
  char buf[160];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  Serial.printf("[badge] %s\n", buf);
}

void reject(const char *why) {
  say("refused: %s", why);
  leds::flash(leds::Status::Rejected, 700);
}

// --- SELECT combos ---------------------------------------------------------
// Each combo fires once when the hold time is reached and then stays latched
// until the button is let go, so holding does not repeat the command.
bool gComboLatched[btn::COUNT] = {false};

bool comboFired(btn::Id b, uint32_t holdMs) {
  if (!btn::down(btn::SELECT) || !btn::down(b)) {
    gComboLatched[b] = false;
    return false;
  }
  if (gComboLatched[b]) return false;
  // Time off whichever button went down later, so the combo always needs the
  // full hold with both pressed. Timing off the direction button alone would
  // fire instantly whenever SELECT is the second button reached for.
  const uint32_t held = min(btn::heldMs(btn::SELECT), btn::heldMs(b));
  if (held >= holdMs) {
    gComboLatched[b] = true;
    return true;
  }
  return false;
}

void handleCombos() {
  const auto &s = dronelink::state();

  if (comboFired(btn::A, COMBO_HOLD_MS)) {
    if (s.armed)               reject("already armed");
    else if (!gpsGoodEnough()) reject("no GPS fix, will not arm");
    else {
      say("arming");
      dronelink::sendSetMode(FLIGHT_MODE);
      dronelink::sendArm(true, false);
    }
  }

  if (comboFired(btn::B, COMBO_HOLD_MS)) {
    if (!s.armed)      reject("already disarmed");
    else if (airborne()) reject("airborne, land first");
    else {
      say("disarming");
      dronelink::sendArm(false, false);
    }
  }

  if (comboFired(btn::UP, COMBO_HOLD_MS)) {
    if (!s.armed)      reject("not armed");
    else if (airborne()) reject("already flying");
    else {
      say("takeoff to %.1f m", (double)TAKEOFF_ALT_M);
      // Takeoff is only accepted in GUIDED, and the mode change needs a moment
      // to be applied before the command lands. This is a one-shot combo, so a
      // short stall here is not worth a state machine.
      dronelink::sendSetMode(COPTER_MODE_GUIDED);
      delay(120);
      dronelink::sendTakeoff(TAKEOFF_ALT_M);
    }
  }

  if (comboFired(btn::DOWN, COMBO_HOLD_MS)) {
    say("landing");
    centreSticks();
    dronelink::sendLand();
  }

  if (comboFired(btn::LEFT, COMBO_HOLD_MS)) {
    say("return to launch");
    centreSticks();
    dronelink::sendRtl();
  }

  if (comboFired(btn::RIGHT, COMBO_HOLD_LONG_MS)) {
    if (airborne())            reject("land the drone on the spot you want marked");
    else if (!gpsGoodEnough()) reject("no GPS fix, cannot mark");
    else {
      beaconSave(s.latE7, s.lonE7);
      say("beacon marked at %.7f, %.7f", s.latE7 / 1e7, s.lonE7 / 1e7);
      leds::flash(leds::Status::BeaconSet, 900);
    }
  }
}

// --- flying ----------------------------------------------------------------
uint32_t gLastStickMs = 0;

void updateSticks() {
  const uint32_t now = millis();
  const uint32_t dt = gLastStickMs ? (now - gLastStickMs) : 20;
  gLastStickMs = now;

  float pitch = 0, roll = 0, yaw = 0, thr = 0;

  // SELECT is the command modifier, so the sticks go quiet while it is held.
  // Without this you would yaw every time you reached for a combo.
  if (!btn::down(btn::SELECT)) {
    if (btn::down(btn::A))     pitch += STICK_PITCH;
    if (btn::down(btn::B))     pitch -= STICK_PITCH;
    if (btn::down(btn::UP))    thr   += STICK_THROTTLE;
    if (btn::down(btn::DOWN))  thr   -= STICK_THROTTLE;

    // The SW11 slide switch picks what left and right mean. One position turns
    // the aircraft, the other slides it sideways.
    const bool strafe = btn::down(btn::SLIDE) ? !DPAD_LR_IS_ROLL : (bool)DPAD_LR_IS_ROLL;
    if (btn::down(btn::LEFT))  { if (strafe) roll -= STICK_ROLL; else yaw -= STICK_YAW; }
    if (btn::down(btn::RIGHT)) { if (strafe) roll += STICK_ROLL; else yaw += STICK_YAW; }
  }

  gPitch.step(pitch, dt);
  gRoll.step(roll, dt);
  gYaw.step(yaw, dt);
  gThrottle.step(thr, dt);
}

void sendSticks() {
  const int16_t z = (int16_t)constrain(THROTTLE_NEUTRAL + gThrottle.out(), -1000, 1000);
  dronelink::sendManualControl(gPitch.out(), gRoll.out(), z, gYaw.out(),
                          btn::rawRegister());
}

// --- come to me ------------------------------------------------------------
uint32_t gComePendingSinceMs = 0;
uint32_t gComeArrivedSinceMs = 0;

bool comeToMeAllowed(const char **why) {
  if (!dronelink::linkAlive())  { *why = "no link"; return false; }
  if (!gBeacon.set)        { *why = "no beacon marked yet"; return false; }
  if (!airborne())         { *why = "drone is not flying"; return false; }
  if (!gpsGoodEnough())    { *why = "GPS not good enough"; return false; }
  return true;
}

void enterComePending() {
  gComePendingSinceMs = millis();
  gComeArrivedSinceMs = 0;
  centreSticks();
  gMode = Mode::ComePending;
  say("shake detected, flying home in %.1f s unless you press something",
      COME_CONFIRM_MS / 1000.0);
}

void enterComeActive() {
  gMode = Mode::ComeActive;
  gComeArrivedSinceMs = 0;
  dronelink::sendSetMode(COPTER_MODE_GUIDED);
  say("coming to you");
}

void abortCome(const char *why) {
  say("flight home stopped: %s", why);
  dronelink::sendSetMode(FLIGHT_MODE);
  centreSticks();
  gMode = Mode::Flying;
}

// Checked every loop, not at the streaming rate, so letting go of the
// manoeuvre is immediate rather than up to a fifth of a second late.
bool comeActiveShouldStop() {
  const auto &s = dronelink::state();

  if (btn::anyDown())   { abortCome("button pressed"); return true; }
  if (!s.armed)         { gMode = Mode::Ready; return true; }
  if (!gpsGoodEnough()) { abortCome("lost GPS quality"); return true; }
  return false;
}

void serviceComeActive() {
  const auto &s = dronelink::state();

  int32_t tLat, tLon;
  standoffTarget(s.latE7, s.lonE7, tLat, tLon);

  // Hold the altitude it is already at, clamped into a sane band, rather than
  // commanding a climb or a dive on the way over.
  const float alt = constrain(s.relAltM, COME_MIN_ALT_M, COME_MAX_ALT_M);
  dronelink::sendGuidedPosition(tLat, tLon, alt);

  const double d = distanceM(s.latE7, s.lonE7, gBeacon.latE7, gBeacon.lonE7);
  if (d <= COME_STANDOFF_M + 1.5) {
    if (gComeArrivedSinceMs == 0) gComeArrivedSinceMs = millis();
    // Require it to stay there, so one GPS sample drifting close does not end
    // the manoeuvre early.
    if (millis() - gComeArrivedSinceMs > 2000) {
      say("arrived, %.1f m away, holding", d);
      dronelink::sendSetMode(FLIGHT_MODE);
      gMode = Mode::Flying;
    }
  } else {
    gComeArrivedSinceMs = 0;
  }
}

// --- diagnostics -----------------------------------------------------------
// Hold BOOT at power-up to land here. Nothing is transmitted in this mode, so
// it is safe with a battery plugged into the drone. Use it to confirm the
// button bit mapping on your badge and to tune the shake threshold.
uint32_t gLastDiagMs = 0;

// --- I2C bus scan ----------------------------------------------------------
// Diagnostics only, and the fastest way to find out what is actually on the
// bus. The accelerometer (U2) and the MFRC522 NFC reader (U7) share SDA/SCL,
// confirmed from the board file: U2.2 and U7.24 both sit on I2C_SDA_BUS, and
// U2.12 and U7.31 both sit on I2C_SCL_BUS.
//
// The NFC reader has no driver in this firmware. This scan is how you confirm
// it is alive and at what address before writing one.
//
// U7's strapping, all read off the board rather than assumed:
//   U7.1   10k to +3V3 (R16)  -> interface select HIGH, so I2C, not SPI/UART
//   U7.6   10k to +3V3 (R14)  -> NRSTPD high, the part is not held in reset
//   U7.25  10k to +3V3 (R9)   -> address strap 1
//   U7.26  10k to GND  (R10)  -> address strap 0
//   U7.27  10k to GND  (R11)  -> address strap 0
//
// The address is 0x26, per Hack the North's own HAL guide for this board.
// Worth recording how that went: the straps above were read correctly off the
// PCB, but turning them into an address needs the MFRC522's pin-function
// names, and the schematic symbol is an EasyEDA conversion that calls them
// A0/A1/D1-D6 rather than the datasheet's ADR_n. Reading 0x29 out of that was
// a guess dressed as arithmetic. The scan below reports whatever actually
// answers, which is the only reason the mistake was cheap.
const char *i2cWhat(uint8_t addr) {
  if (addr == ACCEL_I2C_ADDR)       return "SC7A20 accelerometer (U2)";
  if (addr == 0x18)                 return "SC7A20 at the SDO-low address; check R4";
  if (addr == 0x26)                 return "MFRC522 NFC reader (U7)";
  if (addr >= 0x24 && addr <= 0x2F) return "MFRC522 NFC reader (U7), unexpected address strap";
  return "unexpected; nothing on this badge should answer here";
}

void i2cScan() {
  Serial.println("[badge] I2C scan on SDA=GPIO5 SCL=GPIO6:");
  int found = 0;
  for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  0x%02X  %s\n", addr, i2cWhat(addr));
      found++;
    }
  }
  if (found == 0) {
    Serial.println("  nothing answered. The 4k7 pull-ups are R34 and R36; check "
                   "those and that SDA/SCL are not swapped.");
  } else {
    Serial.printf("  %d device(s). Expect two: 0x%02X and the NFC reader.\n",
                  found, ACCEL_I2C_ADDR);
  }
}

void serviceDiagnostics() {
  leds::setStatus(leds::Status::Booting);
  if (millis() - gLastDiagMs < 200) return;
  gLastDiagMs = millis();

  char pressed[80] = {0};
  for (int i = 0; i < btn::COUNT; i++) {
    if (btn::down((btn::Id)i)) {
      strncat(pressed, btn::name((btn::Id)i), sizeof(pressed) - strlen(pressed) - 2);
      strncat(pressed, " ", sizeof(pressed) - strlen(pressed) - 1);
    }
  }

  float ax, ay, az;
  imu::accelG(ax, ay, az);
  Serial.printf("reg=0x%02X  held=[%s]  accel=%+.2f %+.2f %+.2f g  jolt=%.2f g%s\n",
                btn::rawRegister(), pressed, ax, ay, az, imu::jolt(),
                imu::shakeDetected() ? "   <-- SHAKE" : "");
}

// --- status ----------------------------------------------------------------
// How long after startup BOOT still selects diagnostics. It cannot be sampled
// at reset, because GPIO9 is the ESP32-C3 boot strapping pin.
constexpr uint32_t kDiagWindowMs = 5000;

uint32_t gLastReportMs = 0;

void report() {
  if (millis() - gLastReportMs < 3000) return;
  gLastReportMs = millis();
  const auto &s = dronelink::state();
  Serial.printf("[badge] wifi=%d link=%d armed=%d mode=%lu alt=%.1fm "
                "sats=%u fix=%u batt=%.2fV beacon=%s\n",
                (int)dronelink::wifiConnected(), (int)dronelink::linkAlive(), (int)s.armed,
                (unsigned long)s.customMode, (double)s.relAltM, s.satellites,
                s.gpsFixType, s.batteryMv / 1000.0,
                gBeacon.set ? "set" : "none");
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);

  btn::begin();
  leds::begin();
  leds::setStatus(leds::Status::Booting);

  btn::poll();
  // NOT sampled at reset, on purpose. SW10 shorts GPIO9 to ground and GPIO9 is
  // the ESP32-C3's boot strapping pin, so holding BOOT through a reset puts the
  // ROM into serial download mode and this firmware never runs. Diagnostics is
  // therefore entered from loop(), within kDiagWindowMs of startup: power up
  // normally, then press and hold BOOT.
  const bool diag = false;

  if (!imu::begin()) {
    Serial.println("[badge] accelerometer did not answer on I2C; "
                   "shake-to-come is disabled");
  } else {
    Serial.printf("[badge] accelerometer up, WHO_AM_I=0x%02X\n", imu::whoAmI());
  }

  beaconLoad();
  Serial.printf("[badge] beacon %s\n",
                gBeacon.set ? "loaded from flash" : "not set; mark one with SELECT+RIGHT");

  if (diag) {
    Serial.println("[badge] BOOT held: diagnostics mode, radio stays off");
    i2cScan();
    gMode = Mode::Diagnostics;
    return;
  }

  Serial.println("[badge] press and hold BOOT in the next 5s for diagnostics "
                 "(console only, radio off)");
  dronelink::begin();
  gMode = Mode::WifiConnecting;
  leds::setStatus(leds::Status::WifiConnecting);
}

void loop() {
  btn::poll();
  imu::poll();

  // Diagnostics is selected after the chip is already running; see the note in
  // setup() about GPIO9. Shut the radio down on the way in so nothing is
  // transmitted, which is what makes this safe with a battery in the drone.
  if (gMode != Mode::Diagnostics && millis() < kDiagWindowMs && btn::down(btn::BOOT)) {
    Serial.println("[badge] BOOT pressed: diagnostics mode, radio off");
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    i2cScan();
    gMode = Mode::Diagnostics;
    centreSticks();
  }

  if (gMode == Mode::Diagnostics) {
    serviceDiagnostics();
    leds::poll();
    return;
  }

  dronelink::poll();

  static uint32_t lastHb = 0, lastCtl = 0, lastGuided = 0;
  const uint32_t now = millis();

  // A ground station has to keep announcing itself or ArduPilot stops accepting
  // MANUAL_CONTROL from it, so this runs in every mode.
  if (now - lastHb >= 1000 / HEARTBEAT_HZ) {
    lastHb = now;
    dronelink::sendHeartbeat();
  }

  // --- connection states ---------------------------------------------------
  if (!dronelink::wifiConnected()) {
    gMode = Mode::WifiConnecting;
    leds::setStatus(leds::Status::WifiConnecting);
    centreSticks();
    leds::poll();
    report();
    return;
  }

  if (!dronelink::linkAlive()) {
    if (gMode == Mode::ComeActive || gMode == Mode::ComePending) {
      say("link lost during flight home; stopped commanding");
    }
    // Never seen a heartbeat at all is a different picture from one that
    // stopped, so the LEDs distinguish them.
    gMode = Mode::LinkWaiting;
    leds::setStatus(dronelink::state().heartbeatSeen ? leds::Status::LinkLost
                                                : leds::Status::LinkWaiting);
    centreSticks();
    leds::poll();
    report();
    return;
  }

  handleCombos();

  const auto &s = dronelink::state();

  // --- mode transitions ----------------------------------------------------
  switch (gMode) {
    case Mode::WifiConnecting:
    case Mode::LinkWaiting:
      gMode = s.armed ? Mode::Flying : Mode::Ready;
      break;

    case Mode::Ready:
      if (s.armed) gMode = Mode::Flying;
      break;

    case Mode::Flying:
      if (!s.armed) gMode = Mode::Ready;
      break;

    case Mode::ComePending:
      if (!s.armed) {
        gMode = Mode::Ready;
      } else if (btn::anyDown()) {
        say("flight home cancelled");
        gMode = Mode::Flying;
      } else if (now - gComePendingSinceMs >= COME_CONFIRM_MS) {
        enterComeActive();
      }
      break;

    case Mode::ComeActive:
      comeActiveShouldStop();
      break;

    default:
      break;
  }

  // --- shake ---------------------------------------------------------------
  if (imu::shakeDetected() && gMode != Mode::ComePending && gMode != Mode::ComeActive) {
    const char *why = nullptr;
    if (comeToMeAllowed(&why)) enterComePending();
    else                       reject(why);
  }

  // --- per-mode work -------------------------------------------------------
  switch (gMode) {
    case Mode::Ready:
      leds::setStatus(leds::Status::Disarmed);
      centreSticks();
      break;

    case Mode::Flying:
      leds::setStatus(airborne() ? leds::Status::Flying : leds::Status::Armed);
      updateSticks();
      if (now - lastCtl >= 1000 / CONTROL_HZ) {
        lastCtl = now;
        sendSticks();
      }
      break;

    case Mode::ComePending:
      leds::setStatus(leds::Status::ComePending);
      centreSticks();
      // Keep the stick stream alive at centre so the autopilot does not see the
      // controller drop out mid-manoeuvre.
      if (now - lastCtl >= 1000 / CONTROL_HZ) {
        lastCtl = now;
        sendSticks();
      }
      break;

    case Mode::ComeActive:
      leds::setStatus(leds::Status::ComeActive);
      if (now - lastGuided >= 1000 / GUIDED_HZ) {
        lastGuided = now;
        serviceComeActive();
      }
      break;

    default:
      break;
  }

  leds::poll();
  report();
}
