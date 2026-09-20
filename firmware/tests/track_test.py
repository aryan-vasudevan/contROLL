#!/usr/bin/env python3
"""Tests for the detection tracker.

The thing worth testing here is not that IoU is computed correctly -- it is
that an id stays attached to the same object while the object moves, and that
a reply which arrives late cannot undo newer ones. The second is the bug the
concurrent detector introduces, and it is invisible in a still scene.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))
from track import Tracker, iou            # noqa: E402

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def box(x, y, w, h, label="person", conf=0.9):
    return (x, y, w, h, label, conf)


print("=== IoU ===")
check(iou(box(0, 0, 10, 10), box(0, 0, 10, 10)) == 1.0, "identical boxes score 1")
check(iou(box(0, 0, 10, 10), box(50, 50, 10, 10)) == 0.0, "disjoint boxes score 0")
# Half-overlapping: intersection 50, union 150.
check(abs(iou(box(0, 0, 10, 10), box(5, 0, 10, 10)) - 50 / 150) < 1e-9,
      "half-overlap scores intersection over union, not over area")

print()
print("=== identity across frames ===")
t = Tracker()
first = t.update([box(10, 10, 20, 40)], seq=1, now=0.0)
check(len(first) == 1 and first[0][6] != 0, "a new box gets a non-zero id")
tid = first[0][6]

# Walking across the frame a few pixels at a time, as a real target does.
moved = t.update([box(14, 10, 20, 40)], seq=2, now=0.1)
check(moved[0][6] == tid, "a box that moved slightly keeps its id")
moved = t.update([box(18, 11, 21, 40)], seq=3, now=0.2)
check(moved[0][6] == tid, "and keeps it as the box drifts and resizes")

jumped = t.update([box(120, 10, 20, 40)], seq=4, now=0.3)
check(jumped[0][6] != tid, "a box that teleported is a different track")

print()
print("=== two targets do not swap ===")
t = Tracker()
a, b = t.update([box(10, 10, 20, 40), box(100, 10, 20, 40)], seq=1, now=0.0)
# Reported in the opposite order next time, which a detector is free to do.
out = t.update([box(104, 10, 20, 40), box(13, 10, 20, 40)], seq=2, now=0.1)
by_x = {o[0]: o[6] for o in out}
check(by_x[13] == a[6], "the left target keeps the left id despite reordering")
check(by_x[104] == b[6], "the right target keeps the right id")

print()
print("=== a label change is not the same object ===")
t = Tracker()
p = t.update([box(10, 10, 20, 40, "person")], seq=1, now=0.0)
c = t.update([box(10, 10, 20, 40, "chair")], seq=2, now=0.1)
check(p[0][6] != c[0][6], "a chair in the same place is not the person")

print()
print("=== out-of-order replies ===")
# This is the one the pool makes possible: eight requests in flight means a
# slow one can land after a fast one sent later.
t = Tracker()
tid = t.update([box(10, 10, 20, 40)], seq=10, now=0.0)[0][6]
t.update([box(18, 10, 20, 40)], seq=14, now=0.1)          # same target, moved right
check(t.find(tid, now=0.1, predict=False)[0] == 18, "the newer reply moved the track")

t.update([box(10, 10, 20, 40)], seq=11, now=0.15)         # sent earlier, landed later
check(t.find(tid, now=0.15, predict=False)[0] == 18,
      "a reply older than one already applied does not move the track back")
check(len(t.current(now=0.15)) == 1,
      "and does not spawn a second track for the same object")

print()
print("=== expiry ===")
t = Tracker(ttl=1.0)
t.update([box(10, 10, 20, 40)], seq=1, now=0.0)
check(len(t.current(now=0.5)) == 1, "a track is live inside its ttl")
check(len(t.current(now=2.0)) == 0, "and gone outside it")
check(t.find(1, now=2.0) is None, "find does not return an expired track")

# A detection dropping out for one frame must not restart the track, or the
# badge's selection would be lost every time the model blinks.
t = Tracker(ttl=1.0)
tid = t.update([box(10, 10, 20, 40)], seq=1, now=0.0)[0][6]
again = t.update([box(11, 10, 20, 40)], seq=2, now=0.4)
check(again[0][6] == tid, "a one-frame gap does not reset the id")

print()
print("=== prediction bridges the cloud delay ===")
t = Tracker()
tid = t.update([box(10, 10, 20, 40)], seq=1, now=0.0)[0][6]
t.update([box(22, 10, 20, 40)], seq=2, now=0.2)      # walking right, 60 px/s
ahead = t.find(tid, now=0.4)
check(ahead[0] > 26, f"a fifth of a second later the box has moved on ({ahead[0]})")
# capture-time anchoring: a reply that took half a second draws ahead of
# where it measured, because the person kept walking while it was in flight
t2 = Tracker()
t2id = t2.update([(10, 10, 20, 40, "person", 0.9)], seq=1, now=0.5, captured_at=0.0)[0][6]
t2.update([(22, 10, 20, 40, "person", 0.9)], seq=2, now=0.7, captured_at=0.2)
late = t2.find(t2id, now=0.7)
check(late[0] > 30, f"a stale reply is drawn ahead of its measurement ({late[0]})")
check(t.find(tid, now=0.4, predict=False)[0] == 22, "raw position still available")
far = t.find(tid, now=5.0)
check(far is None, "prediction never outlives the ttl")
still = Tracker()
sid = still.update([box(50, 10, 20, 40)], seq=1, now=0.0)[0][6]
still.update([box(50, 10, 20, 40)], seq=2, now=0.2)
check(still.find(sid, now=0.5)[0] == 50, "a still person does not drift")

print()
print("=== find ===")
t = Tracker()
out = t.update([box(10, 10, 20, 40), box(100, 10, 20, 40)], seq=1, now=0.0)
want = out[1][6]
got = t.find(want, now=0.0)
check(got is not None and got[0] == 100, "find returns the box with that id")
check(t.find(0, now=0.0) is None, "id 0 never matches; it means nothing selected")

print()
if fails:
    print(f"SOME TESTS FAILED ({len(fails)})")
    sys.exit(1)
print("tracker ok")
