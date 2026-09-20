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
import os
import socket
import struct
import sys
import time

MAGIC = b"BJPF"
# Chunks stay under a 1500-byte MTU. Letting IP fragment a 10 KB datagram means
# losing the whole frame if any one fragment goes missing, and some of those
# fragments would be lost on exactly the weak link this is meant to survive.
CHUNK = 1400
DET_MAGIC = b"BDET"
DEFAULT_PORT = 14557          # 14555/14556 belong to the button link


def build_pipeline(dai, width, height, fps, quality, model=None, confidence=0.5,
                   nn_fps=None):
    """Camera -> hardware MJPEG encoder, and optionally a detector alongside.

    Returns (pipeline, frame queue, detection queue or None).

    The detector runs on the camera's own Myriad X, not on the Pi and not in
    the cloud. That is the whole point: inference costs the Pi nothing, needs
    no network, and keeps up with the frame rate instead of trailing it by a
    few hundred milliseconds.

    The network takes its own output from the camera rather than sharing the
    encoder's. Encoding at 160x120 while the network sees whatever resolution
    it wants means neither holds the other back, at the cost of pairing the
    newest boxes with the newest frame rather than exactly their own. At 30
    detections a second that is a frame of slip; the cloud path was thirty
    times worse.
    """
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

    det_queue, labels = None, []
    if model:
        # fps throttles how often the Myriad X runs the network, and with it
        # how much current the camera draws. An OAK that crashes and
        # reconnects in a loop under load is almost always a power problem --
        # a Pi 5 allows 600 mA total across its USB ports unless it is on a
        # 5 V/5 A supply with usb_max_current_enable=1 -- and detections do not
        # need to be as frequent as frames to look right.
        net = pipeline.create(dai.node.DetectionNetwork).build(
            cam, dai.NNModelDescription(model),
            **({"fps": float(nn_fps)} if nn_fps else {}))
        net.setConfidenceThreshold(float(confidence))
        try:
            labels = list(net.getClasses() or [])
        except Exception:
            labels = []
        det_queue = net.out.createOutputQueue(maxSize=2, blocking=False)
        print(f"  detector: {model} on the camera, confidence {confidence}"
              + (f", capped at {nn_fps} fps" if nn_fps else ""))
        if labels:
            print(f"  {len(labels)} classes, person is "
                  f"{'present' if 'person' in [l.lower() for l in labels] else 'ABSENT'}")

    # maxSize 2, non-blocking: if the consumer falls behind we want the newest
    # frame, not a backlog. Latency matters more than completeness for video.
    return pipeline, out.createOutputQueue(maxSize=2, blocking=False), det_queue, labels


def read_boxes(queue, labels, width, height, wanted):
    """Newest detections as (x, y, w, h, label, confidence) in source pixels.

    Drains rather than reads one: anything behind the newest is older than the
    frame about to be sent.
    """
    packet = None
    while True:
        try:
            newer = queue.tryGet()
        except Exception:
            newer = None
        if newer is None:
            break
        packet = newer
    if packet is None:
        return None                       # nothing new; keep what we had

    boxes = []
    for d in packet.detections:
        try:
            name = d.labelName
        except Exception:
            name = labels[d.label] if 0 <= d.label < len(labels) else str(d.label)
        if wanted and name.lower() not in wanted:
            continue
        # depthai gives 0..1 normalised corners; the badge wants source pixels.
        x = int(d.xmin * width)
        y = int(d.ymin * height)
        w = int((d.xmax - d.xmin) * width)
        h = int((d.ymax - d.ymin) * height)
        boxes.append((x, y, max(w, 1), max(h, 1), name[:15], float(d.confidence)))
    return boxes


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
    pipeline, queue, _, _ = build_pipeline(dai, args.width, args.height, args.fps, args.quality)
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
    wanted = {c.strip().lower() for c in args.classes.split(',') if c.strip()}
    pipeline, queue, det_queue, labels = build_pipeline(
        dai, args.width, args.height, args.fps, args.quality,
        model=args.nn, confidence=args.confidence, nn_fps=args.nn_fps)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.settimeout(1.0)

    print(f"oak_stream on {args.bind}:{args.port} (UDP)")
    print(f"  {args.width}x{args.height} @ {args.fps} fps, MJPEG quality {args.quality}")
    print("  waiting for the badge; ctrl-c to stop")

    detector = None
    if args.detect and not args.nn:
        # Cloud fallback, kept for when a model is wanted that the camera
        # cannot run. Measured at 323-1485 ms a call against serverless, so
        # boxes visibly trail anything that moves. --nn does not.
        import detect as detect_mod
        detector = detect_mod.from_env(interval=args.detect_interval)
        if detector is None:
            print("  --detect asked for but RF_API_KEY is not set; skipping")
        else:
            detector.start()
            print(f"  cloud detection every {args.detect_interval}s, out of band")

    boxes = []

    frame_id = 0
    frames = dropped = total_bytes = 0
    last_report = time.monotonic()
    peer = None

    with pipeline:
        pipeline.start()

        # The negotiated link speed, not the port it is plugged into. A USB-C
        # cable carrying only the USB 2.0 pairs sits happily in a SuperSpeed
        # port and negotiates high-speed, and nothing looks wrong until the
        # camera is asked to do real work and starts browning out.
        try:
            speed = pipeline.getDefaultDevice().getUsbSpeed()
            bad = "SUPER" not in str(speed).upper()
            print(f"  usb link: {speed}"
                  + ("   <-- USB 2.0. That is the cable, not the port." if bad else ""))
        except Exception as exc:
            print(f"  usb link: could not read ({type(exc).__name__})")
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

                if det_queue is not None:
                    # Boxes from the camera itself. read_boxes returns None when
                    # nothing new has arrived, which means keep the last set
                    # rather than blink them off between detections.
                    fresh = read_boxes(det_queue, labels, args.width, args.height, wanted)
                    if fresh is not None:
                        boxes = fresh
                elif detector is not None:
                    detector.offer(jpeg)
                    boxes = detector.boxes()

                if boxes:
                    blob = DET_MAGIC + struct.pack("<BH", len(boxes), frame_id)
                    for x, y, w, h, label, conf in boxes[:16]:
                        name = label.encode()[:15]
                        blob += struct.pack("<hhHHBB", x, y, w, h,
                                            int(conf * 100), len(name)) + name
                    sock.sendto(blob, peer)

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
    ap.add_argument("--nn", nargs="?", const="yolov6-nano", metavar="MODEL",
                    help="run detection ON THE CAMERA, e.g. --nn or --nn yolov6-nano. "
                         "Needs internet once to cache the model, then never again. "
                         "Costs the Pi nothing and keeps up with the frame rate.")
    ap.add_argument("--nn-fps", type=float, default=5.0, metavar="FPS",
                    help="how often the camera runs the network (default 5). "
                         "Lower it if the OAK crashes and reconnects in a loop: "
                         "that is a power problem, and this is the one lever "
                         "that reduces its draw. 0 for unthrottled.")
    ap.add_argument("--classes", default="person",
                    help="comma separated classes to keep, or empty for all")
    ap.add_argument("--confidence", type=float, default=0.5)
    ap.add_argument("--detect", action="store_true",
                    help="run the Roboflow workflow alongside the stream and send "
                         "boxes to the badge. Needs RF_API_KEY in the environment "
                         "and internet on this machine.")
    ap.add_argument("--detect-interval", type=float, default=0.5,
                    help="seconds between inference calls (default 0.5). A round "
                         "trip is about half a second, so going below this just "
                         "queues up.")
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
