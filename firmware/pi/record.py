#!/usr/bin/env python3
"""
Record what the camera saw, and what the detector made of it.

Two files per take, sharing a name:

    2026-09-19_2311.mjpeg    the frames, exactly as they went to the badge
    2026-09-19_2311.jsonl    one line per frame: time, and every box

The video is written as concatenated JPEGs -- which is all Motion JPEG is --
because the frames already exist in that form. Nothing is re-encoded, nothing
is decoded, and a Pi 5 has no hardware JPEG encoder, so any other container
would mean burning CPU on a machine that is also driving motors. ffmpeg reads
it directly:

    ffmpeg -f mjpeg -r 18.8 -i take.mjpeg take.mp4

The sidecar is the part worth having. A video of a demo is nice; a video with
the detections aligned to it frame by frame is evidence, and it is the only
way to work out afterwards why the car drove at the wrong person.

Recording never blocks the stream. If the disk cannot keep up the frame is
dropped from the recording and the badge never notices -- the live feed
outranks the archive, always.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import shutil
import subprocess
import urllib.request

DEFAULT_DIR = os.path.expanduser("~/recordings")

# Frames waiting to be written. Small on purpose: an SD card that stalls
# should cost the recording, not memory on a machine with motors attached.
QUEUE_DEPTH = 64


class Recorder:
    """Writes frames and detections to disk, on its own thread."""

    def __init__(self, directory=DEFAULT_DIR, name=None):
        self.directory = directory
        self.name = name
        self.path = None
        self.frames = 0
        self.dropped = 0
        self.bytes = 0
        self.started_at = 0.0
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
        self._thread = None
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> str:
        if self.active():
            return self.path
        os.makedirs(self.directory, exist_ok=True)
        stamp = self.name or time.strftime("%Y-%m-%d_%H%M%S")
        self.path = os.path.join(self.directory, stamp)
        self.frames = self.dropped = self.bytes = 0
        self.started_at = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self.path

    def stop(self) -> dict:
        if not self.active():
            return {}
        self._stop.set()
        self._thread.join(timeout=3.0)
        span = max(time.monotonic() - self.started_at, 1e-6)
        # Ship the take to Supabase on its own thread: uploads take seconds
        # over the phone and the video loop must never wait on them.
        threading.Thread(target=_upload_take, args=(self.path,),
                         daemon=True).start()
        return dict(path=self.path, frames=self.frames, dropped=self.dropped,
                    seconds=span, fps=self.frames / span,
                    megabytes=self.bytes / 1e6)

    def toggle(self):
        """Returns (now_recording, summary_or_path)."""
        if self.active():
            return False, self.stop()
        return True, self.start()

    # -- called from the video loop -----------------------------------------

    def offer(self, jpeg, boxes=()):
        """Hand over a frame. Never blocks; drops if the writer is behind."""
        if not self.active():
            return False
        try:
            self._queue.put_nowait((time.time(), jpeg, tuple(boxes)))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    # -- the writer ----------------------------------------------------------

    def _run(self):
        video = open(self.path + ".mjpeg", "wb")
        meta = open(self.path + ".jsonl", "w")
        try:
            while True:
                try:
                    stamp, jpeg, boxes = self._queue.get(timeout=0.2)
                except queue.Empty:
                    # Only leave once the queue is genuinely empty, so stopping
                    # does not truncate the last second of the take.
                    if self._stop.is_set():
                        break
                    continue
                video.write(jpeg)
                self.frames += 1
                self.bytes += len(jpeg)
                meta.write(json.dumps({
                    "t": round(stamp, 3),
                    "frame": self.frames,
                    "bytes": len(jpeg),
                    "boxes": [
                        {"x": b[0], "y": b[1], "w": b[2], "h": b[3],
                         "label": b[4], "conf": round(float(b[5]), 3),
                         "id": b[6] if len(b) > 6 else None}
                        for b in boxes
                    ],
                }) + "\n")
        finally:
            video.close()
            meta.close()


def _upload_take(base):
    """Push <base>.mjpeg and <base>.jsonl to Supabase storage.

    Configured entirely from the environment (the same file the API key
    lives in); with no SUPA_URL set this is silently a no-op, so recording
    keeps working on a Pi with no cloud configured at all.

        SUPA_URL=https://<project>.supabase.co
        SUPA_KEY=<service key>
        SUPA_BUCKET=recordings
    """
    url = os.environ.get("SUPA_URL")
    key = os.environ.get("SUPA_KEY")
    bucket = os.environ.get("SUPA_BUCKET", "recordings")
    if not url or not key or not base:
        return
    take = os.path.basename(base)

    # A real video for humans. MJPEG is what the camera emits and what the
    # sidecar indexes, but nothing outside a lab wants to open it -- so when
    # ffmpeg is present the take is turned into an ordinary H.264 .mp4 at the
    # rate it was actually recorded (measured from the sidecar's own
    # timestamps), and THAT is what gets uploaded. At 160x120 this takes
    # about a second even in software.
    uploads = [(".mjpeg", "video/x-motion-jpeg"), (".jsonl", "application/x-ndjson")]
    if shutil.which("ffmpeg"):
        try:
            lines = open(base + ".jsonl").read().strip().split("\n")
            import json as _json
            t0 = _json.loads(lines[0])["t"]
            t1 = _json.loads(lines[-1])["t"]
            fps = max(1.0, (len(lines) - 1) / max(t1 - t0, 0.1))
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "mjpeg",
                 "-framerate", f"{fps:.2f}", "-i", base + ".mjpeg",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-pix_fmt", "yuv420p", base + ".mp4"],
                check=True, timeout=120)
            uploads = [(".mp4", "video/mp4"), (".jsonl", "application/x-ndjson")]
        except Exception as exc:                       # noqa: BLE001
            print(f"  [record] mp4 conversion failed ({type(exc).__name__}); "
                  "uploading raw mjpeg instead")

    for ext, ctype in uploads:
        path = base + ext
        try:
            with open(path, "rb") as f:
                data = f.read()
            req = urllib.request.Request(
                f"{url}/storage/v1/object/{bucket}/takes/{take}{ext}",
                data=data, method="POST",
                headers={"Authorization": f"Bearer {key}", "apikey": key,
                         "Content-Type": ctype, "x-upsert": "true"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
            print(f"  [record] uploaded {take}{ext} ({len(data) / 1e6:.1f} MB) -> "
                  f"{url}/storage/v1/object/public/{bucket}/takes/{take}{ext}")
        except Exception as exc:                       # noqa: BLE001
            # The local files remain either way; the upload is a bonus.
            print(f"  [record] upload of {take}{ext} failed: "
                  f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    import sys
    # A self-test with synthetic frames, so the writer can be exercised without
    # a camera. Checks the files land and the sidecar lines up with the video.
    directory = sys.argv[1] if len(sys.argv) > 1 else "/tmp/rec-selftest"
    rec = Recorder(directory=directory, name="selftest")
    path = rec.start()
    print(f"writing to {path}.mjpeg and {path}.jsonl")

    fake = b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9"
    for i in range(50):
        rec.offer(fake, [(i, 10, 20, 40, "person", 0.9, 1)])
        time.sleep(0.005)
    summary = rec.stop()
    print(f"  {summary['frames']} frames, {summary['dropped']} dropped, "
          f"{summary['megabytes']:.2f} MB, {summary['fps']:.1f} fps")

    lines = open(path + ".jsonl").read().strip().split("\n")
    size = os.path.getsize(path + ".mjpeg")
    ok = (len(lines) == summary["frames"]
          and size == summary["frames"] * len(fake)
          and json.loads(lines[0])["boxes"][0]["id"] == 1)
    print(f"  sidecar {len(lines)} lines, video {size} bytes  "
          f"{'ok' if ok else 'MISMATCH'}")
    sys.exit(0 if ok else 1)
