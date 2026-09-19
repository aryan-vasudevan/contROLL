#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Turn a Raspberry Pi 5 into the Wi-Fi bridge between the badge and the flight
# controller. Run it on the Pi, not on your laptop.
#
#   sudo ./setup.sh
#   sudo AP_PASS='something-better' AP_SSID='f450-badge' ./setup.sh
#
# It does four things:
#   1. enables the GPIO UART, if that is the serial port you picked
#   2. builds and installs mavlink-router
#   3. writes /etc/mavlink-router/main.conf
#   4. turns the onboard Wi-Fi into an access point at a fixed address
#
# Every one of those changes system state, so it asks before starting unless
# you pass --yes.
# ---------------------------------------------------------------------------
set -euo pipefail

# --- settings ---------------------------------------------------------------
AP_SSID="${AP_SSID:-f450-badge}"
AP_PASS="${AP_PASS:-}"

# 192.168.4.1 is what the badge firmware ships with as DRONE_IP, so keeping the
# Pi on that address means the badge needs no change beyond the Wi-Fi name.
AP_ADDR="${AP_ADDR:-192.168.4.1}"
AP_CIDR="${AP_CIDR:-24}"
AP_IFACE="${AP_IFACE:-wlan0}"
AP_CONN="${AP_CONN:-badge-ap}"

# Serial link to the flight controller.
#   /dev/ttyAMA0  GPIO pins 14 and 15 on the Pi 5 header
#   /dev/ttyACM0  a USB cable straight into the flight controller
FC_DEVICE="${FC_DEVICE:-/dev/ttyAMA0}"
FC_BAUD="${FC_BAUD:-921600}"

ASSUME_YES=0
[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && ASSUME_YES=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*"; }
die()  { printf '\033[31m    error: %s\033[0m\n' "$*" >&2; exit 1; }

# --- preflight --------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "run this with sudo"

if ! grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null; then
  warn "this does not look like a Raspberry Pi; continuing anyway"
fi

if [[ -z "$AP_PASS" ]]; then
  die "set an access point password, for example:
       sudo AP_PASS='choose-something' ./setup.sh
     it must be at least 8 characters, which is a WPA2 requirement"
fi
[[ ${#AP_PASS} -ge 8 ]] || die "AP_PASS must be at least 8 characters"

cat <<SUMMARY

This will change the following on this Pi:

  serial port    $FC_DEVICE at $FC_BAUD baud
  installs       mavlink-router, built from source into /usr/bin
  config file    /etc/mavlink-router/main.conf, overwritten
  wifi           $AP_IFACE becomes an access point named "$AP_SSID"
                 at $AP_ADDR/$AP_CIDR on the 2.4 GHz band

Turning $AP_IFACE into an access point ends any Wi-Fi connection this Pi is
currently using. Keep an ethernet cable or a keyboard attached so you do not
lock yourself out.

SUMMARY

if [[ $ASSUME_YES -eq 0 ]]; then
  read -r -p "Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "nothing changed"; exit 0; }
fi

# --- 1. serial port ---------------------------------------------------------
say "Configuring the serial port"

if [[ "$FC_DEVICE" == "/dev/ttyAMA0" ]]; then
  CONFIG_TXT=/boot/firmware/config.txt
  [[ -f "$CONFIG_TXT" ]] || CONFIG_TXT=/boot/config.txt
  [[ -f "$CONFIG_TXT" ]] || die "cannot find config.txt"

  if grep -qE '^\s*dtparam=uart0=on' "$CONFIG_TXT"; then
    echo "    uart0 already enabled in $CONFIG_TXT"
  else
    cp "$CONFIG_TXT" "$CONFIG_TXT.badge-backup.$(date +%s)"
    printf '\n# Added for the badge drone bridge: UART on GPIO 14 and 15.\ndtparam=uart0=on\n' >> "$CONFIG_TXT"
    echo "    enabled uart0, backup saved next to $CONFIG_TXT"
    NEEDS_REBOOT=1
  fi

  # On the Pi 5 the Linux console lives on the separate three-pin debug header,
  # not on GPIO 14 and 15, so this usually finds nothing. It is here for the
  # case where an older image left a console on the header UART.
  if grep -qE 'console=(serial0|ttyAMA0)' /boot/firmware/cmdline.txt 2>/dev/null; then
    sed -i 's/console=serial0,[0-9]* //; s/console=ttyAMA0,[0-9]* //' /boot/firmware/cmdline.txt
    echo "    removed a serial console from cmdline.txt"
    NEEDS_REBOOT=1
  fi

  # Worth stating plainly: on the Pi 5, /dev/serial0 is the debug connector,
  # not the GPIO header. Code carried over from a Pi 4 points at the wrong port.
  echo "    note: on the Pi 5 use /dev/ttyAMA0, not /dev/serial0"
else
  echo "    using $FC_DEVICE, no boot configuration needed"
fi

# --- 2. mavlink-router ------------------------------------------------------
say "Installing mavlink-router"

if command -v mavlink-routerd >/dev/null 2>&1; then
  echo "    already installed: $(mavlink-routerd --version 2>&1 | head -1)"
else
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    git ca-certificates meson ninja-build pkg-config gcc g++ python3 python3-venv

  BUILD_DIR=/usr/local/src/mavlink-router
  if [[ -d "$BUILD_DIR/.git" ]]; then
    git -C "$BUILD_DIR" pull --ff-only
    git -C "$BUILD_DIR" submodule update --init --recursive
  else
    rm -rf "$BUILD_DIR"
    git clone --recursive https://github.com/mavlink-router/mavlink-router.git "$BUILD_DIR"
  fi

  cd "$BUILD_DIR"
  meson setup --wipe build . >/dev/null 2>&1 || meson setup build .
  ninja -C build
  ninja -C build install
  cd - >/dev/null
  echo "    installed $(mavlink-routerd --version 2>&1 | head -1)"
fi

# --- 3. router config -------------------------------------------------------
say "Writing /etc/mavlink-router/main.conf"

[[ -f "$HERE/main.conf.template" ]] || die "main.conf.template missing next to this script"
mkdir -p /etc/mavlink-router
if [[ -f /etc/mavlink-router/main.conf ]]; then
  cp /etc/mavlink-router/main.conf "/etc/mavlink-router/main.conf.badge-backup.$(date +%s)"
  echo "    existing config backed up"
fi
sed -e "s|__DEVICE__|$FC_DEVICE|" -e "s|__BAUD__|$FC_BAUD|" \
    "$HERE/main.conf.template" > /etc/mavlink-router/main.conf
echo "    serial $FC_DEVICE at $FC_BAUD, UDP server on 14550, TCP on 5760"

systemctl enable mavlink-router >/dev/null 2>&1 || warn "could not enable the service"

# --- 4. access point --------------------------------------------------------
say "Setting up the access point"

command -v nmcli >/dev/null 2>&1 || die "NetworkManager is required; this expects Raspberry Pi OS Bookworm or newer"

nmcli connection delete "$AP_CONN" >/dev/null 2>&1 || true

nmcli connection add type wifi ifname "$AP_IFACE" con-name "$AP_CONN" \
  autoconnect yes ssid "$AP_SSID" >/dev/null

# Band bg is 2.4 GHz. This is not optional: the badge's ESP32-C3 has no 5 GHz
# radio, so an access point on 5 GHz is invisible to it.
nmcli connection modify "$AP_CONN" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "$AP_PASS" \
  ipv4.method shared \
  ipv4.addresses "$AP_ADDR/$AP_CIDR" \
  connection.autoconnect-priority 10 >/dev/null

echo "    \"$AP_SSID\" on 2.4 GHz at $AP_ADDR"

# --- done -------------------------------------------------------------------
say "Done"

cat <<NEXT

Put these in the badge's include/config.h:

    #define WIFI_SSID  "$AP_SSID"
    #define WIFI_PASS  "$AP_PASS"
    #define DRONE_IP   "$AP_ADDR"

Then, in this order:

  1. Wire the Pi to a TELEM port. Pi TX to FC RX, Pi RX to TX, and ground to
     ground. Do not power the Pi from the TELEM 5V rail; it cannot supply
     enough current. Use a separate 5 V BEC rated for 5 A.

  2. Set these on the flight controller, for TELEM2:
         SERIAL2_PROTOCOL = 2
         SERIAL2_BAUD     = $(( FC_BAUD / 1000 ))
         BRD_SER2_RTSCTS  = 0
         SYSID_MYGCS      = 255
     That last one is not optional. ArduPilot ignores manual control from any
     other ground station id, and the badge sends 255.

  3. Bring it up and confirm the flight controller is actually talking:
         sudo nmcli connection up $AP_CONN
         sudo systemctl start mavlink-router
         python3 $HERE/listen.py
     You want heartbeats. If you see nothing, the problem is the serial link,
     and no amount of badge debugging will find it.

  4. Only once that works, join a laptop to "$AP_SSID" and connect Mission
     Planner or QGroundControl over UDP on port 14550.

  5. Only once that works, flash the badge. Take the propellers off first.

NEXT

if [[ "${NEEDS_REBOOT:-0}" == "1" ]]; then
  echo "A reboot is needed for the serial port change to take effect."
fi
