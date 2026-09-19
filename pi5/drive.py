#!/usr/bin/env python3
"""
Drive the car one move: straight, right, or left, through an L298N.

Set MOVE in the block below, then on the Pi:

    python3 drive.py                  # runs the move you set in MOVE
    python3 drive.py --move ri        # override it for one run
    python3 drive.py --dry-run        # print the pin states, touch no hardware
    python3 drive.py --stop           # cut the motors and exit, for when
                                      # something went wrong mid-run

The car does the one move and stops. It does not loop, and it always stops the
motors on the way out, including on Ctrl-C and on a crash.

Wiring, L298N to the Pi's 40-pin header:

    ENA  -> GPIO 12, pin 32      speed, left channel
    IN1  -> GPIO 5,  pin 29      left forward
    IN2  -> GPIO 6,  pin 31      left reverse
    ENB  -> GPIO 13, pin 33      speed, right channel
    IN3  -> GPIO 16, pin 36      right forward
    IN4  -> GPIO 26, pin 37      right reverse
    GND  -> pin 39               shared ground, not optional

Both left motors go on OUT1/OUT2 in parallel, both right motors on OUT3/OUT4.
The motor pack feeds +12V and GND on the screw terminals; the Pi is powered
separately. Only the ground is shared. GPIO 14 and 15 are left alone because
the MAVLink bridge in firmware/pi uses them for the UART.

If a side spins backwards, swap that side's two motor wires at the screw
terminal. Do not fix it in software, or reverse stops matching forward.
"""

from __future__ import annotations

import argparse
import sys
import time

# ----------------------------------------------------------------------------
# Set these. Everything below this block is machinery.
# ----------------------------------------------------------------------------

MOVE = "str"            # "str" straight | "ri" right | "lft" left

SPEED = 0.60            # 0.0 to 1.0, duty cycle on ENA/ENB
SECONDS = 1.5           # how long the move runs
START_DELAY = 3.0       # countdown before it moves, so you can put it down

TURN_STYLE = "pivot"    # "pivot" turns on the spot, "arc" curves forwards
ARC_INNER = 0.35        # inner-wheel speed as a fraction of SPEED, arc only

LEFT_TRIM = 1.00        # straightness. If it veers right, lower LEFT_TRIM
RIGHT_TRIM = 1.00       # or raise RIGHT_TRIM, in steps of 0.05.

KICK_SPEED = 0.85       # brief burst to break the gearboxes loose
KICK_SECONDS = 0.15     # set KICK_SECONDS = 0 to disable

# ----------------------------------------------------------------------------

PWM_HZ = 1000           # L298N is happy here; audible but harmless

LEFT_PINS = dict(forward=5, reverse=6, enable=12)
RIGHT_PINS = dict(forward=16, reverse=26, enable=13)

MOVES = {
    "str": "straight",
    "ri": "right",
    "lft": "left",
}


_probed = False   # the pin-factory check above runs once per process


class DriveError(Exception):
    """Something is wrong with the setup, and the message says what."""


def _outputs(dry_run: bool):
    """Return the two classes used to build pins: (pwm, digital)."""
    if dry_run:
        return _Sink, _Sink

    try:
        from gpiozero import DigitalOutputDevice, PWMOutputDevice
    except ImportError:
        raise DriveError(
            "gpiozero is not installed.\n"
            "  On Raspberry Pi OS:  sudo apt install python3-gpiozero python3-lgpio\n"
            "  In a venv:           pip install gpiozero lgpio\n"
            "Not on a Pi? Use --dry-run to check the logic without hardware."
        )

    # Probe the pin factory once per process, not once per Side. _outputs() is
    # called while building each side, and by the time the second one runs the
    # first already holds this pin -- so probing it again reports "already in
    # use" and blames the pin factory for the script's own grip on it.
    global _probed
    if not _probed:
        try:
            DigitalOutputDevice(LEFT_PINS["forward"]).close()
        except Exception as exc:
            raise DriveError(
                f"gpiozero could not take GPIO {LEFT_PINS['forward']}: {exc}\n"
                "On a Pi 5 this is almost always the pin factory. RPi.GPIO does "
                "not work on the Pi 5 at all; gpiozero needs lgpio.\n"
                "  sudo apt install python3-lgpio\n"
                "If another process holds the pin, find it with: sudo lsof /dev/gpiochip0"
            )
        _probed = True

    return PWMOutputDevice, DigitalOutputDevice


class _Sink:
    """Stands in for a gpiozero output under --dry-run."""

    def __init__(self, pin: int, frequency: int | None = None) -> None:
        self.pin = pin
        self.value = 0.0

    def close(self) -> None:
        pass


class Side:
    """One L298N channel: two motors wired in parallel, one direction pair."""

    def __init__(self, name: str, pins: dict, trim: float, dry_run: bool) -> None:
        pwm, digital = _outputs(dry_run)
        self.name = name
        self.trim = trim
        self.forward = digital(pins["forward"])
        self.reverse = digital(pins["reverse"])
        self.enable = pwm(pins["enable"], frequency=PWM_HZ)

    def drive(self, speed: float) -> None:
        """speed is -1.0 to 1.0. Negative reverses. Trim applies to magnitude."""
        speed = max(-1.0, min(1.0, speed))
        duty = min(1.0, abs(speed) * self.trim)

        # Set direction before power, so the H-bridge never sees a live
        # direction change.
        self.forward.value = 1 if speed > 0 else 0
        self.reverse.value = 1 if speed < 0 else 0
        self.enable.value = duty

    def stop(self) -> None:
        """Coast. Both inputs low leaves the bridge open, so the car rolls."""
        self.enable.value = 0.0
        self.forward.value = 0
        self.reverse.value = 0

    def close(self) -> None:
        self.stop()
        for pin in (self.enable, self.forward, self.reverse):
            pin.close()


class Car:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.left = Side("left", LEFT_PINS, LEFT_TRIM, dry_run)
        self.right = Side("right", RIGHT_PINS, RIGHT_TRIM, dry_run)

    def drive(self, left: float, right: float) -> None:
        self.left.drive(left)
        self.right.drive(right)
        if self.dry_run:
            print(f"    left {left:+.2f}  right {right:+.2f}")

    def stop(self) -> None:
        self.left.stop()
        self.right.stop()

    def close(self) -> None:
        self.left.close()
        self.right.close()


def wheel_speeds(move: str, speed: float) -> tuple[float, float]:
    """Left and right track speeds for a move, before the kick and the trim."""
    if move == "str":
        return speed, speed

    # A right turn slows or reverses the right side, and vice versa.
    if TURN_STYLE == "pivot":
        inner = -speed
    elif TURN_STYLE == "arc":
        inner = speed * ARC_INNER
    else:
        raise DriveError(f'TURN_STYLE must be "pivot" or "arc", not "{TURN_STYLE}"')

    return (speed, inner) if move == "ri" else (inner, speed)


def run_move(car: Car, move: str, speed: float, seconds: float) -> None:
    left, right = wheel_speeds(move, speed)

    kicked = KICK_SECONDS > 0 and speed < KICK_SPEED
    if kicked:
        # Static friction in a gearbox is much higher than rolling friction, so
        # a duty cycle that sustains a roll will not always start one. This is
        # also the first thing to look at when the car buzzes and does not move.
        scale = KICK_SPEED / speed
        car.drive(left * scale, right * scale)
        time.sleep(KICK_SECONDS)

    # The kick counts towards the move, so --seconds means what it says.
    car.drive(left, right)
    time.sleep(max(0.0, seconds - (KICK_SECONDS if kicked else 0.0)))
    car.stop()


def countdown(seconds: float) -> None:
    remaining = int(seconds)
    while remaining > 0:
        print(f"  {remaining}...")
        time.sleep(1.0)
        remaining -= 1
    time.sleep(seconds - int(seconds))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive the car one move through an L298N.",
        epilog="With no arguments it runs the MOVE constant set at the top of the file.",
    )
    parser.add_argument("--move", choices=sorted(MOVES), help="override MOVE")
    parser.add_argument("--speed", type=float, help="override SPEED, 0.0 to 1.0")
    parser.add_argument("--seconds", type=float, help="override SECONDS")
    parser.add_argument("--now", action="store_true", help="skip the countdown")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what the pins would do, drive nothing")
    parser.add_argument("--stop", action="store_true",
                        help="cut both channels and exit")
    args = parser.parse_args()

    move = args.move or MOVE
    speed = SPEED if args.speed is None else args.speed
    seconds = SECONDS if args.seconds is None else args.seconds

    if move not in MOVES:
        print(f'MOVE is "{move}". It must be one of: {", ".join(sorted(MOVES))}',
              file=sys.stderr)
        return 2
    if not 0.0 < speed <= 1.0:
        print(f"SPEED is {speed}. It must be above 0 and at most 1.0.", file=sys.stderr)
        return 2

    car = None
    try:
        car = Car(dry_run=args.dry_run)

        if args.stop:
            car.stop()
            print("Motors stopped.")
            return 0

        print(f"{MOVES[move]} at {speed:.0%} for {seconds:.1f}s"
              + (f" ({TURN_STYLE})" if move != "str" else "")
              + (" [dry run]" if args.dry_run else ""))

        if not args.now and not args.dry_run:
            countdown(START_DELAY)

        run_move(car, move, speed, seconds)
        print("Done.")
        return 0

    except DriveError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    finally:
        # Runs on every path, including the exceptions above. A car that keeps
        # driving after the script dies is the one failure worth this much care.
        if car is not None:
            car.close()


if __name__ == "__main__":
    sys.exit(main())
