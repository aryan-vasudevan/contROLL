#include "nfc.h"

#include <Arduino.h>
#include <Wire.h>

namespace nfc {
namespace {

// --- MFRC522 registers -----------------------------------------------------
// In I2C mode the register index goes out as a plain byte. This is worth
// stating because the SPI path shifts it left by one and ORs in a direction
// bit, and every MFRC522 example online is SPI, so the shifted form is what
// you will see if you go looking.
constexpr uint8_t REG_COMMAND      = 0x01;
constexpr uint8_t REG_COM_IRQ      = 0x04;
constexpr uint8_t REG_DIV_IRQ      = 0x05;
constexpr uint8_t REG_ERROR        = 0x06;
constexpr uint8_t REG_FIFO_DATA    = 0x09;
constexpr uint8_t REG_FIFO_LEVEL   = 0x0A;
constexpr uint8_t REG_CONTROL      = 0x0C;
constexpr uint8_t REG_BIT_FRAMING  = 0x0D;
constexpr uint8_t REG_COLL         = 0x0E;
constexpr uint8_t REG_MODE         = 0x11;
constexpr uint8_t REG_TX_CONTROL   = 0x14;
constexpr uint8_t REG_TX_ASK       = 0x15;
constexpr uint8_t REG_CRC_RESULT_H = 0x21;
constexpr uint8_t REG_CRC_RESULT_L = 0x22;
constexpr uint8_t REG_T_MODE       = 0x2A;
constexpr uint8_t REG_T_PRESCALER  = 0x2B;
constexpr uint8_t REG_T_RELOAD_H   = 0x2C;
constexpr uint8_t REG_T_RELOAD_L   = 0x2D;
constexpr uint8_t REG_VERSION      = 0x37;

// --- MFRC522 commands ------------------------------------------------------
constexpr uint8_t CMD_IDLE       = 0x00;
constexpr uint8_t CMD_CALC_CRC   = 0x03;
constexpr uint8_t CMD_TRANSCEIVE = 0x0C;
constexpr uint8_t CMD_SOFT_RESET = 0x0F;

// --- PICC (the tag) commands ----------------------------------------------
constexpr uint8_t PICC_REQA   = 0x26;
constexpr uint8_t PICC_CT     = 0x88;   // cascade tag: "the UID continues"
constexpr uint8_t PICC_SEL_1  = 0x93;
constexpr uint8_t PICC_SEL_2  = 0x95;
constexpr uint8_t PICC_SEL_3  = 0x97;

uint8_t gAddr    = 0;
uint8_t gVersion = 0;
bool    gPresent = false;

// --- raw register access ---------------------------------------------------

void writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(gAddr);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

void writeReg(uint8_t reg, const uint8_t *data, uint8_t len) {
  Wire.beginTransmission(gAddr);
  Wire.write(reg);
  for (uint8_t i = 0; i < len; i++) Wire.write(data[i]);
  Wire.endTransmission();
}

uint8_t readReg(uint8_t reg) {
  Wire.beginTransmission(gAddr);
  Wire.write(reg);
  // Repeated start rather than a stop: a stop here lets the accelerometer get
  // a word in between the address and the read, and the reader would hand back
  // whichever register it was last pointed at.
  if (Wire.endTransmission(false) != 0) return 0;
  if (Wire.requestFrom((int)gAddr, 1) != 1) return 0;
  return Wire.read();
}

void setBits(uint8_t reg, uint8_t mask)   { writeReg(reg, readReg(reg) | mask); }
void clearBits(uint8_t reg, uint8_t mask) { writeReg(reg, readReg(reg) & (uint8_t)~mask); }

// --- CRC_A, calculated by the reader rather than in software ---------------

bool calcCrc(const uint8_t *data, uint8_t len, uint8_t *out) {
  writeReg(REG_COMMAND, CMD_IDLE);
  writeReg(REG_DIV_IRQ, 0x04);          // clear CRCIRq
  writeReg(REG_FIFO_LEVEL, 0x80);       // flush
  writeReg(REG_FIFO_DATA, data, len);
  writeReg(REG_COMMAND, CMD_CALC_CRC);

  const uint32_t deadline = millis() + 10;
  while (millis() < deadline) {
    if (readReg(REG_DIV_IRQ) & 0x04) {
      writeReg(REG_COMMAND, CMD_IDLE);
      out[0] = readReg(REG_CRC_RESULT_L);
      out[1] = readReg(REG_CRC_RESULT_H);
      return true;
    }
  }
  writeReg(REG_COMMAND, CMD_IDLE);
  return false;
}

// --- one transmit/receive exchange with whatever is in the field -----------
//
// txLastBits is how many bits of the final transmitted byte are real; 0 means
// all eight. REQA is a seven bit "short frame", which is the only reason this
// parameter has to exist.
//
// Returns false on timeout (nothing in the field, the common case) and on a
// collision or parity error, which are both treated the same way: no tag this
// poll, try again next time.
bool transceive(const uint8_t *tx, uint8_t txLen, uint8_t txLastBits,
                uint8_t *rx, uint8_t *rxLen, uint8_t *rxLastBits) {
  writeReg(REG_COMMAND, CMD_IDLE);
  writeReg(REG_COM_IRQ, 0x7F);          // clear every interrupt flag
  writeReg(REG_FIFO_LEVEL, 0x80);       // flush
  writeReg(REG_FIFO_DATA, tx, txLen);
  writeReg(REG_BIT_FRAMING, txLastBits & 0x07);
  writeReg(REG_COMMAND, CMD_TRANSCEIVE);
  setBits(REG_BIT_FRAMING, 0x80);       // StartSend

  // The reader's own timer decides how long to wait; begin() sets it to about
  // 5 ms. The millis() bound here is only a backstop for a wedged bus.
  const uint32_t deadline = millis() + 40;
  uint8_t irq = 0;
  while (millis() < deadline) {
    irq = readReg(REG_COM_IRQ);
    if (irq & 0x30) break;              // RxIRq or IdleIRq: we have an answer
    if (irq & 0x01) {                   // TimerIRq: nothing answered
      clearBits(REG_BIT_FRAMING, 0x80);
      writeReg(REG_COMMAND, CMD_IDLE);
      return false;
    }
  }
  clearBits(REG_BIT_FRAMING, 0x80);
  if (!(irq & 0x30)) { writeReg(REG_COMMAND, CMD_IDLE); return false; }

  // BufferOvfl | ParityErr | ProtocolErr. Collisions are reported separately
  // and are not fatal here: two tags in the field is a user problem.
  if (readReg(REG_ERROR) & 0x13) { writeReg(REG_COMMAND, CMD_IDLE); return false; }

  const uint8_t avail = readReg(REG_FIFO_LEVEL);
  if (avail == 0 || avail > *rxLen) { writeReg(REG_COMMAND, CMD_IDLE); return false; }
  for (uint8_t i = 0; i < avail; i++) rx[i] = readReg(REG_FIFO_DATA);
  *rxLen = avail;
  const uint8_t ctrl = readReg(REG_CONTROL) & 0x07;
  if (rxLastBits) *rxLastBits = ctrl;
  writeReg(REG_COMMAND, CMD_IDLE);
  return true;
}

// Wakes anything in the field. ATQA comes back as two bytes.
bool requestA() {
  // ValuesAfterColl must be cleared before an anticollision sequence or the
  // reader keeps stale bit positions from the previous exchange.
  clearBits(REG_COLL, 0x80);
  uint8_t cmd = PICC_REQA;
  uint8_t atqa[2];
  uint8_t len = sizeof(atqa);
  return transceive(&cmd, 1, 7, atqa, &len, nullptr) && len == 2;
}

}  // namespace

// --- public ----------------------------------------------------------------

bool begin() {
  gPresent = false;
  gAddr = 0;
  gVersion = 0;

  // Believe the bus, not the schematic. The strapping predicts 0x29 but the
  // symbol those straps were read from is an EasyEDA conversion, so sweep the
  // whole strap range and take whatever identifies itself as an MFRC522.
  for (uint8_t addr = 0x28; addr <= 0x2F; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() != 0) continue;
    gAddr = addr;
    const uint8_t v = readReg(REG_VERSION);
    // 0x91 and 0x92 are the documented silicon revisions. Anything else that
    // answers is some other part and must not be driven as a reader.
    if (v == 0x91 || v == 0x92) { gVersion = v; gPresent = true; break; }
    gAddr = 0;
  }
  if (!gPresent) return false;

  writeReg(REG_COMMAND, CMD_SOFT_RESET);
  delay(50);                            // the datasheet's own settling time

  // Timer: 13.56 MHz / (2*0xA9 + 1) = 40 kHz, so 25 us a tick. A reload of 200
  // gives a 5 ms timeout rather than the 25 ms the stock examples use. A tag
  // answers REQA in well under a millisecond, so the only thing the longer
  // timeout buys is 20 ms of stall per empty poll -- which this firmware
  // cannot afford, because the same loop is decoding video.
  writeReg(REG_T_MODE, 0x80);           // TAuto: restart at the end of transmit
  writeReg(REG_T_PRESCALER, 0xA9);
  writeReg(REG_T_RELOAD_H, 0x00);
  writeReg(REG_T_RELOAD_L, 0xC8);       // 200 ticks

  writeReg(REG_TX_ASK, 0x40);           // force 100% ASK
  writeReg(REG_MODE, 0x3D);             // CRC preset 0x6363
  setBits(REG_TX_CONTROL, 0x03);        // antenna drivers on
  return true;
}

bool present()    { return gPresent; }
uint8_t address() { return gAddr; }
uint8_t version() { return gVersion; }

bool poll(Tag &out) {
  out.len = 0;
  out.sak = 0;
  if (!gPresent) return false;
  if (!requestA()) return false;        // nothing in the field: the usual case

  // Anticollision, one cascade level at a time. A 4 byte UID finishes at level
  // one; the 7 byte UIDs on NTAG stickers need two, and 10 byte would need
  // three. Each level that is not the last returns the cascade tag 0x88 as its
  // first byte, and that byte is a marker rather than part of the UID.
  const uint8_t levels[3] = {PICC_SEL_1, PICC_SEL_2, PICC_SEL_3};
  uint8_t uidLen = 0;

  for (uint8_t level = 0; level < 3; level++) {
    // Anticollision: NVB 0x20 means "I am sending two bytes, tell me the rest".
    uint8_t anti[2] = {levels[level], 0x20};
    uint8_t got[5];
    uint8_t gotLen = sizeof(got);
    if (!transceive(anti, sizeof(anti), 0, got, &gotLen, nullptr) || gotLen != 5) {
      return false;
    }
    // The fifth byte is BCC, the XOR of the other four. A bad BCC means the
    // read was corrupt, not that the tag is odd.
    if ((uint8_t)(got[0] ^ got[1] ^ got[2] ^ got[3]) != got[4]) return false;

    // Select this level so the tag tells us whether the UID continues.
    uint8_t sel[9] = {levels[level], 0x70, got[0], got[1], got[2], got[3], got[4], 0, 0};
    if (!calcCrc(sel, 7, &sel[7])) return false;
    uint8_t sak[3];
    uint8_t sakLen = sizeof(sak);
    if (!transceive(sel, sizeof(sel), 0, sak, &sakLen, nullptr) || sakLen != 3) {
      return false;
    }

    const bool more = (sak[0] & 0x04) != 0;
    // Skip the cascade tag on every level that continues.
    const uint8_t first = more ? 1 : 0;
    for (uint8_t i = first; i < 4 && uidLen < kMaxUid; i++) out.uid[uidLen++] = got[i];

    if (!more) { out.sak = sak[0]; out.len = uidLen; return true; }
  }
  return false;   // more than three cascade levels is not a thing
}

void formatUid(const Tag &t, char *out, size_t cap) {
  static const char kHex[] = "0123456789ABCDEF";
  size_t o = 0;
  for (uint8_t i = 0; i < t.len && o + 2 < cap; i++) {
    out[o++] = kHex[t.uid[i] >> 4];
    out[o++] = kHex[t.uid[i] & 0x0F];
  }
  if (o < cap) out[o] = '\0';
  else if (cap) out[cap - 1] = '\0';
}

}  // namespace nfc
