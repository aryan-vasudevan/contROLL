#!/usr/bin/env python3
"""
Drive the car towards a detection, using nothing but the camera view.

There is no range finder, no encoder and no map. The only thing known about
the target is the rectangle the detector drew around it, so both questions the
controller has to answer are answered from that rectangle:

    which way is it     the horizontal offset of the box centre from the
                        centre of the frame

    how far away is it  the height of the box as a fraction of the frame

Height rather than width for the distance proxy. A standing person's width
changes when they move their arms or turn side-on; their height barely changes
at all until they are close enough that the frame crops them, and a cropped
box reading as "very close" happens to be exactly right.

This is one pure function on purpose. Everything that needs a clock, a motor
or a socket lives in badgedrive.py, so the part with the actual decisions in
it can be tested on a laptop -- which matters, because getting this wrong
means a car driving into someone rather than a failed assertion.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChaseConfig:
    # Stop once the target fills this much of the frame's height.
    #
    # Worked out from geometry rather than guessed, because the first guess
    # (0.55) was wrong in a way that looked like a refusal to drive: with the
    # OAK's ~55 degree vertical field of view, a standing adult fills 55% of
    # the frame from about THREE metres -- so indoors the controller declared
    # "arrived" from across the room and never moved. 0.85 is that same
    # person at roughly 1.8-2 m: close enough to be an obvious arrival, far
    # enough that nobody gets nudged.
    stop_fill: float = 0.85
    # Below this the target is far off and worth full approach speed. Between
    # the two the car eases off, so it arrives slowly instead of braking.
    slow_fill: float = 0.40

    # Ignore this much centre error. Detector boxes jitter by a few pixels
    # every frame and without a deadband the car hunts left and right forever
    # while sitting still.
    deadband: float = 0.06
    # Beyond this much error, turn on the spot instead of arcing. A tracked
    # car arcs badly at low speed, and a target 40% of the frame off centre is
    # quicker to point at than to curve towards.
    pivot_err: float = 0.34
    steer_gain: float = 1.5

    # Deliberately slower than the 0.60 a human gets. Detections are a few
    # hundred milliseconds old by the time they arrive, and every bit of speed
    # turns that staleness into distance.
    max_speed: float = 0.60
    # Gearmotors through an L298N do not turn below roughly a third of duty
    # once rolling -- below that they sit and buzz. Heard on the real car:
    # 0.28 was a hum, not a wheel. Starting from rest needs more still, which
    # is the kick in badgedrive.py, not a number here.
    min_speed: float = 0.35


ARRIVED = "arrived"
PIVOT = "pivot"
APPROACH = "approach"


def _floor(speed: float, cfg: ChaseConfig) -> float:
    """Keep a non-zero command above the stall floor, preserving direction."""
    if speed == 0.0:
        return 0.0
    magnitude = max(abs(speed), cfg.min_speed)
    return magnitude if speed > 0 else -magnitude


def chase(box, img_w: int, img_h: int, cfg: ChaseConfig = ChaseConfig()):
    """Wheel speeds that move the car towards `box`.

    `box` is (x, y, w, h, ...) in source-image pixels, the same coordinates the
    detector and the badge overlay use. Returns (left, right, status), each
    speed -1.0 to 1.0.
    """
    if box is None or img_w <= 0 or img_h <= 0:
        return 0.0, 0.0, ARRIVED

    x, y, w, h = box[0], box[1], box[2], box[3]

    # How far off centre, as a fraction of a half-frame: -1 hard left, +1 hard
    # right. Using a half-frame as the unit means the gain does not have to be
    # retuned when the stream resolution changes.
    centre = x + w / 2.0
    err = (centre - img_w / 2.0) / (img_w / 2.0)
    err = max(-1.0, min(1.0, err))

    fill = max(0.0, min(1.0, h / float(img_h)))

    # Close enough. Still allow turning, so the car keeps facing a target that
    # walks across in front of it rather than losing it the moment it arrives.
    if fill >= cfg.stop_fill:
        if abs(err) <= cfg.deadband:
            return 0.0, 0.0, ARRIVED
        turn = _floor(max(-1.0, min(1.0, cfg.steer_gain * err)) * cfg.max_speed, cfg)
        return turn, -turn, PIVOT

    steer = 0.0 if abs(err) <= cfg.deadband else err
    if abs(steer) >= cfg.pivot_err:
        # Point at it first. Driving forward while this far off aims the car
        # at where the target is not.
        turn = _floor(max(-1.0, min(1.0, cfg.steer_gain * steer)) * cfg.max_speed, cfg)
        return turn, -turn, PIVOT

    # Approach speed tapers between slow_fill and stop_fill, so the last part
    # of the approach is slow enough that a stale detection cannot carry the
    # car much past where it meant to stop.
    span = max(cfg.stop_fill - cfg.slow_fill, 1e-6)
    nearness = (fill - cfg.slow_fill) / span
    throttle = 1.0 - max(0.0, min(1.0, nearness))
    throttle = max(0.25, throttle)        # never crawl to a halt short of the target

    left = throttle + steer * cfg.steer_gain * 0.75
    right = throttle - steer * cfg.steer_gain * 0.75

    # Normalise rather than clip, for the reason badgedrive gives: clipping the
    # outer wheel at full throttle quietly straightens the car mid-corner.
    peak = max(abs(left), abs(right), 1.0)
    left = _floor(left / peak * cfg.max_speed, cfg)
    right = _floor(right / peak * cfg.max_speed, cfg)
    return left, right, APPROACH


if __name__ == "__main__":
    # A quick look at the controller's behaviour without a car attached.
    cfg = ChaseConfig()
    W, H = 160, 120
    print(f"frame {W}x{H}, stop at {cfg.stop_fill:.0%} fill\n")
    print(f"{'target':>22}  {'err':>6} {'fill':>5}   {'L':>6} {'R':>6}  status")
    cases = [
        ("centred, far",        (70, 40, 20, 20)),
        ("centred, mid",        (64, 30, 32, 45)),
        ("centred, arrived",    (50, 20, 60, 70)),
        ("slightly left",       (52, 35, 24, 30)),
        ("hard left",           (5, 35, 24, 30)),
        ("hard right",          (130, 35, 24, 30)),
        ("arrived, off centre", (10, 20, 60, 70)),
    ]
    for name, box in cases:
        left, right, status = chase(box, W, H, cfg)
        err = ((box[0] + box[2] / 2) - W / 2) / (W / 2)
        print(f"{name:>22}  {err:+6.2f} {box[3]/H:5.2f}   "
              f"{left:+6.2f} {right:+6.2f}  {status}")
