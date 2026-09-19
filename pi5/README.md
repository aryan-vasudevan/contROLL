# OAK-1 on a Raspberry Pi 5

Vision for the drone. A Luxonis OAK-1 on USB, running object detection on the
camera's own processor, with the results available to the Pi and a live view you
can open from a laptop.

```
./setup.sh                    # once
source ~/oakenv/bin/activate
python3 check.py              # is the camera actually working
python3 detect.py --stream    # then open http://<pi>:8080/
```

## Your 4 GB board is fine

The neural network runs on the camera's Myriad X processor, not on the Pi. The
Pi receives finished frames and a list of boxes, draws overlays, and serves the
stream. That is light work.

So a 4 GB Pi 5 runs this exactly as well as an 8 GB one, and you can develop on
the 4 GB board with no surprises when you move to the 8 GB. Extra memory buys
you headroom for whatever else you put on the aircraft later, not frame rate.

**Power is the real constraint, not memory.** Read the next section before you
blame the software.

## You do not need the Pi to start

DepthAI ships a macOS arm64 wheel, so the camera works plugged straight into a
Mac. Everything except `setup.sh`, the udev rule and the power checks is
platform independent, and `check.py` skips the Linux-only parts rather than
failing on them.

```
python3 -m venv ~/oakenv
~/oakenv/bin/pip install depthai opencv-python
~/oakenv/bin/python check.py
~/oakenv/bin/python detect.py --stream
```

Note the explicit `~/oakenv/bin/python`. Running plain `python3` uses the
system interpreter, which has none of this installed, and Homebrew's Python is
externally managed so a plain `pip install` into it is refused.

The install needs roughly 400 MB. If pip dies partway through you get a broken
library rather than a clean failure, and the error mentions a dylib that cannot
be loaded rather than anything about disk. `check.py` recognises that case and
says so.

So: develop the detection and streaming on a laptop, move to the Pi when you
need it on the aircraft. The only thing you cannot test off the Pi is the USB
power budget, which is the section below.

## The failure everyone hits first

By default a Raspberry Pi 5 limits its USB ports to 600 mA in total. The OAK-1
pulls more than that in bursts, because the Myriad X and the 4K sensor spike
together.

The symptom is not a clean error. The camera enumerates, works for a while, then
disappears mid-stream. It reads exactly like a software crash and it is not one.

Fix it with any of:

- the official Raspberry Pi 27 W USB-C supply, which raises the limit to 1.6 A
  automatically
- `usb_max_current_enable=1` in `/boot/firmware/config.txt`, but only if your
  supply can genuinely deliver it, otherwise you have moved the brownout rather
  than fixed it
- a powered USB hub between the Pi and the camera

On the aircraft, where the Pi runs from a BEC rather than the official supply,
the hub or a BEC with real headroom is the honest answer. `check.py` reads the
current limit and tells you which side of this you are on.

## Most DepthAI tutorials online will not run

DepthAI v3 is the current release and is what this code targets. Nearly every
tutorial and Stack Overflow answer still shows v2, which built pipelines out of
`ColorCamera` and `XLinkOut` nodes and connected with `dai.Device(pipeline)`.

v3 replaced that with `Camera.build()`, output queues created directly off a
node, and models pulled by name from the Luxonis zoo. Pasting v2 code into this
project produces `AttributeError` on node types that no longer exist. If you are
reading a guide, check which API it uses before you spend an hour on it.

## Using it

```
python3 detect.py --stream                 # serve MJPEG on :8080
python3 detect.py --classes person         # only report people
python3 detect.py --json                   # one JSON object per frame
python3 detect.py --model yolov6-nano      # any model from the Luxonis zoo
python3 detect.py --show                   # local window, needs ./setup.sh --gui
```

`--json` is the integration point. Each line carries the frame sequence, the
detections, each box in normalised coordinates, its centre, and its area as a
fraction of the frame. Centre drives anything that needs to point at a target;
area is a rough proxy for distance. Pipe it into whatever comes next rather than
importing this module, and the vision side stays independent of the flight side.

To build on it directly:

```python
from oakcam import OakCamera

with OakCamera(model="yolov6-nano", classes=["person"]) as cam:
    for frame in cam.frames():
        for d in frame.detections:
            x, y = d.center
            print(f"{d.label} at {x:.2f},{y:.2f} covering {d.area:.1%}")
```

## Why MJPEG for the video

There is no monitor on a flying drone. If the Pi is already the Wi-Fi access
point for the badge, joining that network and opening a browser is the only
practical way to see through the camera.

MJPEG is deliberately unsophisticated: every frame is a standalone JPEG, so any
browser plays it with no player, plugin or JavaScript. It uses more bandwidth
than H.264 and it survives a flaky link, because a dropped frame never corrupts
the frames after it the way a dropped H.264 keyframe does. Over a marginal
wireless link that trade is worth making.

Overlays are drawn on the Pi, which is why the stream is host-encoded rather
than using the camera's hardware encoder. If you want raw video and do not care
about seeing boxes, the camera can encode H.264 itself and save the Pi the work.

## Files

| File | What it is |
|---|---|
| `setup.sh` | venv, DepthAI, udev rule, power check. Run once. |
| `check.py` | diagnoses the whole chain and says what to fix. Needs the camera. |
| `selftest.py` | logic tests that need no camera and no Pi |
| `oakcam.py` | the camera wrapper everything else builds on |
| `detect.py` | the application: detection, streaming, JSON output |
| `mjpeg.py` | the HTTP server behind `--stream` |

## What is tested and what is not

`selftest.py` passes 22 checks with no hardware attached: detection geometry
including out-of-range and inverted boxes, overlay drawing, JPEG encoding, and
the HTTP server's real behaviour over a socket, including a client joining late
and a client hanging up mid-stream.

Every DepthAI call used here was verified against depthai 3.10 by introspection,
so the API surface is right.

**Nothing here has run against a real OAK-1.** The pipeline shape follows the
Luxonis v3 examples, but frame rates, the passthrough resolution and the thermal
behaviour are all unmeasured. `check.py` is the first thing to run when the
hardware arrives, and it is written to tell you what is wrong rather than just
that something is.

## How this fits the drone

`firmware/pi/` sets the Pi up as the MAVLink bridge between the badge and the
flight controller. This runs alongside it on the same Pi. They share nothing but
the board, and the vision side deliberately has no dependency on the flight side
so that a camera problem can never take the control link down with it.
