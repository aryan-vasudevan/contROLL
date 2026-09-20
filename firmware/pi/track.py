#!/usr/bin/env python3
"""
Stable identities for detections across frames.

A detector answers "there is a person at (x, y, w, h)" and nothing more. Two
consecutive replies that both say "person" do not say whether it is the same
person. Nothing that follows a target can be built on that: to chase a
detection the car has to agree with the badge about *which* detection, from
one frame to the next, and a bare list of boxes reshuffles whenever the model
feels like it.

So each box gets an id here, and the id is what the badge selects and the car
chases. Association is greedy by IoU: the strongest overlap between a new box
and a live track claims it, then the next strongest, and anything left over
starts a new track.

IoU rather than centre distance because it is scale-aware. A person walking
towards the camera moves their centre very little while their box doubles in
height, and centre distance happily confuses them with someone standing behind
them; IoU does not.

Out-of-order results are the other half of the job, and they are new. The
cloud detector runs several requests in flight at once, so replies do NOT come
back in the order the frames were sent -- a slow request for frame 100 can
land after a fast one for frame 104. Feeding that to a tracker walks the boxes
backwards in time. Every update carries the sequence number of the frame it
came from, and anything older than what has already been applied is dropped.
"""

from __future__ import annotations

import time

# Two boxes must overlap at least this much to be considered the same thing.
# Low, deliberately: detections jitter frame to frame, and at 160x120 a person
# a few metres away is a small box where a few pixels of jitter is a large
# fraction of the area. Too high and every frame starts a new track; too low
# and two people standing together swap identities.
MIN_IOU = 0.25

# A track survives this long without being seen before it is forgotten.
#
# The pool infers on roughly every frame, so detections land about 50 ms
# apart -- but the phone-tethered uplink stalls for 2-5 s at a time when the
# cell link hiccups, and a ttl shorter than a stall kills the identity of
# whoever was being followed, selection and all. 1.8 s rides out most of a
# spike, and prediction keeps the box moving with its person meanwhile. Not
# longer:
# a track that outlives its subject is a ghost box on the badge, and worse,
# something the car will happily drive at. If the detection rate is dropped
# (RF_WORKERS=1, or a non-zero interval) raise this to match, or tracks will
# expire between detections and ids will churn.
TRACK_TTL = 2.2

# Ids are one byte on the wire, and 0 means "nothing selected".
MAX_ID = 255


def iou(a, b) -> float:
    """Intersection over union of two (x, y, w, h) boxes."""
    ax, ay, aw, ah = a[0], a[1], a[2], a[3]
    bx, by, bw, bh = b[0], b[1], b[2], b[3]

    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


# Never extrapolate further than this. The window has to cover the WHOLE
# age of a detection -- dispatch wait plus the cloud round trip is ~450 ms on
# a good link -- with room for a latency spike on top.
PREDICT_CAP = 0.9
# Association may look further ahead than drawing does. A drawn box flung two
# seconds along a guessed velocity looks broken; a MATCH attempted there
# merely re-links a walker to their own identity after an uplink stall, and
# the IoU gate still rejects it if the guess was wrong.
ASSOC_CAP = 2.0
# Sole-survivor re-link. IoU association fails outright while the CAR turns:
# the camera pans, everyone slides sideways in frame faster than prediction
# can follow, and the person being chased comes back as a stranger -- which
# aborts the chase mid-turn. But when exactly one track of a label is lost
# and exactly one new box of that label appears, there is no ambiguity to
# protect against; re-linking them is the only sane reading. Distance-gated
# so a person leaving and another entering opposite sides still count as two.
RELINK_DIST = 90.0        # px at the 160x120 stream resolution
# And never at more than this speed: on a 160-wide frame, 200 px/s is a
# person jogging past at close range; anything faster is an association
# glitch, not motion, and predicting along it would fling the box.
MAX_VEL = 200.0


class Track:
    __slots__ = ("id", "box", "label", "conf", "seen_at", "anchor", "hits",
                 "vx", "vy")

    def __init__(self, tid, box, label, conf, now, anchor=None):
        self.id = tid
        self.box = box
        self.label = label
        self.conf = conf
        self.seen_at = now          # arrival time; governs expiry
        # When the FRAME behind this box was captured. The two differ by the
        # whole inference round trip, and prediction must bridge that gap,
        # not just the time since the reply landed -- anchoring at arrival
        # left every box permanently ~450 ms behind its subject.
        self.anchor = now if anchor is None else anchor
        self.hits = 1
        self.vx = 0.0        # px/s, smoothed
        self.vy = 0.0

    def predicted(self, now, cap=PREDICT_CAP):
        """The box moved to where its subject probably is by `now`.

        Detections arrive several hundred milliseconds old -- the cloud round
        trip -- so drawing them raw puts every box visibly behind a walking
        person. Extrapolating along the measured velocity closes most of that
        gap for the price of being slightly wrong when someone changes
        direction, which reads far better than always trailing.
        """
        dt = min(max(now - self.anchor, 0.0), cap)
        return (int(self.box[0] + self.vx * dt),
                int(self.box[1] + self.vy * dt),
                self.box[2], self.box[3])


class Tracker:
    """Turns a stream of box lists into a stream of identified box lists."""

    def __init__(self, min_iou=MIN_IOU, ttl=TRACK_TTL, enter_conf=0.0):
        self.min_iou = min_iou
        self.ttl = ttl
        # Hysteresis. A NEW track must arrive at least this confident; an
        # existing one keeps updating at whatever the detector still emits.
        # This is the difference between "a hand can never become a box"
        # (enter high) and "a real person flickers whenever their score dips
        # crossing a doorway" (stay low). One threshold cannot do both jobs.
        self.enter_conf = enter_conf
        self._tracks: list[Track] = []
        self._next_id = 1
        self._last_seq = -1

    def _allocate(self) -> int:
        """Next free id. Wraps, and never returns 0 or one currently in use."""
        live = {t.id for t in self._tracks}
        for _ in range(MAX_ID):
            tid = self._next_id
            self._next_id = self._next_id % MAX_ID + 1
            if tid not in live:
                return tid
        return self._next_id       # every id live at once; reuse is the least bad

    def update(self, boxes, seq=None, now=None, captured_at=None):
        """Associate `boxes` with existing tracks and return them with ids.

        `boxes` are (x, y, w, h, label, conf). The return adds the id as a
        seventh element. `seq` is the frame number the boxes came from; a seq
        older than one already applied is discarded and the previous state is
        returned unchanged, because replies can overtake each other in flight.

        `captured_at` is when the frame these boxes describe left the camera;
        velocity and prediction are computed against it, so a slow reply
        automatically gets a longer forward glide.
        """
        now = time.monotonic() if now is None else now
        captured_at = now if captured_at is None else captured_at

        if seq is not None:
            if seq <= self._last_seq:
                return self.current(now)      # a straggler; we already moved on
            self._last_seq = seq

        # Anything not seen recently is gone before matching, so a stale track
        # cannot capture a new box.
        self._tracks = [t for t in self._tracks if now - t.seen_at <= self.ttl]

        # Score every (new box, live track) pair, then take them strongest
        # first. Greedy rather than optimal: a Hungarian assignment would be
        # better with many overlapping targets and is not worth it for the
        # handful of boxes that fit on a 320x240 panel.
        pairs = []
        for bi, box in enumerate(boxes):
            for ti, track in enumerate(self._tracks):
                if track.label != box[4]:
                    continue                  # a person never becomes a chair
                # Match against the track's PREDICTED position at this frame's
                # capture time, not its last raw box. A walker covers 30-40 px
                # during one uplink stall; their old box no longer overlaps
                # where they are, and matching raw meant every stall broke the
                # identity of exactly the people who were moving -- the ones
                # being chased. Their predicted box moves with them.
                score = iou(box, track.predicted(captured_at, cap=ASSOC_CAP))
                if score >= self.min_iou:
                    pairs.append((score, bi, ti))
        pairs.sort(reverse=True)

        taken_box: set[int] = set()
        taken_track: set[int] = set()
        assigned: dict[int, Track] = {}
        for _score, bi, ti in pairs:
            if bi in taken_box or ti in taken_track:
                continue
            taken_box.add(bi)
            taken_track.add(ti)
            assigned[bi] = self._tracks[ti]

        # The sole-survivor pass, before new tracks are minted.
        left_boxes = [bi for bi in range(len(boxes)) if bi not in taken_box]
        left_tracks = [ti for ti in range(len(self._tracks)) if ti not in taken_track]
        if len(left_boxes) == 1 and len(left_tracks) == 1:
            bi, ti = left_boxes[0], left_tracks[0]
            box, track = boxes[bi], self._tracks[ti]
            if track.label == box[4]:
                bx, by = box[0] + box[2] / 2.0, box[1] + box[3] / 2.0
                tx, ty = track.box[0] + track.box[2] / 2.0, track.box[1] + track.box[3] / 2.0
                if ((bx - tx) ** 2 + (by - ty) ** 2) ** 0.5 <= RELINK_DIST:
                    assigned[bi] = track
                    # The jump was mostly camera rotation, not subject motion;
                    # a velocity fitted to it would fling the prediction.
                    track.vx = track.vy = 0.0

        out = []
        for bi, box in enumerate(boxes):
            track = assigned.get(bi)
            if track is None:
                if box[5] < self.enter_conf:
                    continue          # not confident enough to become anything
                track = Track(self._allocate(), box[:4], box[4], box[5], now,
                              anchor=captured_at)
                self._tracks.append(track)
            else:
                dt = captured_at - track.anchor
                if 0.0 < dt < 1.0:
                    # Blend rather than replace: detector jitter at 160x120 is
                    # a few px, and raw frame-to-frame velocity from that is
                    # noise. 60/40 toward the new measurement follows a real
                    # turn within a couple of detections.
                    nvx = (box[0] - track.box[0]) / dt
                    nvy = (box[1] - track.box[1]) / dt
                    track.vx = max(-MAX_VEL, min(MAX_VEL, 0.6 * nvx + 0.4 * track.vx))
                    track.vy = max(-MAX_VEL, min(MAX_VEL, 0.6 * nvy + 0.4 * track.vy))
                track.box = box[:4]
                track.conf = box[5]
                track.seen_at = now
                track.anchor = captured_at
                track.hits += 1
            out.append((box[0], box[1], box[2], box[3], box[4], box[5], track.id))
        return out

    def current(self, now=None, predict=True):
        """The live tracks, moved to where their subjects should be by now."""
        now = time.monotonic() if now is None else now
        out = []
        for t in self._tracks:
            if now - t.seen_at > self.ttl:
                continue
            x, y, w, h = t.predicted(now) if predict else t.box
            out.append((x, y, w, h, t.label, t.conf, t.id))
        return out

    def find(self, tid, now=None, predict=True):
        """One track by id, or None. This is what the chase loop asks."""
        now = time.monotonic() if now is None else now
        for t in self._tracks:
            if t.id == tid and now - t.seen_at <= self.ttl:
                x, y, w, h = t.predicted(now) if predict else t.box
                return (x, y, w, h, t.label, t.conf, t.id)
        return None
