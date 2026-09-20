#!/usr/bin/env python3
"""
Tests that need no camera and no Pi.

    python3 selftest.py

Covers the parts that are pure logic: detection geometry, the overlay drawing,
JPEG encoding, and the MJPEG server's HTTP behaviour. Anything that needs the
camera itself is covered by check.py instead, which does need hardware.

Run this after changing oakcam.py, mjpeg.py or detect.py and before putting a
build on the aircraft.
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from detect import draw                       # noqa: E402
from mjpeg import MjpegServer                 # noqa: E402
from oakcam import Detection, Frame, OakCamera, OakCameraError, list_devices  # noqa: E402
from automode import AUTO, MANUAL, Box, ModeSwitch, follow  # noqa: E402

failures = 0


def check(ok: bool, msg: str) -> None:
    global failures
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        failures += 1


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def make_frame() -> Frame:
    img = np.full((480, 640, 3), 40, np.uint8)
    cv2.circle(img, (320, 240), 90, (80, 160, 220), -1)
    return Frame(bgr=img, sequence=1, timestamp=1.0, detections=[
        Detection("person", 0, 0.93, 0.30, 0.18, 0.70, 0.95),
        Detection("backpack", 3, 0.61, 0.05, 0.02, 0.28, 0.40),  # touches the top edge
    ])


# --- geometry --------------------------------------------------------------
section("detection geometry")

d = Detection("person", 0, 0.91, 0.25, 0.10, 0.75, 0.90)
check(d.pixel_box(640, 480) == (160, 48, 480, 432), "box converts to pixels")
check(tuple(round(c, 3) for c in d.center) == (0.5, 0.5), "centre is the box midpoint")
check(round(d.area, 3) == 0.4, "area is the box fraction of the frame")

# Networks do emit boxes that run off the edge; they must not index out of frame.
wild = Detection("x", 1, 0.5, -0.3, -0.2, 1.4, 1.9)
check(wild.pixel_box(640, 480) == (0, 0, 640, 480), "out-of-range boxes clamp to the frame")

tiny = Detection("x", 1, 0.5, 0.5, 0.5, 0.5, 0.5)
check(tiny.area == 0.0, "a zero-size box has zero area")

inverted = Detection("x", 1, 0.5, 0.8, 0.8, 0.2, 0.2)
check(inverted.area == 0.0, "an inverted box reports zero area, not negative")

# --- no hardware -----------------------------------------------------------
section("behaviour with no camera attached")

if list_devices():
    print("  --   a camera IS attached; skipping the no-device check")
else:
    try:
        OakCamera().start()
        check(False, "should refuse to start with no camera")
    except OakCameraError as exc:
        check("no OAK device" in str(exc), "fails with an actionable message")
    except Exception as exc:
        check(False, f"raised {type(exc).__name__} instead of OakCameraError")

# --- drawing ---------------------------------------------------------------
section("overlay drawing")

frame = make_frame()
before = frame.bgr.copy()
draw(frame, 27.4)
check(not np.array_equal(before, frame.bgr), "draw() marks the frame")
check(frame.bgr.shape == before.shape, "draw() preserves frame dimensions")
check(frame.bgr.dtype == np.uint8, "draw() preserves dtype")
# A box against the top edge must keep its caption on screen.
check(frame.bgr[0:30, 0:220].std() > 5, "caption for a top-edge box stays in frame")

empty = Frame(bgr=np.zeros((240, 320, 3), np.uint8))
draw(empty, 0.0)
check(True, "draw() handles a frame with no detections")

# --- encoding --------------------------------------------------------------
section("jpeg encoding")

ok, buf = cv2.imencode(".jpg", frame.bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
jpeg = buf.tobytes()
check(ok, "frame encodes")
check(jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9", "output has valid JPEG markers")
check(len(jpeg) > 1000, f"output is a plausible size ({len(jpeg)} bytes)")

# --- http server -----------------------------------------------------------
section("mjpeg server")

server = MjpegServer(port=8099, host="127.0.0.1")
server.start()
time.sleep(0.4)
try:
    page = urllib.request.urlopen("http://127.0.0.1:8099/", timeout=5).read()
    check(b"stream.mjpg" in page, "index page points at the stream")

    try:
        urllib.request.urlopen("http://127.0.0.1:8099/nope", timeout=5)
        check(False, "unknown path should 404")
    except urllib.error.HTTPError as exc:
        check(exc.code == 404, "unknown path returns 404")

    # A client connecting after the last frame must still get that frame,
    # rather than waiting for the next one that may never come.
    server.update(jpeg)
    time.sleep(0.2)
    response = urllib.request.urlopen("http://127.0.0.1:8099/stream.mjpg", timeout=5)
    check("multipart/x-mixed-replace" in response.headers.get("Content-Type", ""),
          "stream declares multipart content type")
    # Read less than one part. Asking for more than the server has queued
    # would block until the next frame, which is exactly what this check is
    # meant to prove is not necessary.
    head = response.read(2048)
    check(b"--frameboundary" in head, "multipart boundary present")
    check(b"Content-Type: image/jpeg" in head, "each part declares its type")
    check(b"\xff\xd8" in head, "a late-joining client gets the current frame")
    response.close()

    # And it must keep serving new frames after that.
    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            server.update(jpeg)
            time.sleep(0.05)

    threading.Thread(target=pump, daemon=True).start()
    response = urllib.request.urlopen("http://127.0.0.1:8099/stream.mjpg", timeout=5)
    data = response.read(len(jpeg) * 2)
    stop.set()
    check(data.count(b"\xff\xd8") >= 2, "consecutive frames are delivered")
    response.close()
finally:
    server.stop()

# A browser tab closing mid-stream must not take the server with it.
server = MjpegServer(port=8100, host="127.0.0.1")
server.start()
time.sleep(0.3)
try:
    server.update(jpeg)
    time.sleep(0.1)
    response = urllib.request.urlopen("http://127.0.0.1:8100/stream.mjpg", timeout=5)
    response.read(10)
    response.close()
    time.sleep(0.3)
    server.update(jpeg)
    page = urllib.request.urlopen("http://127.0.0.1:8100/", timeout=5).read()
    check(b"stream.mjpg" in page, "server survives a client hanging up mid-stream")
finally:
    server.stop()

# Restarting must not hit "address already in use".
MjpegServer(port=8101, host="127.0.0.1").start()
time.sleep(0.1)
a = MjpegServer(port=8102, host="127.0.0.1")
a.start()
a.stop()
b = MjpegServer(port=8102, host="127.0.0.1")
b.start()
b.stop()
check(True, "port rebinds immediately after a restart")

# --- nfc mode switching ----------------------------------------------------
section("nfc mode switching")

TAG = "04BD224C9E6180"
sw = ModeSwitch(TAG)
check(sw.mode == MANUAL, "starts in manual, every time")

# The badge counts taps across a Pi restart, so the first count seen is
# history, not an event.
check(sw.on_packet(TAG, 7, 100.0) is False, "first packet adopts the count, does not switch")
check(sw.mode == MANUAL, "still manual after adopting")

check(sw.on_packet(TAG, 8, 101.0) is True, "a new tap switches")
check(sw.mode == AUTO, "manual -> auto")

# The badge repeats the same count 20 times a second.
check(sw.on_packet(TAG, 8, 101.1) is False, "the same count again does nothing")
check(sw.mode == AUTO, "repeats do not toggle back")

check(sw.on_packet(TAG, 9, 103.0) is True, "the next tap switches back")
check(sw.mode == MANUAL, "auto -> manual")

# Any tag reads; only one tag counts.
sw2 = ModeSwitch(TAG)
sw2.on_packet(TAG, 1, 0.0)
check(sw2.on_packet("DEADBEEF", 2, 1.0) is False, "an unknown tag is ignored")
check(sw2.mode == MANUAL, "an unknown tag does not switch")
check(sw2.rejected == "DEADBEEF", "the unknown tag is reported")

# Case and separators differ between readers; the badge sends bare uppercase.
sw3 = ModeSwitch("04:bd:22:4c:9e:61:80")
sw3.on_packet(TAG, 1, 0.0)
check(sw3.on_packet(TAG.lower(), 2, 1.0) is True, "uid compare ignores case and colons")

# A packet with no nfc fields at all is the old protocol.
sw4 = ModeSwitch(TAG)
check(sw4.on_packet(None, None, 0.0) is False, "a packet with no nfc fields is inert")

sw5 = ModeSwitch(TAG, cooldown=1.5)
sw5.on_packet(TAG, 1, 0.0)
sw5.on_packet(TAG, 2, 10.0)
check(sw5.mode == AUTO, "switched once")
check(sw5.on_packet(TAG, 3, 10.2) is False, "a second tap inside the cooldown is dropped")
check(sw5.mode == AUTO, "cooldown holds the mode")

sw6 = ModeSwitch(TAG)
sw6.on_packet(TAG, 1, 0.0)
sw6.on_packet(TAG, 2, 10.0)
check(sw6.force_manual() is True, "HOME forces manual")
check(sw6.mode == MANUAL, "and the mode really changed")
check(sw6.force_manual() is False, "forcing manual twice reports no change")

# --- the follow law --------------------------------------------------------
section("follow law")

centred = [Box("person", 0.50, 0.5, 0.25)]
check(follow(centred, 0.0) == (0.0, 0.0), "centred and at distance: no drive")

left_of_centre = follow([Box("person", 0.20, 0.5, 0.25)], 0.0)
check(left_of_centre[0] < left_of_centre[1], "a subject to the left steers left")

right_of_centre = follow([Box("person", 0.80, 0.5, 0.25)], 0.0)
check(right_of_centre[0] > right_of_centre[1], "a subject to the right steers right")

far = follow([Box("person", 0.5, 0.5, 0.05)], 0.0)
check(far[0] > 0 and far[1] > 0, "a small (far) subject drives forward")

near = follow([Box("person", 0.5, 0.5, 0.60)], 0.0)
check(near[0] < 0 and near[1] < 0, "a large (near) subject backs off")

check(follow(centred, 5.0) == (0.0, 0.0), "stale detections stop the car")
check(follow([], 0.0) == (0.0, 0.0), "no detections at all stop the car")
check(follow([Box("backpack", 0.2, 0.5, 0.3)], 0.0) == (0.0, 0.0),
      "a non-target class is not followed")

biggest = follow([Box("person", 0.9, 0.5, 0.05), Box("person", 0.1, 0.5, 0.40)], 0.0)
check(biggest[0] < biggest[1], "the nearest of two people is the one followed")

for box in (Box("person", 0.0, 0.5, 0.9), Box("person", 1.0, 0.5, 0.01)):
    l, r = follow([box], 0.0)
    check(abs(l) <= 0.451 and abs(r) <= 0.451, f"speed stays capped at cx={box.cx}")

# --- summary ---------------------------------------------------------------
print()
print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)
