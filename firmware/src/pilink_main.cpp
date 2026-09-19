// ---------------------------------------------------------------------------
// Badge -> Raspberry Pi button link.
//
// This is the first half of the badge-flies-the-drone chain, and deliberately
// the dumbest possible version of it:
//
//     badge buttons  --Wi-Fi/UDP-->  Pi (logs them)  --MAVLink-->  Pixhawk
//     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
//     this file covers exactly this much
//
// The badge sends button STATE, not commands. It has no idea a drone exists.
// All the deciding happens on the Pi, which is what makes this testable: the
// Pi can log packets long before it is wired to a flight controller, and the
// badge never needs reflashing as the drone side changes.
//
// This is a different architecture from main.cpp, which speaks MAVLink
// directly and needs a flying aircraft with a GPS position estimate. Both are
// kept: `pio run -e badge` builds that one, `pio run -e pilink` builds this
// one. They share the button and LED drivers and nothing else.
//
// Wire format is one line of ASCII per packet, on purpose. You can read it
// with netcat before writing any parser at all:
//
//     BADGE1 seq=42 ms=12345 raw=0xFB down=[UP A]
//
// There is a local mode that prints the same lines to the USB console and
// never touches the radio, which works with no Pi, no network and no drone. It
// is the first thing to try on a fresh badge.
//
// To get it: power up normally, THEN press and hold BOOT within five seconds.
//
// Do NOT hold BOOT while resetting, even though that is the obvious thing to
// try and what an earlier version of these docs said. SW10 shorts GPIO9 to
// ground (R31 pulls it to 3V3, read off the board), and GPIO9 is the
// ESP32-C3's boot strapping pin: low at reset puts the ROM into serial
// download mode and this firmware never runs at all. The symptom is a serial
// port that enumerates, prints a ROM banner, and then says nothing.
// ---------------------------------------------------------------------------

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <string.h>

#include "badge_pins.h"
#include "config.h"
#include "buttons.h"
#include "leds.h"

namespace {

WiFiUDP   gUdp;
IPAddress gPeer;
uint32_t  gSeq         = 0;
uint32_t  gLastSendMs  = 0;
uint32_t  gLastAckMs   = 0;
uint32_t  gLastLogMs   = 0;
bool      gEverAcked   = false;
bool      gLocalOnly   = false;   // BOOT held at reset: print, never transmit

// The Pi has to answer for the badge to believe the link is up. Two seconds of
// silence and the LEDs go red, same failsafe idea as the MAVLink firmware.
constexpr uint32_t kAckTimeoutMs = 2000;

// How long after startup BOOT still selects local mode. It cannot be sampled
// at reset -- see the note at the top of this file about GPIO9 being the boot
// strapping pin -- so the window has to live here instead.
constexpr uint32_t kModeWindowMs = 5000;

bool gScanned = false;   // the one-shot diagnostic scan below

// Arduino's WiFi status codes, spelled out. Worth having by name: 1 and 4
// look the same from outside and mean completely different things.
const char *wifiStatusName(int s) {
  switch (s) {
    case WL_IDLE_STATUS:     return "idle";
    case WL_NO_SSID_AVAIL:   return "SSID not seen on the air";
    case WL_CONNECTED:       return "connected";
    case WL_CONNECT_FAILED:  return "rejected, usually a wrong password";
    case WL_CONNECTION_LOST: return "connection lost";
    case WL_DISCONNECTED:    return "disconnected, still trying";
    default:                 return "unknown";
  }
}

// Reported in a fixed order so the Pi side can rely on it. BOOT is excluded:
// it is the mode strap, not a game button.
const btn::Id kReported[] = {btn::UP, btn::DOWN, btn::LEFT, btn::RIGHT,
                             btn::A,  btn::B,    btn::SELECT, btn::SLIDE};

// Fills `out` with the names of every held button, space separated.
void heldNames(char *out, size_t cap) {
  out[0] = 0;
  for (btn::Id b : kReported) {
    if (!btn::down(b)) continue;
    if (out[0]) strncat(out, " ", cap - strlen(out) - 1);
    strncat(out, btn::name(b), cap - strlen(out) - 1);
  }
}

int formatLine(char *line, size_t cap) {
  char held[96];
  heldNames(held, sizeof(held));
  return snprintf(line, cap, "BADGE1 seq=%lu ms=%lu raw=0x%02X down=[%s]",
                  (unsigned long)++gSeq, (unsigned long)millis(),
                  btn::rawRegister(), held);
}

void sendState() {
  char line[192];
  const int len = formatLine(line, sizeof(line));
  if (len <= 0) return;

  gUdp.beginPacket(gPeer, PI_UDP_PORT);
  gUdp.write(reinterpret_cast<const uint8_t *>(line), len);
  gUdp.endPacket();
}

// Anything coming back counts as an acknowledgement. The Pi's reply content is
// not parsed, because the only question being asked is "is something alive at
// the other end". Re-targets the peer if the reply came from somewhere else,
// which covers a Pi on a different address than the one in config.h.
void pollAck() {
  int size = gUdp.parsePacket();
  while (size > 0) {
    uint8_t scratch[64];
    gUdp.read(scratch, sizeof(scratch));
    gLastAckMs = millis();
    if (!gEverAcked) {
      gEverAcked = true;
      Serial.printf("[pilink] Pi answered from %s\n",
                    gUdp.remoteIP().toString().c_str());
    }
#if AUTO_DISCOVER_PEER
    if (gUdp.remoteIP() != gPeer) {
      gPeer = gUdp.remoteIP();
      Serial.printf("[pilink] peer is now %s\n", gPeer.toString().c_str());
    }
#endif
    size = gUdp.parsePacket();
  }
}

// Local mode: the same line, to the console, with the radio left off.
void serviceLocalOnly() {
  if (millis() - gLastLogMs < 200) return;
  gLastLogMs = millis();
  char line[192];
  formatLine(line, sizeof(line));
  Serial.println(line);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);

  btn::begin();
  leds::begin();
  leds::setStatus(leds::Status::Booting);

  btn::poll();
  Serial.println("[pilink] press and hold BOOT in the next 5s for local mode "
                 "(console only, radio off)");

  Serial.printf("[pilink] joining \"%s\"\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  leds::setStatus(leds::Status::WifiConnecting);

  gPeer.fromString(PI_IP);
  gUdp.begin(PI_UDP_LOCAL_PORT);
  Serial.printf("[pilink] sending to %s:%d\n", PI_IP, PI_UDP_PORT);
}

void loop() {
  btn::poll();

  // Local mode is selected after the chip is already running, not at reset.
  // Shut the radio down on the way in so "radio off" is literally true.
  if (!gLocalOnly && millis() < kModeWindowMs && btn::down(btn::BOOT)) {
    gLocalOnly = true;
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    Serial.println("[pilink] BOOT pressed: local mode, radio off");
    Serial.println("[pilink] press buttons; lines below are what the Pi would see");
  }

  if (gLocalOnly) {
    // Deliberately NOT Status::Booting. That is chase(120,120,120) and kBright
    // is 40/255, so every channel lands near 19/255, where a WS2812B's colour
    // balance is poor enough that the "white" chase reads as blue -- which is
    // exactly what the Wi-Fi chase looks like. Steady cyan cannot be confused
    // with a blue chase, and the green sweep gives per-press feedback while
    // you are checking the button map.
    leds::setStatus(btn::anyDown() ? leds::Status::Flying
                                   : leds::Status::BeaconSet);
    serviceLocalOnly();
    leds::poll();
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    leds::setStatus(leds::Status::WifiConnecting);
    leds::poll();
    if (millis() - gLastLogMs >= 2000) {
      gLastLogMs = millis();
      // The status code separates the two failures that look identical from
      // the outside: 1 means the SSID was never seen on the air, 4 usually
      // means it was seen and the password was rejected.
      Serial.printf("[pilink] waiting for \"%s\"  status=%d (%s)\n",
                    WIFI_SSID, (int)WiFi.status(), wifiStatusName(WiFi.status()));
    }
    // One scan, once, after giving the normal path a fair chance. Says whether
    // the network is even on the air and what the badge's radio can actually
    // see, which is the question you otherwise end up guessing at.
    if (!gScanned && millis() > 12000) {
      gScanned = true;
      Serial.println("[pilink] scanning for visible 2.4 GHz networks...");
      // A scan started while a connect attempt is still running comes back
      // empty on the ESP32, which reads as "the radio is dead" when it only
      // means "the radio was busy". Stop trying first, then scan.
      WiFi.disconnect(false, false);
      delay(300);
      const int n = WiFi.scanNetworks(false /*async*/, true /*show hidden*/);
      if (n <= 0) {
        Serial.println("[pilink]   nothing visible at all");
      } else {
        for (int i = 0; i < n; i++) {
          const bool match = WiFi.SSID(i) == String(WIFI_SSID);
          Serial.printf("[pilink]   %-34s rssi %4d  %s%s\n",
                        WiFi.SSID(i).c_str(), WiFi.RSSI(i),
                        WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open" : "encrypted",
                        match ? "   <-- this is the one we want" : "");
        }
      }
      WiFi.scanDelete();
      WiFi.begin(WIFI_SSID, WIFI_PASS);   // scanning drops the attempt; restart it
    }
    return;
  }

  pollAck();

  const uint32_t now    = millis();
  const bool     active = btn::anyDown();

  // Fast while something is held so the Pi sees a press promptly; slow when
  // idle so there is still a liveness signal without pointless traffic.
  const uint32_t period = 1000 / (active ? BADGE_SEND_HZ : BADGE_IDLE_HZ);
  if (now - gLastSendMs >= period) {
    gLastSendMs = now;
    sendState();
  }

  const bool linked = gEverAcked && (now - gLastAckMs < kAckTimeoutMs);
  if (linked) {
    // Steady dim green idle, green sweep while a button is held, so the badge
    // shows you it registered the press without looking at the Pi.
    leds::setStatus(active ? leds::Status::Flying : leds::Status::Disarmed);
  } else {
    // Never heard from vs stopped hearing are different problems, so they get
    // different colours: blue breathing means the Pi has never answered.
    leds::setStatus(gEverAcked ? leds::Status::LinkLost
                               : leds::Status::LinkWaiting);
  }

  if (now - gLastLogMs >= 2000) {
    gLastLogMs = now;
    Serial.printf("[pilink] ip=%s peer=%s linked=%d seq=%lu\n",
                  WiFi.localIP().toString().c_str(), gPeer.toString().c_str(),
                  (int)linked, (unsigned long)gSeq);
  }

  leds::poll();
}
