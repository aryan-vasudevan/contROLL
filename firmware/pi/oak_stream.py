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

# Detections. The magic is BDT2, not BDET, because the layout changed: every
# box now carries a track id so the badge can select one and the car can chase
# it, and the header carries the source dimensions so a consumer does not have
# to decode the JPEG to know what the coordinates are relative to.
#
# Bumping the magic rather than adding a field is the point. A badge running
# the old firmware ignores BDT2 outright and shows video with no boxes, which
# is obvious and harmless. Had the magic stayed BDET it would have parsed the
# new layout with the old field offsets and drawn boxes in the wrong places --
# a bug that looks like bad detection rather than a version mismatch.
#
#     "BDT2" u8 count  u16 frame_id  u16 img_w  u16 img_h
#     then count x:  s16 x  s16 y  u16 w  u16 h  u8 conf  u8 id  u8 len  <label>
DET_MAGIC = b"BDT2"
DET_HEADER = "<BHHH"
DET_BOX = "<hhHHBBB"

DEFAULT_PORT = 14557          # 14555/14556 belong to the button link

# Where badgedrive.py listens for boxes, so it can drive at the one the badge
# has selected. Loopback: the autopilot runs on this same Pi, and the only
# thing that ever needs to reach it is this process.
AUTOPILOT_ADDR = ("127.0.0.1", 14559)


def control_command(msg):
    """The control command in a datagram, or None if it is a frame request.

    Split out to be testable, because getting it wrong is not subtle: the
    sender of a frame request becomes the video destination, so a control
    packet misread as a request would point the whole stream at whichever
    process sent it and leave the badge with a blank screen.

    "C:" is the marker. A frame request is "R", and note that "REC1" also
    begins with R -- which is exactly why the prefix is checked first and why
    the commands are not bare words.
    """
    if not msg.startswith(b"C:"):
        return None
    return msg[2:].strip()


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

    These come back without ids -- the camera reports what it sees, not what it
    saw last time -- so the caller runs them through the same Tracker the cloud
    path uses. Both detectors then produce identical box tuples and everything
    downstream stops caring which one is running.
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

    from track import Tracker

    detector = None
    # The camera's own detections arrive anonymous; the cloud detector does its
    # own tracking internally. Giving the on-camera path a Tracker here means
    # both produce boxes with stable ids, and the badge and the autopilot never
    # learn which detector is running.
    cam_tracker = Tracker() if args.nn else None
    cam_seq = 0

    if args.detect and not args.nn:
        # Not a fallback any more. Measured against serverless on a 160x120
        # frame: 3.6 fps one call at a time, 36.6 fps with eight in flight.
        # The badge shows 18.8, so this keeps up with every frame with room
        # to spare -- see the note at the top of detect.py for why the old
        # "the cloud can only manage 3 fps" conclusion was wrong.
        import detect as detect_mod
        detector = detect_mod.from_env(workers=args.detect_workers,
                                       interval=args.detect_interval)
        if detector is None:
            print("  --detect asked for but RF_API_KEY is not set; skipping")
        else:
            detector.start()
            print(f"  cloud detection: {detector.workers} requests in flight to "
                  f"{detector.url}")
            if args.detect_interval:
                print(f"  throttled to one frame every {args.detect_interval}s")
            else:
                print("  every frame is inferred on; drops only when all "
                      "workers are busy")

    # Boxes also go to the autopilot, so the car can drive at the one the badge
    # selected. Same packet, same coordinates: one producer, two consumers, and
    # no chance of the badge highlighting one target while the car chases
    # another.
    auto_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    auto_addr = (args.autopilot_host, args.autopilot_port)
    if args.autopilot_port:
        print(f"  boxes also to the autopilot at {auto_addr[0]}:{auto_addr[1]}")

    # Recording is toggled by the badge, which reaches this process through
    # badgedrive.py -- the badge itself only ever speaks to the button port.
    from record import Recorder
    recorder = Recorder(directory=args.record_dir)

    boxes = []

    frame_id = 0
    frames = dropped = total_bytes = 0
    last_calls = last_drops = 0
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
                    msg, sender = sock.recvfrom(64)
                    # Control packets share this socket but must NOT become the
                    # video destination: badgedrive.py sends them from the Pi
                    # itself, and treating one as a frame request would redirect
                    # the whole stream to loopback and blank the badge.
                    cmd = control_command(msg)
                    if cmd is not None:
                        if cmd in (b"REC1", b"REC0"):
                            want = cmd == b"REC1"
                            if want and not recorder.active():
                                print(f"  recording to {recorder.start()}.mjpeg")
                            elif not want and recorder.active():
                                done = recorder.stop()
                                print(f"  recorded {done['frames']} frames, "
                                      f"{done['dropped']} dropped, "
                                      f"{done['megabytes']:.1f} MB, "
                                      f"{done['fps']:.1f} fps -> {done['path']}.mjpeg")
                        continue
                    peer = sender
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
                # Offered after the badge has its copy, and non-blocking, so a
                # slow SD card costs the recording rather than the live feed.
                recorder.offer(jpeg, boxes)

                if det_queue is not None:
                    # Boxes from the camera itself. read_boxes returns None when
                    # nothing new has arrived, which means keep the last set
                    # rather than blink them off between detections.
                    fresh = read_boxes(det_queue, labels, args.width, args.height, wanted)
                    if fresh is not None:
                        cam_seq += 1
                        boxes = cam_tracker.update(fresh, seq=cam_seq)
                    else:
                        boxes = cam_tracker.current()
                elif detector is not None:
                    # Offer every frame. The pool drops it if all workers are
                    # busy, which is the backpressure: better to skip a frame
                    # than to queue one that will be answered about a view the
                    # car has already driven past.
                    detector.offer(jpeg)
                    boxes = detector.boxes()

                # Sent even when the list is empty, and that matters. "No
                # detections" and "the packet went missing" look identical
                # otherwise, so both ends would sit on boxes for their whole
                # stale timeout after the last person left the frame -- the
                # badge drawing a ghost, and the car driving at it. Eleven
                # bytes a frame buys an unambiguous answer.
                if det_queue is not None or detector is not None:
                    blob = DET_MAGIC + struct.pack(DET_HEADER, min(len(boxes), 16),
                                                   frame_id, args.width, args.height)
                    for x, y, w, h, label, conf, tid in boxes[:16]:
                        name = label.encode()[:15]
                        blob += struct.pack(DET_BOX, x, y, w, h,
                                            int(conf * 100), tid, len(name)) + name
                    sock.sendto(blob, peer)
                    if args.autopilot_port:
                        auto_sock.sendto(blob, auto_addr)

                now = time.monotonic()
                if now - last_report >= 5.0:
                    span = now - last_report
                    line = (f"  {frames / span:5.1f} fps   "
                            f"{total_bytes / span / 1024:6.1f} KB/s   "
                            f"{total_bytes // max(frames, 1):5d} B/frame   "
                            f"{dropped} stale dropped")
                    if detector is not None:
                        st = detector.stats()
                        # inference fps against video fps is the number that
                        # says whether the cloud is keeping up with the stream.
                        # If dropped climbs, the pool is saturated: raise
                        # --detect-workers or accept fewer detections.
                        rate = (st["calls"] - last_calls) / span
                        line += (f"\n    detect {rate:5.1f}/s  p50 {st['p50']:4.0f} ms  "
                                 f"p90 {st['p90']:4.0f} ms  "
                                 f"{st['dropped'] - last_drops} skipped  "
                                 f"{st['failures']} failed  {len(boxes)} box")
                        if st["failures"] and detector.last_error:
                            line += f"\n    last error: {detector.last_error}"
                        last_calls, last_drops = st["calls"], st["dropped"]
                    print(line)
                    last_report, frames, total_bytes, dropped = now, 0, 0, 0
        except KeyboardInterrupt:
            print("\n  stopping")
        finally:
            if recorder.active():
                done = recorder.stop()
                print(f"  recording closed: {done['frames']} frames -> "
                      f"{done['path']}.mjpeg")
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
    ap.add_argument("--detect-workers", type=int, default=8, metavar="N",
                    help="how many inference requests to keep in flight (default 8). "
                         "This, not the round trip, is what sets the detection rate: "
                         "one at a time measured 3.6 fps and eight measured 36.6 "
                         "against the same endpoint.")
    ap.add_argument("--detect-interval", type=float, default=0.0,
                    help="minimum seconds between inference calls (default 0, "
                         "meaning every frame). Every call is billed, so raise "
                         "this to trade detection rate for credits.")
    ap.add_argument("--autopilot-host", default="127.0.0.1",
                    help="where to send boxes for the chase controller")
    ap.add_argument("--autopilot-port", type=int, default=14559,
                    help="0 to not send boxes to the autopilot at all")
    ap.add_argument("--record-dir", default=os.path.expanduser("~/recordings"),
                    help="where B+UP on the badge writes takes")
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
