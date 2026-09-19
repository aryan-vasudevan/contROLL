#!/usr/bin/env python3
"""
Person detection for the badge stream, via a Roboflow workflow.

Runs as a background thread inside oak_stream.py. It does NOT sit in the video
path: frames keep flowing to the badge at full rate while this samples one
every so often and publishes whatever boxes come back. The badge draws those
over the live picture.

That separation is the whole design, and it comes from measurement. A round
trip to serverless.roboflow.com is 260-290 ms, so putting it inline would cap
the stream near 3 fps, against the 18.8 the badge manages on its own. Boxes a
few hundred milliseconds stale on top of smooth video look far better than
fresh boxes on a slideshow.

It sends the model as an inline workflow specification rather than calling the
saved one, because the saved workflow returns a rendered image and no
coordinates, and coordinates are the only thing that can be drawn over live
video. See SPEC below.

No inference-sdk: it requires Python <3.13 and the Pi runs 3.13.5, so it will
not install there at all. The SDK is a thin wrapper over one HTTP POST, which
is all this does, with nothing but the standard library.

The API key comes from the environment, never from the repo:

    export RF_API_KEY=...
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://serverless.roboflow.com/infer/workflows"

# The saved workflow (dev-f8zc3/custom-workflow-3) renders an annotated image
# and returns only that, with no coordinates -- which cannot be drawn over live
# video. Rather than ask anyone to edit it, this sends the same model as an
# inline specification and asks for the predictions instead.
#
# It is also about twice as fast, 260-290 ms against 480-560 ms, because the
# server no longer draws boxes onto a JPEG and base64s it back. We only ever
# wanted the numbers.
#
# Lifted from the saved workflow's own definition, so the model, the class
# filter and the IoU threshold all match what the editor shows.
SPEC = {
    "version": "1.0",
    "inputs": [{"type": "InferenceImage", "name": "image"}],
    "steps": [{
        "type": "roboflow_core/roboflow_object_detection_model@v3",
        "name": "model",
        "images": "$inputs.image",
        "model_id": "rfdetr-nano",
        "class_filter": ["person"],
        "iou_threshold": 0.5,
    }],
    "outputs": [{
        "type": "JsonField",
        "name": "predictions",
        "coordinates_system": "own",
        "selector": "$steps.model.predictions",
    }],
}


class Detector:
    """Samples frames, publishes boxes. Never blocks the video path."""

    def __init__(self, api_key, model_id="rfdetr-nano", classes=("person",),
                 interval=0.5, timeout=8.0, url=None):
        self.url = url or DEFAULT_URL
        self.spec = json.loads(json.dumps(SPEC))      # a copy we can retune
        self.spec["steps"][0]["model_id"] = model_id
        self.spec["steps"][0]["class_filter"] = list(classes)
        self.api_key = api_key
        self.interval = interval
        self.timeout = timeout

        self._lock = threading.Lock()
        self._latest_jpeg = None      # most recent frame, set by the video loop
        self._boxes = []              # most recent detections
        self._boxes_at = 0.0
        self._stop = threading.Event()
        self._thread = None

        self.calls = 0
        self.failures = 0
        self.last_error = None
        self.last_ms = 0.0
        self.saw_predictions = None   # None until the first successful reply

    # -- called from the video loop -----------------------------------------

    def offer(self, jpeg):
        """Hand over the newest frame. Cheap; just a reference swap."""
        with self._lock:
            self._latest_jpeg = jpeg

    def boxes(self, max_age=2.0):
        """Detections as (x, y, w, h, label, confidence), in source pixels.

        Stale boxes are dropped rather than left on screen: a box that no
        longer matches what the camera sees is worse than no box.
        """
        with self._lock:
            if time.monotonic() - self._boxes_at > max_age:
                return []
            return list(self._boxes)

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                jpeg = self._latest_jpeg
                self._latest_jpeg = None
            if jpeg is None:
                time.sleep(0.05)
                continue
            try:
                started = time.monotonic()
                boxes = self._infer(jpeg)
                self.last_ms = (time.monotonic() - started) * 1000.0
                self.calls += 1
                with self._lock:
                    self._boxes = boxes
                    self._boxes_at = time.monotonic()
            except Exception as exc:                 # noqa: BLE001
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.interval)

    # -- the one HTTP call ---------------------------------------------------

    def _infer(self, jpeg):
        body = json.dumps({
            "api_key": self.api_key,
            "specification": self.spec,
            "inputs": {"image": {"type": "base64",
                                 "value": base64.b64encode(jpeg).decode()}},
        }).encode()
        req = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        return self._extract(payload)

    def _extract(self, payload):
        """Pull boxes out of whatever the workflow happens to return.

        Workflows differ in what they expose. A visualization-only workflow
        returns an annotated image and no coordinates, which cannot be drawn
        over live video -- so say so once, clearly, rather than silently
        showing nothing.
        """
        outputs = payload.get("outputs") or []
        if not outputs:
            return []
        out = outputs[0]

        preds = None
        for key, value in out.items():
            if isinstance(value, dict) and isinstance(value.get("predictions"), list):
                preds = value["predictions"]
                break
            if key.endswith("predictions") and isinstance(value, list):
                preds = value
                break
        if preds is None:
            if self.saw_predictions is None:
                self.saw_predictions = False
                print("  [detect] the workflow returns no predictions, only "
                      f"{list(out.keys())}.")
                print("  [detect] add the detection model's predictions as a "
                      "workflow output to get boxes on the badge.")
            return []

        self.saw_predictions = True
        boxes = []
        for p in preds:
            try:
                # Roboflow gives centre-x/centre-y plus size; the badge wants a
                # top-left corner.
                w = float(p["width"])
                h = float(p["height"])
                x = float(p["x"]) - w / 2.0
                y = float(p["y"]) - h / 2.0
                label = str(p.get("class", p.get("label", "?")))[:15]
                conf = float(p.get("confidence", 0.0))
                boxes.append((int(x), int(y), int(w), int(h), label, conf))
            except (KeyError, TypeError, ValueError):
                continue
        return boxes


def from_env(interval=0.5):
    """Build a Detector from the environment, or None if it is not configured."""
    key = os.environ.get("RF_API_KEY")
    if not key:
        return None
    return Detector(
        api_key=key,
        model_id=os.environ.get("RF_MODEL", "rfdetr-nano"),
        classes=os.environ.get("RF_CLASSES", "person").split(","),
        interval=interval,
    )


if __name__ == "__main__":
    import sys
    det = from_env()
    if det is None:
        sys.exit("set RF_API_KEY first")
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} <image.jpg>")
    data = open(sys.argv[1], "rb").read()
    print(f"posting {len(data)} bytes to {det.url}")
    t = time.monotonic()
    try:
        boxes = det._infer(data)
    except urllib.error.URLError as exc:
        sys.exit(f"no answer: {exc}  (does this machine have internet?)")
    print(f"round trip {(time.monotonic() - t) * 1000:.0f} ms, {len(boxes)} box(es)")
    for x, y, w, h, label, conf in boxes:
        print(f"  {label:15} {conf:.2f}  at {x},{y} {w}x{h}")
