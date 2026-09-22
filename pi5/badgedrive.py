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

    B + LEFT/RIGHT   pick a detection on the badge screen
    B + A            chase it, or stop chasing
    B + UP           start or stop recording
    B + DOWN         honk
    shake the badge  honk, with no free hand needed

There is no speaker on the badge or the car -- the horn is played on the drive
motors by moving the PWM carrier into the audio range. See honk.py.

Recording happens in oak_stream.py, next to the camera, so this only relays
the toggle. `rec=` is sent as a level rather than an edge, so a lost packet
cannot leave the two ends disagreeing about whether tape is rolling.

The badge decides which detection is selected, because the badge is what the
operator is looking at; it reports the choice as `sel=<id> auto=<0|1>` on the
end of the line it already sends. Boxes arrive separately from oak_stream.py
on the loopback, so the same coordinates drive the car and the overlay.

**Manual always wins.** Any direction held cancels the chase immediately,
here as well as on the badge, because the badge might be off. So does HOME,
and so does the 200 ms failsafe -- a chase is stopped by silence exactly like
a driven command is.

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
import re
import select
import socket
import struct
import sys
import time

try:
    from drive import Car, DriveError, KICK_SPEED, KICK_SECONDS
except ImportError:
    sys.exit("drive.py must sit next to this script")

try:
    from chase import ChaseConfig, chase
except ImportError:
    sys.exit("chase.py must sit next to this script")

try:
    from honk import Horn
except ImportError:
    sys.exit("honk.py must sit next to this script")

try:
    from sound import Speaker
except ImportError:
    sys.exit("sound.py must sit next to this script")

PORT = 14555            # the badge's button port, same as badge_listen.py
BOX_PORT = 14559        # where oak_stream.py sends detections

# Must match oak_stream.py. See the note there on why the magic is BDT2.
DET_MAGIC = b"BDT2"
DET_HEADER = "<BHHH"
DET_BOX = "<hhHHBBB"

# Boxes older than this are not steered by. The detector runs at roughly the
# frame rate, so this is many detections' worth of grace -- long enough to
# ride out a burst of packet loss, short enough that the car is not driving
# at where somebody used to be.
BOX_STALE = 0.5

# The chase COMMITS. Once engaged, losing the box does not abort the mission:
# the car holds its last bearing at reduced speed for this long, and if a
# person reappears anywhere near by, it re-locks onto them -- whatever id the
# tracker gave them this time. The user chose a person, not a track number.
# Only after this much genuine silence does it stop and admit defeat.
CHASE_MEMORY = 4.0

# Silence for this long and the motors cut. The badge sends every 50 ms while a
# button is held and every 500 ms when idle, so 200 ms is comfortably longer
# than a held-button gap and comfortably shorter than an idle one: releasing a
# button stops the car within 200 ms without ever stuttering mid-press.
LINK_TIMEOUT = 0.20

SPEED = 0.70            # normal
BOOST = 0.95            # with A
CRAWL = 0.30            # with B
STEER = 0.85            # how hard a turn bites, 0 to 1

LINE = re.compile(r"^BADGE1\s+seq=(\d+)\s+ms=(\d+)\s+raw=0x([0-9A-Fa-f]{2})\s+down=\[([^\]]*)\]")
# Optional, and matched separately so a badge running older firmware -- which
# sends no such fields -- still drives normally.
SEL = re.compile(r"\bsel=(\d+)\b")
AUTO = re.compile(r"\bauto=(\d+)\b")
REC = re.compile(r"\brec=(\d+)\b")
HONK = re.compile(r"\bhonk=(\d+)\b")
SHAKE = re.compile(r"\bshake=(\d+)\b")
NFCSEQ = re.compile(r"\bnfcseq=(\d+)\b")
NFCUID = re.compile(r"\bnfc=([0-9A-Fa-f]+)\b")

# --- power-up ---------------------------------------------------------------
# Tapping the tag adds POWERUP_STEP to the normal speed for POWERUP_SECONDS,
# then it lapses on its own. Tapping again restarts the clock rather than
# stacking, so the car cannot be walked up to a speed it will not steer at.
# Both are --powerup-step/--powerup-seconds, because a lapse nobody can afford
# to sit through in a test is a lapse nobody tests.
#
# It moves the *base* only: A still boosts and B still crawls, because those
# are the two speeds you reach for when something is about to go wrong.
POWERUP_STEP = 0.05
POWERUP_SECONDS = 10.0

# Shake the badge, the car spins. Aggressive on purpose -- it is a party
# trick, and a hesitant party trick is worse than none. Manual input or the
# failsafe cancels it like anything else.
SPIN_DUTY = 0.90
# A full 360. Calibration lineage: the handoff measured 360 degrees = 3.0 s
# at 60% duty; speed scales roughly with duty, so 0.9 gives ~180 deg/s and
# two seconds closes the circle. No encoders -- like every duration on this
# car it drifts as the pack drains, so if the spin comes up short late in
# the day, this number grows.
SPIN_SECONDS = 2.0

# oak_stream.py's frame port. Recording lives with the camera, but the badge
# only ever speaks to this process, so the toggle is relayed. "C:" marks it as
# control so the stream does not mistake it for a frame request and start
# sending video to the Pi itself.
STREAM_ADDR = ("127.0.0.1", 14557)

# Held directions mean a human has taken over.
MANUAL = {"UP", "DOWN", "LEFT", "RIGHT"}


def parse_boxes(data: bytes):
    """Detections out of a BDT2 datagram: (img_w, img_h, {id: box}), or None.

    Mirrors parseDetections() in badgecam_main.cpp, and is bounds-checked for
    the same reason: these bytes come off a socket.
    """
    head = 4 + struct.calcsize(DET_HEADER)
    fixed = struct.calcsize(DET_BOX)
    if len(data) < head or data[:4] != DET_MAGIC:
        return None
    count, _frame, img_w, img_h = struct.unpack_from(DET_HEADER, data, 4)

    boxes, off = {}, head
    for _ in range(count):
        if off + fixed > len(data):
            break
        x, y, w, h, conf, tid, nlen = struct.unpack_from(DET_BOX, data, off)
        off += fixed
        if off + nlen > len(data):
            break
        label = data[off:off + nlen].decode("utf-8", "replace")
        off += nlen
        boxes[tid] = (x, y, w, h, label, conf / 100.0, tid)
    return img_w, img_h, boxes


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
    ap.add_argument("--box-port", type=int, default=BOX_PORT,
                    help="where oak_stream.py sends detections; 0 to disable "
                         "chasing entirely")
    ap.add_argument("--chase-speed", type=float, default=ChaseConfig().max_speed,
                    help="ceiling on autonomous speed. Lower is the right "
                         "instinct: detections are a few hundred ms old.")
    ap.add_argument("--powerup-step", type=float, default=POWERUP_STEP,
                    help="how much a tag tap adds to the base speed")
    ap.add_argument("--powerup-seconds", type=float, default=POWERUP_SECONDS,
                    help="how long a tap lasts. The tests turn this right "
                         "down; ten seconds is the number for a person.")
    args = ap.parse_args()

    cfg = ChaseConfig(max_speed=args.chase_speed)

    ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", args.port))
    except OSError as exc:
        print(f"cannot bind UDP {args.port}: {exc}", file=sys.stderr)
        print("badge-listen is probably still running:  sudo systemctl stop badge-listen",
              file=sys.stderr)
        return 1

    boxsock = None
    if args.box_port:
        boxsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        boxsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            boxsock.bind(("127.0.0.1", args.box_port))
        except OSError as exc:
            print(f"cannot bind UDP {args.box_port} for boxes: {exc}", file=sys.stderr)
            print("chasing disabled; manual driving still works", file=sys.stderr)
            boxsock.close()
            boxsock = None

    car = None
    latched = False          # HOME pressed: ignore the sticks until all clear
    last_drive = (0.0, 0.0)
    moving = False
    packets = 0
    last_seen = 0.0
    nfc_last: int | None = None   # None until the badge's counter is adopted
    boost_until = 0.0

    def drive_speed() -> float:
        """Normal speed, plus the power-up while it lasts."""
        boosted = time.monotonic() < boost_until
        return args.speed + (args.powerup_step if boosted else 0.0)

    boxes: dict = {}
    boxes_at = 0.0
    src_w = src_h = 0
    # the chase's own memory of its target
    mem_box = None            # last box actually seen for the engaged target
    mem_at = 0.0
    mem_cmd = (0.0, 0.0)      # what we were doing when we lost them
    locked_id = 0             # may drift from `selected` after a re-lock
    last_status = ""
    recording = False
    # Pivoting is done in PULSES: a short burst of rotation, then a pause
    # long enough for a detection to land on a stable image. A continuous
    # pivot pans the camera so fast that every box slides out from under the
    # tracker between updates -- the car would turn, lose the very person it
    # was turning towards, and abort. Seen on hardware, not theorized.
    PIVOT_ON = 0.12          # seconds of turning per cycle -- ~14 degrees
    PIVOT_CYCLE = 0.55       # full cycle; the rest is camera-settling time.
    # Both retuned on the real car: 0.18/0.45 rotated ~22 degrees per pulse
    # and kept pulsing on ~300 ms-stale bearings, overshooting the person and
    # then losing them off the side of the frame. Smaller bites, longer looks.
    PIVOT_DUTY = 0.60        # strong enough to actually rotate in a burst

    # Breaking the gearboxes loose. drive.py has always known these motors
    # need ~85% for a moment before they will start (KICK_SPEED), but only
    # the standalone test tool ever used it -- the autopilot commanded 0.3-0.5
    # from rest and the car buzzed instead of moving. Whenever the target
    # goes from stopped to moving, the kick is applied first, without ever
    # blocking the failsafe loop.
    kick_until = 0.0
    spin_until = 0.0
    last_applied = (0.0, 0.0)

    try:
        car = Car(dry_run=args.dry_run)
        horn = Horn(car)
        speaker = Speaker()
        if speaker.available():
            print("USB speaker found; honks use it (motor horn is the fallback)")
        print(f"listening on UDP {args.port}, cutting out after "
              f"{args.timeout * 1000:.0f} ms of silence"
              + ("  [dry run]" if args.dry_run else ""))
        if boxsock is not None:
            print(f"detections on UDP {args.box_port}; B+LEFT/RIGHT to pick, "
                  f"B+A to chase, max {cfg.max_speed:.2f}")
        print("wheels off the ground for the first try. ctrl-c to stop.\n")

        waiting = [s for s in (sock, boxsock) if s is not None]
        held: set[str] = set()
        selected, autonomous = 0, False

        # Wake often enough to notice silence promptly. The failsafe is a
        # deadline on the clock, NOT "select returned without the button
        # socket" -- with two sockets those are different things, and the
        # difference is a bug: every detection packet arriving between badge
        # packets would have looked like a dead link and cut the motors, so
        # the car stuttered exactly when it had something to chase.
        poll = min(args.timeout / 4.0, 0.05)

        while True:
            ready, _, _ = select.select(waiting, [], [], poll)

            if boxsock is not None and boxsock in ready:
                try:
                    blob, _ = boxsock.recvfrom(2048)
                except OSError:
                    blob = b""
                parsed = parse_boxes(blob)
                if parsed is not None:
                    src_w, src_h, boxes = parsed
                    boxes_at = time.monotonic()

            if sock in ready:
                data, peer = sock.recvfrom(512)
                text = data.decode("utf-8", "replace").strip()
                match = LINE.match(text)
                if match:
                    packets += 1
                    last_seen = time.monotonic()
                    held = set(match.group(4).split())
                    sock.sendto(b"OK", peer)   # the badge's LEDs key off this

                    # A change in the badge's tap counter is one tap. The
                    # first value seen is adopted, not acted on: the badge
                    # keeps counting across a restart of this script, and a
                    # restart is not a tap.
                    seq_m = NFCSEQ.search(text)
                    if seq_m:
                        seq_i = int(seq_m.group(1))
                        if nfc_last is None:
                            nfc_last = seq_i
                        elif seq_i != nfc_last:
                            nfc_last = seq_i
                            boost_until = time.monotonic() + args.powerup_seconds
                            uid_m = NFCUID.search(text)
                            print(f"  *** POWER UP!  +{args.powerup_step:.2f} for "
                                  f"{args.powerup_seconds:.0f}s  "
                                  f"({uid_m.group(1) if uid_m else '?'}) ***")

                    sel_m, auto_m = SEL.search(text), AUTO.search(text)
                    selected = int(sel_m.group(1)) if sel_m else 0
                    autonomous = bool(auto_m and auto_m.group(1) == "1")

                    # honk= is a one-shot the badge sets for a single packet,
                    # from B+DOWN or from shaking it.
                    shake_m = SHAKE.search(text)
                    shaken = bool(shake_m and shake_m.group(1) == "1")
                    if shaken:
                        spin_until = time.monotonic() + SPIN_SECONDS
                        if not args.quiet:
                            print("  SHAKE -> spin")

                    honk_m = HONK.search(text)
                    # A shake also raises the honk flag on the badge; here the
                    # spin wins -- the horn IS the motors, and both at once
                    # would fight over the same coils.
                    if not shaken and honk_m and honk_m.group(1) == "1":
                        # Speaker first; the motor horn is back as the loud
                        # fallback until real audio hardware exists.
                        played = speaker.play_random()
                        if played is None and horn.play("honk"):
                            played = "motor horn"
                        if not args.quiet:
                            print(f"  honk ({played or 'nothing to play'})")

                    # rec= is level, not an edge, so a lost packet cannot leave
                    # the two ends disagreeing about whether tape is rolling.
                    rec_m = REC.search(text)
                    if rec_m:
                        want = rec_m.group(1) == "1"
                        if want != recording:
                            recording = want
                            ctrl.sendto(b"C:REC1" if want else b"C:REC0",
                                        STREAM_ADDR)
                            if not args.quiet:
                                print(f"  recording {'started' if want else 'stopped'}")

                    # HOME is a deliberate stop, and it stays stopped until
                    # every button is up, so leaning on it cannot be undone by
                    # still holding a direction.
                    if "SELECT" in held or "HOME" in held:
                        latched = True
                    elif not held:
                        latched = False

            # One place decides, every time round, whatever woke us. Running
            # the decision on box packets too means the chase steers at the
            # detection rate rather than only when the badge speaks.
            now = time.monotonic()
            silent = now - last_seen > args.timeout

            if not (autonomous and selected):
                locked_id = 0
                mem_box = None

            status = ""
            if silent:
                # Released, out of range, or switched off. Every one of those
                # means stop, chase or no chase.
                left, right = 0.0, 0.0
                status = "linkloss"
            elif latched:
                left, right = 0.0, 0.0
            elif held & MANUAL:
                # A hand on the controls outranks the autopilot, always, and
                # the check is here as well as on the badge because the badge
                # can be switched off and this cannot.
                left, right = wheels(held, drive_speed())
            elif autonomous and selected:
                if locked_id == 0:
                    locked_id = selected
                target = boxes.get(locked_id) or boxes.get(selected)
                fresh = now - boxes_at <= BOX_STALE
                if target is None and fresh:
                    # Target id gone but people are visible: adopt whoever is
                    # nearest to where our target was last seen. The operator
                    # picked a PERSON; the id was only ever the tracker's name
                    # for them, and it does not survive every hiccup.
                    people = [b for b in boxes.values() if b[4] == "person"]
                    if people and mem_box is not None:
                        mx = mem_box[0] + mem_box[2] / 2.0
                        best = min(people, key=lambda b: abs(b[0] + b[2] / 2.0 - mx))
                        if abs(best[0] + best[2] / 2.0 - mx) < src_w * 0.45:
                            target = best
                            locked_id = best[6]
                if target is not None and fresh:
                    left, right, status = chase(target, src_w, src_h, cfg)
                    mem_box, mem_at, mem_cmd = target, now, (left, right)
                elif mem_box is not None and now - mem_at <= CHASE_MEMORY:
                    # Blind but committed: hold the last bearing, gently, and
                    # keep looking. Unless we were already basically there.
                    if mem_box[3] >= cfg.stop_fill * 0.9 * src_h:
                        left, right = 0.0, 0.0
                        status = "arrived"
                    else:
                        left = max(-0.4, min(0.4, mem_cmd[0]))
                        right = max(-0.4, min(0.4, mem_cmd[1]))
                        status = "seeking"
                else:
                    left, right = 0.0, 0.0
                    status = "lost"
            else:
                left, right = wheels(held, drive_speed())

            if (left, right) != last_drive or status != last_status:
                if horn.active() and (left, right) != (0.0, 0.0):
                    # Driving outranks the horn. yield_to_drive is the
                    # non-blocking handback; waiting for a note to finish here
                    # would stall the failsafe loop.
                    horn.yield_to_drive()
                    horn.restore_carrier()
                if last_drive == (0.0, 0.0) and (left, right) != (0.0, 0.0):
                    kick_until = now + KICK_SECONDS
                was_moving, moving = moving, (left != 0.0 or right != 0.0)
                # actual motor write happens in the apply block below
                last_drive, last_status = (left, right), status
                if not args.quiet:
                    if status == "linkloss":
                        if was_moving:
                            print(f"  stop  (no packet for "
                                  f"{(now - last_seen) * 1000:.0f} ms)")
                    else:
                        names = " ".join(sorted(held)) or "--"
                        tag = ("  HOME" if latched else
                               f"  chase #{selected} {status}" if status else "")
                        print(f"  {names:28} L {left:+.2f}  R {right:+.2f}{tag}")

            # The kick rides on top of whatever the target command is: full
            # breakaway power in the commanded directions for KICK_SECONDS,
            # then the real speeds. Applied every pass so it also ENDS on
            # time, with no sleep anywhere near the failsafe.
            if now < spin_until:
                # The shake spin. Outranked by everything human: any held
                # direction or HOME kills it instantly, and link silence cuts
                # the motors exactly as it always does.
                if held & MANUAL or latched or silent:
                    spin_until = 0.0
                    apply = (left, right)
                else:
                    apply = (SPIN_DUTY, -SPIN_DUTY)
            elif status == "pivot":
                # Pulsed: rotate hard for PIVOT_ON, hold still for the rest
                # of the cycle so the tracker gets an unsmeared look.
                if (now % PIVOT_CYCLE) < PIVOT_ON and (left, right) != (0.0, 0.0):
                    apply = (PIVOT_DUTY if left > 0 else -PIVOT_DUTY,
                             PIVOT_DUTY if right > 0 else -PIVOT_DUTY)
                else:
                    apply = (0.0, 0.0)
            elif now < kick_until and (left, right) != (0.0, 0.0):
                apply = (KICK_SPEED if left > 0 else -KICK_SPEED if left < 0 else 0.0,
                         KICK_SPEED if right > 0 else -KICK_SPEED if right < 0 else 0.0)
            else:
                apply = (left, right)
            if apply != last_applied:
                car.drive(*apply)
                last_applied = apply

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
        ctrl.close()
        sock.close()
        if boxsock is not None:
            boxsock.close()


if __name__ == "__main__":
    sys.exit(main())
