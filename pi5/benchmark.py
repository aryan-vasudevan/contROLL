#!/usr/bin/env python3
"""
Measure how well this machine copes with the OAK-1.

    python3 benchmark.py                  # 30 s with detection
    python3 benchmark.py --duration 60
    python3 benchmark.py --compare        # several configurations, tabulated

Run it over SSH from wherever you like; it prints a report at the end and
needs no display.

What it is really looking for is not the frame rate. The OAK-1 does inference
on its own processor, so the host rarely runs out of CPU. What it runs out of
is USB current, and the Raspberry Pi records that in a throttling register
rather than reporting it as an error. A run that looks fine on frame rate but
comes back with undervoltage flags set is a run you should not trust.

No third-party dependencies beyond what the camera already needs: CPU, memory
and temperature come from /proc and /sys directly.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from oakcam import OakCamera, OakCameraError  # noqa: E402

IS_LINUX = sys.platform.startswith("linux")

# Bits in the Raspberry Pi's throttling register. The low bits are "happening
# right now", the high bits are "has happened since boot". The high bits are
# the ones that catch a brownout that already came and went.
THROTTLE_BITS = {
    0: ("undervoltage", "right now"),
    1: ("arm frequency capped", "right now"),
    2: ("throttled", "right now"),
    3: ("soft temperature limit", "right now"),
    16: ("undervoltage", "since boot"),
    17: ("arm frequency capped", "since boot"),
    18: ("throttled", "since boot"),
    19: ("soft temperature limit", "since boot"),
}


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def cpu_times() -> tuple[int, int] | None:
    """(busy, total) jiffies across all cores."""
    try:
        parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        values = [int(v) for v in parts]
    except Exception:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values) - idle, sum(values)


def memory_used_mb() -> float | None:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])
        return (info["MemTotal"] - info["MemAvailable"]) / 1024.0
    except Exception:
        return None


def soc_temperature() -> float | None:
    for path in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            return int(Path(path).read_text().strip()) / 1000.0
        except Exception:
            pass
    out = run(["vcgencmd", "measure_temp"])          # temp=48.2'C
    if "=" in out:
        try:
            return float(out.split("=")[1].split("'")[0])
        except Exception:
            pass
    return None


def throttle_flags() -> tuple[int | None, list[str]]:
    if not shutil.which("vcgencmd"):
        return None, []
    out = run(["vcgencmd", "get_throttled"])         # throttled=0x0
    if "=" not in out:
        return None, []
    try:
        value = int(out.split("=")[1], 16)
    except ValueError:
        return None, []
    return value, [f"{name} ({when})"
                   for bit, (name, when) in THROTTLE_BITS.items()
                   if value & (1 << bit)]


@dataclass
class Sample:
    cpu_percent: float | None = None
    memory_mb: float | None = None
    soc_c: float | None = None
    vpu_c: float | None = None


@dataclass
class Result:
    label: str
    frames: int = 0
    seconds: float = 0.0
    intervals: list[float] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)
    usb_speed: str = "UNKNOWN"
    throttle_value: int | None = None
    throttle_notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def fps(self) -> float:
        return self.frames / self.seconds if self.seconds else 0.0

    def percentile(self, p: float) -> float:
        """Frame interval in milliseconds at the given percentile."""
        if not self.intervals:
            return 0.0
        ordered = sorted(self.intervals)
        index = min(len(ordered) - 1, int(len(ordered) * p))
        return ordered[index] * 1000.0

    def _mean(self, attr: str) -> float | None:
        values = [getattr(s, attr) for s in self.samples
                  if getattr(s, attr) is not None]
        return statistics.fmean(values) if values else None

    def _peak(self, attr: str) -> float | None:
        values = [getattr(s, attr) for s in self.samples
                  if getattr(s, attr) is not None]
        return max(values) if values else None


class Monitor(threading.Thread):
    """Samples host load in the background so measuring costs almost nothing."""

    def __init__(self, camera: OakCamera, period: float = 0.5) -> None:
        super().__init__(daemon=True)
        self.camera = camera
        self.period = period
        self.samples: list[Sample] = []
        self._stop = threading.Event()

    def run(self) -> None:
        previous = cpu_times()
        while not self._stop.wait(self.period):
            sample = Sample()

            current = cpu_times()
            if previous and current:
                busy = current[0] - previous[0]
                total = current[1] - previous[1]
                if total > 0:
                    sample.cpu_percent = 100.0 * busy / total
            previous = current

            sample.memory_mb = memory_used_mb()
            sample.soc_c = soc_temperature()
            sample.vpu_c = self.camera.chip_temperature()
            self.samples.append(sample)

    def stop(self) -> None:
        self._stop.set()


def measure(label: str, duration: float, model: str | None,
            size: tuple[int, int], confidence: float) -> Result:
    result = Result(label=label)
    print(f"  {label}: warming up ...", end="", flush=True)

    try:
        with OakCamera(model=model, size=size, confidence=confidence) as camera:
            result.usb_speed = camera.device.usb_speed

            monitor = Monitor(camera)
            monitor.start()

            # Discard the first second. Model upload and autoexposure settling
            # are one-off costs and would drag the average down unfairly.
            warmup_until = time.monotonic() + 1.0
            for _ in camera.frames():
                if time.monotonic() >= warmup_until:
                    break

            print(f"\r  {label}: measuring for {duration:.0f} s ...",
                  end="", flush=True)

            started = time.monotonic()
            deadline = started + duration
            previous = started
            for _ in camera.frames():
                now = time.monotonic()
                result.frames += 1
                result.intervals.append(now - previous)
                previous = now
                if now >= deadline:
                    break

            result.seconds = time.monotonic() - started
            monitor.stop()
            monitor.join(timeout=2.0)
            result.samples = monitor.samples

    except OakCameraError as exc:
        result.error = str(exc)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"

    result.throttle_value, result.throttle_notes = throttle_flags()
    print("\r" + " " * 70 + "\r", end="")
    return result


def report(results: list[Result]) -> int:
    print()
    print("=" * 78)
    print("RESULTS")
    print("=" * 78)

    header = (f"{'configuration':<26}{'fps':>7}{'p50 ms':>9}"
              f"{'p99 ms':>9}{'cpu %':>8}{'ram MB':>9}{'soc C':>8}")
    print(header)
    print("-" * 78)

    for r in results:
        if r.error:
            print(f"{r.label:<26}  failed: {r.error[:44]}")
            continue

        def fmt(value, spec=">8.0f"):
            return format(value, spec) if value is not None else f"{'-':>8}"

        print(f"{r.label:<26}"
              f"{r.fps:>7.1f}"
              f"{r.percentile(0.50):>9.1f}"
              f"{r.percentile(0.99):>9.1f}"
              f"{fmt(r._mean('cpu_percent'))}"
              f"{fmt(r._mean('memory_mb'), '>9.0f')}"
              f"{fmt(r._peak('soc_c'))}")

    print("-" * 78)

    ok = [r for r in results if not r.error]
    if not ok:
        print("\nNothing measured.")
        return 1

    print(f"\nUSB link: {ok[0].usb_speed}", end="")
    if ok[0].usb_speed not in ("SUPER", "SUPER_PLUS"):
        print("   <-- not SuperSpeed. Frame rate is limited by the cable or port,")
        print("        not by this machine. Use a USB3 cable and a blue port.")
    else:
        print()

    vpu = [r._peak("vpu_c") for r in ok if r._peak("vpu_c") is not None]
    if vpu:
        peak = max(vpu)
        print(f"Camera processor peaked at {peak:.0f} C", end="")
        print("   <-- it throttles above about 70 C; give it airflow"
              if peak > 70 else "")

    # This is the part that matters more than the frame rate.
    print()
    throttled = [r for r in ok if r.throttle_notes]
    if any(r.throttle_value is None for r in ok):
        print("Throttling register unavailable, so power problems cannot be ruled")
        print("out. That register only exists on a Raspberry Pi.")
    elif throttled:
        print("POWER OR THERMAL PROBLEM DETECTED")
        for note in sorted({n for r in throttled for n in r.throttle_notes}):
            print(f"  - {note}")
        print()
        print("  Undervoltage means the supply could not keep up. The numbers")
        print("  above were measured on a struggling board and understate what")
        print("  it can do. Use the 27 W supply, or a powered USB hub for the")
        print("  camera, and run this again before drawing any conclusions.")
        return 1
    else:
        print("No undervoltage or throttling recorded. These numbers are honest.")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=30.0,
                    help="seconds per configuration (default: 30)")
    ap.add_argument("--model", default="yolov6-nano")
    ap.add_argument("--confidence", type=float, default=0.5)
    ap.add_argument("--compare", action="store_true",
                    help="run video-only and detection, to price the network")
    args = ap.parse_args()

    where = "Raspberry Pi" if IS_LINUX else sys.platform
    model_line = Path("/proc/device-tree/model")
    if model_line.exists():
        try:
            where = model_line.read_text(errors="replace").strip("\x00").strip()
        except OSError:
            pass

    print(f"Benchmarking on: {where}")
    print(f"Duration: {args.duration:.0f} s per configuration\n")

    before_value, before_notes = throttle_flags()
    if before_notes:
        print("Note: throttling flags were already set before starting:")
        for note in before_notes:
            print(f"  - {note}")
        print("  Clear them with a reboot for a clean reading.\n")

    plans = [("detection " + args.model, args.model, (640, 480))]
    if args.compare:
        plans = [
            ("video 640x480", None, (640, 480)),
            ("video 1280x720", None, (1280, 720)),
            ("detection " + args.model, args.model, (640, 480)),
        ]

    results = [measure(label, args.duration, model, size, args.confidence)
               for label, model, size in plans]
    return report(results)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
