// ---------------------------------------------------------------------------
// Badge screen + button link.
//
//     OAK-1 --USB--> Pi --Wi-Fi/TCP--> badge --SPI--> ST7789 panel
//     buttons -------------------------> Pi (UDP, as before)
//
// The panel is an HS20HS072RX: a 320x240 colour TFT driven by an ST7789T3 over
// 4-wire SPI. LCSC part C5329582. Nothing had ever driven it before this file,
// and an earlier version of the repo docs called it an OLED with an
// unidentifiable controller, which it is not.
//
// Pins came out of badge.kicad_pcb (see badge_pins.h) and are repeated as
// badge_pins.h and are passed to the driver at construction.
//
//
// The backlight has no control line: FPC pin 2 is the LED cathode through a
// 10R resistor (R38) to ground, and the anode sits on the 3V3 pins. It is on
// whenever the badge is.
//
// On JPEG rather than raw pixels: a 320x240 RGB565 frame is 153,600 bytes and
// the C3 has 327,680 in total. Decoding MCU block at a time straight to the
// panel means never holding a whole frame, and it cuts what crosses the radio
// by roughly an order of magnitude.
// ---------------------------------------------------------------------------

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_system.h>
#include <Preferences.h>
#include <string.h>

#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <JPEGDEC.h>

#include "badge_pins.h"
#include "config.h"
#include "buttons.h"
#include "imu.h"
#include "leds.h"

namespace {

// Explicit SPI instance so the pins are the ones read off badge.kicad_pcb
// rather than whatever the core's defaults happen to be for this chip.
SPIClass         gSpi(FSPI);
Adafruit_ST7789  gTft(&gSpi, PIN_DISP_CS, PIN_DISP_DC, PIN_DISP_RST);
JPEGDEC          gJpeg;
Preferences      gPrefs;
bool             gFullClock = false;

WiFiUDP    gVideo;
WiFiUDP    gUdp;
IPAddress  gPeer;

// Biggest JPEG we will accept. A 320x240 frame at quality 70 runs 10-16 KB;
// this leaves room for a busy scene without letting a corrupt length field
// convince us to allocate something absurd.
constexpr size_t   kMaxFrame     = 48 * 1024;
constexpr uint16_t kStreamPort   = 14557;
constexpr uint16_t kVideoLocalPort = 14558;
constexpr size_t   kChunk        = 1400;
constexpr int      kMaxBoxes     = 16;
// Short, deliberately. The frame request is one tiny datagram; on a radio
// that is now AP + hotspot client at once, it gets lost far more often than
// it did, and every loss used to freeze the picture for the full 4 s the
// original values allowed. Re-asking is nearly free -- worst case a
// duplicate frame -- so recovery is priced at under a second.
constexpr uint32_t kReconnectMs  = 400;
constexpr uint32_t kStallMs      = 600;
// Only after THIS long does the badge admit the link is down on screen.
// Re-asking is silent and cheap and happens at kStallMs; painting the
// "waiting" banner is neither -- it blanks the whole panel and turns the
// LEDs blue, and firing it on every sub-second hiccup made the badge flash
// black/blue every few seconds, which read as broken when it was merely
// impatient.
constexpr uint32_t kOutageMs     = 3500;

uint8_t  gFrame[kMaxFrame];

// One decoded strip, expanded.
//
// JPEGDEC does NOT hand back 16x16 blocks. It hands back a strip up to the
// full width of the image and up to one MCU tall, so for a 160 wide source
// that is 160x16, and 320x32 once doubled. Sizing this for 16x16 overran it
// and corrupted the stack, which showed up as a panic with a garbage PC.
//
// The scale is always chosen so the result fits the panel, so the widest
// possible strip is the panel width, and the tallest is one MCU scaled up.
constexpr int kMaxScale   = 4;
constexpr int kMaxStripPx = 320 * (16 * kMaxScale);
uint16_t gScaled[kMaxStripPx];
int      gScale   = 1;          // worked out per frame from the image size
int      gOffsetX = 0, gOffsetY = 0;
int      gImgW = 0, gImgH = 0;
uint32_t gLastFrameMs = 0;
uint32_t gLastTryMs   = 0;
uint32_t gLastStatMs  = 0;
uint32_t gFrames      = 0;
uint32_t gBytes       = 0;
uint32_t gSeq         = 0;
uint32_t gLastBtnMs   = 0;

// --- display ---------------------------------------------------------------

// JPEGDEC hands back one decoded block at a time. Pushing each straight to the
// panel is the whole reason no framebuffer is needed.
int onJpegBlock(JPEGDRAW *block) {
  // The 1x fast path draws the decoder's buffer directly, which cannot be
  // reversed in place, so rotation always goes the long way round.
  if (gScale == 1 && !CAM_ROTATE_180) {
    gTft.drawRGBBitmap(block->x + gOffsetX, block->y + gOffsetY,
                       block->pPixels, block->iWidth, block->iHeight);
    return 1;
  }

  // Nearest-neighbour expansion. The camera sends a quarter of the panel's
  // pixels because decode time is what limits the frame rate, and duplicating
  // pixels here is far cheaper than decoding four times as many. The picture
  // is softer; that is the trade being made deliberately.
  const int sw = block->iWidth, sh = block->iHeight;
  const int dw = sw * gScale, dh = sh * gScale;

  // Never trust the decoder's block geometry against a fixed buffer. Drawing
  // one strip unscaled is a visible glitch; running off the end of this array
  // is a reboot.
  if ((size_t)dw * dh > kMaxStripPx) {
    gTft.drawRGBBitmap(block->x + gOffsetX, block->y + gOffsetY,
                       block->pPixels, sw, sh);
    return 1;
  }
  if (gScale == 1) {
    memcpy(gScaled, block->pPixels, (size_t)sw * sh * sizeof(uint16_t));
  }
  for (int y = 0; gScale > 1 && y < sh; y++) {
    uint16_t *dst = gScaled + (size_t)y * gScale * dw;
    const uint16_t *src = block->pPixels + (size_t)y * sw;
    for (int x = 0; x < sw; x++) {
      const uint16_t px = src[x];
      for (int k = 0; k < gScale; k++) dst[x * gScale + k] = px;
    }
    // The remaining rows of this source row are identical; copy rather than
    // recompute.
    for (int r = 1; r < gScale; r++) {
      memcpy(dst + (size_t)r * dw, dst, (size_t)dw * sizeof(uint16_t));
    }
  }
#if CAM_ROTATE_180
  // A 180 degree rotation of a row-major image is just reversing the buffer end
  // to end: that mirrors rows and columns in a single pass.
  for (int a = 0, b = dw * dh - 1; a < b; a++, b--) {
    const uint16_t t = gScaled[a]; gScaled[a] = gScaled[b]; gScaled[b] = t;
  }
  // The strip lands as far from the far edge as it started from the near one.
  const int dx = gOffsetX + (gImgW - block->x - sw) * gScale;
  const int dy = gOffsetY + (gImgH - block->y - sh) * gScale;
#else
  const int dx = block->x * gScale + gOffsetX;
  const int dy = block->y * gScale + gOffsetY;
#endif
  gTft.drawRGBBitmap(dx, dy, gScaled, dw, dh);
  return 1;
}

// Drawn before the radio starts, so a blank screen here means the panel or its
// wiring, and never the network. Worth having: this is the first firmware to
// drive this display at all.
void testPattern() {
  const uint16_t bars[] = {ST77XX_RED,  ST77XX_GREEN,  ST77XX_BLUE, ST77XX_YELLOW,
                           ST77XX_CYAN, ST77XX_MAGENTA, ST77XX_WHITE, ST77XX_BLACK};
  const int w = gTft.width() / 8;
  for (int i = 0; i < 8; i++) {
    gTft.fillRect(i * w, 0, w, gTft.height() - 40, bars[i]);
  }
  gTft.fillRect(0, gTft.height() - 40, gTft.width(), 40, ST77XX_BLACK);
  gTft.setTextColor(ST77XX_WHITE, ST77XX_BLACK);
  gTft.setTextSize(1);
  gTft.setCursor(6, gTft.height() - 34);
  gTft.printf("%dx%d ST7789", gTft.width(), gTft.height());
  gTft.setCursor(6, gTft.height() - 18);
  gTft.print("waiting for wifi");
}

void banner(const char *line1, const char *line2) {
  gTft.fillScreen(ST77XX_BLACK);
  gTft.setTextColor(ST77XX_WHITE, ST77XX_BLACK);
  gTft.setTextSize(1);
  gTft.setCursor(8, 8);
  gTft.print(line1);
  if (line2) {
    gTft.setCursor(8, 28);
    gTft.setTextColor(0x7BEF, ST77XX_BLACK);
    gTft.print(line2);
  }
}

// --- stream ----------------------------------------------------------------

// Reassemble one frame from its chunks.
//
// UDP rather than TCP because a lost packet on a weak link makes TCP stall the
// entire stream until it is retransmitted, and a frame that arrives late is
// worth less than the one behind it. Here a lost chunk costs exactly one
// frame: the partial is abandoned the moment a newer frame_id turns up.
//
// Chunks are sized by the Pi to fit inside an MTU, so IP never fragments them.
// A fragmented 10 KB datagram would be lost entire if any one fragment went
// missing, which is precisely what happens on the link this is meant to ride.
uint16_t gAsmId      = 0xFFFF;
uint8_t  gAsmCount   = 0;
uint32_t gAsmMask    = 0;      // bit per chunk received; 32 chunks is 44 KB
size_t   gAsmLen     = 0;
bool     gAsmActive  = false;

// Detections, drawn over the video. They arrive out of band and a little
// behind the frames -- inference is a network round trip -- so they are held
// and redrawn on every frame until replaced or they go stale. Boxes that no
// longer match what the camera sees are worse than no boxes.
//
// `id` is a tracker id from the Pi and is the whole reason a target can be
// selected: it names the same person from one frame to the next, where a
// position in the list does not.
// --- detection packet ------------------------------------------------------
// [detparse] Everything between these markers is compiled and tested on a
// host by tests/detpacket_test.cpp. It is pulled out verbatim by extract.py,
// so it must not touch Arduino APIs or globals.
//
// The handoff's complaint about the old overlay was exact and worth not
// repeating: "a Python script packed boxes and unpacked them with a Python
// re-implementation of the C++ parsing, and the values matched. That proves
// the Python agrees with itself." So this is the real parser, and the test
// feeds it bytes the real packer produced.

struct DetBox { int16_t x, y; uint16_t w, h; uint8_t conf; uint8_t id; char label[16]; };

// Returns the number of boxes parsed, or -1 if this is not a detection packet.
//
//     "BDT2" u8 count  u16 frame_id  u16 img_w  u16 img_h
//     then count x:  s16 x  s16 y  u16 w  u16 h  u8 conf  u8 id  u8 len  <label>
//
// Every read is bounds-checked against n. These bytes come off a UDP socket,
// so the length fields are attacker-controlled in the same sense that any
// network input is: a corrupt one must truncate the parse, never index past
// the buffer.
int parseDetections(const unsigned char *pkt, int n, DetBox *out, int maxBoxes,
                    uint16_t *imgW, uint16_t *imgH) {
  constexpr int kHeader = 11;      // magic 4 + count 1 + frame 2 + w 2 + h 2
  constexpr int kFixed  = 11;      // x 2 + y 2 + w 2 + h 2 + conf 1 + id 1 + len 1
  if (n < kHeader) return -1;
  if (pkt[0] != 'B' || pkt[1] != 'D' || pkt[2] != 'T' || pkt[3] != '2') return -1;

  int count = pkt[4];
  if (count > maxBoxes) count = maxBoxes;
  if (imgW) *imgW = (uint16_t)pkt[7]  | ((uint16_t)pkt[8]  << 8);
  if (imgH) *imgH = (uint16_t)pkt[9]  | ((uint16_t)pkt[10] << 8);

  int off = kHeader, parsed = 0;
  while (parsed < count && off + kFixed <= n) {
    DetBox &b = out[parsed];
    b.x    = (int16_t)((uint16_t)pkt[off]     | ((uint16_t)pkt[off + 1] << 8));
    b.y    = (int16_t)((uint16_t)pkt[off + 2] | ((uint16_t)pkt[off + 3] << 8));
    b.w    = (uint16_t)pkt[off + 4] | ((uint16_t)pkt[off + 5] << 8);
    b.h    = (uint16_t)pkt[off + 6] | ((uint16_t)pkt[off + 7] << 8);
    b.conf = pkt[off + 8];
    b.id   = pkt[off + 9];
    const int len = pkt[off + 10];
    off += kFixed;
    // A length that runs off the end means the datagram was truncated. Stop
    // with what we have rather than reading whatever follows in memory.
    if (off + len > n) break;
    int copy = len;
    if (copy > (int)sizeof(b.label) - 1) copy = (int)sizeof(b.label) - 1;
    for (int i = 0; i < copy; i++) b.label[i] = (char)pkt[off + i];
    b.label[copy] = 0;
    off += len;
    parsed++;
  }
  return parsed;
}
// [/detparse]

// One declaration, shared with the parser, so the two cannot drift apart.
using Box = DetBox;
Box      gBoxes[kMaxBoxes];
int      gBoxCount   = 0;
uint16_t gSrcW = 0, gSrcH = 0;      // what the coordinates are relative to
uint32_t gBoxesAtMs  = 0;
constexpr uint32_t kBoxStaleMs = 2000;

// The target the car is being asked to drive at. 0 means none, which is why
// the Pi never allocates id 0.
uint8_t  gSelected   = 0;
bool     gAuto       = false;

// HOME press bookkeeping: under the threshold it is a selection tap, past it
// it is the stop the Pi has always known.
uint32_t gSelDownMs  = 0;
constexpr uint32_t kSelHoldMs = 600;
bool     gOutageShown = false;

// How long a selected id may be absent before the selection is abandoned.
// Long enough to ride out an uplink stall, short enough that the yellow does
// not migrate to a stranger who later gets the recycled id.
uint32_t gSelMissingMs = 0;
constexpr uint32_t kSelGraceMs = 2500;

// Shake, recording, and the horn.
//
// imu::shakeDetected() is true on exactly one poll, and the badge sends at
// 20 Hz at best -- so the event has to be latched here or it is lost between
// packets. Cleared once it has actually gone out on the wire.
bool     gShake      = false;
bool     gRecording  = false;
bool     gHonk       = false;
uint32_t gShakeAtMs  = 0;

void requestFrame() {
  gVideo.beginPacket(gPeer, kStreamPort);
  gVideo.write((const uint8_t *)"R", 1);
  gVideo.endPacket();
}

// True when a whole frame has been reassembled into gFrame.
bool pumpVideo() {
  uint8_t pkt[kChunk + 16];
  int size;
  bool complete = false;

  while ((size = gVideo.parsePacket()) > 0) {
    const int n = gVideo.read(pkt, sizeof(pkt));
    const int boxes = parseDetections(pkt, n, gBoxes, kMaxBoxes, &gSrcW, &gSrcH);
    if (boxes >= 0) {
      gBoxCount  = boxes;
      gBoxesAtMs = millis();
      // The selection belongs to the OPERATOR, not to the detector. It used
      // to be dropped when the id went missing -- first instantly, then with
      // a grace period -- and either way the user watched their choice
      // evaporate because a phone link hiccupped. No more: HOME, START and
      // the arrows are the only things that change a selection. A missing
      // target is the Pi's safety problem (it stops the car); it is not a
      // reason to forget what the user asked for.
      continue;
    }
    if (n < 10 || memcmp(pkt, "BJPF", 4) != 0) continue;

    const uint16_t id    = (uint16_t)pkt[4] | ((uint16_t)pkt[5] << 8);
    const uint8_t  idx   = pkt[6];
    const uint8_t  count = pkt[7];
    const uint16_t len   = (uint16_t)pkt[8] | ((uint16_t)pkt[9] << 8);

    if (count == 0 || count > 32 || idx >= count) continue;
    if (len > kChunk || (size_t)(10 + len) > (size_t)n) continue;
    if ((size_t)idx * kChunk + len > kMaxFrame) continue;

    if (id != gAsmId) {
      // A newer frame started. Whatever was half-assembled is now stale, and
      // waiting for its missing chunk would only add latency.
      gAsmId = id; gAsmCount = count; gAsmMask = 0; gAsmLen = 0; gAsmActive = true;
    }
    memcpy(gFrame + (size_t)idx * kChunk, pkt + 10, len);
    gAsmMask |= (1UL << idx);
    if (idx == count - 1) gAsmLen = (size_t)idx * kChunk + len;

    const uint32_t want = (count >= 32) ? 0xFFFFFFFFUL : ((1UL << count) - 1);
    if (gAsmActive && gAsmMask == want && gAsmLen > 0) {
      gAsmActive = false;
      complete = true;
      // Keep draining: if a newer frame is already queued behind this one, we
      // would rather decode that than something older.
    }
  }
  return complete;
}

// Boxes come in source-image pixels, so they scale with the picture.
//
// The selected target is drawn differently rather than merely labelled: at
// 320x240 with several people in frame, a colour change is readable at arm's
// length and a small "*" is not.
void drawBoxes() {
  if (gBoxCount == 0 || millis() - gBoxesAtMs > kBoxStaleMs) return;
  for (int i = 0; i < gBoxCount; i++) {
    const Box &b = gBoxes[i];
    const bool chosen = (b.id != 0 && b.id == gSelected);
    const uint16_t tint = chosen ? (gAuto ? ST77XX_RED : ST77XX_YELLOW)
                                 : ST77XX_GREEN;
#if CAM_ROTATE_180
    const int sx = gImgW - b.x - (int)b.w, sy = gImgH - b.y - (int)b.h;
#else
    const int sx = b.x, sy = b.y;
#endif
    const int x = sx * gScale + gOffsetX;
    const int y = sy * gScale + gOffsetY;
    const int w = b.w * gScale, h = b.h * gScale;
    gTft.drawRect(x, y, w, h, tint);
    gTft.drawRect(x - 1, y - 1, w + 2, h + 2, tint);   // 2px, more legible
    if (chosen) {
      gTft.drawRect(x - 2, y - 2, w + 4, h + 4, tint); // 3px for the target
    }

    // Label sits above the box, or inside it when the box is against the top.
    const int ty = (y >= 10) ? y - 9 : y + 2;
    gTft.setTextSize(1);
    gTft.setTextColor(tint, ST77XX_BLACK);
    gTft.setCursor(x + 1, ty);
    gTft.printf("%s %u%%", b.label, (unsigned)b.conf);
  }
}

// A single status line along the bottom. Small, but it is the only feedback
// that says whether the badge thinks it is driving the car or aiming it, and
// getting that wrong with a moving vehicle is the expensive kind of confusion.
void drawHud() {
  const int y = gTft.height() - 9;
  gTft.setTextSize(1);
  gTft.setTextColor(gAuto ? ST77XX_RED : ST77XX_WHITE, ST77XX_BLACK);
  gTft.setCursor(2, y);
  if (gAuto) {
    gTft.printf("CHASE #%u   START stop", (unsigned)gSelected);
  } else if (gSelected) {
    gTft.printf("TARGET #%u  START go ", (unsigned)gSelected);
  } else if (gBoxCount > 0) {
    gTft.printf("%d seen  HOME picks   ", gBoxCount);
  } else {
    gTft.print("manual              ");
  }

  // Recording and a recent shake get their own corner, so they are legible
  // whatever the chase is doing.
  gTft.setCursor(gTft.width() - 64, y);
  if (gRecording) {
    gTft.setTextColor(ST77XX_RED, ST77XX_BLACK);
    gTft.print(" REC");
  } else if (millis() - gShakeAtMs < 700) {
    gTft.setTextColor(ST77XX_CYAN, ST77XX_BLACK);
    gTft.print("HONK");
  } else {
    gTft.setTextColor(ST77XX_BLACK, ST77XX_BLACK);
    gTft.print("    ");
  }
}

// --- target selection ------------------------------------------------------
//
// B is the chord key, because it is already the "be careful" button (crawl)
// and nothing that follows should ever happen by accident:
//
//     HOME, tapped     step through the detections (the chosen one is YELLOW)
//     HOME, held       stop, exactly as before -- see below
//     START            drive to the yellow one; press again to stop driving
//     B + LEFT/RIGHT   also steps through them, as before
//     B + A            engage or cancel the chase
//     A, tapped        start / stop recording (uploads when it stops)
//     B, tapped        honk -- a random sound effect on the car's speaker
//     B + UP           also toggles recording, as before
//     B + DOWN         also honks
//     shake the badge  honk, without needing a free hand
//     anything else    manual driving, and that always wins
//
// HOME does two jobs split by duration: a tap (under 600 ms) picks the next
// target, a hold is the deliberate-stop latch it has always been. The split
// lives here on the badge: sendButtons only reports HOME to the Pi once the
// hold passes the threshold, so a tap never so much as twitches the motors,
// and the Pi's stop logic is untouched.
//
// Selection lives on the badge rather than the Pi so that what is highlighted
// on the panel and what the car is chasing cannot disagree -- there is one
// copy of the decision and it is the one the operator can see.
void pickTarget(int step) {
  if (gBoxCount == 0) { gSelected = 0; gAuto = false; return; }
  int at = -1;
  for (int i = 0; i < gBoxCount; i++) {
    if (gBoxes[i].id == gSelected) { at = i; break; }
  }
  at = (at < 0) ? 0 : (at + step + gBoxCount) % gBoxCount;
  gSelected = gBoxes[at].id;
}

void pollSelection() {
  // Edge-triggered: holding a button steps once, not thirty times a second.
  static bool wasLeft = false, wasRight = false, wasA = false;
  static bool wasUp = false, wasDown = false;
  static bool wasSel = false;
  const bool chord = btn::down(btn::B);
  const bool left  = chord && btn::down(btn::LEFT);
  const bool right = chord && btn::down(btn::RIGHT);
  const bool a     = chord && btn::down(btn::A);
  const bool up    = chord && btn::down(btn::UP);
  const bool down  = chord && btn::down(btn::DOWN);

  if (left && !wasLeft)   pickTarget(-1);
  if (right && !wasRight) pickTarget(+1);
  if (a && !wasA) {
    if (gAuto)            gAuto = false;
    else if (gSelected)   gAuto = true;
  }
  if (up && !wasUp)     gRecording = !gRecording;
  if (down && !wasDown) gHonk = true;

  // Bare taps of A and B, distinguished from their driving jobs (boost and
  // crawl) by being short and clean: nothing else pressed while they were
  // down. Holding A with UP still boosts exactly as it always has.
  static bool wasABare = false, wasBBare = false;
  static bool aDirty = false, bDirty = false;
  static uint32_t aDownAt = 0, bDownAt = 0;
  const bool aNow = btn::down(btn::A);
  const bool bNow = btn::down(btn::B);
  const bool anyDir = btn::down(btn::UP) || btn::down(btn::DOWN) ||
                      btn::down(btn::LEFT) || btn::down(btn::RIGHT);
  if (aNow && !wasABare) { aDownAt = millis(); aDirty = false; }
  if (bNow && !wasBBare) { bDownAt = millis(); bDirty = false; }
  if (aNow && anyDir) aDirty = true;                       // it was a boost
  if (bNow && (anyDir || btn::down(btn::A))) bDirty = true; // crawl or chord
  if (!aNow && wasABare && !aDirty && millis() - aDownAt < 500)
    gRecording = !gRecording;
  // Honk on the PRESS edge -- instant, no release-wait. The cost: starting
  // a B-chord or a crawl also honks once. Accepted; the horn is the point.
  if (bNow && !wasBBare)
    gHonk = true;
  wasABare = aNow; wasBBare = bNow;

  // HOME: tap to pick the next person, hold to stop (reported by
  // sendButtons only after the hold threshold).
  const bool sel = btn::down(btn::SELECT);
  if (sel && !wasSel)  gSelDownMs = millis();
  if (!sel && wasSel && millis() - gSelDownMs < kSelHoldMs) pickTarget(+1);
  wasSel = sel;

  // START: go. One press drives at the yellow target, another stands down.
  // btn::BOOT is SW10 on GPIO9 -- the boot strapping pin, which is exactly
  // why it is read like any other button at runtime but must never be held
  // through a reset (that is the ROM's download-mode strap, handoff par.4).
  static bool wasStart = false;
  const bool start = btn::down(btn::BOOT);
  if (start && !wasStart) {
    if (gAuto)          gAuto = false;
    else if (gSelected) gAuto = true;
  }
  wasStart = start;

  wasLeft = left; wasRight = right; wasA = a;
  wasUp = up; wasDown = down;

  // Shaking the badge honks. The detector needs several threshold crossings
  // inside a window, which is what separates a deliberate shake from the badge
  // swinging on a lanyard while somebody walks -- see imu.h. It is on a
  // cooldown, so leaning on it cannot machine-gun the horn.
  if (imu::present() && imu::shakeDetected()) {
    gShake = true;
    gHonk  = true;
    gShakeAtMs = millis();
  }

  // Any bare direction is a human taking the wheel. The Pi enforces this too
  // -- it must, since the badge could be switched off mid-chase -- but doing
  // it here as well means the panel stops claiming CHASE the instant the
  // driver overrides, rather than a round trip later.
  if (!chord && (btn::down(btn::UP) || btn::down(btn::DOWN) ||
                 btn::down(btn::LEFT) || btn::down(btn::RIGHT))) {
    gAuto = false;
  }
}

bool decodeFrame() {
  const size_t len = gAsmLen;
  if (len == 0 || len > kMaxFrame) return false;

  if (!gJpeg.openRAM(gFrame, (int)len, onJpegBlock)) {
    Serial.println("[cam] JPEGDEC refused the frame");
    return false;
  }
  // Work the scale out from the image itself, so the Pi can change resolution
  // without the badge needing a reflash to match.
  const int iw = gJpeg.getWidth(), ih = gJpeg.getHeight();
  int scale = 1;
  if (iw > 0 && ih > 0) {
    scale = min(gTft.width() / iw, gTft.height() / ih);
    scale = constrain(scale, 1, kMaxScale);
  }
  if (scale != gScale) {
    Serial.printf("[cam] %dx%d source, drawing at %dx\n", iw, ih, scale);
    gTft.fillScreen(ST77XX_BLACK);
  }
  gScale   = scale;
  gImgW    = iw;
  gImgH    = ih;
  gOffsetX = (gTft.width()  - iw * scale) / 2;
  gOffsetY = (gTft.height() - ih * scale) / 2;

  gJpeg.setPixelType(RGB565_LITTLE_ENDIAN);
  gJpeg.decode(0, 0, 0);
  gJpeg.close();

  drawBoxes();
  drawHud();

  gFrames++;
  gBytes += len;
  gLastFrameMs = millis();
  return true;
}

// --- buttons ---------------------------------------------------------------
// Unchanged from pilink: the same line, the same port. The camera is an
// addition to the link, not a replacement for it.

const btn::Id kReported[] = {btn::UP, btn::DOWN, btn::LEFT, btn::RIGHT,
                             btn::A,  btn::B,    btn::SELECT, btn::SLIDE};

void sendButtons() {
  char held[96] = {0};
  for (btn::Id b : kReported) {
    if (!btn::down(b)) continue;
    // A short HOME press is a selection tap and stays on the badge; only a
    // real hold reaches the Pi, where it means stop.
    if (b == btn::SELECT && millis() - gSelDownMs < kSelHoldMs) continue;
    if (held[0]) strncat(held, " ", sizeof(held) - strlen(held) - 1);
    strncat(held, btn::name(b), sizeof(held) - strlen(held) - 1);
  }
  char line[192];
  // sel/auto are appended after down=[...] rather than inserted, so a Pi
  // running the older badge_listen.py -- whose regex stops at the closing
  // bracket -- keeps working untouched.
  const int n = snprintf(line, sizeof(line),
                         "BADGE1 seq=%lu ms=%lu raw=0x%02X down=[%s] sel=%u auto=%d "
                         "shake=%d rec=%d honk=%d",
                         (unsigned long)++gSeq, (unsigned long)millis(),
                         btn::rawRegister(), held,
                         (unsigned)gSelected, gAuto ? 1 : 0,
                         gShake ? 1 : 0, gRecording ? 1 : 0, gHonk ? 1 : 0);
  // One-shot events, cleared the moment they are on the wire. Level state
  // (rec) is not cleared: the Pi should be able to recover it from any packet,
  // not just the one where it changed.
  gShake = false;
  gHonk  = false;
  if (n <= 0) return;
  gUdp.beginPacket(gPeer, PI_UDP_PORT);
  gUdp.write(reinterpret_cast<const uint8_t *>(line), n);
  gUdp.endPacket();
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);
  // Remember across boots. A badge that dies on battery cannot be watched over
  // USB, and plugging it in to look is itself a power-on that destroys the
  // evidence. Storing the reason means the next USB boot can report what
  // happened on the last battery one.
  const int why = (int)esp_reset_reason();
  gPrefs.begin("badgecam", false);
  const int prev = gPrefs.getInt("lastreset", -1);
  gPrefs.putInt("lastreset", why);
  gPrefs.end();
  // 9 is ESP_RST_BROWNOUT, 4 is ESP_RST_PANIC.
  Serial.printf("[cam] boot, reset now: %d, previous boot: %d%s\n", why, prev,
                prev == 9 ? "  <-- BROWNOUT last time: the supply, not the code" :
                prev == 4 ? "  <-- PANIC last time" : "");

  btn::begin();
  leds::begin();
  leds::setStatus(leds::Status::Booting);

  // The accelerometer shares GPIO 5/6 with the NFC reader. The driver has
  // existed and been host-tested since the drone firmware; this is the first
  // build that compiles it into the camera badge. A badge without one still
  // works -- present() is false and shaking simply does nothing.
  if (imu::begin()) {
    Serial.printf("[cam] accelerometer at 0x19, WHO_AM_I 0x%02X; shake to honk\n",
                  imu::whoAmI());
  } else {
    Serial.println("[cam] no accelerometer; shake-to-honk disabled");
  }

  // Panel first, before anything that can fail for network reasons.
  // The panel is 240x320 in its native portrait orientation; rotation 1 puts
  // it in the 320x240 landscape the camera feed is sized for.
  gSpi.begin(PIN_DISP_SCLK, -1 /* no MISO */, PIN_DISP_MOSI, PIN_DISP_CS);
  gTft.init(240, 320);
  gTft.setSPISpeed(80000000);   // 40 MHz was costing ~15 ms a frame
  // Rotation 3, not 1. Found on hardware, 2026-09-20: with rotation 1 the
  // panel's origin lands at the physical bottom-right of the badge as held,
  // so every glyph came out upside down. The old firmware masked this by
  // flipping the *video* in software (CAM_ROTATE_180), which fixed the
  // picture and quietly left all text inverted -- unnoticed because nothing
  // drew text over live video until the overlay did. Rotation 3 turns the
  // whole coordinate system instead, so video, boxes and text agree, and the
  // software flip (a full reversal of every decoded strip) is gone.
  gTft.setRotation(3);
  gTft.fillScreen(ST77XX_BLACK);
  testPattern();
  Serial.printf("[cam] panel %dx%d\n", gTft.width(), gTft.height());

  // Associate at the low clock. The current crunch is during association --
  // that is where the radio transmits hardest -- and the badge cannot afford
  // to be decoding-fast and associating at the same time on two AA cells.
  // The clock goes up once there is actually a link worth decoding for.
  setCpuFrequencyMhz(CPU_MHZ);
  delay(RADIO_SETTLE_MS);

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setMinSecurity(WIFI_AUTH_WPA_PSK);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  // Full transmit power here, unlike the button-only firmware. Dropped frames
  // at a distance are a link-budget problem, and the badge's uplink carries
  // the frame requests -- lose those and the video stops entirely.
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  WiFi.setSleep(false);   // modem sleep adds latency to every frame request

  gPeer.fromString(PI_IP);
  gUdp.begin(PI_UDP_LOCAL_PORT);
  gVideo.begin(kVideoLocalPort);
  Serial.printf("[cam] joining \"%s\", stream from %s:%u\n",
                WIFI_SSID, PI_IP, kStreamPort);
}

void loop() {
  btn::poll();
  leds::poll();
  imu::poll();
  pollSelection();

  if (WiFi.status() != WL_CONNECTED) {
    if (gFullClock) {
      gFullClock = false;
      gAsmActive = false;
      setCpuFrequencyMhz(CPU_MHZ);   // back to the cheap clock to re-associate
    }
    leds::setStatus(leds::Status::WifiConnecting);
    static uint32_t saidAt = 0;
    if (millis() - saidAt > 2000) {
      saidAt = millis();
      Serial.printf("[cam] waiting for \"%s\" (status %d)\n",
                    WIFI_SSID, (int)WiFi.status());
      banner("no wifi", WIFI_SSID);
    }
    return;
  }

  if (!gFullClock) {
    // Associated. Now buy the clock speed that JPEG decode needs.
    gFullClock = true;
    setCpuFrequencyMhz(160);
    Serial.printf("[cam] on the network; cpu -> %u MHz for decoding\n",
                  (unsigned)getCpuFrequencyMhz());
    gLastFrameMs = millis();
    requestFrame();
  }

  // Buttons carry on regardless of whether video is flowing.
  //
  // gAuto counts as activity even with nothing held, and it has to. The Pi
  // cuts the motors after 200 ms of silence, while an idle badge sends every
  // 500 ms -- so a chase with no button pressed would be stopped by the
  // failsafe three times a second. Autonomy is exactly the case where nobody
  // is touching the badge, so the heartbeat has to come from the mode rather
  // than from a finger.
  // A recent shake counts as activity too. The spin it triggers runs on the
  // Pi with nobody touching a button, and at the idle 2 Hz heartbeat the
  // Pi's 200 ms failsafe fires BETWEEN packets -- which cut every spin off
  // at half a revolution and looked like a duration bug. Fast heartbeats for
  // a few seconds keep the failsafe fed for the whole 360.
  const bool spinActive = millis() - gShakeAtMs < 3000;
  if (millis() - gLastBtnMs >= (uint32_t)(1000 / ((btn::anyDown() || gAuto || spinActive)
                                                  ? BADGE_SEND_HZ
                                                  : BADGE_IDLE_HZ))) {
    gLastBtnMs = millis();
    sendButtons();
  }

  // No connection to hold open: the request is the only state there is, and it
  // doubles as telling the Pi where to send. Re-asking after a gap covers a
  // lost request as well as a Pi that restarted.
  if (millis() - gLastFrameMs > kStallMs && millis() - gLastTryMs > kReconnectMs) {
    gLastTryMs = millis();
    requestFrame();          // silent: a lost request costs a re-ask, not a blank
    if (millis() - gLastFrameMs > kOutageMs && !gOutageShown) {
      gOutageShown = true;   // once per outage, not once per retry
      Serial.printf("[cam] no frames; asking %s:%u again\n", PI_IP, kStreamPort);
      banner("waiting for video", PI_IP);
      leds::setStatus(leds::Status::LinkWaiting);
    }
  }

  if (pumpVideo() && decodeFrame()) {
    gOutageShown = false;
    leds::setStatus(btn::anyDown() ? leds::Status::Flying : leds::Status::Disarmed);
    requestFrame();   // ask for the next now that this one is on the panel
  }

  if (millis() - gLastStatMs >= 5000) {
    const float secs = (millis() - gLastStatMs) / 1000.0f;
    Serial.printf("[cam] %.1f fps  %.1f KB/s  %lu B/frame\n",
                  gFrames / secs, gBytes / secs / 1024.0f,
                  (unsigned long)(gBytes / (gFrames ? gFrames : 1)));
    gLastStatMs = millis();
    gFrames = gBytes = 0;
  }
}
