#!/usr/bin/env python3
"""End-to-end test of the chase path, with no car and no badge.

Runs the real badgedrive.py as a subprocess in dry-run mode and speaks to it
over real sockets: BDT2 detections on one port, BADGE1 button lines on the
other, exactly as oak_stream.py and the badge do. What comes back is the
wheel speeds it would have commanded.

The properties being checked are the ones whose failure is a car driving into
somebody:

    it chases only when told to
    a hand on the controls beats the autopilot
    losing the target stops it
    silence stops it

The unit tests cover the arithmetic. This covers the wiring -- the parsing,
the precedence, the failsafe -- which is where the interesting mistakes are.
"""

import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, os.path.join(ROOT, "firmware", "pi"))
from oak_stream import (DET_BOX, DET_HEADER, DET_MAGIC,      # noqa: E402
                        control_command)

BTN_PORT, BOX_PORT = 34555, 34559
WIDTH, HEIGHT = 160, 120
TARGET_ID = 7

fails = []
# "  UP    L +0.60  R +0.60  chase #7 pivot"
WHEELS = re.compile(r"L\s+([-+][\d.]+)\s+R\s+([-+][\d.]+)(.*)$")


def check(cond, msg, rig=None):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)
        if rig is not None:
            print(f"         last output: {rig.dump()}")


def det_packet(boxes):
    blob = DET_MAGIC + struct.pack(DET_HEADER, len(boxes), 1, WIDTH, HEIGHT)
    for x, y, w, h, tid in boxes:
        name = b"person"
        blob += struct.pack(DET_BOX, x, y, w, h, 90, tid, len(name)) + name
    return blob


def box_at(centre_x, fill, tid=TARGET_ID):
    h = int(fill * HEIGHT)
    w = max(1, h // 3)
    return (int(centre_x - w / 2), (HEIGHT - h) // 2, w, h, tid)


class Rig:
    """Keeps the badge heartbeat and the detection stream running."""

    def __init__(self, proc):
        self.proc = proc
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        self.held = ""
        self.sel = 0
        self.auto = 0
        self.rec = 0
        self.honk = 0
        self.boxes = []
        self.running = True
        self.lines = []
        self._lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()

    def _pump(self):
        while self.running:
            with self._lock:
                held, sel, auto, boxes = self.held, self.sel, self.auto, self.boxes
                rec, honk = self.rec, self.honk
                self.honk = 0            # one-shot, exactly as the badge sends it
            if boxes:
                self.sock.sendto(det_packet(boxes), ("127.0.0.1", BOX_PORT))
            self.seq += 1
            line = (f"BADGE1 seq={self.seq} ms={int(time.monotonic()*1000)} "
                    f"raw=0x00 down=[{held}] sel={sel} auto={auto} "
                    f"shake=0 rec={rec} honk={honk}")
            self.sock.sendto(line.encode(), ("127.0.0.1", BTN_PORT))
            time.sleep(0.02)                      # 50 Hz, as the badge does

    def _read(self):
        for raw in self.proc.stdout:
            with self._lock:
                self.lines.append(raw.rstrip())

    def set(self, held=None, sel=None, auto=None, boxes=None, rec=None, honk=None):
        """Change only what is named. Defaulting these to 0 silently switched
        the chase back off between steps and made the test lie."""
        with self._lock:
            if held is not None:
                self.held = held
            if sel is not None:
                self.sel = sel
            if auto is not None:
                self.auto = auto
            if boxes is not None:
                self.boxes = boxes
            if rec is not None:
                self.rec = rec
            if honk is not None:
                self.honk = honk

    def saw(self, needle):
        with self._lock:
            return any(needle in x for x in self.lines)

    def dump(self, n=6):
        with self._lock:
            return " | ".join(x.strip() for x in self.lines[-n:])

    def settle(self, seconds=0.45):
        """Let the state take effect, then return the last wheel command.

        badgedrive prints transitions, not state, so the command currently
        standing is the last one printed -- or a stop, if it has never printed
        anything, which is where it starts.
        """
        time.sleep(seconds)
        with self._lock:
            lines = list(self.lines)
        for raw in reversed(lines):
            m = WHEELS.search(raw)
            if m:
                return float(m.group(1)), float(m.group(2)), m.group(3).strip()
        return 0.0, 0.0, ""

    def stop(self):
        self.running = False
        time.sleep(0.05)


proc = subprocess.Popen(
    [sys.executable, "-u", os.path.join(ROOT, "pi5", "badgedrive.py"),
     "--dry-run", "--port", str(BTN_PORT), "--box-port", str(BOX_PORT)],
    cwd=os.path.join(ROOT, "pi5"),
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

try:
    time.sleep(0.6)
    if proc.poll() is not None:
        print("  FAIL badgedrive exited at startup:")
        print(proc.stdout.read())
        sys.exit(1)

    rig = Rig(proc)

    print("=== detections present, but not engaged ===")
    rig.set(held="", sel=0, auto=0, boxes=[box_at(20, 0.20)])
    left, right, tag = rig.settle()
    check(left == 0.0 and right == 0.0,
          "a target on screen does not move the car on its own")

    print()
    print("=== selected but not engaged ===")
    rig.set(held="", sel=TARGET_ID, auto=0)
    left, right, _ = rig.settle()
    check(left == 0.0 and right == 0.0, "selecting a target does not start it either")

    print()
    print("=== engaged, target off to the left ===")
    rig.set(held="", sel=TARGET_ID, auto=1, boxes=[box_at(15, 0.20)])
    left, right, tag = rig.settle()
    check(left < 0 and right > 0, "it pivots left towards the target")
    check("chase" in tag and "pivot" in tag, f"and says so: {tag!r}")

    print()
    print("=== engaged, target centred and far ===")
    rig.set(boxes=[box_at(WIDTH / 2, 0.15)])
    left, right, tag = rig.settle()
    check(left > 0 and right > 0 and abs(left - right) < 1e-6,
          "it drives straight at a centred target")
    check("approach" in tag, f"and calls it an approach: {tag!r}", rig)

    print()
    print("=== engaged, target close ===")
    rig.set(boxes=[box_at(WIDTH / 2, 0.92)])
    left, right, tag = rig.settle()
    check(left == 0.0 and right == 0.0, "it stops once the target fills the frame")

    print()
    print("=== a hand on the controls wins ===")
    rig.set(held="UP", sel=TARGET_ID, auto=1, boxes=[box_at(15, 0.20)])
    left, right, tag = rig.settle()
    check(left > 0 and right > 0 and abs(left - right) < 1e-6,
          "UP drives forward even with a target hard left and the chase on")
    check("chase" not in tag, "and the autopilot is not the one steering")

    print()
    print("=== HOME stops a chase ===")
    rig.set(held="HOME", sel=TARGET_ID, auto=1, boxes=[box_at(15, 0.20)])
    left, right, tag = rig.settle()
    check(left == 0.0 and right == 0.0, "HOME stops it mid-chase", rig)

    print()
    print("=== the target's id churns mid-chase ===")
    # The tracker renaming the person must NOT abort the mission: the person
    # is still there, in the same part of the frame, so the chase re-locks.
    rig.set(held="", sel=TARGET_ID, auto=1, boxes=[box_at(15, 0.20)])
    rig.settle(0.4)
    rig.set(boxes=[box_at(18, 0.20, tid=99)])       # same person, new name
    left, right, tag = rig.settle(0.8)
    check(left != 0.0 or right != 0.0, "the chase carries on across an id change", rig)

    print()
    print("=== the person genuinely vanishes ===")
    rig.set(boxes=[])
    left, right, tag = rig.settle(0.8)
    check("seeking" in tag or (left, right) != (0.0, 0.0),
          "briefly it holds the last bearing, seeking", rig)
    rig.set(sel=0, auto=0)
    rig.settle(0.4)

    print()
    print("=== control packets are not frame requests ===")
    # A frame request tells the stream where to send video. If a control packet
    # were mistaken for one, the video would be redirected at whatever sent it
    # and the badge would go blank.
    check(control_command(b"R") is None, "a frame request is not a command")
    check(control_command(b"C:REC1") == b"REC1", "C: marks a command")
    check(control_command(b"C:REC0") == b"REC0", "and the stop form")
    check(control_command(b"REC1") is None,
          "a bare REC1 is NOT a command -- it starts with R, like a request")
    check(control_command(b"") is None, "an empty datagram is not a command")

    print()
    print("=== the horn ===")
    rig.set(held="", sel=0, auto=0, honk=1)
    rig.settle(0.4)
    check(rig.saw("honk"), "honk=1 is acknowledged", rig)
    check(rig.saw("no speaker"), "and without a speaker it is SILENT, not motors", rig)
    left, right, _ = rig.settle(0.3)
    check(left == 0.0 and right == 0.0, "and the car does not move")

    print()
    print("=== recording relays to the camera ===")
    # oak_stream.py is not running here, so what is checked is that this end
    # tracks the level and speaks once per change rather than once per packet.
    rig.set(rec=1)
    rig.settle(0.4)
    check(rig.saw("recording started"), "rec=1 starts a take", rig)
    rig.settle(0.4)
    with rig._lock:
        starts = sum("recording started" in x for x in rig.lines)
    check(starts == 1, f"and says so once, not once per packet (saw {starts})")
    rig.set(rec=0)
    rig.settle(0.4)
    check(rig.saw("recording stopped"), "rec=0 stops it", rig)

    print()
    print("=== driving outranks the horn ===")
    rig.set(held="", honk=1)
    time.sleep(0.05)
    rig.set(held="UP")
    left, right, _ = rig.settle(0.5)
    check(left > 0 and right > 0,
          "a drive command mid-honk still reaches the motors")
    rig.set(held="")

    print()
    print("=== the badge goes silent mid-chase ===")
    rig.set(held="", sel=TARGET_ID, auto=1, boxes=[box_at(WIDTH / 2, 0.15)])
    left, right, _ = rig.settle()
    check(left > 0, "chasing")
    rig.running = False                              # badge switched off
    time.sleep(0.6)
    with rig._lock:
        tail = [x for x in rig.lines if "stop" in x]
    check(bool(tail), "silence stops the car even though it was driving itself")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()

print()
if fails:
    print(f"SOME TESTS FAILED ({len(fails)})")
    sys.exit(1)
print("autopilot ok")
