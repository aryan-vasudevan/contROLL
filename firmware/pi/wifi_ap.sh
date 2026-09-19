#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Turn the Pi's onboard Wi-Fi into the access point the badge joins. Nothing
# else: no mavlink-router, no serial port, no flight controller.
#
#   sudo AP_PASS='choose-something' ./wifi_ap.sh
#
# This is the access-point step of setup.sh on its own, for bringing the badge
# link up before the Pi is anywhere near a Pixhawk. Run setup.sh later for the
# rest; it is safe to run after this and will redo this step the same way.
#
# The defaults match what the badge firmware ships with in include/config.h,
# so a badge flashed with the matching WIFI_PASS needs no other change:
#
#   SSID     f450-badge      WIFI_SSID
#   address  192.168.4.1     PI_IP, via DRONE_IP
#   band     2.4 GHz         the ESP32-C3 has no 5 GHz radio
#
#   sudo ./wifi_ap.sh --undo      put the Wi-Fi back to a normal client
# ---------------------------------------------------------------------------
set -euo pipefail

AP_SSID="${AP_SSID:-f450-badge}"
AP_PASS="${AP_PASS:-}"
AP_ADDR="${AP_ADDR:-192.168.4.1}"
AP_CIDR="${AP_CIDR:-24}"
AP_IFACE="${AP_IFACE:-wlan0}"
AP_CONN="${AP_CONN:-badge-ap}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*"; }
die()  { printf '\033[31m    error: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo"
command -v nmcli >/dev/null 2>&1 || die "NetworkManager is required; this expects Raspberry Pi OS Bookworm or newer"

if [[ "${1:-}" == "--undo" ]]; then
  say "Removing the access point"
  nmcli connection down "$AP_CONN" >/dev/null 2>&1 || true
  nmcli connection delete "$AP_CONN" >/dev/null 2>&1 || true
  echo "    \"$AP_CONN\" removed; $AP_IFACE is a normal client again"
  echo "    you may need to reconnect it to your usual network"
  exit 0
fi

if [[ -z "$AP_PASS" ]]; then
  die "set an access point password, for example:
       sudo AP_PASS='choose-something' ./wifi_ap.sh
     it must be at least 8 characters, which is a WPA2 requirement"
fi
[[ ${#AP_PASS} -ge 8 ]] || die "AP_PASS must be at least 8 characters"

cat <<SUMMARY

This turns $AP_IFACE into an access point:

  name     "$AP_SSID"
  address  $AP_ADDR/$AP_CIDR
  band     2.4 GHz

That ends any Wi-Fi connection this Pi is currently using. If you are SSHed in
over Wi-Fi you will be disconnected mid-command. Use ethernet, or a keyboard
and monitor, or be ready to reconnect to "$AP_SSID" itself afterwards.

SUMMARY

read -r -p "Continue? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "nothing changed"; exit 0; }

say "Setting up the access point"

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

nmcli connection up "$AP_CONN" >/dev/null || die "could not bring up \"$AP_CONN\"; check: nmcli connection show $AP_CONN"

say "Done"

cat <<NEXT

  "$AP_SSID" is up at $AP_ADDR on 2.4 GHz.

  On the badge, set these in firmware/include/config.h and reflash:

      #define WIFI_SSID  "$AP_SSID"
      #define WIFI_PASS  "$AP_PASS"

  PI_IP is already $AP_ADDR, so nothing else needs changing.

  Then, here on the Pi:

      python3 badge_listen.py

  Power the badge up and do NOT press START, so it stays in Wi-Fi mode. Its
  LEDs go from a blue chase to steady green once the Pi answers.

  To undo:  sudo ./wifi_ap.sh --undo

NEXT
