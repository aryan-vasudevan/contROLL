#!/usr/bin/env python3
"""Generate detection packets with the firmware's real packer, and say what a
correct parser must recover from them.

This exists because of a specific mistake recorded in the handoff: the overlay
was once called "wire format verified" when what had actually happened was
that a Python script packed boxes and unpacked them again with a Python
re-implementation of the C++ parsing. That proves Python agrees with itself.

So the expectations written here are the *inputs* -- the box values we asked
for -- never a re-parse of the bytes. The bytes are produced by the same
struct formats oak_stream.py sends with, and the C++ parser lifted out of
badgecam_main.cpp has to recover the inputs from them. Nothing in the loop is
a reimplementation of anything else in it.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))
from oak_stream import DET_BOX, DET_HEADER, DET_MAGIC     # noqa: E402

# (label, img_w, img_h, [(x, y, w, h, conf, id, label)], mangle)
#
# `mangle` chops the finished packet, which is how a real one arrives when a
# datagram is truncated or a length field is wrong. A parser that indexes past
# its buffer on those is a reboot on the badge, so they are tested first-class
# rather than assumed away.
CASES = [
    ("empty", 160, 120, [], None),
    ("one", 160, 120, [(10, 20, 30, 40, 91, 7, "person")], None),
    ("many", 160, 120, [
        (0, 0, 16, 32, 50, 1, "person"),
        (140, 88, 20, 32, 99, 2, "person"),
        (70, 40, 21, 55, 75, 254, "person"),
    ], None),
    # A box partly off the left edge: x is signed on the wire precisely so
    # this survives, and a detector really does report it when someone walks
    # out of frame.
    ("negative", 160, 120, [(-12, -5, 40, 60, 60, 9, "person")], None),
    ("maxid", 160, 120, [(1, 2, 3, 4, 100, 255, "person")], None),
    ("longlabel", 160, 120, [(5, 5, 10, 10, 80, 3, "abcdefghijklmno")], None),
    ("bigframe", 1920, 1080, [(960, 540, 100, 200, 88, 4, "person")], None),
    # Three boxes claimed, bytes for one and a bit. Must return what is whole.
    ("truncated", 160, 120, [
        (10, 10, 20, 20, 90, 1, "person"),
        (30, 30, 20, 20, 90, 2, "person"),
        (50, 50, 20, 20, 90, 3, "person"),
    ], 11 + 11 + 6 + 4),
    # Header only: the count says one, there are no box bytes at all.
    ("headeronly", 160, 120, [(10, 10, 20, 20, 90, 1, "person")], 11),
    # Cut mid-header.
    ("stub", 160, 120, [], 5),
]


def pack(img_w, img_h, boxes, frame_id=1234):
    blob = DET_MAGIC + struct.pack(DET_HEADER, len(boxes), frame_id, img_w, img_h)
    for x, y, w, h, conf, tid, label in boxes:
        name = label.encode()[:15]
        blob += struct.pack(DET_BOX, x, y, w, h, conf, tid, len(name)) + name
    return blob


def expected(name, img_w, img_h, boxes, mangle):
    """What the C++ must print. Derived from the inputs, never from the bytes."""
    if mangle is None:
        keep = boxes
    else:
        # How many whole boxes fit in the bytes that survived. 11 header bytes,
        # then 11 + len(label) each.
        room = mangle - 11
        keep = []
        for b in boxes:
            need = 11 + len(b[6].encode()[:15])
            if room < need:
                break
            room -= need
            keep.append(b)
        if mangle < 11:
            return f"{name} -1"            # too short to be a packet at all
    parts = [f"{name} {len(keep)} {img_w} {img_h}"]
    for x, y, w, h, conf, tid, label in keep:
        parts.append(f"{x},{y},{w},{h},{conf},{tid},{label[:15]}")
    return " ".join(parts)


if __name__ == "__main__":
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    packets, expects = [], []

    for name, img_w, img_h, boxes, mangle in CASES:
        blob = pack(img_w, img_h, boxes)
        if mangle is not None:
            blob = blob[:mangle]
        packets.append(f"{name} {blob.hex()}")
        expects.append(expected(name, img_w, img_h, boxes, mangle))

    # Things that are not detection packets at all. The parser shares a socket
    # with the video stream, so it sees every BJPF chunk that arrives and must
    # reject them rather than parse them as boxes.
    packets.append("video " + (b"BJPF" + struct.pack("<HBBH", 1, 0, 1, 4) + b"\xde\xad\xbe\xef").hex())
    expects.append("video -1")
    packets.append("oldmagic " + (b"BDET" + struct.pack("<BH", 1, 7)
                                  + struct.pack("<hhHHBB", 1, 2, 3, 4, 50, 6) + b"person").hex())
    expects.append("oldmagic -1")
    packets.append("garbage " + bytes(range(40)).hex())
    expects.append("garbage -1")
    packets.append("empty_datagram ")
    expects.append("empty_datagram -1")

    with open(os.path.join(out_dir, "packets.txt"), "w") as f:
        f.write("\n".join(packets) + "\n")
    with open(os.path.join(out_dir, "expect.txt"), "w") as f:
        f.write("\n".join(expects) + "\n")
    print(f"  {len(packets)} packets from the real packer")
