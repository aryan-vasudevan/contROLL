#!/usr/bin/env python3
"""
Stream MJPEG from an OAK-1 to the badge.

Run on the Pi, with the depthai venv:

    ~/oak/bin/python oak_stream.py

The OAK-1 has a hardware video encoder on its Myriad X, so the JPEG is
produced on the camera itself. The Pi never touches pixels -- it accepts a TCP
connection and forwards bytes. That matters because a Pi 5 has no hardware
JPEG encoder, so doing this in software would burn CPU for no reason.

Frames go out at the badge panel's native 320x240, so the badge never scales
anything either. The ESP32-C3 is the slowest thing in the chain and every
pixel it does not have to touch is worth having.

UDP rather than TCP, on purpose. A lost packet on a weak link makes TCP stop
the whole stream until it is retransmitted, which for video is backwards: a
frame that arrives late is worth less than the one after it. Over UDP a lost
packet costs one frame and the next is already on its way.

Frames are chunked by hand rather than left to IP fragmentation, so every
datagram fits inside an MTU:

    "BJPF" u16 frame_id  u8 chunk  u8 chunk_count  u16 length  <payload>

The badge asks for each frame, which is also how the Pi learns its address.

Useful without a badge:

    ~/oak/bin/python oak_stream.py --list           what devices are attached
    ~/oak/bin/python oak_stream.py --save 5         grab 5 frames to disk
"""

import argparse
import socket
import struct
import sys
import time

MAGIC = b"BJPF"
# Chunks stay under a 1500-byte MTU. Letting IP fragment a 10 KB datagram means
# losing the whole frame if any one fragment goes missing, and some of those
# fragments would be lost on exactly the weak link this is meant to survive.
CHUNK = 1400
DEFAULT_PORT = 14557          # 14555/14556 belong to the button link


def build_pipeline(dai, width, height, fps, quality):
    """Camera -> hardware MJPEG encoder. Returns (pipeline, output queue)."""
    pipeline = dai.Pipeline()

    cam = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
    # Ask the camera for the panel's exact resolution. The ISP does the scaling
    # on-device, so nothing downstream has to. The IMX378 is 4:3 and so is
    # 320x240, so there is no aspect mismatch to crop around.
    stream = cam.requestOutput((width, height), dai.ImgFrame.Type.NV12, fps=float(fps))

    enc = pipeline.create(dai.node.VideoEncoder).build(
        stream,
        frameRate=float(fps),
        profile=dai.VideoEncoderProperties.Profile.MJPEG,
        quality=int(quality),
    )

    out = getattr(enc, "out", None)
    if out is None:
        # Fail loudly and usefully rather than with an AttributeError, since
        # the depthai v3 API differs from every v2 example in circulation.
        attrs = [a for a in dir(enc) if not a.startswith("_")]
        raise RuntimeError(
            "VideoEncoder has no .out in this depthai build. Available: " + ", ".join(attrs))

    # maxSize 2, non-blocking: if the consumer falls behind we want the newest
    # frame, not a backlog. Latency matters more than completeness for video.
    return pipeline, out.createOutputQueue(maxSize=2, blocking=False)


def list_devices(dai):
    devices = dai.Device.getAllAvailableDevices()
    if not devices:
        print("no OAK devices found")
        print("  check: lsusb | grep 03e7   (2485 = unbooted, f63b = booted)")
        return 1
    for d in devices:
        print(f"  {d.deviceId}  {d.name}  {d.state}  {d.protocol}")
    return 0


def save_frames(dai, args):
    """Prove the camera produces valid JPEGs, with no badge in the picture."""
    pipeline, queue = build_pipeline(dai, args.width, args.height, args.fps, args.quality)
    with pipeline:
        pipeline.start()
        print(f"capturing {args.save} frame(s) at {args.width}x{args.height} q{args.quality}")
        for i in range(args.save):
            pkt = queue.get()
            jpeg = bytes(pkt.getData())
            name = f"oak_frame_{i:02d}.jpg"
            with open(name, "wb") as f:
                f.write(jpeg)
            # SOI/EOI markers are the cheap check that this really is a JPEG
            # and not, say, a raw NV12 buffer that silently came through.
            ok = jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9"
            print(f"  {name}  {len(jpeg):6d} bytes  {'valid JPEG' if ok else 'NOT A JPEG'}")
    return 0


def serve(dai, args):
    pipeline, queue = build_pipeline(dai, args.width, args.height, args.fps, args.quality)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.settimeout(1.0)

    print(f"oak_stream on {args.bind}:{args.port} (UDP)")
    print(f"  {args.width}x{args.height} @ {args.fps} fps, MJPEG quality {args.quality}")
    print("  waiting for the badge; ctrl-c to stop")

    frame_id = 0
    frames = dropped = total_bytes = 0
    last_report = time.monotonic()
    peer = None

    with pipeline:
        pipeline.start()
        try:
            while True:
                # One request, one frame. Over UDP this is also the only way we
                # learn where the badge is, and it doubles as the signal that it
                # is still alive and keeping up.
                try:
                    _, peer = sock.recvfrom(64)
                except socket.timeout:
                    try:
                        queue.tryGet()      # keep the camera's queue current
                    except Exception:
                        pass
                    continue

                pkt = queue.get()
                jpeg = bytes(pkt.getData())
                while True:
                    try:
                        newer = queue.tryGet()
                    except Exception:
                        newer = None
                    if newer is None:
                        break
                    jpeg = bytes(newer.getData())
                    dropped += 1

                frame_id = (frame_id + 1) & 0xFFFF
                total = (len(jpeg) + CHUNK - 1) // CHUNK
                for i in range(total):
                    part = jpeg[i * CHUNK:(i + 1) * CHUNK]
                    header = MAGIC + struct.pack("<HBBH", frame_id, i, total, len(part))
                    sock.sendto(header + part, peer)

                frames += 1
                total_bytes += len(jpeg)

                now = time.monotonic()
                if now - last_report >= 5.0:
                    span = now - last_report
                    print(f"  {frames / span:5.1f} fps   "
                          f"{total_bytes / span / 1024:6.1f} KB/s   "
                          f"{total_bytes // max(frames, 1):5d} B/frame   "
                          f"{dropped} stale dropped")
                    last_report, frames, total_bytes, dropped = now, 0, 0, 0
        except KeyboardInterrupt:
            print("\n  stopping")
        finally:
            sock.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Half the panel's resolution in each axis, so the badge scales up by an
    # exact factor of two. Decode cost scales with pixel count and it is the
    # badge's bottleneck, so a quarter of the pixels is most of the frame rate.
    # Softer, and much faster. 320x240 for a sharp, slow picture.
    ap.add_argument("--width", type=int, default=160)
    ap.add_argument("--height", type=int, default=120)
    ap.add_argument("--fps", type=int, default=30,
                    help="ceiling only; the badge asks for each frame, so the "
                         "real rate is whatever it can keep up with")
    ap.add_argument("--quality", type=int, default=70, help="MJPEG quality, 1-100")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--list", action="store_true", help="list attached OAK devices and exit")
    ap.add_argument("--save", type=int, metavar="N",
                    help="write N frames to .jpg files and exit; no badge needed")
    args = ap.parse_args()

    try:
        import depthai as dai
    except ImportError:
        print("depthai is not importable. Use the venv:\n"
              "    ~/oak/bin/python oak_stream.py", file=sys.stderr)
        return 1

    if args.list:
        return list_devices(dai)
    if args.save:
        return save_frames(dai, args)
    return serve(dai, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
