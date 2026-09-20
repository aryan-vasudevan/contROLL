#!/usr/bin/env python3
"""
Drive the car from the badge's buttons.

    sudo systemctl stop badge-listen     # it holds the same port
    python3 badgedrive.py

    python3 badgedrive.py --dry-run      # print wheel speeds, drive nothing

    UP            forward
    DOWN          backward
    LEFT / RIGHT  turn, or arc if held with UP or DOWN
    A             boost
    B             crawl, for lining something up
    HOME          stop, and ignore the sticks until everything is released

The badge sends button state 20 times a second while anything is held and
twice a second when nothing is. So silence means one of two things -- released,
or out of range -- and both want the same answer, which is to stop. That is
the whole failsafe: no packet for LINK_TIMEOUT and the motors cut. It is not a
fallback for something else going wrong, it is the primary way the car stops.

Reuses Car from drive.py so trims and pin handling stay in one place, and
replies to the badge so its LEDs go green exactly as badge_listen.py does.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import threading
import time

try:
    from drive import Car, DriveError
except ImportError:
    sys.exit("drive.py must sit next to this script")

try:
    from automode import AUTO, MANUAL, Box, ModeSwitch, follow
except ImportError:
    sys.exit("automode.py must sit next to this script")

PORT = 14555            # the badge's button port, same as badge_listen.py
VISION_PORT = 14556     # detections, published locally by the camera owner

# The tag stuck on the car. Any NFC tag will read -- a transit card, a hotel
# key -- so the UID is checked before anything switches. Change it here rather
# than in the firmware: editing this line costs a restart, reflashing the badge
# costs a rebuild.
ROBOT_TAG = "04BD224C9E6180"

# Silence for this long and the motors cut. The badge sends every 50 ms while a
# button is held and every 500 ms when idle, so 200 ms is comfortably longer
# than a held-button gap and comfortably shorter than an idle one: releasing a
# button stops the car within 200 ms without ever stuttering mid-press.
LINK_TIMEOUT = 0.20

SPEED = 0.60            # normal
BOOST = 0.95            # with A
CRAWL = 0.30            # with B
STEER = 0.85            # how hard a turn bites, 0 to 1

# The nfc fields are optional: a badge built before the reader existed, or one
# that has never been shown a tag, simply leaves them off.
LINE = re.compile(
    r"^BADGE1\s+seq=(\d+)\s+ms=(\d+)\s+raw=0x([0-9A-Fa-f]{2})\s+down=\[([^\]]*)\]"
    r"(?:\s+nfc=([0-9A-Fa-f]+)\s+nfcseq=(\d+))?")

DIRECTIONS = {"UP", "DOWN", "LEFT", "RIGHT"}


class Vision:
    """
    Subscribes to detections published by whichever process owns the camera.

    It has to be a subscription rather than opening the camera here, because
    the OAK-1 has exactly one owner and on this Pi that is the video streamer.
    Two processes cannot both hold it.

    Everything is behind a lock and nothing blocks the drive loop: if no
    detections have arrived, age() is large and the follow law stops the car.
    """

    def __init__(self, port: int = VISION_PORT) -> None:
        self._boxes: list[Box] = []
        self._at = 0.0
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.settimeout(0.5)
        self.packets = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                data, _ = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
                boxes = [Box(d["label"], float(d["cx"]), float(d["cy"]),
                             float(d["area"])) for d in msg.get("det", [])]
            except (ValueError, KeyError, TypeError):
                continue      # a malformed publish must not kill the driver
            with self._lock:
                self._boxes = boxes
                self._at = time.monotonic()
                self.packets += 1

    def read(self) -> tuple[list[Box], float]:
        """Latest boxes and how many seconds old they are."""
        with self._lock:
            if self._at == 0.0:
                return [], 1e9
            return list(self._boxes), time.monotonic() - self._at


def wheels(held: set[str], base: float = SPEED) -> tuple[float, float]:
    """Buttons to left and right track speeds, each -1.0 to 1.0."""
    speed = BOOST if "A" in held else CRAWL if "B" in held else base

    throttle = (1.0 if "UP" in held else 0.0) - (1.0 if "DOWN" in held else 0.0)
    steer = (1.0 if "RIGHT" in held else 0.0) - (1.0 if "LEFT" in held else 0.0)

    if throttle == 0.0 and steer != 0.0:
        # Turning on the spot: the tracks oppose.
        return steer * speed, -steer * speed

    left = throttle + steer * STEER
    right = throttle - steer * STEER

    # Normalise rather than clip. Clipping a turn at full throttle would quietly
    # slow the outer wheel and straighten the car out mid-corner.
    peak = max(abs(left), abs(right), 1.0)
    return left / peak * speed, right / peak * speed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--speed", type=float, default=SPEED)
    ap.add_argument("--timeout", type=float, default=LINK_TIMEOUT)
    ap.add_argument("--dry-run", action="store_true", help="drive nothing")
    ap.add_argument("--quiet", action="store_true", help="only print changes")
    ap.add_argument("--tag", default=ROBOT_TAG,
                    help="UID of the tag that toggles mode")
    ap.add_argument("--no-vision", action="store_true",
                    help="do not listen for detections; AUTO will just hold still")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", args.port))
    except OSError as exc:
        print(f"cannot bind UDP {args.port}: {exc}", file=sys.stderr)
        print("badge-listen is probably still running:  sudo systemctl stop badge-listen",
              file=sys.stderr)
        return 1
    sock.settimeout(args.timeout)

    switch = ModeSwitch(args.tag)
    vision = None if args.no_vision else Vision()

    car = None
    latched = False          # HOME pressed: ignore the sticks until all clear
    last_drive = (0.0, 0.0)
    moving = False
    packets = 0
    last_seen = 0.0

    try:
        car = Car(dry_run=args.dry_run)
        print(f"listening on UDP {args.port}, cutting out after "
              f"{args.timeout * 1000:.0f} ms of silence"
              + ("  [dry run]" if args.dry_run else ""))
        print(f"mode {switch.mode}; tag {switch.tag_uid} toggles it"
              + ("" if vision else "  [no vision]"))
        print("wheels off the ground for the first try. ctrl-c to stop.\n")

        while True:
            try:
                data, peer = sock.recvfrom(512)
            except socket.timeout:
                # Released, or out of range. Both mean stop.
                if moving:
                    car.stop()
                    moving = False
                    last_drive = (0.0, 0.0)
                    if not args.quiet:
                        age = time.monotonic() - last_seen
                        print(f"  stop  (no packet for {age * 1000:.0f} ms)")
                continue

            match = LINE.match(data.decode("utf-8", "replace").strip())
            if not match:
                continue
            packets += 1
            last_seen = time.monotonic()

            held = set(match.group(4).split())
            uid, nfcseq = match.group(5), match.group(6)

            now = time.monotonic()
            if switch.on_packet(uid, int(nfcseq) if nfcseq else None, now):
                print(f"  *** {switch.mode} ***")
            elif switch.rejected:
                print(f"  ignored unknown tag {switch.rejected}")

            # The badge keys its LEDs off this reply, and now also its mode
            # colour. Still one datagram, still on the same path the failsafe
            # watches, so nothing new can go silently stale.
            sock.sendto(f"OK MODE={switch.mode}".encode(), peer)

            # HOME is a deliberate stop, and it stays stopped until every
            # button is up, so leaning on it cannot be undone by still holding
            # a direction. It also hands control back to the human: a stop
            # that leaves the car free to drive itself again is not a stop.
            if "SELECT" in held or "HOME" in held:
                latched = True
                if switch.force_manual():
                    print("  *** MANUAL (HOME) ***")
            elif not held:
                latched = False

            if latched:
                left, right = 0.0, 0.0
            elif switch.mode == AUTO and not (held & DIRECTIONS):
                boxes, age = vision.read() if vision else ([], 1e9)
                left, right = follow(boxes, age)
            else:
                # MANUAL, or AUTO with a direction held: touching a direction
                # takes the car back off the camera instantly, without needing
                # to find the tag first.
                left, right = wheels(held, args.speed)

            if (left, right) != last_drive:
                car.drive(left, right)
                last_drive = (left, right)
                moving = left != 0.0 or right != 0.0
                if not args.quiet:
                    names = " ".join(sorted(held)) or "--"
                    tag = "  HOME" if latched else ""
                    print(f"  [{switch.mode[:3]}] {names:24} "
                          f"L {left:+.2f}  R {right:+.2f}{tag}")

    except DriveError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\nstopped after {packets} packets")
        return 0
    finally:
        # Every path. A car still driving after this exits is the failure that
        # matters more than any of the others.
        if car is not None:
            car.close()
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
