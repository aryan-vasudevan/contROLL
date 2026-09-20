# HANDOFF — badge, car, camera

Hack the North 2026. Everything learned and built, including the things that
did not work and why, because those cost the most time and are the easiest to
repeat.

Read §1 and §2. The rest is reference.

---

## 1. What this is now

A Hack the North 2026 hacker badge drives an RC car over Wi-Fi and shows the
car's camera feed on the badge's screen.

It started as a drone project. **The drone is abandoned** — no indoor flying
allowed — but the hard problem was always getting button presses from the
badge to a computer on the vehicle, and that transferred across unchanged.
The MAVLink/flight-controller work is still in the repo and still builds; it is
just not what this is any more.

The target is Solana's **Best Use of Badge** track.

```
   badge (ESP32-C3)                      Raspberry Pi 5 (on the car)
 ┌────────────────────┐            ┌──────────────────────────────┐
 │ buttons ───────────┼──UDP 14555─▶ badgedrive.py ─GPIO─▶ L298N ─▶ motors
 │                    │            │                              │
 │ 320x240 screen ◀───┼──UDP 14557─┤ oak_stream.py ◀──USB── OAK-1 │
 └────────────────────┘            └──────────────────────────────┘
        joins "f450-badge", the Pi's own access point. No internet anywhere.
```

Both Pi programs run as systemd services from boot. Power the Pi, power the
badge, drive. No laptop in the loop.

---

## 2. State of play

### Works, verified on hardware

- **Driving.** Badge buttons to motors, with a 200 ms failsafe.
- **Video.** 18.8 fps at 160x120, upscaled 2x to fill the 320x240 panel,
  ~2 KB a frame, 38 KB/s.
- **Both as services.** `badgedrive` and `oak-stream` start on boot.
- **Badge self-heals.** Reconnects on its own if either end restarts.
- **135 host tests pass**; all three firmwares build with zero warnings.

### Does not work

| Thing | Why |
|---|---|
| Object detection on the OAK (`--nn`) | the camera browns out and crash-loops |
| Cloud detection (`--detect`) | the Pi has no internet |
| Badge on battery | browns out on 2xAA; confirmed by the chip's own reset reason |
| The box/HUD overlay | **written, never run.** See §7. |

### Never built

NFC, voice, anything on-chain. **There is no Solana code in this project at
all**, which is worth staring at given the track.

---

## 3. Access

| | |
|---|---|
| Wi-Fi | `f450-badge` / `dronebadge2026` — WPA2, 2.4 GHz, ch 6 |
| Pi | `ssh pi@192.168.4.1`, password `dronebadge2026` |
| Badge IP | DHCP from the Pi, usually `192.168.4.105` |
| Roboflow | workspace `dev-f8zc3`, workflow `custom-workflow-3` |

**Rotate all of these.** The Wi-Fi/Pi password and a Roboflow API key
(`ICfoZuJJQfYI2bdGe8yI`) were both typed in plaintext during the session that
produced this. A Wi-Fi password was also briefly committed to the public repo
before being rewritten out of history — treat it as compromised.

Your Mac must be joined to `f450-badge` to reach the Pi. It loses internet
while it is, because the Pi's access point has no uplink.

```bash
journalctl -u badgedrive -f     # buttons and wheel speeds
journalctl -u oak-stream -f     # fps, KB/s, bytes per frame
systemctl status badgedrive oak-stream
```

---

## 4. The badge

HTN 2026 hacker badge, ESP32-C3-MINI-1-N4. KiCad source in `hardware/`.
Hack the North publish a HAL guide at solana-htn.com; where it and our
findings overlap, they agree.

### Button map — confirmed three ways

Derived from the board file, corrected against real hardware, then confirmed
by the HAL guide's documented shift order (`A, B, Home, Down, Left, Right, Up,
Aux1`, A first).

| Bit | Net | Switch | Silkscreen |
|---|---|---|---|
| 7 | `SW_HPM` | SW6 | **A** (outer of the diagonal pair) |
| 6 | `BTN_7` | SW5 | **B** (inner) |
| 5 | `BTN_6` | SW7 | **HOME** — the firmware calls it SELECT |
| 4 | `BTN_5` | SW8 | DOWN |
| 3 | `BTN_4` | SW4 | LEFT |
| 2 | `BTN_3` | SW3 | RIGHT |
| 1 | `BTN_2` | SW2 | UP |
| 0 | `BTN_1` | SW11 | unlabelled slide switch, may be unpopulated |

`BTN_1`..`BTN_7` sit on bits 0..6 in order; `SW_HPM` on bit 7.

**The bug that cost an hour:** the firmware originally had the low nibble
reversed, assuming shift-register pins 11/12/13/14 were `D3/D2/D1/D0`. They are
`D0/D1/D2/D3`. UP read as RIGHT, RIGHT as UP, LEFT as SLIDE. The high nibble
was right, so A, B, HOME and DOWN worked and it looked half-functional.

`tests/netlist_test.py` passed throughout, because it derived the map from the
board file using the same wrong assumption as the firmware. **A test that
re-derives a fact the same way the code does proves only self-consistency.**

### The board labels its own buttons

The silkscreen names every button. It is drawn as **vector outlines, not text
objects**, so searching the board file finds nothing — an earlier version of
the repo docs concluded there were no labels and treated A/B as a guess.
Rendering the `F.SilkS` artwork shows them plainly. The guess was right.

### START is the boot strapping pin

**SW10 is silkscreened START** and is the right-hand one of the HOME/START
pair at the bottom centre. It is wired straight to GPIO9.

**Do not hold it through a reset.** GPIO9 low at reset puts the ROM into
serial download mode and the firmware never runs — a serial port enumerates,
prints a ROM banner, then goes quiet. Both firmwares select their console-only
mode from `loop()` within 5 s of startup instead: **power up normally, then
press and hold START.**

(The same behaviour is useful in reverse: hold START while plugging in USB to
force download mode if a flash ever gets stuck.)

### SW1 gates the battery, not the LEDs

U12 (LM66200) OR-es USB VBUS with the MT3608 boost output, and the 3V3 LDO
hangs off the result. **On USB the badge runs with SW1 either way; on battery
alone it must be ON.** Earlier docs said it gated the LEDs. It does not.

### Display

| | |
|---|---|
| Part | HS20HS072RX, LCSC **C5329582** |
| Controller | **ST7789T3** |
| Resolution | 320x240, RGB565, 4-wire SPI |
| Pins | CS 2, DC 0, SCLK 1, MOSI 10, RST 4 |
| Backlight | hardwired on — FPC pin 2 is the LED cathode via 10R (R38) to GND |

The repo previously called this an OLED with an unidentifiable controller.
Both wrong: the schematic's part number resolves to a datasheet, and a
backlight cathode means LCD.

**Use Adafruit_ST7789, not TFT_eSPI.** TFT_eSPI is faster and was tried first,
but its ESP32-C3 path reaches SPI peripheral registers through base pointers
that come out null on this chip — it panics inside `begin_tft_write` with a
store fault at address `0x10` before drawing anything. Adafruit's driver goes
through the Arduino SPI class, so there is nothing chip-specific to get wrong.
It costs frame rate.

HTN's HAL guide specifies `invert_color(true)`, `swap_xy(true)`,
`mirror(true, false)`. We arrived at `setRotation(1)` plus our own 180 degree
flip empirically. If colours ever look wrong, their sequence is the reference.

### NFC — populated, unused

| | |
|---|---|
| Part | MFRC522B (U7), I2C address **0x26** |
| Bus | shared with the accelerometer, GPIO 5/6 |
| Support parts | X1 27.12 MHz crystal, L2/L3 470nH, L4 22nH, R19-R22 4R4 matching |
| IRQ | broken out to test point TP6 |

The entire RF front end is populated and **running zero lines of code**.

The address was originally guessed at `0x29` from the strapping resistors. The
straps were read correctly; turning them into an address needs the chip's
pin-function names, and the schematic symbol is an EasyEDA conversion calling
them A0/A1/D1-D6 rather than ADR_n. **The HAL guide gives 0x26.** The I2C scan
in the badge firmware's diagnostics mode reports whatever actually answers.

### Other

- **Accelerometer** SC7A20 at 0x19. Driver exists with a tested shake
  detector; not compiled into the camera firmware.
- **LEDs** 6x WS2812B on GPIO3 via a level shifter, arranged as a ring.
- **No microphone. No audio hardware of any kind.** Checked the whole board.
- 327,680 bytes of RAM, no PSRAM. A 320x240 RGB565 frame is 153,600 bytes, so
  JPEG is decoded one strip at a time straight to the panel, never buffered.

---

## 5. The Pi

**It arrived running QNX**, not Linux — a workshop image (`qnx_sdp.ifs`,
`aiworkshop.tar.gz`, hostname `qnxpi68`). Everything in `firmware/pi/` assumed
Raspberry Pi OS, so none of it applied.

Reflashed to **Raspberry Pi OS Lite (Trixie) 64-bit**, checksum verified. The
QNX card is backed up at `~/qnx-card-backup` (330 MB) if anyone wants it.

**A Pi 5's USB-C port is power only.** Tested: nothing enumerates. There is no
console to plug into, unlike a microcontroller.

Headless setup went onto the card's FAT32 partition: `userconf.txt` (user
`pi`, SHA-512 hash), `ssh`, and a first-run script hooked via `cmdline.txt`
that writes the NetworkManager AP profile and removes itself. See
`firmware/pi/sd_install_ap.py`.

Two things that will waste a day if you do not know them:

- **`cmdline.txt` must stay one line.** The kernel ignores everything after
  the first newline, silently dropping every parameter past it.
- **A Wi-Fi radio with no regulatory domain will not start an access point at
  all**, and the failure looks like a bad config. The country is set three
  ways: kernel parameter `cfg80211.ieee80211_regdom=CA`, `modprobe.d`, and
  `wpa_supplicant.conf`.

### The internet problem

**The Pi has one Wi-Fi radio and it is the access point.** A single radio can
be an AP or a client, not both. Everything that wanted internet — `pip`,
Docker, the Luxonis model zoo, cloud inference — hit this.

Options, none of which were completed:

1. **Ethernet.** A second interface, so the Pi stays an AP and gets internet.
   Needs a jack; useless while the car is driving.
2. **Phone USB-tethered to the Pi.** Android works out of the box. **iPhone
   needs `usbmuxd`**, which is not on Pi OS Lite and needs internet to
   install — circular.
3. **AP and station on one radio.** The Broadcom chip supports it as a second
   virtual interface, same channel only. Untried.
4. **NAT through the Mac.** Workable, ~10 minutes of setup on both machines.

**Sideloading works and was done twice**: the `depthai` wheels and the YOLO
model were both fetched on the Mac and copied over. See §8.

---

## 6. The car

Two motors per side, wired in parallel per channel, through an L298N.

| L298N | Pi |
|---|---|
| ENA | GPIO 12 (pin 32) |
| IN1 / IN2 | GPIO 5 / 6 |
| ENB | GPIO 13 (pin 33) |
| IN3 / IN4 | GPIO 16 / 26 |
| GND | pin 39 — shared ground, not optional |

**Motors get their own battery.** The Pi's 5 V rail cannot start them, and
trying turns the Pi's LED red (undervoltage) and risks corrupting the SD card.
Only grounds are shared.

**A 360 degree turn is 3.0 s** at 60% speed, calibrated on the demo surface.
There are no encoders, so this is a duration, not an angle — it drifts as the
pack drains. Re-check with `testdrive.py --calibrate`.

Two wiring faults found, both fixed at the terminals rather than in software:

- Two motors on one side spinning opposite ways — one had its leads reversed.
  Software cannot fix this: both share OUT1/OUT2 and get identical voltage.
- The right channel dead — the ENB line.

**`drive.py` could never reach the motors** until fixed: its pin-factory check
took GPIO 5 and released it, but ran once per `Side`. Building `Car` creates
left (which then holds GPIO 5), then right, whose check finds GPIO 5 taken and
blames the pin factory. The process holding the pin was the script itself.

---

## 7. The overlay has never run

`badgecam_main.cpp` has a `BDET` packet type and a `drawBoxes()` that draws
rectangles and labels over the video. **It has never executed.** No detector
ever produced boxes that reached the badge — the OAK crashed and the cloud
path needed internet.

It was described earlier as "wire format verified". What that actually meant:
a Python script packed boxes and unpacked them with a Python re-implementation
of the C++ parsing, and the values matched. That proves the Python agrees with
itself. It does not prove the C++ parses it, and it does not prove anything
renders.

**Anything built on the overlay should start by drawing a fixed HUD with made
up numbers**, to find out whether that path works at all. Ten minutes.

---

## 8. Gotchas that cost real time

**nmcli `key-mgmt=wpa-psk` alone is not WPA2.** Without an explicit proto it
brings an access point up as **original WPA with TKIP**. An ESP32-C3
negotiates TKIP badly — it associated once, then reported `AUTH_EXPIRE` and
`AUTH_FAIL`, which reads exactly like a wrong password. Both AP scripts now
pin `proto=rsn pairwise=ccmp group=ccmp`.

**iOS Personal Hotspot isolates its clients.** Two devices on one iPhone
hotspot cannot reach each other at all, whatever their addresses say. Proven
both directions: UDP dropped, ARP failed, ICMP 100% loss despite a resolved
ARP entry.

**iOS device names use a typographic apostrophe** (U+2019, `e2 80 99`), not
ASCII. An SSID copied by eye never matches.

**An IPv6-only carrier gives macOS a 464XLAT address** (`192.0.0.2/32`, marked
`clat46`) and no LAN IPv4 at all. It looks like a normal address and is
reachable from nothing.

**ESP32 `scanNetworks()` during an active connect attempt returns zero
networks**, which reads as a dead radio when it only means a busy one.

**ESP32 refuses APs below WPA2 by default** and reports them as
`WL_NO_SSID_AVAIL` — "not found" for a network its own scan lists at -53 dBm.
`WiFi.setMinSecurity(WIFI_AUTH_WPA_PSK)` overrides it.

**JPEGDEC hands back a strip up to the full image width, not a 16x16 block.**
Sizing a scaling buffer for a block overran it and corrupted the stack,
showing up as a panic with a garbage PC.

**`inference-sdk` and `inference` require Python <3.13.** The Pi runs 3.13.5
and the Mac 3.14, so neither installs. Both wrap HTTP calls that the standard
library can make directly.

**Depthai v3 differs substantially from the v2 examples** everywhere online:
`Camera.build`/`requestOutput` and `VideoEncoder.build` rather than
`ColorCamera` and `setProfile`.

**`MessageQueue` is a pybind11 object with no `__dict__`** — you cannot hang
attributes on it.

**Everything on this project has been power.** The badge on 2xAA, the OAK
under inference load, the Pi when the motors were on its rail. Three separate
devices, one recurring cause. Be generous with supplies before debugging
software.

### Sideloading recipe

Both of these worked and are the pattern for anything else the Pi needs:

```bash
# Python wheels for the Pi, fetched on a Mac
python3 -m pip download depthai numpy --only-binary=:all: \
  --platform manylinux_2_28_aarch64 --python-version 313 \
  --implementation cp --abi cp313 -d ./wheels
# then scp, and install with --no-index --find-links

# A model from the Luxonis zoo, fetched on a Mac (needs Python 3.12)
conda create -y -p ./dai312 python=3.12 && ./dai312/bin/pip install 'depthai>=3.10,<4'
./dai312/bin/python -c "
import depthai as dai
dai.getModelFromZoo(dai.NNModelDescription('yolov6-nano', platform='RVC2'),
                    cacheDirectory='./zoocache')"
# scp to the Pi, extract into ~/.cache/depthai/models,
# then run with DEPTHAI_ZOO_CACHE_PATH=$HOME/.cache/depthai/models
```

`pip` itself is absent on Trixie and PEP 668 blocks system installs. A pip
wheel is runnable as a zip, which bootstraps it offline:
`python3 pip-*.whl/pip install --no-index --find-links=. pip`

---

## 9. Detection: everything tried, and why it failed

**The model is already cached on the Pi.** `--nn` needs no internet.

### On the OAK's Myriad X — right approach, blocked by power

`oak_stream.py --nn` runs `yolov6-nano` (512x288 RVC2 blob) on the camera.
Inference costs the Pi nothing and keeps up with the frame rate.

It crash-loops: `X_LINK_ERROR` on both streams, reconnect, crash, every ~4
seconds. Video falls to 0.2 fps and the badge shows green garbage, which is
truncated JPEGs from a device dying mid-frame.

Plain video ran at 18.8 fps on the same cable for hours. **The network is what
pushed it over.** `--nn-fps 3` did not help, which rules out compute load and
leaves power or the USB link.

- The OAK enumerates as a **high-speed (USB 2.0)** device. Many USB-C cables
  carry only the USB 2.0 pairs and sit happily in a SuperSpeed port.
- **A Pi 5 caps total USB output at 600 mA** unless on a 5 V/5 A supply with
  `usb_max_current_enable=1` in `/boot/firmware/config.txt`.

Untried and most likely to work: **a powered USB hub**, so the OAK draws from
its own supply. Also untried: a different USB-C cable, and the current cap.

### Roboflow cloud — works, needs internet the Pi does not have

The saved workflow `custom-workflow-3` returns **only a rendered image**, no
coordinates, which cannot be drawn over live video. Reading its definition
back from the API shows what it wraps:

```
rfdetr-nano, class_filter ["person"], IoU 0.5
  -> bounding_box_visualization -> label_visualization
```

`detect.py` sends **that same model as an inline workflow specification** and
asks for `$steps.model.predictions`, so nobody has to edit anything in the
editor. It is also about twice as fast, since the server no longer renders a
JPEG and base64s it back. Verified against a real photo: 16 people, correct
coordinates.

Measured from a Mac on a frame the size the badge stream sends (5.5 KB):

```
best 323 ms, median 776 ms, worst 1485 ms
```

The spread is cold starts and queueing, not compute. An enterprise GPU
endpoint would remove both — estimate 100-250 ms, consistently. Network round
trip to a US datacenter is ~20-50 ms of that and is irreducible.

**Cloud inference would also fix the crashing**, as a side effect: the OAK
goes back to only encoding video, which it does reliably.

### Roboflow local server — untried

`pip install inference-cli && inference server start` runs the same workflow
API on `localhost:9001`. `detect.py` supports it: `export
RF_URL=http://localhost:9001/infer/workflows`. Needs Docker and a 1-3 GB image
pull, then nothing. Inference then competes with video encoding and motor
control for the Pi's CPU. `detect.py --bench` exists to measure it.

### ArUco / AprilTag — not tried, and the only one that needs nothing

Classical CV, not a neural network. Milliseconds on the Pi CPU, no model, no
internet, **no load on the OAK**. A `cp313 aarch64` wheel for
`opencv-python-headless` exists (39.6 MB) and can be sideloaded.

This is the one detection path that avoids every blocker hit so far. Printed
markers as floor "coins" would work; a person detector would not.

---

## 10. Ideas discussed, not built

### NFC mode switch

Tap a QNX sticker to switch the car between manual and voice control. The
driver is the work; the address (`0x26`) and bus are known. This is also the
natural Solana hook: tap to authorise, submit a transaction, unlock the car,
show the signature on the badge.

### Voice

**The badge has no microphone.** The Mac does, and it is an M5 Pro with 24 GB.

Running speech on the Mac is architecturally right rather than a workaround:
the mic belongs where the person is, unlike the camera which had to be on the
car. It needs no Pi internet and no new hardware, and the Mac already reaches
the Pi on `f450-badge`.

```
speak -> Mac mic -> whisper.cpp (local, Metal) -> parse -> UDP 14555 -> Pi -> car
```

Transport is the existing button port; `badgedrive.py` already parses text
lines from UDP. Voice becomes another source alongside buttons, and buttons
should win when both arrive.

**`~/drone/fly.py` already has the parser** — `parse_offline()` maps "creep
forward slowly" and "spin clockwise 3s" to axes and durations. Adapting it
from quadcopter mixing to two-track steering is mostly deletion. It also
shows the right structure: a deterministic parser as the default, an LLM as an
optional upgrade.

Nothing is installed on the Mac yet: no ollama, whisper, sox, ffmpeg, or
python audio libraries. Homebrew is there.

The cost: **the laptop has to be present.** For voice that is fine.

### Game / AR

Printed ArUco markers on the floor as coins, detected by the Pi in the video
it already produces, with score and timer drawn over the live feed on the
badge. NFC sticker as start/finish. Coins minted or logged on-chain.

**NFC cannot be the collector** — the reader is on the badge in your hand, not
on the car. Only the camera can see the floor.

### HTN OS

Hack the North publish a replacement firmware that exposes every badge
peripheral over HTTP via `badge.solana-htn.com`. **NFC for free**, no driver.
Also badge-to-badge apps and an app store listing (`POST /v1/apps`).

**It cannot do the camera feed.** Their docs: a full-screen image is *"about
150 KB on the wire and takes roughly a second"*, with a 400 KB/s cap. That is
~1-2 fps against the 18.8 we have. It also needs the badge on an
internet-connected Wi-Fi, which conflicts with it being on the Pi's access
point.

Shapes and text are fast — their Pong sample runs at 10 fps with three
commands a frame. So HTN OS suits a vector-graphics game, not video.

It does not help with detection (it never touches the Pi or the camera) and it
cannot add a microphone that the hardware does not have.

---

## 11. Repo

| Path | What |
|---|---|
| `firmware/src/badgecam_main.cpp` | badge: video + buttons (`-e badgecam`) |
| `firmware/src/pilink_main.cpp` | badge: buttons only (`-e pilink`) |
| `firmware/src/main.cpp` | badge: MAVLink to a flight controller (`-e badge`) |
| `firmware/src/buttons.cpp` | 74HC165 driver, corrected bit map |
| `firmware/include/config.h` | tunables, **no credentials** |
| `firmware/include/secrets.h` | credentials, **gitignored** |
| `firmware/pi/oak_stream.py` | OAK to badge video, `--nn` / `--detect` |
| `firmware/pi/detect.py` | Roboflow detector, cloud or local server |
| `firmware/pi/badge_listen.py` | button logger, stdlib only, `--selftest` |
| `firmware/pi/wifi_ap.sh` | AP only, for a Pi you can log into |
| `firmware/pi/sd_install_ap.py` | AP via the SD card, for one you cannot |
| `pi5/drive.py` | L298N driver, one move per run |
| `pi5/testdrive.py` | four moves plus 360 calibration |
| `pi5/pintest.py` | isolates a dead motor channel |
| `pi5/badgedrive.py` | **buttons to wheels, with the failsafe** |
| `pi5/oakcam.py`, `pi5/detect.py` | teammate's OAK stack, browser MJPEG |
| `hardware/` | HTN's KiCad schematic and board |

`pi5/` and `firmware/pi/` both contain `detect.py`, `setup.sh` and
`README.md`. **They are different programs.** Do not copy both into `~` on the
Pi.

PlatformIO is at `~/.pio-venv/bin/pio`, not on PATH. It needs Rosetta — the
RISC-V toolchain is x86_64 only.

Services: `badge-listen` (superseded by `badgedrive`), `oak-stream`,
`badgedrive`. Installers sit next to each unit file.

---

## 12. What to do next

1. **Draw a test HUD over the video.** Ten minutes, and everything in the game
   idea depends on it. It has never run.
2. **Borrow a powered USB hub.** ~$15, most likely fix for the OAK, and it
   ends with detection running fully offline on a car with nothing attached.
3. **NFC**, for the Solana story. Nothing else in this project touches chain.
4. **ArUco coins**, if a game is wanted — the only CV path that needs neither
   internet nor the camera's spare power.
5. **Test the failsafe deliberately.** Drive forward, switch the badge off
   mid-move. It should stop within 200 ms. Worth having seen before the car
   runs near anyone.
6. **Rotate every credential in §3.**
7. `~/drone` — the old drone work, including `fly.py` — **is not a git
   repository.** One `.bak` file is its only backup.
