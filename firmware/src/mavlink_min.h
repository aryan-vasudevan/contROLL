#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// A small MAVLink v2 encoder and parser covering only the messages this
// controller needs. The official headers are enormous and awkward to vendor
// into a PlatformIO project, and we use nine message types, so they are built
// by hand here.
//
// Two details make or break wire compatibility, and both are handled below:
//
//  1. Payload fields are NOT laid out in the order the XML declares them. They
//     are sorted by field size, largest first, with declaration order breaking
//     ties. Every pack/unpack routine here follows that sorted order.
//  2. The checksum is CRC-16/MCRF4XX over the header from the length byte
//     onward plus the payload, then one extra message-specific CRC_EXTRA byte
//     mixed in at the end.
//
// MAVLink v2 also trims trailing zero bytes off the payload before the CRC is
// taken, which the encoder does.
// ---------------------------------------------------------------------------

namespace mav {

constexpr uint8_t STX_V2 = 0xFD;
constexpr uint8_t STX_V1 = 0xFE;
constexpr size_t  MAX_FRAME = 280;

// --- message ids -----------------------------------------------------------
enum : uint32_t {
  MSG_HEARTBEAT                     = 0,
  MSG_SYS_STATUS                    = 1,
  MSG_SET_MODE                      = 11,
  MSG_GPS_RAW_INT                   = 24,
  MSG_ATTITUDE                      = 30,
  MSG_GLOBAL_POSITION_INT           = 33,
  MSG_MANUAL_CONTROL                = 69,
  MSG_VFR_HUD                       = 74,
  MSG_COMMAND_LONG                  = 76,
  MSG_COMMAND_ACK                   = 77,
  MSG_SET_POSITION_TARGET_GLOBAL_INT= 86,
  MSG_STATUSTEXT                    = 253,
};

// --- commands and flags we use --------------------------------------------
enum : uint16_t {
  CMD_NAV_RETURN_TO_LAUNCH = 20,
  CMD_NAV_TAKEOFF          = 22,
  CMD_DO_SET_MODE          = 176,
  CMD_COMPONENT_ARM_DISARM = 400,
};

constexpr uint8_t  MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 0x01;
constexpr uint8_t  MAV_MODE_FLAG_SAFETY_ARMED        = 0x80;
constexpr uint8_t  FRAME_GLOBAL_RELATIVE_ALT_INT     = 6;

// Position-only target: ignore the velocity, acceleration, yaw and yaw-rate
// fields and act on lat/lon/alt alone.
constexpr uint16_t POSITION_TARGET_TYPE_MASK = 0x0DF8;

// --- checksum --------------------------------------------------------------
void crcAccumulate(uint8_t data, uint16_t *crc);
uint16_t crcCalculate(const uint8_t *buf, uint16_t len, uint16_t seed = 0xFFFF);

// CRC_EXTRA byte for each message id above. Returns 0 for anything unknown.
uint8_t crcExtra(uint32_t msgid);

// --- decoded frame ---------------------------------------------------------
struct Frame {
  uint8_t  sysid   = 0;
  uint8_t  compid  = 0;
  uint32_t msgid   = 0;
  uint8_t  payload[255] = {0};   // zero padded back out to full length
  uint8_t  len     = 0;
};

// Incremental byte-at-a-time parser. Accepts v1 and v2 frames, since some
// bridges still emit v1 heartbeats, and rejects anything with a bad checksum.
class Parser {
 public:
  // Returns true once for each complete, checksum-valid frame.
  bool parseByte(uint8_t c, Frame &out);

 private:
  enum class St : uint8_t { Idle, Len, IncompatFlags, CompatFlags, Seq, SysId,
                            CompId, MsgId1, MsgId2, MsgId3, Payload, Crc1, Crc2 };
  St       st_ = St::Idle;
  bool     v2_ = true;
  uint8_t  len_ = 0, idx_ = 0, incompat_ = 0;
  uint32_t msgid_ = 0;
  uint8_t  sysid_ = 0, compid_ = 0;
  uint8_t  payload_[255] = {0};
  uint16_t crc_ = 0, rxCrc_ = 0;
  void reset() { st_ = St::Idle; idx_ = 0; msgid_ = 0; }
};

// --- encoder ---------------------------------------------------------------
class Encoder {
 public:
  Encoder(uint8_t sysid, uint8_t compid) : sysid_(sysid), compid_(compid) {}

  // Wraps a already-laid-out payload in a v2 frame. Returns frame length.
  size_t frame(uint32_t msgid, const uint8_t *payload, uint8_t payloadLen,
               uint8_t *out, size_t outCap);

  size_t heartbeat(uint8_t *out, size_t cap);

  size_t manualControl(uint8_t targetSys, int16_t x, int16_t y, int16_t z,
                       int16_t r, uint16_t buttons, uint8_t *out, size_t cap);

  size_t setMode(uint8_t targetSys, uint8_t baseMode, uint32_t customMode,
                 uint8_t *out, size_t cap);

  size_t commandLong(uint8_t targetSys, uint8_t targetComp, uint16_t command,
                     uint8_t confirmation, float p1, float p2, float p3,
                     float p4, float p5, float p6, float p7,
                     uint8_t *out, size_t cap);

  size_t setPositionTargetGlobalInt(uint8_t targetSys, uint8_t targetComp,
                                    uint32_t timeBootMs, int32_t latE7,
                                    int32_t lonE7, float altM,
                                    uint8_t *out, size_t cap);

 private:
  uint8_t sysid_, compid_, seq_ = 0;
};

// --- payload readers -------------------------------------------------------
// Each returns false if the payload is too short for the field it wants.
struct Heartbeat      { uint32_t customMode; uint8_t type, autopilot, baseMode, systemStatus; };
struct SysStatus      { uint16_t voltageBattery; int16_t currentBattery; int8_t batteryRemaining; };
struct GpsRawInt      { uint8_t fixType, satellites; };
struct GlobalPosInt   { int32_t lat, lon, alt, relativeAlt; int16_t vx, vy, vz; uint16_t hdg; };
struct VfrHud         { float airspeed, groundspeed, alt, climb; int16_t heading; uint16_t throttle; };
struct CommandAck     { uint16_t command; uint8_t result; };

bool readHeartbeat(const Frame &f, Heartbeat &o);
bool readSysStatus(const Frame &f, SysStatus &o);
bool readGpsRawInt(const Frame &f, GpsRawInt &o);
bool readGlobalPosInt(const Frame &f, GlobalPosInt &o);
bool readVfrHud(const Frame &f, VfrHud &o);
bool readCommandAck(const Frame &f, CommandAck &o);
bool readStatusText(const Frame &f, char *out, size_t cap, uint8_t &severity);

}  // namespace mav
