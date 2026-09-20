#!/usr/bin/env python3
"""
Mode switching and the vision follow law, kept apart from anything that talks
to hardware so both can be tested on a laptop.

Two pieces:

    ModeSwitch   turns "the badge saw tag X" into MANUAL <-> AUTO
    follow()     turns detections into left and right track speeds

Neither imports depthai, gpiozero or a socket. badgedrive.py wires them to the
real thing; selftest.py exercises them with no camera, no Pi and no car.
"""

from __future__ import annotations

from dataclasses import dataclass

MANUAL = "MANUAL"
AUTO = "AUTO"


@dataclass(frozen=True)
class Box:
    """One detection, in normalised frame coordinates."""
    label: str
    cx: float       # 0 at the left edge, 1 at the right
    cy: float
    area: float     # fraction of the frame the box covers


class ModeSwitch:
    """
    Holds the current mode and decides when a tag changes it.

    The badge sends a tap counter rather than "a tag is present", so the
    decision here is on a *change* in that counter. That makes the switch
    idempotent: the badge repeats the same count 20 times a second, a dropped
    packet loses nothing, and a duplicated one toggles nothing.
    """

    def __init__(self, tag_uid: str, cooldown: float = 1.5) -> None:
        self.tag_uid = tag_uid.upper().replace(":", "").replace(" ", "")
        self.cooldown = cooldown
        self.mode = MANUAL          # every boot starts manual, always
        self._last_seq: int | None = None
        # Minus infinity, not zero: a freshly started Pi has no history, and a
        # cooldown measured from t=0 would swallow the first tap of the session.
        self._last_change = float("-inf")
        self.rejected: str | None = None   # last unknown tag, for the console

    def on_packet(self, uid: str | None, seq: int | None, now: float) -> bool:
        """Feed one badge packet. True when the mode just changed."""
        self.rejected = None
        if seq is None:
            return False

        # First packet of a session: adopt the badge's count without acting on
        # it. The badge keeps counting across a Pi restart, and a restart is
        # not a tap.
        if self._last_seq is None:
            self._last_seq = seq
            return False

        if seq == self._last_seq:
            return False
        self._last_seq = seq

        # Any tag will read. Only one tag is the car's.
        if (uid or "").upper() != self.tag_uid:
            self.rejected = uid or "?"
            return False

        if now - self._last_change < self.cooldown:
            return False

        self.mode = AUTO if self.mode == MANUAL else MANUAL
        self._last_change = now
        return True

    def force_manual(self) -> bool:
        """HOME, or anything else that must put a human back in charge."""
        if self.mode == MANUAL:
            return False
        self.mode = MANUAL
        return True


# --- the follow law --------------------------------------------------------

AUTO_SPEED = 0.45       # deliberately below the manual default of 0.60
TARGET_AREA = 0.25      # how big the subject should look when at the right distance
STEER_GAIN = 1.6
THROTTLE_GAIN = 2.5
STEER_DEADBAND = 0.05   # ignore this much off-centre, or it hunts
AREA_DEADBAND = 0.04
STALE_AFTER = 0.5       # seconds; older than this and we do not trust it


def pick(boxes: list[Box], target: str = "person") -> Box | None:
    """The biggest matching box, which is the nearest one."""
    matches = [b for b in boxes if b.label == target]
    return max(matches, key=lambda b: b.area) if matches else None


def follow(boxes: list[Box], age: float, *, target: str = "person",
           speed: float = AUTO_SPEED) -> tuple[float, float]:
    """
    Detections to track speeds.

    Returns a dead stop whenever there is nothing to follow or the detections
    are stale. Coasting on a stale box is how a robot drives into a wall a
    second after the person it was following left the frame.
    """
    if age > STALE_AFTER:
        return 0.0, 0.0
    box = pick(boxes, target)
    if box is None:
        return 0.0, 0.0

    offset = box.cx - 0.5
    steer = 0.0 if abs(offset) < STEER_DEADBAND else offset * 2.0 * STEER_GAIN

    gap = TARGET_AREA - box.area
    throttle = 0.0 if abs(gap) < AREA_DEADBAND else gap * THROTTLE_GAIN

    left = throttle + steer
    right = throttle - steer

    # Normalise rather than clip, for the same reason the manual path does:
    # clipping a turn at full throttle quietly straightens the car out.
    peak = max(abs(left), abs(right), 1.0)
    return left / peak * speed, right / peak * speed
