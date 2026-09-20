#!/usr/bin/env python3
"""
Draw boxes on the badge with nothing else plugged in.

    python3 badgetest.py            on the Pi, instead of oak-stream
    python3 badgetest.py --static   boxes that do not move
    python3 badgetest.py --boxes 4

No OAK, no internet, no Roboflow, no car. It stands in for oak_stream.py:
answers the badge's frame requests with a small built-in JPEG and sends made-up
detections alongside, at the same rate and in the same wire format.

This exists because of §7 of the handoff. The overlay was written, described as
"wire format verified", and had never actually drawn anything -- and the thing
that was verified turned out to be Python agreeing with Python. The C++ parser
now has a real test against real packets, but a parser is not a picture. The
note in §7 asks for exactly this and estimates ten minutes:

    "Anything built on the overlay should start by drawing a fixed HUD with
     made up numbers, to find out whether that path works at all."

So: run this, look at the badge.

    green rectangles with labels     the overlay works
    a status line along the bottom   the HUD works
    B + LEFT/RIGHT changes which     selection works, and the badge is
    box is yellow                    reporting it back

It also prints the frame rate it is actually serving, which is the honest
answer to "how fast is it with boxes drawn" -- the badge asks for each frame,
so the rate this prints is the rate the badge is managing, decode, overlay and
all.

Stop oak-stream first; they both want port 14557.

    sudo systemctl stop oak-stream
"""

import argparse
import base64
import math
import socket
import struct
import sys
import time

MAGIC = b"BJPF"
CHUNK = 1400
DET_MAGIC = b"BDT2"
DET_HEADER = "<BHHH"
DET_BOX = "<hhHHBBB"
PORT = 14557
WIDTH, HEIGHT = 160, 120

# Colour bars with orientation markers. The first card was a photo blurred
# down to almost nothing to keep it small, and on the panel it read as a
# broken white screen -- a test image indistinguishable from a failure is a
# bad test image. This one cannot be mistaken for anything but a pattern, and
# it carries its own orientation check: the RED block with the white notch
# belongs at the TOP-LEFT, the blue one at the bottom-right. Red anywhere
# else means the panel rotation has regressed.
CARD_B64 = (
    "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"
    "FlG4UWaIf//Z"
)

LABELS = ["person", "chair", "cup", "laptop"]


def frame() -> bytes:
    return base64.b64decode("".join(CARD_B64))


def boxes_at(t, count, static):
    """Made-up detections. They move, because a static box proves less:
    a box painted once in the right place and never erased looks identical
    to a box being redrawn correctly every frame."""
    out = []
    for i in range(count):
        if static:
            x = 10 + i * 34
            y = 30
            w, h = 28, 56
        else:
            phase = t * 0.9 + i * (2 * math.pi / max(count, 1))
            w = 26 + i * 4
            h = 50 + i * 6
            x = int((WIDTH - w) / 2 * (1 + math.sin(phase)))
            y = int((HEIGHT - h) / 2 * (1 + math.cos(phase * 0.7)))
        conf = 70 + int(25 * abs(math.sin(t + i)))
        # Ids are stable per slot, which is what makes B+LEFT/RIGHT selection
        # on the badge meaningful to look at.
        out.append((x, y, int(w), int(h), conf, i + 1, LABELS[i % len(LABELS)]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--boxes", type=int, default=3, help="how many to invent (0-16)")
    ap.add_argument("--static", action="store_true", help="do not move them")
    ap.add_argument("--no-boxes", action="store_true",
                    help="video only, to separate an overlay fault from a video one")
    args = ap.parse_args()

    jpeg = frame()
    if jpeg[:2] != b"\xff\xd8" or jpeg[-2:] != b"\xff\xd9":
        print("the built-in frame is not a valid JPEG", file=sys.stderr)
        return 1

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.bind, args.port))
    except OSError as exc:
        print(f"cannot bind UDP {args.port}: {exc}", file=sys.stderr)
        print("oak-stream is probably running:  sudo systemctl stop oak-stream",
              file=sys.stderr)
        return 1
    sock.settimeout(1.0)

    count = max(0, min(16, args.boxes))
    print(f"badgetest on {args.bind}:{args.port}")
    print(f"  {WIDTH}x{HEIGHT}, {len(jpeg)} byte frame, "
          f"{'no boxes' if args.no_boxes else f'{count} invented box(es)'}")
    print("  waiting for the badge to ask for a frame; ctrl-c to stop\n")

    started = time.monotonic()
    frame_id = 0
    frames = 0
    last_report = started
    seen = False

    try:
        while True:
            try:
                msg, peer = sock.recvfrom(64)
            except socket.timeout:
                continue
            if msg.startswith(b"C:"):
                continue                      # control, not a frame request
            if not seen:
                seen = True
                print(f"  badge found at {peer[0]}:{peer[1]}")

            frame_id = (frame_id + 1) & 0xFFFF
            total = (len(jpeg) + CHUNK - 1) // CHUNK
            for i in range(total):
                part = jpeg[i * CHUNK:(i + 1) * CHUNK]
                header = MAGIC + struct.pack("<HBBH", frame_id, i, total, len(part))
                sock.sendto(header + part, peer)

            if not args.no_boxes:
                now = time.monotonic() - started
                boxes = boxes_at(now, count, args.static)
                blob = DET_MAGIC + struct.pack(DET_HEADER, len(boxes), frame_id,
                                               WIDTH, HEIGHT)
                for x, y, w, h, conf, tid, label in boxes:
                    name = label.encode()[:15]
                    blob += struct.pack(DET_BOX, x, y, w, h, conf, tid,
                                        len(name)) + name
                sock.sendto(blob, peer)

            frames += 1
            now = time.monotonic()
            if now - last_report >= 5.0:
                span = now - last_report
                print(f"  {frames / span:5.1f} fps  <- what the badge is managing, "
                      f"decode and overlay included")
                last_report, frames = now, 0
    except KeyboardInterrupt:
        print("\n  stopping")
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
