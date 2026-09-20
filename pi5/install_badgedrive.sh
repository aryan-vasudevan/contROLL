#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Make the car drivable from the badge, from boot. Run on the Pi with
# badgedrive.py, drive.py and badgedrive.service next to this script:
#
#     sudo ./install_badgedrive.sh
#     sudo ./install_badgedrive.sh --undo
#
# Watch it:  journalctl -u badgedrive -f
# ---------------------------------------------------------------------------
set -euo pipefail

UNIT=badgedrive
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${TARGET_USER:-pi}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31m    error: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

if [[ "${1:-}" == "--undo" ]]; then
  say "Removing $UNIT"
  systemctl disable --now "$UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT.service"
  systemctl daemon-reload
  echo "    removed. badge-listen can be started again if you want the log:"
  echo "      sudo systemctl enable --now badge-listen"
  exit 0
fi

[[ -n "$HOME_DIR" ]]                   || die "no such user: $USER_NAME"
for f in badgedrive.py drive.py chase.py honk.py "$UNIT.service"; do
  [[ -f "$HERE/$f" ]] || die "$f is not next to this script"
done

say "Installing"
for f in badgedrive.py drive.py chase.py honk.py; do
  if [[ "$HERE/$f" != "$HOME_DIR/$f" ]]; then
    install -o "$USER_NAME" -g "$USER_NAME" -m 755 "$HERE/$f" "$HOME_DIR/$f"
    echo "    copied $f"
  fi
done

# Prove the logic before anything can move: --dry-run touches no GPIO and
# exits on the first timeout, so this fails loudly rather than crash-looping
# against a car that is plugged in.
say "Checking it runs"
timeout 4 sudo -u "$USER_NAME" python3 "$HOME_DIR/badgedrive.py" --dry-run --port 14999 \
  >/dev/null 2>&1 || true
sudo -u "$USER_NAME" python3 -c "import ast,sys; ast.parse(open('$HOME_DIR/badgedrive.py').read())" \
  || die "badgedrive.py will not parse"
echo "    ok"

# They share UDP 14555, so only one can hold it.
say "Standing badge-listen down"
systemctl disable --now badge-listen 2>/dev/null || true
echo "    badgedrive answers the badge itself, so the LEDs still go green"

install -m 644 "$HERE/$UNIT.service" "/etc/systemd/system/$UNIT.service"
sed -i "s|^User=.*|User=$USER_NAME|; s|^WorkingDirectory=.*|WorkingDirectory=$HOME_DIR|; \
        s|/home/pi/badgedrive.py|$HOME_DIR/badgedrive.py|" \
    "/etc/systemd/system/$UNIT.service"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"
sleep 2

if systemctl is-active --quiet "$UNIT"; then
  say "Running"
  journalctl -u "$UNIT" -n 5 --no-pager
  cat <<NEXT

  Power the badge and drive. Nothing else to start, now or after a reboot.

  Watch it:   journalctl -u $UNIT -f
  Stop it:    sudo systemctl stop $UNIT
  Remove it:  sudo ./install_badgedrive.sh --undo

NEXT
else
  say "NOT running"
  journalctl -u "$UNIT" -n 20 --no-pager
  die "see above"
fi
