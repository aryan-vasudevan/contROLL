#include "dronelink.h"
#include "config.h"
#include <WiFi.h>
#include <WiFiUdp.h>

namespace dronelink {
namespace {

WiFiUDP     gUdp;
mav::Parser gParser;
mav::Encoder gEnc(GCS_SYSID, GCS_COMPID);

DroneState  gState;
IPAddress   gPeer;
bool        gPeerKnown = false;
bool        gUdpStarted = false;
char        gStatusText[64] = {0};

uint32_t    gLastWifiAttemptMs = 0;
uint8_t     gTxBuf[mav::MAX_FRAME];

void send(size_t n) {
  if (n == 0 || !gPeerKnown || WiFi.status() != WL_CONNECTED) return;
  gUdp.beginPacket(gPeer, DRONE_UDP_PORT);
  gUdp.write(gTxBuf, n);
  gUdp.endPacket();
}

void handleFrame(const mav::Frame &f) {
  const uint32_t now = millis();
  gState.lastAnyMsgMs = now;

  switch (f.msgid) {
    case mav::MSG_HEARTBEAT: {
      mav::Heartbeat hb;
      if (!mav::readHeartbeat(f, hb)) break;

      // Ignore heartbeats from other ground stations sharing the network, and
      // from the bridge itself if it announces one. Only autopilots count.
      if (hb.type == 6 /* MAV_TYPE_GCS */) break;

      gState.heartbeatSeen   = true;
      gState.lastHeartbeatMs = now;
      gState.sysid           = f.sysid;
      gState.compid          = f.compid;
      gState.armed           = (hb.baseMode & mav::MAV_MODE_FLAG_SAFETY_ARMED) != 0;
      gState.customMode      = hb.customMode;
      gState.systemStatus    = hb.systemStatus;
      break;
    }
    case mav::MSG_GPS_RAW_INT: {
      mav::GpsRawInt g;
      if (mav::readGpsRawInt(f, g)) {
        gState.gpsFixType  = g.fixType;
        gState.satellites  = g.satellites;
      }
      break;
    }
    case mav::MSG_GLOBAL_POSITION_INT: {
      mav::GlobalPosInt p;
      if (mav::readGlobalPosInt(f, p)) {
        gState.latE7       = p.lat;
        gState.lonE7       = p.lon;
        gState.relAltM     = p.relativeAlt / 1000.0f;
        gState.headingCdeg = p.hdg;
        // A lat/lon of exactly zero means the estimator has nothing yet.
        gState.posValid    = (p.lat != 0 || p.lon != 0);
      }
      break;
    }
    case mav::MSG_SYS_STATUS: {
      mav::SysStatus s;
      if (mav::readSysStatus(f, s)) {
        gState.batteryMv  = s.voltageBattery;
        gState.batteryPct = s.batteryRemaining;
      }
      break;
    }
    case mav::MSG_STATUSTEXT: {
      uint8_t sev = 0;
      if (mav::readStatusText(f, gStatusText, sizeof(gStatusText), sev)) {
        Serial.printf("[drone] (%u) %s\n", sev, gStatusText);
      }
      break;
    }
    case mav::MSG_COMMAND_ACK: {
      mav::CommandAck a;
      if (mav::readCommandAck(f, a)) {
        Serial.printf("[drone] ack cmd=%u result=%u\n", a.command, a.result);
      }
      break;
    }
    default:
      break;
  }
}

}  // namespace

void begin() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // latency matters more than the milliamps here
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  gLastWifiAttemptMs = millis();

  gPeer = IPAddress();
  gPeerKnown = gPeer.fromString(DRONE_IP);
}

void poll() {
  const uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    gUdpStarted = false;
    // Retry periodically instead of blocking, so the LEDs keep animating.
    if (now - gLastWifiAttemptMs > 5000) {
      gLastWifiAttemptMs = now;
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASS);
    }
    return;
  }

  if (!gUdpStarted) {
    gUdp.begin(LOCAL_UDP_PORT);
    gUdpStarted = true;
    Serial.printf("[link] wifi up, ip=%s, listening on %u\n",
                  WiFi.localIP().toString().c_str(), (unsigned)LOCAL_UDP_PORT);
  }

  // Drain everything queued; a burst of telemetry should not take several
  // loops to clear or the control loop falls behind.
  while (gUdp.parsePacket() > 0) {
#if AUTO_DISCOVER_PEER
    const IPAddress from = gUdp.remoteIP();
    if (!gPeerKnown || from != gPeer) {
      gPeer = from;
      gPeerKnown = true;
      Serial.printf("[link] peer is %s\n", gPeer.toString().c_str());
    }
#endif
    uint8_t buf[1024];
    int n = gUdp.read(buf, sizeof(buf));
    for (int i = 0; i < n; i++) {
      mav::Frame f;
      if (gParser.parseByte(buf[i], f)) handleFrame(f);
    }
  }
}

bool wifiConnected() { return WiFi.status() == WL_CONNECTED; }

bool linkAlive() {
  return gState.heartbeatSeen &&
         (millis() - gState.lastHeartbeatMs) < LINK_TIMEOUT_MS;
}

const DroneState &state() { return gState; }
IPAddress peer() { return gPeer; }
const char *lastStatusText() { return gStatusText; }

void sendHeartbeat() {
  send(gEnc.heartbeat(gTxBuf, sizeof(gTxBuf)));
}

void sendManualControl(int16_t pitch, int16_t roll, int16_t throttle, int16_t yaw,
                       uint16_t buttons) {
  // MANUAL_CONTROL names its axes after the aircraft body frame:
  //   x = pitch, forward positive
  //   y = roll, right positive
  //   z = throttle
  //   r = yaw, clockwise positive
  send(gEnc.manualControl(gState.sysid, pitch, roll, throttle, yaw, buttons,
                          gTxBuf, sizeof(gTxBuf)));
}

void sendSetMode(uint32_t customMode) {
  send(gEnc.setMode(gState.sysid, mav::MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    customMode, gTxBuf, sizeof(gTxBuf)));
}

void sendArm(bool arm, bool force) {
  // param2 = 21196 is the documented magic number that forces the request past
  // the autopilot's own arming checks. Only ever passed on a disarm here.
  send(gEnc.commandLong(gState.sysid, gState.compid,
                        mav::CMD_COMPONENT_ARM_DISARM, 0,
                        arm ? 1.0f : 0.0f, force ? 21196.0f : 0.0f,
                        0, 0, 0, 0, 0, gTxBuf, sizeof(gTxBuf)));
}

void sendRtl() {
  send(gEnc.commandLong(gState.sysid, gState.compid,
                        mav::CMD_NAV_RETURN_TO_LAUNCH, 0,
                        0, 0, 0, 0, 0, 0, 0, gTxBuf, sizeof(gTxBuf)));
}

void sendLand() {
  sendSetMode(COPTER_MODE_LAND);
}

void sendTakeoff(float relAltM) {
  // MAV_CMD_NAV_TAKEOFF carries the target altitude in param7. ArduPilot only
  // accepts it while the aircraft is armed and in GUIDED.
  send(gEnc.commandLong(gState.sysid, gState.compid, mav::CMD_NAV_TAKEOFF, 0,
                        0, 0, 0, 0, 0, 0, relAltM, gTxBuf, sizeof(gTxBuf)));
}

void sendGuidedPosition(int32_t latE7, int32_t lonE7, float relAltM) {
  send(gEnc.setPositionTargetGlobalInt(gState.sysid, gState.compid, millis(),
                                       latE7, lonE7, relAltM,
                                       gTxBuf, sizeof(gTxBuf)));
}

}  // namespace dronelink
