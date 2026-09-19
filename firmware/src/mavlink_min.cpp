#include "mavlink_min.h"
#include <string.h>

namespace mav {

// --- checksum: CRC-16/MCRF4XX ---------------------------------------------
void crcAccumulate(uint8_t data, uint16_t *crc) {
  uint8_t tmp = data ^ (uint8_t)(*crc & 0xFF);
  tmp ^= (uint8_t)(tmp << 4);
  *crc = (uint16_t)((*crc >> 8) ^ ((uint16_t)tmp << 8) ^ ((uint16_t)tmp << 3) ^
                    ((uint16_t)tmp >> 4));
}

uint16_t crcCalculate(const uint8_t *buf, uint16_t len, uint16_t seed) {
  uint16_t crc = seed;
  for (uint16_t i = 0; i < len; i++) crcAccumulate(buf[i], &crc);
  return crc;
}

uint8_t crcExtra(uint32_t msgid) {
  switch (msgid) {
    case MSG_HEARTBEAT:                      return 50;
    case MSG_SYS_STATUS:                     return 124;
    case MSG_SET_MODE:                       return 89;
    case MSG_GPS_RAW_INT:                    return 24;
    case MSG_ATTITUDE:                       return 39;
    case MSG_GLOBAL_POSITION_INT:            return 104;
    case MSG_MANUAL_CONTROL:                 return 243;
    case MSG_VFR_HUD:                        return 20;
    case MSG_COMMAND_LONG:                   return 152;
    case MSG_COMMAND_ACK:                    return 143;
    case MSG_SET_POSITION_TARGET_GLOBAL_INT: return 5;
    case MSG_STATUSTEXT:                     return 83;
    default:                                 return 0;
  }
}

// --- little-endian helpers -------------------------------------------------
namespace {

inline void put8 (uint8_t *b, size_t &o, uint8_t  v) { b[o++] = v; }
inline void put16(uint8_t *b, size_t &o, uint16_t v) { memcpy(b + o, &v, 2); o += 2; }
inline void put32(uint8_t *b, size_t &o, uint32_t v) { memcpy(b + o, &v, 4); o += 4; }
inline void putF (uint8_t *b, size_t &o, float    v) { memcpy(b + o, &v, 4); o += 4; }

inline uint16_t get16(const uint8_t *b, size_t o) { uint16_t v; memcpy(&v, b + o, 2); return v; }
inline int16_t  getI16(const uint8_t *b, size_t o){ int16_t  v; memcpy(&v, b + o, 2); return v; }
inline uint32_t get32(const uint8_t *b, size_t o) { uint32_t v; memcpy(&v, b + o, 4); return v; }
inline int32_t  getI32(const uint8_t *b, size_t o){ int32_t  v; memcpy(&v, b + o, 4); return v; }
inline float    getF (const uint8_t *b, size_t o) { float    v; memcpy(&v, b + o, 4); return v; }

}  // namespace

// --- parser ----------------------------------------------------------------
bool Parser::parseByte(uint8_t c, Frame &out) {
  switch (st_) {
    case St::Idle:
      if (c == STX_V2)      { v2_ = true;  crc_ = 0xFFFF; st_ = St::Len; }
      else if (c == STX_V1) { v2_ = false; crc_ = 0xFFFF; st_ = St::Len; }
      return false;

    case St::Len:
      len_ = c;
      crcAccumulate(c, &crc_);
      st_ = v2_ ? St::IncompatFlags : St::Seq;
      return false;

    case St::IncompatFlags:
      incompat_ = c;
      crcAccumulate(c, &crc_);
      st_ = St::CompatFlags;
      return false;

    case St::CompatFlags:
      crcAccumulate(c, &crc_);
      st_ = St::Seq;
      return false;

    case St::Seq:
      crcAccumulate(c, &crc_);
      st_ = St::SysId;
      return false;

    case St::SysId:
      sysid_ = c;
      crcAccumulate(c, &crc_);
      st_ = St::CompId;
      return false;

    case St::CompId:
      compid_ = c;
      crcAccumulate(c, &crc_);
      st_ = St::MsgId1;
      return false;

    case St::MsgId1:
      msgid_ = c;
      crcAccumulate(c, &crc_);
      if (!v2_) {
        // v1 has a single-byte id and goes straight to the payload.
        idx_ = 0;
        memset(payload_, 0, sizeof(payload_));
        st_ = len_ ? St::Payload : St::Crc1;
      } else {
        st_ = St::MsgId2;
      }
      return false;

    case St::MsgId2:
      msgid_ |= (uint32_t)c << 8;
      crcAccumulate(c, &crc_);
      st_ = St::MsgId3;
      return false;

    case St::MsgId3:
      msgid_ |= (uint32_t)c << 16;
      crcAccumulate(c, &crc_);
      idx_ = 0;
      memset(payload_, 0, sizeof(payload_));
      st_ = len_ ? St::Payload : St::Crc1;
      return false;

    case St::Payload:
      payload_[idx_++] = c;
      crcAccumulate(c, &crc_);
      if (idx_ >= len_) st_ = St::Crc1;
      return false;

    case St::Crc1:
      // Mix in CRC_EXTRA before comparing, as the spec requires.
      crcAccumulate(crcExtra(msgid_), &crc_);
      rxCrc_ = c;
      st_ = St::Crc2;
      return false;

    case St::Crc2: {
      rxCrc_ |= (uint16_t)c << 8;
      bool ok = (rxCrc_ == crc_);
      // Signed v2 frames carry 13 trailing signature bytes. We do not verify
      // signatures; the parser simply resyncs on the next start byte.
      if (ok) {
        out.sysid  = sysid_;
        out.compid = compid_;
        out.msgid  = msgid_;
        out.len    = len_;
        // Copy the whole buffer so truncated trailing fields read back as zero,
        // which is exactly what v2 truncation means.
        memcpy(out.payload, payload_, sizeof(out.payload));
      }
      reset();
      return ok;
    }
  }
  reset();
  return false;
}

// --- encoder ---------------------------------------------------------------
size_t Encoder::frame(uint32_t msgid, const uint8_t *payload, uint8_t payloadLen,
                      uint8_t *out, size_t outCap) {
  // v2 truncation: drop trailing zero bytes, but always keep at least one.
  uint8_t len = payloadLen;
  while (len > 1 && payload[len - 1] == 0) len--;
  if (payloadLen == 0) len = 0;

  const size_t total = 10 + (size_t)len + 2;
  if (outCap < total) return 0;

  size_t o = 0;
  out[o++] = STX_V2;
  out[o++] = len;
  out[o++] = 0;            // incompat_flags, 0 = unsigned
  out[o++] = 0;            // compat_flags
  out[o++] = seq_++;
  out[o++] = sysid_;
  out[o++] = compid_;
  out[o++] = (uint8_t)(msgid & 0xFF);
  out[o++] = (uint8_t)((msgid >> 8) & 0xFF);
  out[o++] = (uint8_t)((msgid >> 16) & 0xFF);
  memcpy(out + o, payload, len);
  o += len;

  uint16_t crc = crcCalculate(out + 1, (uint16_t)(9 + len));
  crcAccumulate(crcExtra(msgid), &crc);
  out[o++] = (uint8_t)(crc & 0xFF);
  out[o++] = (uint8_t)(crc >> 8);
  return o;
}

size_t Encoder::heartbeat(uint8_t *out, size_t cap) {
  // Field order: custom_mode(u32), type, autopilot, base_mode, system_status,
  // mavlink_version.
  uint8_t p[9];
  size_t o = 0;
  put32(p, o, 0);
  put8(p, o, 6);    // MAV_TYPE_GCS
  put8(p, o, 8);    // MAV_AUTOPILOT_INVALID, correct for a ground station
  put8(p, o, 0);    // base_mode
  put8(p, o, 4);    // MAV_STATE_ACTIVE
  put8(p, o, 3);    // mavlink_version
  return frame(MSG_HEARTBEAT, p, sizeof(p), out, cap);
}

size_t Encoder::manualControl(uint8_t targetSys, int16_t x, int16_t y, int16_t z,
                              int16_t r, uint16_t buttons, uint8_t *out, size_t cap) {
  // Field order: x, y, z, r (all i16), buttons(u16), target(u8).
  uint8_t p[11];
  size_t o = 0;
  put16(p, o, (uint16_t)x);
  put16(p, o, (uint16_t)y);
  put16(p, o, (uint16_t)z);
  put16(p, o, (uint16_t)r);
  put16(p, o, buttons);
  put8(p, o, targetSys);
  return frame(MSG_MANUAL_CONTROL, p, sizeof(p), out, cap);
}

size_t Encoder::setMode(uint8_t targetSys, uint8_t baseMode, uint32_t customMode,
                        uint8_t *out, size_t cap) {
  // Field order: custom_mode(u32), target_system, base_mode.
  uint8_t p[6];
  size_t o = 0;
  put32(p, o, customMode);
  put8(p, o, targetSys);
  put8(p, o, baseMode);
  return frame(MSG_SET_MODE, p, sizeof(p), out, cap);
}

size_t Encoder::commandLong(uint8_t targetSys, uint8_t targetComp, uint16_t command,
                            uint8_t confirmation, float p1, float p2, float p3,
                            float p4, float p5, float p6, float p7,
                            uint8_t *out, size_t cap) {
  // Field order: param1..param7 (f32), command(u16), target_system,
  // target_component, confirmation.
  uint8_t p[33];
  size_t o = 0;
  putF(p, o, p1); putF(p, o, p2); putF(p, o, p3); putF(p, o, p4);
  putF(p, o, p5); putF(p, o, p6); putF(p, o, p7);
  put16(p, o, command);
  put8(p, o, targetSys);
  put8(p, o, targetComp);
  put8(p, o, confirmation);
  return frame(MSG_COMMAND_LONG, p, sizeof(p), out, cap);
}

size_t Encoder::setPositionTargetGlobalInt(uint8_t targetSys, uint8_t targetComp,
                                           uint32_t timeBootMs, int32_t latE7,
                                           int32_t lonE7, float altM,
                                           uint8_t *out, size_t cap) {
  // Field order: time_boot_ms(u32), lat_int(i32), lon_int(i32), alt(f32),
  // vx, vy, vz, afx, afy, afz, yaw, yaw_rate (f32), type_mask(u16),
  // target_system, target_component, coordinate_frame.
  uint8_t p[53];
  memset(p, 0, sizeof(p));
  size_t o = 0;
  put32(p, o, timeBootMs);
  put32(p, o, (uint32_t)latE7);
  put32(p, o, (uint32_t)lonE7);
  putF(p, o, altM);
  for (int i = 0; i < 8; i++) putF(p, o, 0.0f);  // vel, accel, yaw, yaw_rate
  put16(p, o, POSITION_TARGET_TYPE_MASK);
  put8(p, o, targetSys);
  put8(p, o, targetComp);
  put8(p, o, FRAME_GLOBAL_RELATIVE_ALT_INT);
  return frame(MSG_SET_POSITION_TARGET_GLOBAL_INT, p, sizeof(p), out, cap);
}

// --- payload readers -------------------------------------------------------
bool readHeartbeat(const Frame &f, Heartbeat &o) {
  if (f.msgid != MSG_HEARTBEAT) return false;
  o.customMode   = get32(f.payload, 0);
  o.type         = f.payload[4];
  o.autopilot    = f.payload[5];
  o.baseMode     = f.payload[6];
  o.systemStatus = f.payload[7];
  return true;
}

bool readSysStatus(const Frame &f, SysStatus &o) {
  if (f.msgid != MSG_SYS_STATUS) return false;
  // 3 x u32 sensor bitmasks, then load(u16), voltage_battery(u16),
  // current_battery(i16), drop_rate_comm, errors_comm, errors_count1..4,
  // then battery_remaining(i8).
  o.voltageBattery   = get16(f.payload, 14);
  o.currentBattery   = getI16(f.payload, 16);
  o.batteryRemaining = (int8_t)f.payload[30];
  return true;
}

bool readGpsRawInt(const Frame &f, GpsRawInt &o) {
  if (f.msgid != MSG_GPS_RAW_INT) return false;
  // time_usec(u64), lat, lon, alt (i32), eph, epv, vel, cog (u16),
  // fix_type, satellites_visible.
  o.fixType    = f.payload[28];
  o.satellites = f.payload[29];
  return true;
}

bool readGlobalPosInt(const Frame &f, GlobalPosInt &o) {
  if (f.msgid != MSG_GLOBAL_POSITION_INT) return false;
  o.lat         = getI32(f.payload, 4);
  o.lon         = getI32(f.payload, 8);
  o.alt         = getI32(f.payload, 12);
  o.relativeAlt = getI32(f.payload, 16);
  o.vx          = getI16(f.payload, 20);
  o.vy          = getI16(f.payload, 22);
  o.vz          = getI16(f.payload, 24);
  o.hdg         = get16(f.payload, 26);
  return true;
}

bool readVfrHud(const Frame &f, VfrHud &o) {
  if (f.msgid != MSG_VFR_HUD) return false;
  o.airspeed    = getF(f.payload, 0);
  o.groundspeed = getF(f.payload, 4);
  o.alt         = getF(f.payload, 8);
  o.climb       = getF(f.payload, 12);
  o.heading     = getI16(f.payload, 16);
  o.throttle    = get16(f.payload, 18);
  return true;
}

bool readCommandAck(const Frame &f, CommandAck &o) {
  if (f.msgid != MSG_COMMAND_ACK) return false;
  o.command = get16(f.payload, 0);
  o.result  = f.payload[2];
  return true;
}

bool readStatusText(const Frame &f, char *out, size_t cap, uint8_t &severity) {
  if (f.msgid != MSG_STATUSTEXT || cap == 0) return false;
  severity = f.payload[0];
  size_t n = cap - 1 < 50 ? cap - 1 : 50;
  memcpy(out, f.payload + 1, n);
  out[n] = '\0';
  return true;
}

}  // namespace mav
