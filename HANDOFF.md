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
- **Host tests pass**; all three firmwares build with zero warnings. The suite
  now also covers the detection wire format across the language boundary, the
  tracker, the chase controller, and the whole autopilot end to end
  (`firmware/tests/run.sh`).

### Does not work

| Thing | Why |
|---|---|
| Object detection on the OAK (`--nn`) | the camera browns out and crash-loops |
| Cloud detection (`--detect`) | **the code now keeps up; the Pi still has no internet.** See §9 |
| Badge on battery | browns out on 2xAA; confirmed by the chip's own reset reason |
| The box/HUD overlay | written, and now tested on a host, but **never run on a badge.** See §7 |
| Autonomous chase | written and tested end to end on a host. **Never run on a car.** See §13 |
| Shake, horn, recording | on the badge and booting; the accelerometer answers. Horn and recording **never run on the car.** See §15 |

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
| Roboflow (Aryan) | workspace `dev-f8zc3`, workflow `custom-workflow-3`. **No GPU tier** |
| Roboflow (Aarnav) | workspace `aarnavs-space`, `aarnav.shah.12@gmail.com`. This is the account with GPU access, and the one the dedicated deployment lives on |
| Dedicated GPU | `badge-car.roboflow.cloud`, `prod-gpu`, created 2026-09-19, **expires 24 h later and deletes itself**. `roboflow deployment create badge-car -m prod-gpu -e aarnav.shah.12@gmail.com --duration 24` recreates it |

Two Roboflow accounts, and the difference matters: the dedicated-deployment
API validates the creator's email against the workspace **the API key belongs
to**. Using Aryan's key with Aarnav's email fails with a message about the
email, which reads as the email being wrong when in fact it is the key.

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

On boot the Arduino core logs `spiAttachMISO(): SPI Does not have default pins
on ESP32C3!`. **It is harmless and pre-dates all of this**: the panel is
write-only so `gSpi.begin()` is given `-1` for MISO deliberately, and the core
complains about it every time. The line to look for immediately after is
`[cam] panel 320x240`, which means the display came up.

**Use Adafruit_ST7789, not TFT_eSPI.** TFT_eSPI is faster and was tried first,
but its ESP32-C3 path reaches SPI peripheral registers through base pointers
that come out null on this chip — it panics inside `begin_tft_write` with a
store fault at address `0x10` before drawing anything. Adafruit's driver goes
through the Arduino SPI class, so there is nothing chip-specific to get wrong.
It costs frame rate.

HTN's HAL guide specifies `invert_color(true)`, `swap_xy(true)`,
`mirror(true, false)`. We first arrived at `setRotation(1)` plus our own 180
degree software flip empirically. If colours ever look wrong, their sequence
is the reference.

**Corrected on hardware, 2026-09-20.** The first time text was drawn over
live video (the overlay's labels and HUD), every glyph came out upside down --
which exposed what the old combination really was: rotation 1 puts the
panel's origin at the physical bottom-right of the badge as held, and the
software flip un-flipped the *video only*, leaving all text inverted and
nobody the wiser. It is now `setRotation(3)` with `CAM_ROTATE_180 0`: same
net video orientation, text the right way up, and the full-buffer pixel
reversal is gone from the decode path. If video is ever upside down again,
fix it at ONE of those two places, never both -- two 180s cancel and look
like neither works.

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

- **Accelerometer** SC7A20 at 0x19. **Confirmed on hardware 2026-09-19**: it
  answers, and `WHO_AM_I` reads `0x11`, which is exactly the SC7A20 value the
  driver expects. Previously this was read off the schematic and assumed. It is
  now compiled into the camera firmware and shaking the badge honks (§15).
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
   virtual interface, same channel only. **Tried on 2026-09-20: IT WORKS.**
   `sta0` added with `iw dev wlan0 interface add`, joined an iPhone hotspot
   (Maximize Compatibility on, so 2.4 GHz -- it landed on channel 6, already
   the AP's channel) while `f450-badge` kept serving the badge. Cloud
   detection ran over it end to end. Two costs, both real: some airtime is
   shared three ways on one channel, and iOS picks its hotspot channel
   itself -- if it drifts off the AP's, the join fails until the AP moves to
   match (`ap_sta.sh` does this automatically). Two dead ends proven the
   same night: iOS *Bluetooth* tethering pairs, leases an address, and
   forwards nothing to Linux; and full-rate detection uploads through the
   shared radio + cellular uplink starved the video to under 1 fps --
   throttle to ~5/s (`--detect-interval 0.2`).
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

### Since then

The parser is now lifted out of `badgecam_main.cpp` verbatim by `extract.py`,
compiled on a host with the address sanitizer on, and fed bytes that
`oak_stream.py`'s **real** packer produced -- including truncated datagrams,
a count that overruns the buffer, a label length running off the end, and the
old `BDET` packets, which it must reject. It recovers the values that went in.

That is a genuine round trip: nothing in the loop is a reimplementation of
anything else in it. It closes the specific hole described above.

It still does not prove anything renders. **The panel has never drawn a box.**
The ten-minute test above is still the next thing to do, and it is now the
only untested link left in that chain.

`firmware/pi/badgetest.py` is that test, made runnable. It stands in for
oak_stream.py -- answers the badge's frame requests with a small built-in
JPEG and sends invented detections in the real wire format -- so the overlay
can be seen working with **no OAK, no internet, no Roboflow and no car**:

```bash
sudo systemctl stop oak-stream
python3 badgetest.py
```

    green rectangles with labels     the overlay works
    a status line along the bottom   the HUD works
    B + LEFT/RIGHT recolours one     selection works, and the badge is
                                     reporting it back

The boxes move by default, because a box painted once in the right place and
never erased looks identical to one being redrawn correctly every frame.

It also prints the frame rate it is serving, and that is the honest answer to
"what is the frame rate with boxes drawn": the badge asks for every frame, so
the rate it serves is the rate the badge is managing, decode and overlay
included. Nobody has run it yet -- that number has never been measured.

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

#### The conclusion drawn from those numbers was wrong

They were read as "about 3 fps, so the cloud cannot do live video". That does
not follow, and the mistake is worth keeping written down because it is easy
to make twice: **it measured latency and assumed throughput was its
reciprocal.** Latency is how long one request takes. Throughput is how many
can be in flight at once. Nothing about an HTTP API says you may only have
one, and `detect.py` only had one because it was written as a loop.

Re-measured against the same serverless endpoint, same key, same model, on a
160x120 frame:

```
one at a time     p50 307 ms   ->   3.3 fps
4 in flight       p50 224 ms   ->  16.3 fps
8 in flight       p50 174 ms   ->  41.9 fps
```

and again on a busier frame, eight people in it, to be sure it was not an
artefact of an empty picture:

```
one at a time     p50 276 ms   ->   3.6 fps
8 in flight       p50 203 ms   ->  36.6 fps
```

The badge shows 18.8 fps. **Serverless already clears that by about 2x**, on
the free path, before any GPU is involved. `detect.py` is now a pool of eight
workers and infers on every frame it is given.

Then the same thing measured through the shipped code rather than a benchmark
-- `detect.py --soak`, which feeds the real `Detector` at the real frame rate
exactly as the video loop does, with the real tracker behind it:

```
offered      376 frames  (18.8 fps)
inferred     374         (18.7 fps)
skipped        0         (all workers busy)
failed         0
latency      p50 146 ms   p90 160 ms
detections   8-8 per frame, 8 distinct track ids, 0 id churn
```

**99% of frames, and a p90 of 160 ms against the 472 ms the one-at-a-time
bench showed.** The tail collapsed because a busy pool never goes cold: the
thing the dedicated GPU was wanted for turns out to be mostly fixed by
keeping eight requests in flight. Run `--soak` from the Pi on the day's
network before trusting any of this, since these numbers are from a laptop on
campus Wi-Fi.

What a dedicated GPU deployment buys is therefore not throughput, which is
already there, but the *tail*: p90 was 472 ms against serverless, and that
tail is cold starts and queueing on shared capacity. A dedicated endpoint is
also billed by the hour rather than by the call, which matters a lot once
every frame is a call — see §14.

#### Resolution: 160x120 is enough

Worth checking before building on it, since the stream is a quarter of the
panel's resolution and detections at that size might simply have been empty.
Same model, same photo, shrunk:

```
close person      160x120  1 person   conf 0.97      (full res: 0.98)
two people        160x120  2 people   conf 0.94      (full res: 0.95)
busy street       160x120  8 people                  (320x240: 9, full: 10)
```

So the frame the badge already receives is the frame to infer on. No second,
larger stream is needed, and the boxes come back in exactly the coordinates
the overlay already expects.

One caveat, found the hard way: a first attempt used a dark night-time street
where the people were a few pixels tall, got zero detections at every size
below 512 wide, and nearly became a finding about resolution. It was a
finding about that photograph. **Test with the picture the camera will
actually see** — a car's-eye view of people a few metres away.

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
| `firmware/pi/detect.py` | Roboflow detector, **a pool**, cloud or local server |
| `firmware/pi/track.py` | stable ids for detections, so a target can be chased |
| `firmware/pi/uplink.sh` | can this Pi reach the cloud while still being the AP |
| `firmware/pi/badgetest.py` | boxes on the badge with no camera, no internet, no car |
| `firmware/pi/badge_listen.py` | button logger, stdlib only, `--selftest` |
| `firmware/pi/wifi_ap.sh` | AP only, for a Pi you can log into |
| `firmware/pi/sd_install_ap.py` | AP via the SD card, for one you cannot |
| `pi5/drive.py` | L298N driver, one move per run |
| `pi5/testdrive.py` | four moves plus 360 calibration |
| `pi5/pintest.py` | isolates a dead motor channel |
| `pi5/badgedrive.py` | **buttons to wheels, with the failsafe**, and the autopilot |
| `pi5/chase.py` | visual servoing: box on screen to wheel speeds |
| `pi5/honk.py` | a horn played on the drive motors, since nothing here has a speaker |
| `firmware/pi/record.py` | video plus a detection sidecar, written to disk |
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
8. **Get an uplink onto the car** if the cloud path is wanted — §14. An
   Android phone USB-tethered to the Pi is the whole answer and needs no
   laptop.

---

## 13. Chasing a detection

Written, tested on a host, **never run on a car.** Read §12.5 first: the
failsafe is what stops this thing.

```
oak_stream.py ──BDT2──▶ badge          pick a target, draw it, say which
              └─BDT2──▶ badgedrive.py  chase whichever the badge says
badge ────────BADGE1 ... sel=N auto=1──▶ badgedrive.py
```

**The badge owns the selection**, not the Pi. What is highlighted on the
panel and what the car is driving at are then the same variable, and the
operator can see it. The Pi is told; it does not decide.

| | |
|---|---|
| `B` + `LEFT`/`RIGHT` | step through the detections on screen |
| `B` + `A` | engage or cancel the chase |
| anything else | manual, and manual always wins |

`B` is the chord key because it is already the careful button (crawl), and
nothing here should ever happen by accident.

### How it steers

There is no range finder, no encoder and no map, so both questions are
answered from the rectangle:

- **which way** — the box centre's offset from the frame centre
- **how far** — the box *height* as a fraction of the frame

Height, not width: a person's width changes when they turn or move their
arms, their height barely changes until they are close enough that the frame
crops them — and a cropped box reading as "very close" is exactly right.

Off centre by more than about a third of a half-frame it pivots on the spot
rather than arcing, because a two-track chassis arcs badly at low speed. It
tapers the approach so it arrives slowly, and stops at 55% fill. It never
commands below 0.28 duty, because an L298N and a gearbox below roughly a
quarter duty sit and buzz — a command the car ignores is worse than none.

### Things that had to be got right

**Stale detections are the hazard.** Boxes are 200-300 ms old when they
arrive. Autonomous speed is capped at 0.45 against a human's 0.60 for exactly
that reason: every bit of speed turns that staleness into distance.

**Ids, not list positions.** A detector returns boxes in whatever order it
likes; "the second one" is a different person from one frame to the next.
`track.py` associates by IoU and hands out ids that survive, and the id is
what gets selected and chased.

**Replies overtake each other.** Eight requests in flight means a slow one
for frame 100 can land after a fast one for frame 104. Applying it walks the
boxes backwards in time. Every frame carries a sequence number and the
tracker drops anything older than what it already applied. This is invisible
in a still scene and obvious the moment anything moves.

**The failsafe nearly killed the feature.** The Pi cuts the motors after
200 ms of silence; an idle badge sends every 500 ms. Nobody holds a button
during a chase — so the first version was stopped by its own failsafe three
times a second. The badge now treats `auto` as activity and sends at 20 Hz
while chasing. The failsafe is unchanged and still the primary way the car
stops.

**A second socket is not a second heartbeat.** `badgedrive.py` waits on the
button port and the box port together. The first version treated "select()
returned without the button socket" as silence — so every detection packet
arriving between badge packets looked like a dead link and cut the motors,
and the car stuttered precisely when it had something to chase. The failsafe
is now a deadline on the clock. `tests/autopilot_test.py` caught this, which
is the argument for that test existing.

### What is actually proven

`firmware/tests/autopilot_test.py` runs the real `badgedrive.py` and speaks
to it over real sockets. It shows the car chases only when told, that a held
direction beats the autopilot, that HOME stops it, that losing the selected
id stops it rather than switching to someone else, and that silence stops it
mid-chase.

None of that involves a motor, a camera, a badge or a person. **Wheels off
the ground for the first try**, and test the failsafe deliberately before it
runs near anyone.

---

## 14. What the cloud path costs, and how it gets online

### Credits

`rfdetr-nano` on the serverless API is **0.125 credits per 1,000 images**. A
dedicated GPU deployment is **1 credit/hour**, flat, however many frames go
through it.

At the badge's 18.8 fps that is 67,680 images an hour:

| | credits/hour |
|---|---|
| serverless, every frame | **~8.5** |
| dedicated GPU, every frame | **1** |
| serverless, 2 fps | ~0.9 |

**Break-even is about 2.2 fps.** Above that the dedicated GPU is cheaper, and
it is also the faster one — so for inferring on every frame it wins on both
counts, which is not the usual shape of this trade. Serverless only wins if
detections are deliberately throttled (`--detect-interval`).

A dedicated deployment is `roboflow deployment add <name> -m prod-gpu`, and
lands at `https://<name>.roboflow.cloud`. It runs the same inference server,
so nothing in the code changes:

```bash
export RF_URL=https://<name>.roboflow.cloud/infer/workflows
```

`dev-gpu` is the cheap ephemeral tier and can be evicted; `prod-gpu` has
guaranteed capacity.

### Turning it on, on the Pi

Everything reads from the environment, so this is the whole change:

```bash
sudo tee /etc/default/oak-stream <<'EOF'
RF_API_KEY=<aarnav's key, from app.roboflow.com -> Settings -> API Keys>
RF_URL=https://badge-car.roboflow.cloud/infer/workflows
OAK_ARGS=--detect
EOF
sudo systemctl restart oak-stream
journalctl -u oak-stream -f
```

The key lives only in that file, never in the repo. Drop `RF_URL` to fall
back to serverless, which also keeps up (see §9). Then:

```bash
./uplink.sh frame.jpg          # is there a route out at all, and how fast
python3 detect.py --soak f.jpg # does the pool hold the frame rate here
```

Watch the stream's own line — it prints `detect N/s p50 .. p90 .. skipped ..`
every five seconds. If `skipped` climbs, raise `--detect-workers`. The workspace `dev-f8zc3` already has dedicated
deployments enabled (`allowCustomPythonDedicatedDeployments: true`).

### The uplink — and whether a laptop has to be carried

**No laptop is needed. An uplink on the car is.**

The constraint from §5 has not moved: the Pi has one Wi-Fi radio and it is
the access point, so internet has to arrive on a *second* interface. The
badge never needs internet — it only ever talks to the Pi.

| | works untethered? | |
|---|---|---|
| **Android phone, USB to the Pi** | **yes** | RNDIS, plug and play. The phone rides on the car |
| iPhone, USB to the Pi | yes, after setup | needs `usbmuxd`, absent on Pi OS Lite and needing internet to install — sideload the .deb |
| Ethernet | no | fine on a bench, useless while driving |
| Mac sharing its connection | no | this is the carry-the-laptop option |
| AP + station on one radio | untried | same channel only, and venue Wi-Fi usually has a captive portal and client isolation |

`firmware/pi/uplink.sh` checks the whole chain on the Pi — that something is
still in AP mode, that the default route is *not* the AP, that DNS resolves,
that Roboflow answers — and then times a real inference call. Run it before
assuming the cloud path works somewhere new.

**The path that needs no uplink at all is still `--nn`**, detection on the
camera, which needs a powered USB hub to stop browning out (§9, §12.2). If
the demo has to work with nothing but the car and the badge, that is the one
to fix.

---

## 15. Shake, horn, recording

All three written and host-tested. **None has run on hardware.**

| | |
|---|---|
| `B` + `UP` | start or stop recording |
| `B` + `DOWN` | honk |
| shake the badge | honk, with no free hand needed |

### The horn, and why it is played on the motors

**Nothing in this project has a speaker.** §4 says it plainly -- the badge has
no audio hardware of any kind, the whole board was checked -- and nothing was
added to the car either. So the horn is played on the only things already
being driven.

A brushed motor on an H-bridge is a coil, and PWM makes it vibrate at the
switching frequency. `drive.py` has always noted that its 1 kHz carrier is
"audible but harmless"; `honk.py` moves that carrier into the audio range on
purpose and the motors play notes.

Sound without movement comes from duty cycle. Torque follows duty, and the
tone duty is 8% against the 85% `KICK_SPEED` these gearboxes need to break
loose -- the coils buzz, the wheels stay put. Raise `--duty` and it gets
louder and eventually creeps, so do that with the wheels off the ground.

**It never blocks.** A horn that held the drive loop for a second would be a
car carrying on at its last commanded speed while playing a tune, because
that loop is where the 200 ms failsafe lives. The tune runs on its own thread
and waits on a cancel event rather than sleeping, and any real drive command
takes the motors back through `yield_to_drive()` -- which deliberately does
*not* zero the pins on the way out, since that would countermand the drive
command being issued in the same breath.

The car also plays `found` when a chase arrives. That is the one moment the
operator cannot read off the badge, because the picture looks the same as it
did a second earlier.

### Shake

The SC7A20 driver and its shake detector have existed and been host-tested
since the drone firmware (`tests/shake_test.cpp`); they had simply never been
compiled into the camera badge. `platformio.ini` now includes `imu.cpp` in the
`badgecam` environment.

Flashed and booted 2026-09-19, and the chip is really there:

```
[cam] boot, reset now: 0, previous boot: 0
[cam] accelerometer at 0x19, WHO_AM_I 0x11; shake to honk
[cam] panel 320x240
```

`reset now: 0` is a clean power-on -- not a brownout (9) and not a panic (4).

The detector needs several threshold crossings inside a window, which is what
separates a deliberate shake from the badge swinging on a lanyard while
somebody walks, and it has a cooldown so leaning on it cannot machine-gun the
horn. A badge whose accelerometer does not answer still works: `present()` is
false and shaking does nothing.

### Recording

Two files per take, sharing a name:

```
2026-09-19_2311.mjpeg    the frames, byte for byte as the badge got them
2026-09-19_2311.jsonl    one line per frame: time, and every box with its id
```

Concatenated JPEGs, because the frames already exist in that form -- nothing
is decoded and nothing re-encoded, which matters on a Pi with no hardware JPEG
encoder that is also driving motors. `ffmpeg -f mjpeg -r 18.8 -i take.mjpeg
take.mp4` converts it.

The sidecar is the part worth having. A video of a demo is nice; a video with
the detections aligned to it frame by frame is the only way to work out
afterwards why the car drove at the wrong person.

Writing is on its own thread with a shallow queue: if the SD card stalls, the
recording loses frames and the live feed never notices. The archive never
outranks the badge.

### The bug worth knowing about

Recording lives in `oak_stream.py`, next to the camera, but the badge only
ever talks to `badgedrive.py` -- so the toggle is relayed over loopback to the
stream's frame port. That port is also how the stream learns where to send
video: **whoever sends a frame request becomes the video destination.**

A control packet mistaken for a frame request would therefore point the entire
stream at the Pi itself and leave the badge blank. Control packets are marked
`C:` and classified before anything else, and `control_command()` is split out
as a pure function with a test on it -- including the case that makes the
prefix necessary at all, which is that `REC1` also begins with `R`.

`rec=` is sent as a level rather than an edge, so a lost packet cannot leave
the badge and the Pi disagreeing about whether tape is rolling.

### Still not built

From §10, and unchanged: **NFC**, **voice**, **ArUco coins**, and anything
on-chain. There is still no Solana code in this project.

---

## 16. Reflashing without losing what works

The badge is the one thing here with no source of truth but itself, so before
any reflash there is a full image of it:

```
~/badge-backup/badge-full-<stamp>.bin      all 4 MB, read off the chip
~/badge-backup/badge-full-<stamp>.sha256   so a restore can be trusted
~/badge-backup/restore.sh                  puts it back, verifying first
```

The dump is the **whole** flash -- bootloader, partition table, app and the
stored Preferences -- so restoring brings the badge back byte for byte, not
merely to "a working firmware". Taking a new one is one command:

```bash
~/.pio-venv/bin/python ~/.platformio/packages/tool-esptoolpy/esptool.py \
  --port /dev/cu.usbmodem2101 -b 921600 read_flash 0 0x400000 badge-full.bin
```

The image captured on 2026-09-19 was verified to be the **pre-overlay**
badgecam: it contains `BADGE1 seq=` and none of `CHASE #`, `shake=`,
`accelerometer` or `B+L/R pick`. So it is a real rollback point and not a copy
of the thing being tested.

### A bad flash is never fatal

GPIO9 is the START button and is the ROM's boot strapping pin (§4). **Hold
START while plugging in USB** and the chip enters serial download mode
regardless of how broken the flashed app is. That is the same behaviour that
stops the firmware running if START is held through a reset -- the failure
mode and the rescue are the same mechanism.

### The badge and the Pi can be updated separately

Checked rather than assumed, in both directions:

```
new badge line -> old Pi code : parses, buttons ['UP', 'A']
old badge line -> new Pi code : parses, buttons ['UP', 'A']
```

`sel/auto/shake/rec/honk` are **appended** after `down=[...]`, and both regexes
stop at the closing bracket, so neither end is confused by the other's
version. And the detection magic moved from `BDET` to `BDT2` precisely so a
mismatched pair shows *no boxes* rather than boxes in the wrong places (§9).

So flashing the badge alone leaves driving and video working exactly as they
do now; only the overlay needs both ends updated. There is no big-bang
deployment and no moment where the demo is half-broken.

### Credentials survive a reflash

`firmware/include/secrets.h` is gitignored and lives on the Mac, and the
values are compiled in at build time. Verified present and correct:
`WIFI_SSID` 10 chars, `WIFI_PASS` 14 chars -- `f450-badge` / `dronebadge2026`
as in §3. Nothing to re-enter on the badge, which stores no credentials of its
own.
