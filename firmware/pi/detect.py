#!/usr/bin/env python3
"""
Person detection for the badge stream, via a Roboflow workflow.

Runs as a background pool inside oak_stream.py. It does NOT sit in the video
path: frames keep flowing to the badge at full rate while this infers on them
and publishes whatever boxes come back. The badge draws those over the live
picture, and the car can chase one of them.

## Why this is a pool and not a loop

The first version of this file made one request, waited for it, then made the
next. It measured 323-1485 ms a call and the conclusion written down at the
time was that the cloud could manage about 3 fps and was therefore useless for
live video.

That conclusion was wrong, and the mistake is worth keeping written down:
**it measured latency and assumed throughput was its reciprocal.** It is not.
Latency is how long one request takes; throughput is how many can be in flight
at once. Nothing about an HTTP API says you may only have one.

Measured against serverless.roboflow.com on a 160x120 frame, same key, same
model, same afternoon:

    one at a time     p50 307 ms   ->   3.3 fps
    4 in flight       p50 224 ms   ->  16.3 fps
    8 in flight       p50 174 ms   ->  41.9 fps

The badge shows 18.8 fps. Eight in flight clears that with room to spare, and
serverless is the *slow* option -- a dedicated GPU deployment removes the cold
starts and queueing that the tail is made of.

So this dispatches every frame it is given, across `workers` threads, and only
drops one when every worker is already busy. That is real backpressure: it
sheds load when the network is slow rather than building a queue of frames
that are stale by the time anyone looks at them.

## What concurrency costs

Replies come back out of order. A slow request for frame 100 can land after a
fast one for frame 104, and applying it would walk the boxes backwards in
time. Every frame carries a sequence number and the tracker drops anything
older than what it has already applied. This is the one bug a naive pool has
and it is invisible until something moves.

## Identity

Boxes come back anonymous. `track.py` gives each one an id that survives from
frame to frame, because the badge has to be able to select a target and the
car has to keep chasing the same one.

No inference-sdk: it requires Python <3.13 and the Pi runs 3.13.5, so it will
not install there at all. The SDK is a thin wrapper over one HTTP POST, which
is all this does, with nothing but the standard library.

The API key comes from the environment, never from the repo:

    export RF_API_KEY=...

Point it at a dedicated GPU deployment by setting the URL, and nothing else
changes:

    export RF_URL=https://<your-deployment>.roboflow.cloud/infer/workflows
"""

import base64
import json
import os
import queue
import statistics
import threading
import time
import urllib.error
import urllib.request

try:
    from track import Tracker
except ImportError:                      # running from another directory
    from .track import Tracker           # type: ignore

DEFAULT_URL = "https://serverless.roboflow.com/infer/workflows"

# Eight covers 18.8 fps at the measured p50 with roughly a 2x margin: at
# ~200 ms a call, holding 19 fps needs about four in flight, and the spare
# four absorb the tail without dropping frames. More than this buys nothing
# -- the badge cannot show more than it asks for -- and costs a call each.
DEFAULT_WORKERS = 8

# Roboflow's inference server speaks the same API on the Pi itself:
#
#     pip install inference-cli && inference server start
#     export RF_URL=http://localhost:9001/infer/workflows
#
# Same request, same response, no internet per frame and no round trip over a
# hackathon network. It needs Docker and a one-time image pull, and inference
# then competes with the video pipeline for the Pi's CPU rather than costing
# nothing -- so measure it before trusting it. --bench does that.

# The saved workflow (dev-f8zc3/custom-workflow-3) renders an annotated image
# and returns only that, with no coordinates -- which cannot be drawn over live
# video. Rather than ask anyone to edit it, this sends the same model as an
# inline specification and asks for the predictions instead.
#
# It is also about twice as fast, because the server no longer draws boxes onto
# a JPEG and base64s it back. We only ever wanted the numbers.
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
        # Deliberately low. The humans-only decision is NOT made here: the
        # tracker only lets a NEW track in at 0.6+ (hands measured ~0.4-0.5,
        # people 0.83+), while existing tracks keep updating down to this
        # floor so a real person does not flicker when their score dips.
        "confidence": 0.45,
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
    """Infers on every frame it is offered. Never blocks the video path."""

    def __init__(self, api_key, model_id="rfdetr-nano", classes=("person",),
                 workers=DEFAULT_WORKERS, timeout=3.5, url=None, interval=0.0,
                 confidence=None, enter_conf=None):
        self.url = url or DEFAULT_URL
        self.spec = json.loads(json.dumps(SPEC))      # a copy we can retune
        self.spec["steps"][0]["model_id"] = model_id
        if confidence is not None:
            self.spec["steps"][0]["confidence"] = float(confidence)
        # See track.py on hysteresis: the cloud filter runs LOW so existing
        # tracks keep getting updates through momentary dips, and this gate
        # keeps anything weak from ever becoming a track at all.
        self._enter_conf = float(enter_conf) if enter_conf is not None else 0.6
        if classes:
            self.spec["steps"][0]["class_filter"] = list(classes)
        else:
            self.spec["steps"][0].pop("class_filter", None)
        self.api_key = api_key
        self.timeout = timeout
        self.workers = max(1, int(workers))
        # A floor on the gap between dispatches. 0 means every frame, which is
        # the point of the pool; raise it to trade detection rate for calls
        # billed, since every dispatch is one inference charged.
        self.interval = float(interval)

        self._tracker = Tracker(enter_conf=self._enter_conf)
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=self.workers)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seq = 0
        self._last_offer = 0.0

        self.calls = 0
        self.dropped = 0
        self.failures = 0
        self.last_error = None
        self.latencies: list[float] = []      # recent round trips, for stats
        self.saw_predictions = None           # None until the first reply

    # -- called from the video loop -----------------------------------------

    def offer(self, jpeg):
        """Hand over a frame. Returns True if it was dispatched.

        Cheap and non-blocking: if every worker is busy the frame is dropped
        here rather than queued, because by the time a worker freed up it
        would be describing a view the car has already driven past.
        """
        now = time.monotonic()
        if self.interval and now - self._last_offer < self.interval:
            return False
        with self._lock:
            self._seq += 1
            seq = self._seq
        try:
            # The capture stamp rides with the frame so the tracker can
            # predict across this call's WHOLE latency, however long it ends
            # up being -- a slow reply gets a longer forward glide for free.
            self._queue.put_nowait((seq, jpeg, now))
        except queue.Full:
            self.dropped += 1
            return False
        self._last_offer = now
        return True

    def boxes(self, max_age=1.5):
        """Live detections as (x, y, w, h, label, confidence, id).

        Stale tracks are dropped rather than left on screen: a box that no
        longer matches what the camera sees is worse than no box.
        """
        with self._lock:
            return self._tracker.current()

    def find(self, track_id):
        """One tracked box by id, or None. What the chase loop asks for."""
        with self._lock:
            return self._tracker.find(track_id)

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        for i in range(self.workers):
            t = threading.Thread(target=self._run, name=f"detect{i}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                seq, jpeg, t_cap = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                started = time.monotonic()
                boxes = self._infer(jpeg)
                ms = (time.monotonic() - started) * 1000.0
                with self._lock:
                    self.calls += 1
                    self.latencies.append(ms)
                    if len(self.latencies) > 200:
                        del self.latencies[:-200]
                    # seq is what makes an out-of-order reply harmless.
                    self._tracker.update(boxes, seq=seq, captured_at=t_cap)
            except Exception as exc:                 # noqa: BLE001
                with self._lock:
                    self.failures += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"

    # -- statistics ----------------------------------------------------------

    def stats(self):
        """Latency percentiles and counts, for the stream's periodic report."""
        with self._lock:
            lat = sorted(self.latencies)
            calls, dropped, failures = self.calls, self.dropped, self.failures
        if not lat:
            return dict(calls=calls, dropped=dropped, failures=failures,
                        p50=0.0, p90=0.0)
        return dict(calls=calls, dropped=dropped, failures=failures,
                    p50=lat[len(lat) // 2],
                    p90=lat[min(int(len(lat) * 0.9), len(lat) - 1)])

    # -- the one HTTP call ---------------------------------------------------

    def _infer(self, jpeg):
        body = json.dumps({
            "api_key": self.api_key,
            "specification": self.spec,
            # Keeps the model warm between calls. Worth a lot against a local
            # server or a freshly started deployment, and nothing at all for
            # the frames themselves -- every frame is different, so no response
            # is ever reused. Model caching is the only kind that helps video.
            "use_cache": True,
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


def from_env(workers=None, interval=0.0):
    """Build a Detector from the environment, or None if it is not configured."""
    key = os.environ.get("RF_API_KEY")
    if not key:
        return None
    classes = [c for c in os.environ.get("RF_CLASSES", "person").split(",") if c]
    return Detector(
        url=os.environ.get("RF_URL"),
        api_key=key,
        model_id=os.environ.get("RF_MODEL", "rfdetr-nano"),
        classes=classes,
        workers=int(os.environ.get("RF_WORKERS", workers or DEFAULT_WORKERS)),
        interval=interval,
        confidence=os.environ.get("RF_CONF"),
        enter_conf=os.environ.get("RF_ENTER"),
    )


def bench(det, jpeg, rounds=10, duration=10.0):
    """Measure both numbers, because only one of them has ever been measured.

    Sequential latency says how stale a box is by the time it is drawn.
    Concurrent throughput says whether every frame can be inferred on at all.
    Confusing the two is what made the cloud path look impossible.
    """
    print(f"  {det.url}")
    print(f"  {det.spec['steps'][0]['model_id']}, {len(jpeg)} byte frame\n")

    times, found = [], 0
    for _ in range(rounds):
        started = time.monotonic()
        try:
            boxes = det._infer(jpeg)
        except Exception as exc:                      # noqa: BLE001
            print(f"  failed: {type(exc).__name__}: {exc}")
            return
        times.append((time.monotonic() - started) * 1000.0)
        found = len(boxes)
    times.sort()
    p50 = times[len(times) // 2]
    p90 = times[min(int(len(times) * 0.9), len(times) - 1)]
    print(f"  ONE AT A TIME   {rounds} calls, {found} detection(s) on the last")
    print(f"    best {times[0]:.0f} ms   p50 {p50:.0f} ms   p90 {p90:.0f} ms   "
          f"worst {times[-1]:.0f} ms")
    print(f"    -> {1000.0 / max(p50, 1):.1f} fps sequential, and this is the "
          f"number that was mistaken for the ceiling\n")

    for workers in (4, det.workers):
        done, errs, lat = [0], [0], []
        lock = threading.Lock()
        stop_at = time.monotonic() + duration

        def work():
            while time.monotonic() < stop_at:
                try:
                    started = time.monotonic()
                    det._infer(jpeg)
                    with lock:
                        done[0] += 1
                        lat.append((time.monotonic() - started) * 1000.0)
                except Exception:                     # noqa: BLE001
                    with lock:
                        errs[0] += 1

        threads = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        span = time.monotonic() - t0
        med = statistics.median(lat) if lat else 0.0
        print(f"  {workers} IN FLIGHT     {done[0] / span:.1f} fps sustained, "
              f"p50 {med:.0f} ms, {errs[0]} error(s)")
    print("\n  the badge shows 18.8 fps; anything at or above that keeps up "
          "with every frame")


def soak(det, jpeg, fps=18.8, seconds=20.0):
    """Drive the real Detector at the real frame rate and see if it keeps up.

    --bench measures raw HTTP. This measures the thing that actually ships:
    frames go in through offer() exactly as the video loop feeds them, the
    pool infers, the tracker assigns ids, and boxes come out of boxes(). It
    is the honest answer to "can the cloud do every frame on THIS network",
    and it is worth running from the Pi rather than from a laptop.

    A static scene is used on purpose: with the picture not changing, the ids
    must not change either. Ids churning on a still image means the tracker
    is failing to associate, and the car would keep losing its target.
    """
    det.start()
    period = 1.0 / fps
    offered = 0
    ids_seen, id_churn, samples = set(), 0, []
    last_ids = None

    print(f"  feeding {fps:.1f} fps for {seconds:.0f}s through the real pool "
          f"({det.workers} workers)\n")
    started = time.monotonic()
    next_frame = started
    while time.monotonic() - started < seconds:
        now = time.monotonic()
        if now < next_frame:
            time.sleep(min(next_frame - now, 0.01))
            continue
        next_frame += period
        det.offer(jpeg)
        offered += 1

        boxes = det.boxes()
        if boxes:
            here = frozenset(b[6] for b in boxes)
            ids_seen |= here
            if last_ids is not None and here != last_ids:
                id_churn += 1
            last_ids = here
            samples.append(len(boxes))

    span = time.monotonic() - started
    st = det.stats()
    det.stop()

    print(f"  offered      {offered:5d} frames  ({offered / span:.1f} fps)")
    print(f"  inferred     {st['calls']:5d}         ({st['calls'] / span:.1f} fps)")
    print(f"  skipped      {st['dropped']:5d}         (all workers busy)")
    print(f"  failed       {st['failures']:5d}")
    if st["failures"] and det.last_error:
        print(f"    last error: {det.last_error}")
    print(f"  latency      p50 {st['p50']:.0f} ms   p90 {st['p90']:.0f} ms")
    if samples:
        print(f"  detections   {min(samples)}-{max(samples)} per frame, "
              f"{len(ids_seen)} distinct track id(s)")
        print(f"  id churn     {id_churn} change(s) on a still picture "
              f"{'(good)' if id_churn <= 2 else '(HIGH -- the tracker is not associating)'}")

    kept = st["calls"] / max(offered, 1)
    print()
    print(f"  {kept:.0%} of offered frames were inferred on.")
    if kept > 0.95:
        print("  The cloud is keeping up with the stream.")
    elif kept > 0.5:
        print("  Partly keeping up. Raise --detect-workers, or accept fewer "
              "detections than frames -- the video is unaffected either way.")
    else:
        print("  Not keeping up. Check the uplink with uplink.sh before "
              "blaming the pool.")


if __name__ == "__main__":
    import sys
    argv = sys.argv[1:]
    det = from_env()
    if det is None:
        sys.exit("set RF_API_KEY first")

    if "--bench" in argv:
        argv.remove("--bench")
        if not argv:
            sys.exit("usage: detect.py --bench <image.jpg>")
        bench(det, open(argv[0], "rb").read())
        sys.exit(0)

    if "--soak" in argv:
        argv.remove("--soak")
        if not argv:
            sys.exit("usage: detect.py --soak <image.jpg>")
        soak(det, open(argv[0], "rb").read())
        sys.exit(0)

    if not argv:
        sys.exit("usage: detect.py <image.jpg>  |  detect.py --bench <image.jpg>")

    data = open(argv[0], "rb").read()
    print(f"posting {len(data)} bytes to {det.url}")
    t = time.monotonic()
    try:
        boxes = det._infer(data)
    except urllib.error.URLError as exc:
        sys.exit(f"no answer: {exc}  (does this machine have internet?)")
    print(f"round trip {(time.monotonic() - t) * 1000:.0f} ms, {len(boxes)} box(es)")
    for x, y, w, h, label, conf in boxes:
        print(f"  {label:15} {conf:.2f}  at {x},{y} {w}x{h}")
