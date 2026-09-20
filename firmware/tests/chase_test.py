#!/usr/bin/env python3
"""Tests for the visual-servoing controller.

This is the code that points a car at a person and drives it at them, so the
properties worth asserting are the ones whose failure is physical: it must
stop when it arrives, it must not command a speed the motors ignore, and left
and right must be mirror images so it cannot veer in one direction only.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pi5"))
from chase import APPROACH, ARRIVED, PIVOT, ChaseConfig, chase   # noqa: E402

W, H = 160, 120
cfg = ChaseConfig()
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def at(centre_x, fill):
    """A box centred at centre_x filling `fill` of the frame height."""
    h = int(fill * H)
    w = max(1, h // 3)                    # a person is roughly 1:3
    return (int(centre_x - w / 2), (H - h) // 2, w, h)


print("=== arriving ===")
left, right, status = chase(at(W / 2, 0.92), W, H, cfg)
check(status == ARRIVED, "a centred target past stop_fill is arrived")
check(left == 0.0 and right == 0.0, "and the motors are stopped")

left, right, status = chase(at(W / 2, 0.20), W, H, cfg)
check(status == APPROACH, "a small centred target is approached")
check(left > 0 and right > 0, "both tracks drive forward")
check(abs(left - right) < 1e-9, "dead centre means no steering")

print()
print("=== the approach slows down ===")
far = chase(at(W / 2, 0.10), W, H, cfg)[0]
near = chase(at(W / 2, 0.75), W, H, cfg)[0]
check(far > near, "a nearer target is approached more slowly than a far one")
check(near >= cfg.min_speed, "but never below the speed the motors respond to")

print()
print("=== pointing at it ===")
left, right, status = chase(at(5, 0.20), W, H, cfg)
check(status == PIVOT, "a target near the left edge is pivoted to, not arced at")
check(left < 0 and right > 0, "pivoting left runs the tracks in opposition")

left, right, status = chase(at(W - 5, 0.20), W, H, cfg)
check(status == PIVOT and left > 0 and right < 0, "and mirrored on the right")

print()
print("=== symmetry ===")
# A car that steers harder one way than the other drives in a spiral, and on
# a two-track chassis that is a sign-error away at all times.
for fill in (0.12, 0.25, 0.40):
    for offset in (10, 25, 45, 70):
        lb, rb, sb = chase(at(W / 2 - offset, fill), W, H, cfg)
        lm, rm, sm = chase(at(W / 2 + offset, fill), W, H, cfg)
        check(sb == sm and abs(lb - rm) < 1e-9 and abs(rb - lm) < 1e-9,
              f"offset {offset:+3d} at fill {fill:.2f} mirrors exactly")

print()
print("=== the deadband ===")
# Just inside the deadband: detector jitter must not make the car hunt.
nudge = cfg.deadband * (W / 2) * 0.9
left, right, _ = chase(at(W / 2 + nudge, 0.20), W, H, cfg)
check(abs(left - right) < 1e-9, "error inside the deadband does not steer")
nudge = cfg.deadband * (W / 2) * 1.6
left, right, _ = chase(at(W / 2 + nudge, 0.20), W, H, cfg)
check(left > right, "error outside it does")

print()
print("=== the stall floor ===")
# An L298N and a gearbox below about a quarter duty sits and buzzes. Any
# non-zero command must be a command the car can actually act on.
seen = 0
for fill in (0.05, 0.15, 0.25, 0.35, 0.45, 0.54):
    for centre in range(5, W, 7):
        left, right, status = chase(at(centre, fill), W, H, cfg)
        for speed in (left, right):
            if speed != 0.0:
                seen += 1
                if abs(speed) < cfg.min_speed - 1e-9:
                    check(False, f"commanded {speed:.3f}, below the stall floor")
                if abs(speed) > cfg.max_speed + 1e-9:
                    check(False, f"commanded {speed:.3f}, above max_speed")
check(seen > 100, f"exercised {seen} non-zero commands, all within bounds")

print()
print("=== arrived but off centre ===")
left, right, status = chase(at(20, 0.92), W, H, cfg)
check(status == PIVOT, "at the stop distance it still turns to keep facing")
check(abs(left + right) < 1e-9, "turning on the spot, not creeping forward")

print()
print("=== nothing to chase ===")
left, right, status = chase(None, W, H, cfg)
check(left == 0.0 and right == 0.0 and status == ARRIVED,
      "no box means stopped, not last-known-good")
check(chase(at(W / 2, 0.2), 0, 0, cfg)[:2] == (0.0, 0.0),
      "a zero-sized frame cannot divide by zero")

print()
if fails:
    print(f"SOME TESTS FAILED ({len(fails)})")
    sys.exit(1)
print("chase ok")
