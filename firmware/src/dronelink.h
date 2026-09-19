#pragma once
#include <Arduino.h>
#include "mavlink_min.h"

// ---------------------------------------------------------------------------
// Wi-Fi transport, MAVLink plumbing, and the picture of the aircraft that the
// rest of the firmware reads.
//
// The badge joins the drone's telemetry Wi-Fi as a station and speaks MAVLink
// over UDP, which is what an ESP8266 or ESP32 serial bridge on an F450's TELEM
// port presents. Nothing here assumes a particular bridge: if AUTO_DISCOVER_PEER
// is on, whatever address MAVLink actually arrives from becomes the peer.
// ---------------------------------------------------------------------------

// Named dronelink rather than link: Arduino.h pulls in unistd.h, which declares
// POSIX link(2) at global scope, and a namespace called link collides with it.
namespace dronelink {

struct DroneState {
  bool     heartbeatSeen   = false;
  uint32_t lastHeartbeatMs = 0;
  uint32_t lastAnyMsgMs    = 0;

  uint8_t  sysid   = 1;
  uint8_t  compid  = 1;

  bool     armed       = false;
  uint32_t customMode  = 0;
  uint8_t  systemStatus= 0;

  uint8_t  gpsFixType   = 0;
  uint8_t  satellites   = 0;
  bool     posValid     = false;
  int32_t  latE7        = 0;
  int32_t  lonE7        = 0;
  float    relAltM      = 0;
  uint16_t headingCdeg  = 0;

  uint16_t batteryMv    = 0;
  int8_t   batteryPct   = -1;
};

void begin();

// Pumps Wi-Fi and reads every datagram waiting. Call every loop.
void poll();

bool wifiConnected();
bool linkAlive();                 // MAVLink heard recently enough
const DroneState &state();
IPAddress peer();

// --- outbound --------------------------------------------------------------
void sendHeartbeat();
void sendManualControl(int16_t pitch, int16_t roll, int16_t throttle, int16_t yaw,
                       uint16_t buttons);
void sendSetMode(uint32_t customMode);
void sendArm(bool arm, bool force);
void sendRtl();
void sendLand();
void sendTakeoff(float relAltM);
void sendGuidedPosition(int32_t latE7, int32_t lonE7, float relAltM);

// Last STATUSTEXT the aircraft sent, for the serial console.
const char *lastStatusText();

}  // namespace dronelink
