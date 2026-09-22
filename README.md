# contROLL

A Hack the North 2026 hacker badge drives an RC car over Wi-Fi, watches the
car's camera on its own screen, and can hand the wheel to a cloud vision
model: pick a person on the badge, press START, and the car drives to them
and stops on its own.

```
   badge (ESP32-C3)                      Raspberry Pi 5 (on the car)
 ┌────────────────────┐            ┌──────────────────────────────────┐
 │ buttons ───────────┼──UDP 14555─▶ badgedrive.py ──GPIO──▶ L298N ──▶ motors
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
outranks it and that silence always stops the car.

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
