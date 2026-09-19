#!/usr/bin/env python3
"""
Work through the four moves and check the car does what it is told.

    python3 testdrive.py --calibrate      find how long a 360 takes, first
    python3 testdrive.py --spin 2.4       then run the full sequence
    python3 testdrive.py --dry-run        print the pin states, drive nothing

The sequence is forward, backward, spin right 360, spin left 360, with a pause
between each so you can watch one thing at a time and say whether it was
right. Nothing loops, and the motors are cut on every exit including Ctrl-C.

A 360 is a duration, not an angle: there is no encoder on these motors, so the
car cannot know how far it has turned. How long a full turn takes depends on
the surface, the speed and how charged the pack is, and it changes as the pack
drains. Hence --calibrate, and hence re-checking it when the car starts
under-turning.

Reuses Car from drive.py rather than duplicating the pin handling, so the
trims and the kick behave identically here and there.
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    from drive import Car, DriveError, KICK_SECONDS, KICK_SPEED
except ImportError:
    sys.exit("drive.py must sit next to this script")

SPIN_SECONDS = 2.0      # a guess until --calibrate says otherwise
SPEED = 0.60
STRAIGHT_SECONDS = 1.5
PAUSE = 1.5             # between moves, so each is watched on its own


def kicked(car: Car, left: float, right: float, seconds: float) -> None:
    """One move, with the same gearbox kick drive.py uses."""
    speed = max(abs(left), abs(right))
    use_kick = KICK_SECONDS > 0 and 0 < speed < KICK_SPEED
    if use_kick:
        scale = KICK_SPEED / speed
        car.drive(left * scale, right * scale)
        time.sleep(KICK_SECONDS)
    car.drive(left, right)
    time.sleep(max(0.0, seconds - (KICK_SECONDS if use_kick else 0.0)))
    car.stop()


def step(car: Car, title: str, expect: str, left: float, right: float,
         seconds: float, pause: float) -> None:
    print(f"\n  {title}")
    print(f"    expect: {expect}")
    kicked(car, left, right, seconds)
    print("    done")
    time.sleep(pause)


def sequence(car: Car, speed: float, straight: float, spin: float,
             pause: float) -> None:
    step(car, "1. forward", "straight ahead, both sides the same speed",
         speed, speed, straight, pause)

    step(car, "2. backward", "straight back along the same line",
         -speed, -speed, straight, pause)

    # Pivot: the two sides oppose, so the car turns about its own centre.
    step(car, "3. spin right 360", "one full turn clockwise, back to the start",
         speed, -speed, spin, pause)

    step(car, "4. spin left 360", "one full turn anticlockwise, back to the start",
         -speed, speed, spin, pause)


def calibrate(car: Car, speed: float, seconds: float) -> None:
    print(f"\n  pivoting right at {speed:.0%} for {seconds:.1f}s.")
    print("  Mark which way the front points before it starts.\n")
    time.sleep(1.0)
    kicked(car, speed, -speed, seconds)
    print(f"""
  Measure how far it actually turned, then:

      seconds for a full turn = {seconds:.1f} x 360 / degrees_turned

  So if it went round about 180 degrees, a 360 takes {seconds * 2:.1f}s:

      python3 testdrive.py --spin {seconds * 2:.1f}

  Worth redoing on the surface you will demo on, and again when the pack has
  drained a bit. A slower car turns less in the same time, and this is a time.
""")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--speed", type=float, default=SPEED, help="0.0 to 1.0")
    ap.add_argument("--straight", type=float, default=STRAIGHT_SECONDS,
                    help="seconds for forward and backward")
    ap.add_argument("--spin", type=float, default=SPIN_SECONDS,
                    help="seconds for one full turn; use --calibrate to find it")
    ap.add_argument("--pause", type=float, default=PAUSE, help="seconds between moves")
    ap.add_argument("--calibrate", nargs="?", type=float, const=2.0, metavar="SECONDS",
                    help="pivot right for this long, then work out the 360 time")
    ap.add_argument("--dry-run", action="store_true", help="drive nothing")
    ap.add_argument("--now", action="store_true", help="skip the countdown")
    args = ap.parse_args()

    if not 0.0 < args.speed <= 1.0:
        print(f"--speed is {args.speed}; it must be above 0 and at most 1.0",
              file=sys.stderr)
        return 2

    car = None
    try:
        car = Car(dry_run=args.dry_run)

        if not args.now and not args.dry_run:
            print("\n  Clear floor, nothing within a metre. Starting in:")
            for n in (3, 2, 1):
                print(f"    {n}...")
                time.sleep(1.0)

        if args.calibrate is not None:
            calibrate(car, args.speed, args.calibrate)
        else:
            print(f"\n  {args.speed:.0%} speed, {args.straight:.1f}s straights, "
                  f"{args.spin:.1f}s per 360"
                  + ("  [dry run]" if args.dry_run else ""))
            sequence(car, args.speed, args.straight, args.spin, args.pause)
            print("""
  All four done. What to look for:

    forward and backward along the same line   trims are right
    drifts to one side                         lower that side's trim in
                                               drive.py, in steps of 0.05
    over or under a full turn                  adjust --spin
    one spin fuller than the other             the sides are not matched;
                                               trim rather than changing --spin
""")
        return 0

    except DriveError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130
    finally:
        # Every path, including the exceptions above. A car still driving after
        # the script has exited is the one failure worth this much care.
        if car is not None:
            car.close()


if __name__ == "__main__":
    sys.exit(main())
