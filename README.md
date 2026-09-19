# drone-hacking

Fly an F450 quadcopter from a Hack the North hacker badge over Wi-Fi.

The badge's D-pad and A/B buttons drive the aircraft. Shaking the badge calls
the drone back to a spot you marked earlier.

| | |
|---|---|
| Up, down | climb and descend |
| Left, right | yaw, or strafe, set by the slide switch |
| A, B | forward and backward |
| Shake the badge | fly to the marked spot and hold station near it |

Hold SELECT with a direction for arm, disarm, take off, land, return to launch,
and marking the spot. The full table is in [firmware/README.md](firmware/README.md).

## Where this actually stands

Read this part before you plan around it.

**Done and verified.** The firmware builds clean for the badge's ESP32-C3 with
no warnings. A host test suite of 135 checks passes, covering the MAVLink wire
format against an independent reference implementation, the geometry that keeps
the drone off the person holding the badge, the shake detector's ability to tell
a shake from walking, and the button map checked against the KiCad board file.

**The button map is confirmed.** Every role was cross-checked against the
board's own silkscreen, which is drawn as vector outlines rather than text and
so is invisible to a search of the board file. A is the outer button of the
diagonal pair, B the inner one, and the two buttons below them are HOME and
START; START is the one wired to the ESP32-C3 boot pin.

**Not verified, because it needs hardware nobody has wired up yet.** No real
flight controller has ever accepted these MAVLink frames, and the shake
threshold is tuned against synthetic motion rather than a hand.

**This has never flown.** Treat every number in `firmware/include/config.h` as a
starting point.

## Getting started

You need [PlatformIO](https://platformio.org/). The tests need only `g++` and
`python3`.

```
pip install platformio

cd firmware
pio run                  # build
pio run -t upload        # flash the badge over USB-C
pio device monitor       # serial console at 115200

./tests/run.sh           # host tests, no hardware needed
```

**The most useful first step is flashing a badge, powering it up, and then
pressing and holding BOOT within five seconds.** That lands in a diagnostics
mode which transmits nothing, so it is safe with a battery in the drone, and it
prints button names to the console as you press them. Do not hold BOOT while
resetting: SW10 shorts GPIO9 to ground, GPIO9 is the ESP32-C3's boot strapping
pin, and the ROM would go into serial download mode instead of running this.

Diagnostics answers the A/B question above, tells you whether the accelerometer
responds, and lets you feel out the shake threshold. It costs one USB-C cable
and no other hardware.

## What the drone needs

An F450 is a frame, not a flight stack. This needs a flight controller running
**ArduPilot or PX4**. Betaflight and INAV do not accept MAVLink control
commands, so they cannot be flown with this at all. Check that first.

Then something on a TELEM port bridging MAVLink onto Wi-Fi. A six dollar ESP32
running DroneBridge weighs under eight grams and is the sensible choice. A
Raspberry Pi 5 also works and is set up by [firmware/pi/](firmware/pi/), at
roughly 100 grams and a real power budget.

If you go the Pi route, [pi5/](pi5/) adds a Luxonis OAK-1 camera on the same
board: object detection running on the camera's own processor, with a live view
you can open in a browser from anywhere on the Pi's network.

One flight controller parameter matters more than all the others:

```
SYSID_MYGCS = 255
```

ArduPilot ignores manual control from any other system id. Without it the
telemetry flows, the badge shows a healthy link, and the aircraft ignores every
button. It looks exactly like a broken setup.

## Layout

| Path | What is in it |
|---|---|
| `firmware/` | the badge firmware, a PlatformIO project |
| `firmware/src/` | drivers, MAVLink codec, state machine |
| `firmware/include/config.h` | Wi-Fi, stick feel, failsafe limits, everything you edit |
| `firmware/tests/` | host tests, no hardware needed |
| `firmware/pi/` | Raspberry Pi bridge setup, if you go that route |
| `pi5/` | OAK-1 camera on a Raspberry Pi 5, vision side |
| `hardware/` | the badge's KiCad schematic and board |

`hardware/` is Hack the North's own badge design, from their public
[badge-hardware](https://github.com/hackathon/badge-hardware) repository. It is
here because the firmware's pin map was derived from it and one of the tests
reads it directly to confirm the button map still matches the board.

## Safety

This drives real propellers.

The firmware is deliberately reluctant. Nothing arms at boot. Sticks are only
sent while the aircraft's heartbeat is fresh, and two seconds of silence stops
all commanding. Arming needs a GPS fix and a two-button hold. Held buttons ramp
in over a third of a second rather than stepping to full deflection. Default
deflections are about a third of full stick. The shake gesture opens a cancel
window before anything is sent, and any button aborts it.

None of that replaces a real transmitter bound to the aircraft with a working
failsafe. Keep one in your hands. Fly somewhere legal and open. Test every
button with the propellers off first.
