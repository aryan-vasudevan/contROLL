#pragma once
// ---------------------------------------------------------------------------
// MFRC522 (U7) NFC reader, over I2C.
//
// The badge's reader shares SDA/SCL with the accelerometer. Its strapping was
// read off the board: U7.1 high selects I2C rather than SPI or UART, U7.6 high
// leaves it out of reset, and the address straps predict 0x29. That prediction
// came from an EasyEDA-converted symbol whose pin names do not match the
// datasheet, so begin() scans 0x28-0x2F and believes whatever answers instead.
//
// Only enough of the protocol to answer "which tag is this": REQA, then
// anticollision through as many cascade levels as the UID needs. No
// authentication and no memory reads, because the UID alone identifies a tag
// and every extra command is another thing to get wrong on a badge that is
// already spending all its time decoding JPEG.
// ---------------------------------------------------------------------------

#include <stddef.h>
#include <stdint.h>

namespace nfc {

// ISO/IEC 14443-3A allows 4, 7 and 10 byte UIDs. The NTAG21x stickers this was
// written for are 7, which needs two cascade levels rather than the single
// level nearly every MFRC522 example online handles.
constexpr size_t kMaxUid = 10;

struct Tag {
  uint8_t uid[kMaxUid];
  uint8_t len;      // 0 when no tag was read
  uint8_t sak;      // select acknowledge, for curiosity only
};

// Wire.begin() must already have run: this shares the bus with the
// accelerometer and does not own it. Returns false if nothing answers, which
// is a wiring or strapping problem and not something a retry will fix.
bool begin();

bool    present();   // did begin() find a reader
uint8_t address();   // the I2C address that actually answered
uint8_t version();   // VersionReg; 0x91 or 0x92 on a genuine MFRC522

// One poll of the field. True when a tag answered this call.
//
// Cheap when nothing is there: a single REQA that gives up after the timer
// expires. The timer is deliberately set short in begin() so that polling this
// from a video loop costs a few milliseconds rather than the 25 ms the stock
// MFRC522 timer settings would.
bool poll(Tag &out);

// Uppercase hex, no separators, so it can go straight into a UDP line and be
// compared as a string on the other end. Needs 2*len+1 bytes.
void formatUid(const Tag &t, char *out, size_t cap);

}  // namespace nfc
