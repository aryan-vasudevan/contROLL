#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Set up the Luxonis OAK-1 on a Raspberry Pi 5. Run it on the Pi.
#
#   ./setup.sh              headless, which is what you want on an aircraft
#   ./setup.sh --gui        also install the desktop opencv, for --show
#
# It creates a virtual environment, installs DepthAI, and adds the udev rule
# that lets a normal user open the camera. Only the udev step needs sudo, and
# it asks first.
#
# Raspberry Pi OS Bookworm marks the system Python as externally managed, so
# pip refuses to install into it. The virtual environment is not optional
# tidiness here; it is the only way this installs at all.
# ---------------------------------------------------------------------------
set -euo pipefail

VENV="${VENV:-$HOME/oakenv}"
WITH_GUI=0
[[ "${1:-}" == "--gui" ]] && WITH_GUI=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"


say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*"; }
die()  { printf '\033[31m    error: %s\033[0m\n' "$*" >&2; exit 1; }

# Refuse early on a machine this cannot work on, rather than failing several
# steps later inside apt-get.
if [[ "$(uname -s)" != "Linux" ]]; then
  die "this runs on the Raspberry Pi, not on $(uname -s).

     It installs Linux packages and a udev rule. Neither exists here.

     Copy this folder to the Pi and run it there:
         scp -r $HERE <user>@<pi>:~/
         ssh <user>@<pi>
         cd pi5 && ./setup.sh

     To try the DepthAI API on this machine without a Pi, you only need:
         python3 -m venv ~/oakenv && ~/oakenv/bin/pip install depthai opencv-python
         ~/oakenv/bin/python check.py"
fi

command -v apt-get >/dev/null 2>&1 || die "\`apt-get\` not found.
     This expects Raspberry Pi OS or another Debian. Run it on the Pi."

[[ $EUID -ne 0 ]] || die "run this as your normal user, not with sudo.
     It will ask for sudo only for the udev rule."

# --- board check -----------------------------------------------------------
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
if [[ "$MODEL" == *"Raspberry Pi 5"* ]]; then
  say "Raspberry Pi 5 detected"
  echo "    $MODEL"
else
  warn "this does not look like a Pi 5 ($MODEL); continuing anyway"
fi

# --- system packages -------------------------------------------------------
say "Installing system packages"
SYS_PKGS=(python3-venv python3-pip libusb-1.0-0 udev curl)
if [[ $WITH_GUI -eq 1 ]]; then
  SYS_PKGS+=(libgl1 libglib2.0-0)
fi
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends "${SYS_PKGS[@]}"

# --- virtual environment ---------------------------------------------------
say "Creating the virtual environment at $VENV"
if [[ -d "$VENV" ]]; then
  echo "    already exists, reusing it"
else
  python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip wheel

say "Installing DepthAI"
"$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"

if [[ $WITH_GUI -eq 1 ]]; then
  say "Installing the desktop build of opencv for --show"
  "$VENV/bin/pip" uninstall -y --quiet opencv-python-headless || true
  "$VENV/bin/pip" install --quiet "opencv-python>=4.9"
fi

echo "    depthai $("$VENV/bin/python" -c 'import depthai; print(depthai.__version__)')"

# --- udev ------------------------------------------------------------------
RULE=/etc/udev/rules.d/80-movidius.rules
say "USB permissions"
if [[ -f "$RULE" ]]; then
  echo "    $RULE already present"
else
  cat <<'EXPLAIN'
    The camera is a Movidius device, vendor id 03e7. Without a udev rule it
    enumerates but cannot be opened by a normal user, which looks exactly like
    a broken camera. This needs sudo.
EXPLAIN
  read -r -p "    Install the udev rule? [Y/n] " reply
  if [[ ! "$reply" =~ ^[Nn]$ ]]; then
    echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="03e7", MODE="0666"' \
      | sudo tee "$RULE" > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "    installed. Unplug and replug the camera for it to take effect."
  else
    warn "skipped; the camera will not open until this exists"
  fi
fi

# --- power -----------------------------------------------------------------
say "Power budget"
LIMIT=""
for p in /sys/firmware/devicetree/base/chosen/power/usb_max_current_enable \
         /proc/device-tree/chosen/power/usb_max_current_enable; do
  [[ -r "$p" ]] && LIMIT=$(od -An -tu4 -N4 --endian=big "$p" 2>/dev/null | tr -d ' ') && break
done

if [[ "$LIMIT" == "1" ]]; then
  echo "    USB current limit is raised to 1.6 A. Good."
elif [[ "$LIMIT" == "0" ]]; then
  warn "USB ports are limited to 600 mA in total."
  cat <<'POWER'
    The OAK-1 draws more than that in bursts. The symptom is the camera
    working for a while and then disappearing mid-stream, which reads like a
    software crash and is not one.

    Fix it with one of:
      - the official Raspberry Pi 27 W USB-C supply, which lifts the limit
        automatically
      - usb_max_current_enable=1 in /boot/firmware/config.txt, only if your
        supply can genuinely deliver it
      - a powered USB hub between the Pi and the camera
POWER
else
  warn "could not read the USB current limit; check it before you fly:
      vcgencmd get_config usb_max_current_enable"
fi

# --- done ------------------------------------------------------------------
say "Done"
cat <<NEXT

Activate the environment, then check the camera:

    source $VENV/bin/activate
    cd $HERE
    python3 check.py

check.py walks the whole chain and tells you what to fix. Once it passes:

    python3 detect.py --stream

Then open http://<pi-address>:8080/ from any device on the same network. If
the Pi is already the access point for the badge, join that network and use
192.168.4.1.

NEXT
