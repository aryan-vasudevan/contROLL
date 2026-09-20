#!/usr/bin/env python3
"""
A horn, played on the drive motors.

    python3 honk.py --dry-run
    python3 honk.py                 # on the car
    python3 honk.py --tune alarm

**There is no speaker anywhere in this project.** The badge has no audio
hardware of any kind -- that was checked across the whole board, and it is in
the handoff -- and nothing was added to the car either. So the only things
that can make a noise are the things already being driven.

A brushed motor on an H-bridge is a coil. PWM it and the coil vibrates at the
switching frequency, which is why `drive.py` notes that its 1 kHz carrier is
"audible but harmless". Move that frequency around on purpose and the motors
play notes. It is a real speaker, badly.

The trick is making sound without making movement. Torque follows duty cycle,
so the duty is held far below what it takes to break the gearboxes loose --
the coils buzz, the wheels stay put. `KICK_SPEED` in drive.py is 0.85 for a
reason: these gearboxes need most of the supply to start turning, and 8% does
not come close. Turn it up with --duty and the car will eventually creep, so
do that with the wheels off the ground.

It runs on its own thread and never blocks. That is not a nicety: the drive
loop in badgedrive.py has a 200 ms failsafe, and a honk that blocked it for a
second would be a car that carried on at its last speed while playing a tune.
Any real drive command cancels the horn instantly.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

try:
    from drive import Car, DriveError, PWM_HZ
except ImportError:
    sys.exit("drive.py must sit next to this script")

# Duty cycle while playing. 0.08 was inaudible over a hackathon floor; 0.15
# is properly loud and still under the ~0.25 where these gearboxes start to
# creep. If the car twitches while honking, this is the number to lower.
TONE_DUTY = 0.15

# (hertz, milliseconds). 0 Hz is a rest.
#
# Two pitches a fourth apart is what a car horn actually is -- a single tone
# reads as a fault rather than a honk.
TUNES = {
    "honk":  [(440, 180), (0, 40), (587, 320)],
    "beep":  [(880, 90)],
    "alarm": [(784, 150), (523, 150)] * 4,
    "chirp": [(1047, 60), (0, 30), (1319, 60)],
    # Played when the car reaches what it was chasing. Rising, so it reads as
    # "done" rather than "problem".
    "found": [(523, 90), (659, 90), (784, 160)],
}


class Horn:
    """Plays tunes on a Car's motors, on a background thread."""

    def __init__(self, car: Car, duty: float = TONE_DUTY) -> None:
        self.car = car
        self.duty = duty
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._yielded = False

    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def play(self, name: str = "honk") -> bool:
        """Start a tune. Returns False if one is already playing.

        Non-blocking by design -- see the note at the top of this file about
        the failsafe.
        """
        if self.active():
            return False
        notes = TUNES.get(name)
        if not notes:
            return False
        self._cancel.clear()
        self._yielded = False
        self._thread = threading.Thread(target=self._play, args=(notes,), daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        """Stop mid-note, wait for the thread, and silence the motors."""
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        self._restore()

    def yield_to_drive(self) -> None:
        """Give the motors up *now*, for a caller about to drive them.

        Non-blocking, and deliberately different from cancel(): cancel() joins
        the thread and then zeroes the pins, which is right when stopping but
        wrong here. badgedrive.py calls this from the loop that holds the
        200 ms failsafe, so it cannot afford to wait for a note to end -- and
        if the horn zeroed the pins on its way out it would undo the drive
        command that is about to be issued.

        Setting _yielded makes the horn thread exit without touching anything.
        The caller owns the motors from this moment.
        """
        self._yielded = True
        self._cancel.set()

    def restore_carrier(self) -> None:
        """Put the PWM frequency back to the drive carrier.

        A tone leaves the enable pin switching at a few hundred hertz. The
        motors would still run, but at whatever pitch the horn stopped on, so
        the car would drive humming a note.
        """
        for side in self._sides():
            try:
                side.enable.frequency = PWM_HZ
            except Exception:           # noqa: BLE001
                pass

    def _sides(self):
        return (self.car.left, self.car.right)

    def _restore(self) -> None:
        """Back to silence and the drive carrier. Every exit goes through here."""
        for side in self._sides():
            try:
                side.enable.value = 0.0
                side.enable.frequency = PWM_HZ
                side.forward.value = 0
                side.reverse.value = 0
            except Exception:           # noqa: BLE001  a closed pin, mid-shutdown
                pass

    def _play(self, notes) -> None:
        try:
            for hz, ms in notes:
                if self._cancel.is_set():
                    break
                for side in self._sides():
                    if hz <= 0:
                        side.enable.value = 0.0
                    else:
                        # Direction must be set for current to flow at all;
                        # both pins equal is a braked bridge and silent.
                        side.forward.value = 1
                        side.reverse.value = 0
                        side.enable.frequency = hz
                        side.enable.value = self.duty
                # Wait on the cancel event rather than sleeping, so a drive
                # command does not have to wait out the note.
                self._cancel.wait(ms / 1000.0)
        finally:
            # Not when yielded: something else is driving these motors now and
            # zeroing them here would countermand it.
            if not self._yielded:
                self._restore()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tune", default="honk", choices=sorted(TUNES))
    ap.add_argument("--duty", type=float, default=TONE_DUTY,
                    help=f"PWM duty while playing (default {TONE_DUTY}). "
                         "Louder is also closer to moving; wheels off the ground.")
    ap.add_argument("--dry-run", action="store_true", help="drive nothing")
    ap.add_argument("--all", action="store_true", help="play every tune in turn")
    args = ap.parse_args()

    car = None
    try:
        car = Car(dry_run=args.dry_run)
        horn = Horn(car, duty=args.duty)
        for name in (sorted(TUNES) if args.all else [args.tune]):
            total = sum(ms for _, ms in TUNES[name])
            print(f"  {name:6} {len(TUNES[name])} note(s), {total} ms")
            horn.play(name)
            while horn.active():
                time.sleep(0.02)
        print("done")
        return 0
    except DriveError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if car is not None:
            car.close()


if __name__ == "__main__":
    sys.exit(main())
