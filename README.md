# contROLL

A Hack the North 2026 hacker badge drives an RC car over Wi-Fi, watches the
car's camera on its own screen, and can hand the wheel to a cloud vision
model: pick a person on the badge, press START, and the car drives to them
and stops on its own.

```
   badge (ESP32-C3)                      Raspberry Pi 5 (on the car)
 ┌────────────────────┐            ┌──────────────────────────────────┐
 │ buttons + NFC ─────┼──UDP 14555─▶ badgedrive.py ──GPIO──▶ L298N ──▶ motors
 │                    │            │      ▲                           │
 │ 320x240 screen ◀───┼──UDP 14557─┤ oak_stream.py ◀──USB─── OAK-1    │
 │   live video       │            │      │                           │
 │   + detections     │            │      ▼ (via a phone hotspot)     │
 └────────────────────┘            │  Roboflow cloud inference        │
   joins "f450-badge",             └──────────────────────────────────┘
   the Pi's own access point
```

No laptop anywhere: the Pi is simultaneously the badge's Wi-Fi access point
*and* a client of a phone hotspot on the same radio, which is how a car with
no other connectivity runs cloud inference while driving. It heals that
uplink by itself, from boot.

## Driving

| | |
|---|---|
| UP / DOWN / LEFT / RIGHT | drive; hold two for an arc |
| A (held with a direction) | boost |
| B (held with a direction) | crawl, for lining something up |
| HOME held | stop, latched until everything is released |
| tap an NFC tag | power-up: +0.05 on the base speed for ten seconds |
| silence | the failsafe: 200 ms without a badge packet cuts the motors |

## The vision loop

The car's camera streams MJPEG to the badge at ~15 fps while the Pi feeds
the same frames to a cloud detector (Roboflow, `rfdetr-nano`). Boxes come
back with stable per-person identities, velocity-predicted so they ride on
people rather than trailing them by the network round trip.

| | |
|---|---|
| tap HOME | cycle through the people on screen; the chosen one turns yellow |
| START | chase: the box turns red, the car turns toward them, drives, and stops itself at ~2 m |
| any direction | instant human override, always |
| A, tapped | record; tapping again stops, converts to `.mp4`, and uploads to Supabase with a public link |
| B, tapped | honk (USB speaker with synthesized effects if attached, motor-coil horn otherwise) |
| shake the badge | the car spins a full 360 |

The chase commits: it survives detector dropouts and identity churn,
re-locks onto the chosen person, seeks briefly if blinded, and only then
gives up. The LED ring runs a spinning Solana-palette snake.

## Tap to power up

The badge's MFRC522 is populated on the board and, until now, ran no code at
all. `firmware/src/nfc.cpp` drives it over the I2C bus the accelerometer
already uses: REQA, then anticollision through as many cascade levels as the
UID needs, which is everything required to answer *which tag is this*. No
authentication and no memory reads — the UID identifies a sticker, and every
extra command is another thing to get wrong in a loop that is also decoding
JPEG.

A tap throws `POWER UP!` across the video, flashes the LED ring, and rides
out to the Pi as `nfc=`/`nfcseq=` appended to the button line already being
sent. The Pi reads a change in that counter as one tap and adds 0.05 to the
base speed for ten seconds. It moves the *base* only, so A still boosts and
B still crawls: those are the two speeds you reach for when something is
about to go wrong. Tapping again restarts the clock rather than stacking, so
the car cannot be walked up to a speed it will not steer at.

Three things were worth getting right, and all three are in the tests:

- **A tap is an arrival, not a presence.** Holding the badge against a
  sticker reads it every poll; only the first counts, and the same tag has
  to leave the field for 1.5 s before it can count again.
- **A restart is not a tap.** The badge keeps counting across a restart of
  the Pi script, so the first counter value seen is adopted rather than
  acted on.
- **The reader is at `0x26`**, not the `0x29` its strapping resistors
  predict — the straps were read correctly, but turning them into an address
  needs pin-function names and the schematic symbol is an EasyEDA conversion
  that lacks them. `begin()` sweeps `0x20`–`0x2F` and believes whatever
  answers. It also accepts version bytes outside NXP's documented
  `0x91`/`0x92`, because the part on this badge reports `0x82`.

**The driver has never seen a real tag.** The Pi half is tested end to end;
the badge half compiles and is unproven.

## Getting started

You need [PlatformIO](https://platformio.org/) for the badge firmware. The
host tests need only `g++` and `python3`.

```
cd firmware
pio run -e badgecam              # build the camera badge firmware
pio run -e badgecam -t upload    # flash over USB-C

./tests/run.sh                   # full host suite, no hardware needed
```

On the Pi, `firmware/pi/install_stream.sh` and `pi5/install_badgedrive.sh`
install both halves as systemd services that start on boot. Credentials
(Roboflow API key, Supabase) live in `/etc/default/oak-stream` on the Pi and
are never committed.

Useful without any hardware attached:

```
python3 firmware/pi/badgetest.py    # boxes on the badge: no camera, no cloud
python3 firmware/pi/detect.py --bench frame.jpg   # is the cloud keeping up?
bash firmware/pi/uplink.sh          # can this Pi reach the internet at all?
```

## What is tested

The suite covers the pieces whose failure is physical: the detection wire
format (the badge's real C++ parser, compiled under ASan, against bytes the
Pi's real packer produced), the tracker's identity-keeping across stalls and
out-of-order replies, the chase controller's symmetry and stop conditions,
the shake detector against walking and knocking, and the whole autopilot
end-to-end over real sockets — including that a hand on the controls always
outranks it, that silence always stops the car, and that a tag tap boosts it
exactly once and then lapses.

Each power-up check was confirmed to fail against a deliberately broken
build before being kept. The one bug in this project's history that cost the
most was a test that re-derived a fact the same way the code did and so
proved only that the code agreed with itself.

## History

This began as a badge-controlled F450 quadcopter; indoor flying was not
allowed, so the drone was abandoned and the badge-to-vehicle link carried
over to a car. The MAVLink firmware still builds (`pio run -e badge`) and
its story, along with every dead end this project hit and why, lives in
`HANDOFF.md` in the git history (removed from the tip for submission) — read
it before changing anything:

```
git show 46359d7:HANDOFF.md
```
