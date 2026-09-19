#!/usr/bin/env python3
"""
Run object detection on the OAK-1 and do something useful with the results.

    python3 detect.py --stream                  # watch it from a laptop
    python3 detect.py --classes person          # only report people
    python3 detect.py --json                    # one JSON object per frame
    python3 detect.py --show                    # local window, needs a desktop

The network runs on the camera. The Pi draws boxes and serves the stream, which
is why this stays light enough for a 4 GB Pi 5 to run it alongside the MAVLink
bridge without either noticing the other.

Ctrl-C stops it cleanly.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent))
from mjpeg import MjpegServer  # noqa: E402
from oakcam import Frame, OakCamera, OakCameraError  # noqa: E402

log = logging.getLogger("detect")

# Distinct, readable on both dark and light scenes, and colour-blind safe.
PALETTE = [
    (255, 176, 0), (0, 200, 255), (120, 220, 60),
    (220, 100, 255), (60, 140, 255), (0, 230, 200),
]

running = True


def stop(*_) -> None:
    global running
    running = False


def colour_for(index: int) -> tuple[int, int, int]:
    return PALETTE[index % len(PALETTE)]


def draw(frame: Frame, fps: float) -> None:
    """Burn boxes and labels into the frame, in place."""
    h, w = frame.height, frame.width
    for det in frame.detections:
        x0, y0, x1, y1 = det.pixel_box(w, h)
        colour = colour_for(det.label_index)
        cv2.rectangle(frame.bgr, (x0, y0), (x1, y1), colour, 2)

        caption = f"{det.label} {det.confidence * 100:.0f}%"
        (tw, th), _ = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        # Keep the label inside the frame when the box touches the top edge.
        ty = max(y0, th + 6)
        cv2.rectangle(frame.bgr, (x0, ty - th - 6), (x0 + tw + 8, ty), colour, -1)
        cv2.putText(frame.bgr, caption, (x0 + 4, ty - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)

    banner = f"{fps:4.1f} fps   {len(frame.detections)} object(s)"
    cv2.putText(frame.bgr, banner, (8, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(frame.bgr, banner, (8, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 240, 240), 1, cv2.LINE_AA)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="yolov6-nano",
                    help="model from the Luxonis zoo (default: yolov6-nano)")
    ap.add_argument("--confidence", type=float, default=0.5,
                    help="drop detections below this, 0..1 (default: 0.5)")
    ap.add_argument("--classes", nargs="*", default=None,
                    help="only report these labels, e.g. --classes person car")
    ap.add_argument("--fps", type=float, default=None, help="cap the frame rate")
    ap.add_argument("--stream", action="store_true",
                    help="serve MJPEG over HTTP for a browser")
    ap.add_argument("--port", type=int, default=8080, help="stream port (default: 8080)")
    ap.add_argument("--quality", type=int, default=80,
                    help="JPEG quality for the stream, 1..100 (default: 80)")
    ap.add_argument("--show", action="store_true",
                    help="open a local window, needs a desktop and full opencv")
    ap.add_argument("--json", action="store_true",
                    help="print one JSON object per frame with detections")
    ap.add_argument("--quiet", action="store_true", help="suppress the status line")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    server = None
    if args.stream:
        server = MjpegServer(port=args.port)
        server.start()
        log.info("open http://<pi-address>:%d/ from a device on the same network",
                 args.port)

    try:
        camera = OakCamera(
            model=args.model,
            confidence=args.confidence,
            fps=args.fps,
            classes=args.classes,
        )
        with camera:
            info = camera.device
            log.info("%s on a %s link", info.product, info.usb_speed)

            fps = 0.0
            last = time.monotonic()
            last_status = 0.0

            for frame in camera.frames():
                if not running:
                    break

                now = time.monotonic()
                dt = now - last
                last = now
                if dt > 0:
                    # Exponential smoothing; the raw per-frame rate is too
                    # jumpy to read while something is moving in shot.
                    fps = fps * 0.9 + (1.0 / dt) * 0.1 if fps else 1.0 / dt

                if args.json:
                    print(json.dumps({
                        "seq": frame.sequence,
                        "t": round(frame.timestamp, 3),
                        "fps": round(fps, 2),
                        "detections": [
                            {
                                "label": d.label,
                                "confidence": round(d.confidence, 3),
                                "box": [round(d.xmin, 4), round(d.ymin, 4),
                                        round(d.xmax, 4), round(d.ymax, 4)],
                                "center": [round(c, 4) for c in d.center],
                                "area": round(d.area, 4),
                            }
                            for d in frame.detections
                        ],
                    }), flush=True)

                if server or args.show:
                    draw(frame, fps)

                if server:
                    ok, buf = cv2.imencode(
                        ".jpg", frame.bgr,
                        [int(cv2.IMWRITE_JPEG_QUALITY), args.quality])
                    if ok:
                        server.update(buf.tobytes())

                if args.show:
                    cv2.imshow("OAK-1", frame.bgr)
                    if cv2.waitKey(1) == ord("q"):
                        break

                if not args.json and not args.quiet and now - last_status >= 1.0:
                    last_status = now
                    names = ", ".join(
                        f"{d.label} {d.confidence * 100:.0f}%"
                        for d in frame.detections[:4]) or "nothing"
                    temp = camera.chip_temperature()
                    suffix = f"  vpu {temp:.0f}C" if temp is not None else ""
                    print(f"\r{fps:5.1f} fps  {names:<48}{suffix}",
                          end="", flush=True)

    except OakCameraError as exc:
        log.error("%s", exc)
        return 1
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        return 1
    finally:
        if server:
            server.stop()
        if args.show:
            cv2.destroyAllWindows()
        print()

    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
