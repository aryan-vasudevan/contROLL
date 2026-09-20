#!/usr/bin/env python3
"""
Find out why one side of the car does not move.

    python3 pintest.py            # walk both channels, one pin at a time
    python3 pintest.py --side ri  # just the right channel
    python3 pintest.py --hold 16  # hold one GPIO high so you can measure it

Works down the chain in order, so each step rules something out:

  1. each direction pin on its own, enable off     is the Pi driving the pin?
  2. enable on, one direction at a time            does the channel respond?
  3. both directions low, enable on                brake, should not move

The point is to separate three things that look identical from the driver's
seat: a GPIO that is not being driven, an L298N channel that is dead, and a
motor that is not connected.

Wiring, same as drive.py:

    left    ENA GPIO 12   IN1 GPIO 5    IN2 GPIO 6
    right   ENB GPIO 13   IN3 GPIO 16   IN4 GPIO 26
"""

from __future__ import annotations

import argparse
import sys
import time

SIDES = {
    "lft": dict(name="left",  enable=12, forward=5,  reverse=6),
    "ri":  dict(name="right", enable=13, forward=16, reverse=26),
}


def outputs():
    try:
        from gpiozero import DigitalOutputDevice, PWMOutputDevice
    except ImportError:
        sys.exit("gpiozero is missing: sudo apt install python3-gpiozero python3-lgpio")
    return PWMOutputDevice, DigitalOutputDevice


def hold(pin: int, seconds: float) -> None:
    """Hold one GPIO high, for measuring with a meter."""
    _, digital = outputs()
    out = digital(pin)
    try:
        out.value = 1
        print(f"GPIO {pin} held HIGH for {seconds:.0f}s. "
              f"Measure it against a ground pin; it should read about 3.3 V.")
        time.sleep(seconds)
    finally:
        out.value = 0
        out.close()
        print(f"GPIO {pin} released.")


def walk(side: dict, seconds: float) -> None:
    pwm, digital = outputs()
    name = side["name"]
    print(f"\n=== {name} channel: ENB/ENA GPIO {side['enable']}, "
          f"IN GPIO {side['forward']} and {side['reverse']} ===")

    fwd = digital(side["forward"])
    rev = digital(side["reverse"])
    en = pwm(side["enable"], frequency=1000)

    def step(label: str, f: int, r: int, e: float, expect: str) -> None:
        fwd.value, rev.value, en.value = f, r, e
        print(f"\n  {label}")
        print(f"    IN{'1' if name == 'left' else '3'}={f}  "
              f"IN{'2' if name == 'left' else '4'}={r}  EN={e:.2f}")
        print(f"    expect: {expect}")
        time.sleep(seconds)

    try:
        # Enable low first. If the motor moves here, the enable line is not
        # actually controlling the channel -- usually the board's EN jumper is
        # still fitted, tying it high.
        step("direction only, enable OFF", 1, 0, 0.0,
             "no movement. If it moves, the EN jumper is still on the L298N.")

        step("forward, enable FULL", 1, 0, 1.0, "this side turns one way")
        step("reverse, enable FULL", 0, 1, 1.0, "this side turns the other way")
        step("brake, enable FULL", 0, 0, 1.0, "no movement, and it resists turning")
        step("forward, enable HALF", 1, 0, 0.5, "same way as before, slower")
    finally:
        fwd.value = rev.value = 0
        en.value = 0.0
        for p in (fwd, rev, en):
            p.close()
        print(f"\n  {name} channel released.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--side", choices=sorted(SIDES), help="only this channel")
    ap.add_argument("--seconds", type=float, default=2.0, help="seconds per step")
    ap.add_argument("--hold", type=int, metavar="GPIO",
                    help="hold one GPIO high and wait, for measuring")
    args = ap.parse_args()

    if args.hold is not None:
        hold(args.hold, max(args.seconds, 10.0))
        return 0

    print("Wheels off the ground. Ctrl-C stops everything.")
    try:
        for key in ([args.side] if args.side else ["lft", "ri"]):
            walk(SIDES[key], args.seconds)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130

    print("""
If the right channel did nothing at all, in the order worth checking:

  1. The ENB jumper. L298N boards ship with a jumper linking ENB to 5 V. If it
     is REMOVED, ENB must be wired to GPIO 13 or the channel stays disabled and
     the motors never turn, exactly as you are seeing. If it is FITTED, GPIO 13
     does nothing and the channel runs flat out whenever IN3/IN4 are set.
  2. IN3 and IN4 actually on GPIO 16 (pin 36) and GPIO 26 (pin 37).
  3. The motors on OUT3/OUT4, screws tight on the copper and not the insulation.
  4. Swap the left and right motor pairs at the OUT terminals. If the right
     motors then run, the motors and their wiring are fine and the fault is in
     channel B or its control lines. If they still do not, it is the motors.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
