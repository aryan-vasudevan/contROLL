#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Make the Pi serve camera video from boot. Run on the Pi, with oak_stream.py
# and oak-stream.service next to this script:
#
#     sudo ./install_stream.sh
#     sudo ./install_stream.sh --undo
#
# Watch it:   journalctl -u oak-stream -f
# ---------------------------------------------------------------------------
set -euo pipefail

UNIT=oak-stream
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${TARGET_USER:-pi}"
HOME_DIR="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
VENV="$HOME_DIR/oak"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31m    error: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

if [[ "${1:-}" == "--undo" ]]; then
  say "Removing $UNIT"
  systemctl disable --now "$UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT.service"
  systemctl daemon-reload
  echo "    removed"
  exit 0
fi

[[ -n "$HOME_DIR" ]]                 || die "no such user: $TARGET_USER"
[[ -x "$VENV/bin/python" ]]          || die "no depthai venv at $VENV (run install_oak.sh first)"
[[ -f "$HERE/oak_stream.py" ]]       || die "oak_stream.py is not next to this script"
[[ -f "$HERE/$UNIT.service" ]]       || die "$UNIT.service is not next to this script"

say "Installing the camera stream"

if [[ "$HERE/oak_stream.py" != "$HOME_DIR/oak_stream.py" ]]; then
  install -o "$TARGET_USER" -g "$TARGET_USER" -m 755 \
          "$HERE/oak_stream.py" "$HOME_DIR/oak_stream.py"
  echo "    copied oak_stream.py to $HOME_DIR"
fi

# Prove the camera is there before wiring this to boot. A unit that crash-loops
# against absent hardware is worse than no unit.
#
# If the service is already running it holds the device, and depthai will not
# report a claimed device as available -- which looks exactly like an absent
# camera. Stop the service first so the check tests the hardware rather than
# our own grip on it.
say "Checking the camera is there"
systemctl stop "$UNIT" 2>/dev/null || true
sleep 1
if ! sudo -u "$TARGET_USER" "$VENV/bin/python" "$HOME_DIR/oak_stream.py" --list; then
  if lsusb | grep -q 03e7; then
    die "depthai cannot claim the OAK, though lsusb sees it. Something else is holding it."
  fi
  die "no OAK found on USB at all; check the cable and the port"
fi

install -m 644 "$HERE/$UNIT.service" "/etc/systemd/system/$UNIT.service"
sed -i "s|^User=.*|User=$TARGET_USER|; s|^WorkingDirectory=.*|WorkingDirectory=$HOME_DIR|; \
        s|/home/pi/oak/bin/python|$VENV/bin/python|; s|/home/pi/oak_stream.py|$HOME_DIR/oak_stream.py|" \
    "/etc/systemd/system/$UNIT.service"
echo "    installed /etc/systemd/system/$UNIT.service"

systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"
sleep 3

if systemctl is-active --quiet "$UNIT"; then
  say "Running"
  journalctl -u "$UNIT" -n 6 --no-pager
else
  say "NOT running"
  journalctl -u "$UNIT" -n 20 --no-pager
  die "see above"
fi

cat <<NEXT

  The Pi now serves video on TCP 14557 from boot.

  Watch it:   journalctl -u $UNIT -f
  Stop it:    sudo systemctl stop $UNIT
  Remove it:  sudo ./install_stream.sh --undo

NEXT
