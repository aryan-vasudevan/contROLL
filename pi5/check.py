#!/usr/bin/env python3
"""
Work out why the OAK-1 is not working, before it is bolted to an aircraft.

    python3 check.py

Checks the whole chain in order: Python environment, driver, udev permissions,
USB enumeration, Pi power budget, then an actual connection and a real frame.
Every failure prints what to do about it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

OK, WARN, FAIL, INFO = "ok", "warn", "FAIL", "--"
_MARK = {OK: "\033[32m ok \033[0m", WARN: "\033[33mwarn\033[0m",
         FAIL: "\033[31mFAIL\033[0m", INFO: "    "}

failures = 0
warnings = 0

IS_LINUX = sys.platform.startswith("linux")

# setup.sh only runs on the Pi, so off Linux the advice has to be the manual
# commands instead. Pointing at a script that refuses to run is worse than
# giving no advice at all.
VENV = "~/oakenv"
INSTALL_HINT = (
    "./setup.sh"
    if IS_LINUX
    else f"python3 -m venv {VENV} && {VENV}/bin/pip install depthai opencv-python"
)
PYTHON_HINT = "python3" if IS_LINUX else f"{VENV}/bin/python"


def report(status: str, title: str, detail: str = "", fix: str = "") -> None:
    global failures, warnings
    if status == FAIL:
        failures += 1
    elif status == WARN:
        warnings += 1
    print(f"[{_MARK[status]}] {title}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")
    if fix and status in (FAIL, WARN):
        for line in fix.splitlines():
            print(f"         \033[36m->\033[0m {line}")


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:
        return ""


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


# --- environment -----------------------------------------------------------
section("Environment")

model = ""
for p in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
    try:
        model = Path(p).read_text(errors="replace").strip("\x00").strip()
        break
    except OSError:
        continue

if "Raspberry Pi 5" in model:
    report(OK, "Raspberry Pi 5", model)
elif model:
    report(WARN, "Not a Pi 5", model,
           "This is written for the Pi 5. Other boards may work but the USB "
           "power advice below will not apply.")
else:
    report(INFO, "Not a Raspberry Pi", "Running the checks that still apply.")

report(INFO, f"Python {sys.version.split()[0]}", sys.executable)

in_venv = sys.prefix != sys.base_prefix
if in_venv:
    report(OK, "Running inside a virtual environment")
else:
    detail = ("Raspberry Pi OS Bookworm marks the system Python as externally "
              "managed, so pip refuses to install into it."
              if IS_LINUX else
              "Homebrew's Python is also externally managed, so a plain "
              "pip install will be refused here too.")
    report(WARN, "Not in a virtual environment", detail,
           f"{INSTALL_HINT}\n"
           f"then run this again as:  {PYTHON_HINT} check.py")

# --- driver ----------------------------------------------------------------
section("DepthAI")

try:
    import depthai as dai
    report(OK, f"depthai {dai.__version__} imported")
    if not dai.__version__.startswith("3."):
        report(WARN, "This is not DepthAI v3",
               f"found {dai.__version__}",
               "The code here uses the v3 API. pip install -U depthai")
except Exception as exc:
    if "Library not loaded" in str(exc) or "image not found" in str(exc):
        report(FAIL, "depthai is installed but broken",
               f"{type(exc).__name__}: {str(exc)[:160]}",
               "This is what a half-written package looks like, usually a "
               "pip install that ran out of disk.",
               f"Check free space with  df -h  then reinstall:\n"
               f"rm -rf {VENV} && {INSTALL_HINT}")
        dai = None
        raise SystemExit(1)
    report(FAIL, "depthai not importable", str(exc),
           f"{INSTALL_HINT}\n"
           f"then:  {PYTHON_HINT} check.py")
    dai = None

try:
    import cv2
    report(OK, f"opencv {cv2.__version__} imported")
except ImportError as exc:
    report(FAIL, "opencv not importable", str(exc),
           f"{INSTALL_HINT}\n"
           f"then:  {PYTHON_HINT} check.py")
    cv2 = None

# --- permissions -----------------------------------------------------------
section("USB permissions")

rules = list(Path("/etc/udev/rules.d").glob("*movidius*")) if \
    Path("/etc/udev/rules.d").is_dir() else []
if rules:
    report(OK, "udev rule present", str(rules[0]))
elif os.name == "posix" and Path("/etc/udev").is_dir():
    report(FAIL, "No udev rule for the camera",
           "Without it the device enumerates but cannot be opened, which looks "
           "identical to the camera being broken.",
           "echo 'SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"03e7\", MODE=\"0666\"' "
           "| sudo tee /etc/udev/rules.d/80-movidius.rules",
           )
else:
    report(INFO, "Not a Linux host, skipping udev check")

# --- enumeration -----------------------------------------------------------
section("USB device")

if shutil.which("lsusb"):
    lsusb = run(["lsusb"])
    movidius = [ln for ln in lsusb.splitlines() if "03e7" in ln.lower()]
    if movidius:
        report(OK, "Camera visible on the USB bus", "\n".join(movidius))
        # 2485 is the unbooted ROM. f63b means firmware is loaded and running.
        if any("2485" in m for m in movidius):
            report(INFO, "Device is in bootloader state",
                   "Normal before first use. DepthAI uploads firmware on connect.")
    else:
        report(FAIL, "No Movidius device on the USB bus",
               "Nothing with vendor id 03e7 is attached.",
               "Check the cable. It must be a data cable, not charge-only.\n"
               "Try the other USB3 port, the blue ones.\n"
               "If the camera is warm but invisible, suspect power, below.")
else:
    report(INFO, "lsusb not available, skipping bus scan")

# --- power -----------------------------------------------------------------
section("Power budget")

if "Raspberry Pi 5" in model:
    limit = None
    for path in (
        "/sys/firmware/devicetree/base/chosen/power/usb_max_current_enable",
        "/proc/device-tree/chosen/power/usb_max_current_enable",
    ):
        try:
            limit = int.from_bytes(Path(path).read_bytes()[:4], "big")
            break
        except Exception:
            continue

    if limit is None and shutil.which("vcgencmd"):
        out = run(["vcgencmd", "get_config", "usb_max_current_enable"])
        if "=" in out:
            try:
                limit = int(out.split("=")[1])
            except ValueError:
                limit = None

    if limit == 1:
        report(OK, "USB current limit raised to 1.6 A",
               "Enough headroom for the OAK-1.")
    elif limit == 0:
        report(FAIL, "USB ports limited to 600 mA total",
               "The OAK-1 draws more than this in bursts. The usual symptom is "
               "the camera enumerating, then vanishing mid-stream.",
               "Use the official 27 W USB-C supply, which lifts the limit "
               "automatically.\n"
               "Or add usb_max_current_enable=1 to /boot/firmware/config.txt, "
               "but only with a supply that can genuinely deliver it.\n"
               "Or run the camera through a powered USB hub.")
    else:
        report(WARN, "Could not read the USB current limit",
               "Check it by hand before you trust this in flight.",
               "vcgencmd get_config usb_max_current_enable")

    if shutil.which("vcgencmd"):
        throttled = run(["vcgencmd", "get_throttled"])
        if throttled.endswith("=0x0"):
            report(OK, "No undervoltage or throttling recorded", throttled)
        elif throttled:
            report(WARN, "Power or thermal events recorded", throttled,
                   "Any non-zero value means the supply browned out or the "
                   "board got hot at some point since boot.")
else:
    report(INFO, "Not a Pi 5, skipping the USB current check")

# --- live connection -------------------------------------------------------
section("Camera")

if dai is None:
    report(FAIL, "Skipped, depthai is not installed")
else:
    try:
        devices = dai.Device.getAllAvailableDevices()
    except Exception as exc:
        devices = []
        report(WARN, "Device enumeration raised", str(exc))

    if not devices:
        report(FAIL, "DepthAI cannot see a camera",
               "The bus check above tells you whether this is a cable problem "
               "or a permissions problem.")
    else:
        report(OK, f"{len(devices)} device(s) found",
               "\n".join(str(d.getDeviceId()) for d in devices))

        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from oakcam import OakCamera

            print("         connecting and grabbing a frame ...")
            with OakCamera(model=None, size=(640, 480)) as cam:
                info = cam.device
                report(OK, "Connected",
                       f"{info.product} ({info.name})\n"
                       f"platform {info.platform}\n"
                       f"USB link {info.usb_speed}")

                if info.usb_is_fast:
                    report(OK, "SuperSpeed USB link")
                else:
                    report(WARN, f"USB link is {info.usb_speed}, not SuperSpeed",
                           "Frame rate will be well below what the camera can do.",
                           "Use a USB3 cable and one of the blue USB3 ports.")

                if info.temperature_c is not None:
                    status = OK if info.temperature_c < 70 else WARN
                    report(status, f"VPU temperature {info.temperature_c:.1f} C",
                           fix="Above about 70 C the camera throttles. In a "
                               "closed drone shell, give it airflow.")

                for frame in cam.frames():
                    report(OK, "Got a frame",
                           f"{frame.width} x {frame.height}, "
                           f"dtype {frame.bgr.dtype}")
                    break
        except Exception as exc:
            report(FAIL, "Could not open the camera", f"{type(exc).__name__}: {exc}",
                   "If this says permissions, the udev rule is missing or has "
                   "not been reloaded:\n"
                   "sudo udevadm control --reload-rules && sudo udevadm trigger\n"
                   "then unplug and replug the camera.")

# --- summary ---------------------------------------------------------------
print()
if failures:
    print(f"\033[31m{failures} failure(s)\033[0m, {warnings} warning(s). "
          "Fix the failures top to bottom; later ones are often caused by earlier ones.")
    sys.exit(1)
elif warnings:
    print(f"\033[33mNo failures, {warnings} warning(s).\033[0m "
          "Usable, but read the warnings before flying it.")
    sys.exit(0)
else:
    print("\033[32mAll checks passed.\033[0m Try: python3 detect.py --stream")
    sys.exit(0)
