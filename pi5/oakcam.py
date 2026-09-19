"""
Luxonis OAK-1 wrapper for the Raspberry Pi 5.

Written against the DepthAI v3 API (depthai 3.x), which is the current stable
release and the one Luxonis recommends for RVC2 devices like the OAK-1. The v3
API is not the one most tutorials online still show: v2 built pipelines out of
ColorCamera and XLinkOut nodes, while v3 uses Camera.build() and output queues
created straight off a node. Code copied from a v2 tutorial will not run here.

The neural network runs on the camera's own Myriad X processor, not on the Pi.
The Pi only receives finished frames and detection results, which is why a 4 GB
Pi 5 handles this as comfortably as an 8 GB one.

Usage:

    from oakcam import OakCamera

    with OakCamera(model="yolov6-nano") as cam:
        for f in cam.frames():
            for d in f.detections:
                print(d.label, round(d.confidence, 2), d.pixel_box(f.width, f.height))
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional, Sequence

import depthai as dai
import numpy as np

log = logging.getLogger("oakcam")

# The OAK-1 is a USB3 device. Running it on a USB2 link still works but the
# frame bandwidth drops hard, so it is worth shouting about.
FAST_USB = {"SUPER", "SUPER_PLUS"}


@dataclass
class Detection:
    """One detected object. Box coordinates are normalised to 0..1."""

    label: str
    label_index: int
    confidence: float
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    def pixel_box(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Box in pixels, clamped to the frame."""
        x0 = int(np.clip(self.xmin, 0.0, 1.0) * width)
        y0 = int(np.clip(self.ymin, 0.0, 1.0) * height)
        x1 = int(np.clip(self.xmax, 0.0, 1.0) * width)
        y1 = int(np.clip(self.ymax, 0.0, 1.0) * height)
        return x0, y0, x1, y1

    @property
    def center(self) -> tuple[float, float]:
        """Box centre, normalised. Useful for steering something at it."""
        return (self.xmin + self.xmax) / 2.0, (self.ymin + self.ymax) / 2.0

    @property
    def area(self) -> float:
        """Fraction of the frame the box covers. A rough proxy for distance."""
        return max(0.0, self.xmax - self.xmin) * max(0.0, self.ymax - self.ymin)


@dataclass
class Frame:
    """A frame and whatever was detected in it."""

    bgr: np.ndarray
    detections: list[Detection] = field(default_factory=list)
    sequence: int = 0
    timestamp: float = 0.0

    @property
    def width(self) -> int:
        return self.bgr.shape[1]

    @property
    def height(self) -> int:
        return self.bgr.shape[0]


@dataclass
class DeviceInfo:
    name: str = "unknown"
    product: str = "unknown"
    platform: str = "unknown"
    usb_speed: str = "UNKNOWN"
    temperature_c: Optional[float] = None

    @property
    def usb_is_fast(self) -> bool:
        return self.usb_speed in FAST_USB


class OakCameraError(RuntimeError):
    pass


def list_devices() -> list[str]:
    """Names of every OAK device currently attached. Empty list means none."""
    try:
        return [str(d.getDeviceId()) for d in dai.Device.getAllAvailableDevices()]
    except Exception as exc:  # the library raises several unrelated types here
        log.debug("device enumeration failed: %s", exc)
        return []


def wait_for_device(timeout: float = 30.0, poll: float = 1.0) -> list[str]:
    """Block until a camera shows up, or give up after timeout seconds.

    Worth using on a drone: the Pi finishes booting well before a self-powered
    USB device has finished enumerating, so a service that starts immediately
    would otherwise fail on a race it would have won a second later.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = list_devices()
        if found:
            return found
        if time.monotonic() >= deadline:
            return []
        time.sleep(poll)


class OakCamera:
    """Context manager around a DepthAI pipeline.

    model:      name of a model in the Luxonis model zoo, e.g. "yolov6-nano".
                Pass None for plain video with no inference.
    size:       requested output size. Ignored when a model is set, because the
                frames then come from the network's passthrough so that every
                frame matches the detections drawn on it exactly.
    confidence: detections below this are discarded on the camera.
    fps:        cap the frame rate. None lets the camera decide.
    """

    def __init__(
        self,
        model: Optional[str] = "yolov6-nano",
        size: tuple[int, int] = (640, 480),
        confidence: float = 0.5,
        fps: Optional[float] = None,
        classes: Optional[Sequence[str]] = None,
    ) -> None:
        self.model = model
        self.size = size
        self.confidence = confidence
        self.fps = fps
        self.wanted_classes = {c.lower() for c in classes} if classes else None

        self._pipeline: Optional[dai.Pipeline] = None
        self._q_frame = None
        self._q_det = None
        self._labels: list[str] = []
        self.device = DeviceInfo()

    # --- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "OakCamera":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if not list_devices():
            raise OakCameraError(
                "no OAK device found. Check the USB cable, then run check.py, "
                "which reports the most common causes."
            )

        self._pipeline = dai.Pipeline()
        pipeline = self._pipeline

        camera = pipeline.create(dai.node.Camera).build()

        if self.model:
            # build() wires the camera into the network itself and pulls the
            # model from the Luxonis zoo, caching it under ~/.cache on first run.
            net = pipeline.create(dai.node.DetectionNetwork).build(
                camera, dai.NNModelDescription(self.model)
            )
            net.setConfidenceThreshold(self.confidence)
            try:
                self._labels = list(net.getClasses() or [])
            except Exception:
                self._labels = []

            # passthrough hands back the exact frame the network saw, so a box
            # never lands on a frame it was not computed from.
            self._q_frame = net.passthrough.createOutputQueue()
            self._q_det = net.out.createOutputQueue()
        else:
            out = camera.requestOutput(
                self.size, dai.ImgFrame.Type.BGR888i, fps=self.fps
            )
            self._q_frame = out.createOutputQueue()
            self._q_det = None

        pipeline.start()
        self._read_device_info()

        if not self.device.usb_is_fast:
            log.warning(
                "USB link negotiated at %s, not SuperSpeed. Expect reduced frame "
                "rate. Usually a USB2 cable or a USB2 port.",
                self.device.usb_speed,
            )

    def stop(self) -> None:
        if self._pipeline is not None:
            try:
                if self._pipeline.isRunning():
                    self._pipeline.stop()
            except Exception as exc:
                log.debug("pipeline stop complained: %s", exc)
            self._pipeline = None
        self._q_frame = self._q_det = None

    # --- device introspection ---------------------------------------------
    def _read_device_info(self) -> None:
        """Best effort. A missing field is not worth failing a flight over."""
        info = DeviceInfo()
        try:
            dev = self._pipeline.getDefaultDevice()
        except Exception as exc:
            log.debug("no default device: %s", exc)
            self.device = info
            return

        for attr, getter in (
            ("name", "getDeviceName"),
            ("product", "getProductName"),
            ("platform", "getPlatformAsString"),
        ):
            try:
                setattr(info, attr, str(getattr(dev, getter)()))
            except Exception:
                pass
        try:
            info.usb_speed = dev.getUsbSpeed().name
        except Exception:
            pass
        try:
            info.temperature_c = float(dev.getChipTemperature().average)
        except Exception:
            pass
        self.device = info

    def chip_temperature(self) -> Optional[float]:
        """Live VPU temperature. The OAK-1 throttles when it gets hot, and in a
        drone shell with no airflow it will get hot."""
        try:
            return float(self._pipeline.getDefaultDevice().getChipTemperature().average)
        except Exception:
            return None

    # --- the actual stream -------------------------------------------------
    def _convert(self, raw) -> list[Detection]:
        out: list[Detection] = []
        for d in raw:
            try:
                name = d.labelName
            except Exception:
                name = (
                    self._labels[d.label]
                    if 0 <= d.label < len(self._labels)
                    else str(d.label)
                )
            if self.wanted_classes and name.lower() not in self.wanted_classes:
                continue
            out.append(
                Detection(
                    label=name,
                    label_index=int(d.label),
                    confidence=float(d.confidence),
                    xmin=float(d.xmin),
                    ymin=float(d.ymin),
                    xmax=float(d.xmax),
                    ymax=float(d.ymax),
                )
            )
        return out

    def frames(self) -> Iterator[Frame]:
        """Yield frames until the pipeline stops or the caller breaks out."""
        if self._pipeline is None:
            raise OakCameraError("call start(), or use the context manager")

        while self._pipeline.isRunning():
            img = self._q_frame.get()
            if img is None:
                continue

            detections: list[Detection] = []
            if self._q_det is not None:
                packet = self._q_det.get()
                if packet is not None:
                    detections = self._convert(packet.detections)

            try:
                ts = img.getTimestamp().total_seconds()
            except Exception:
                ts = time.monotonic()

            yield Frame(
                bgr=img.getCvFrame(),
                detections=detections,
                sequence=int(img.getSequenceNum()),
                timestamp=ts,
            )
