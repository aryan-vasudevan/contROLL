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

# --- summary ---------------------------------------------------------------
print()
print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)
