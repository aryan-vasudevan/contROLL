#include "mavlink_min.h"
#include <stdio.h>

static void dump(const char *name, const uint8_t *b, size_t n) {
  printf("%s %zu ", name, n);
  for (size_t i = 0; i < n; i++) printf("%02X", b[i]);
  printf("\n");
}

int main() {
  uint8_t buf[300];
  mav::Encoder enc(255, 190);

  size_t n = enc.heartbeat(buf, sizeof(buf));                     dump("HEARTBEAT", buf, n);
  n = enc.manualControl(1, 300, -200, 500, 350, 0x0042, buf, sizeof(buf));
                                                                   dump("MANUAL_CONTROL", buf, n);
  n = enc.setMode(1, 1, 5, buf, sizeof(buf));                      dump("SET_MODE", buf, n);
  n = enc.commandLong(1, 1, 400, 0, 1, 0, 0, 0, 0, 0, 0, buf, sizeof(buf));
                                                                   dump("ARM", buf, n);
  n = enc.commandLong(1, 1, 22, 0, 0, 0, 0, 0, 0, 0, 3.0f, buf, sizeof(buf));
                                                                   dump("TAKEOFF", buf, n);
  n = enc.setPositionTargetGlobalInt(1, 1, 123456, 434730000, -803010000, 5.0f,
                                     buf, sizeof(buf));            dump("SETPOS", buf, n);

  // Round-trip: feed a synthesised HEARTBEAT and GLOBAL_POSITION_INT back in.
  mav::Parser p;
  mav::Frame f;
  mav::Encoder drone(1, 1);

  uint8_t hb[64];
  // Build an autopilot heartbeat by hand: custom_mode=5, type=2 (quadrotor),
  // autopilot=3 (ArduPilot), base_mode=0x81 (armed|custom), status=4.
  uint8_t pay[9] = {5,0,0,0, 2, 3, 0x81, 4, 3};
  size_t hn = drone.frame(mav::MSG_HEARTBEAT, pay, sizeof(pay), hb, sizeof(hb));
  int got = 0;
  for (size_t i = 0; i < hn; i++) if (p.parseByte(hb[i], f)) got++;
  mav::Heartbeat h{};
  bool ok = (got == 1) && mav::readHeartbeat(f, h);
  printf("ROUNDTRIP_HB ok=%d custom=%u type=%u ap=%u base=0x%02X armed=%d\n",
         ok, h.customMode, h.type, h.autopilot, h.baseMode,
         (h.baseMode & mav::MAV_MODE_FLAG_SAFETY_ARMED) ? 1 : 0);

  // GLOBAL_POSITION_INT with a known lat/lon/relalt.
  uint8_t gp[28] = {0};
  uint32_t t = 1000; int32_t lat = 434730000, lon = -803010000, alt = 100000, rel = 12345;
  memcpy(gp+0,&t,4); memcpy(gp+4,&lat,4); memcpy(gp+8,&lon,4);
  memcpy(gp+12,&alt,4); memcpy(gp+16,&rel,4);
  int16_t vx=1,vy=2,vz=3; uint16_t hdg=18000;
  memcpy(gp+20,&vx,2); memcpy(gp+22,&vy,2); memcpy(gp+24,&vz,2); memcpy(gp+26,&hdg,2);
  uint8_t gb[64];
  size_t gn = drone.frame(mav::MSG_GLOBAL_POSITION_INT, gp, sizeof(gp), gb, sizeof(gb));
  got = 0;
  for (size_t i = 0; i < gn; i++) if (p.parseByte(gb[i], f)) got++;
  mav::GlobalPosInt g{};
  ok = (got == 1) && mav::readGlobalPosInt(f, g);
  printf("ROUNDTRIP_GP ok=%d lat=%d lon=%d rel=%d hdg=%u\n",
         ok, g.lat, g.lon, g.relativeAlt, g.hdg);

  // Corrupt one payload byte and confirm the checksum rejects the frame.
  gb[12] ^= 0xFF;
  got = 0;
  for (size_t i = 0; i < gn; i++) if (p.parseByte(gb[i], f)) got++;
  printf("CORRUPT_REJECTED %d\n", got == 0);
  return 0;
}
