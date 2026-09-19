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
constexpr uint32_t kReconnectMs  = 3000;
constexpr uint32_t kStallMs      = 4000;

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
  if (gScale == 1) {
    gTft.drawRGBBitmap(block->x + gOffsetX, block->y + gOffsetY,
                       block->pPixels, block->iWidth, block->iHeight);
    return 1;
  }

  // Nearest-neighbour expansion. The camera sends a quarter of the panel's
  // pixels because decode time is what limits the frame rate, and duplicating
  // pixels here is far cheaper than decoding four times as many. The picture
  // is softer; that is the trade being made deliberately.
  const int sw = block->iWidth, sh = block->iHeight;
  const int dw = sw * gScale;

  // Never trust the decoder's block geometry against a fixed buffer. Drawing
  // one strip unscaled is a visible glitch; running off the end of this array
  // is a reboot.
  if ((size_t)dw * sh * gScale > kMaxStripPx) {
    gTft.drawRGBBitmap(block->x + gOffsetX, block->y + gOffsetY,
                       block->pPixels, sw, sh);
    return 1;
  }
  for (int y = 0; y < sh; y++) {
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
  gTft.drawRGBBitmap(block->x * gScale + gOffsetX, block->y * gScale + gOffsetY,
                     gScaled, dw, sh * gScale);
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
  gOffsetX = (gTft.width()  - iw * scale) / 2;
  gOffsetY = (gTft.height() - ih * scale) / 2;

  gJpeg.setPixelType(RGB565_LITTLE_ENDIAN);
  gJpeg.decode(0, 0, 0);
  gJpeg.close();

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
    if (held[0]) strncat(held, " ", sizeof(held) - strlen(held) - 1);
    strncat(held, btn::name(b), sizeof(held) - strlen(held) - 1);
  }
  char line[192];
  const int n = snprintf(line, sizeof(line),
                         "BADGE1 seq=%lu ms=%lu raw=0x%02X down=[%s]",
                         (unsigned long)++gSeq, (unsigned long)millis(),
                         btn::rawRegister(), held);
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

  // Panel first, before anything that can fail for network reasons.
  // The panel is 240x320 in its native portrait orientation; rotation 1 puts
  // it in the 320x240 landscape the camera feed is sized for.
  gSpi.begin(PIN_DISP_SCLK, -1 /* no MISO */, PIN_DISP_MOSI, PIN_DISP_CS);
  gTft.init(240, 320);
  gTft.setSPISpeed(80000000);   // 40 MHz was costing ~15 ms a frame
  gTft.setRotation(1);
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
  if (millis() - gLastBtnMs >= (uint32_t)(1000 / (btn::anyDown() ? BADGE_SEND_HZ
                                                                 : BADGE_IDLE_HZ))) {
    gLastBtnMs = millis();
    sendButtons();
  }

  // No connection to hold open: the request is the only state there is, and it
  // doubles as telling the Pi where to send. Re-asking after a gap covers a
  // lost request as well as a Pi that restarted.
  if (millis() - gLastFrameMs > kStallMs && millis() - gLastTryMs > kReconnectMs) {
    gLastTryMs = millis();
    Serial.printf("[cam] no frames; asking %s:%u again\n", PI_IP, kStreamPort);
    banner("waiting for video", PI_IP);
    requestFrame();
    leds::setStatus(leds::Status::LinkWaiting);
  }

  if (pumpVideo() && decodeFrame()) {
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
