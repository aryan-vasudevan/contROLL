#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Make the Pi log badge buttons from boot, with no laptop and no login.
#
# Run this ON THE PI, with badge_listen.py and badge-listen.service sitting
# next to it in the home directory:
#
#     sudo ./install_listener.sh
#
# After this the whole system is two devices: power the Pi, power the badge,
# press buttons. Watch the log with
#
#     journalctl -u badge-listen -f
#
#     sudo ./install_listener.sh --undo     to remove it again
# ---------------------------------------------------------------------------
set -euo pipefail

UNIT=badge-listen
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${TARGET_USER:-pi}"
HOME_DIR="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31m    error: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

if [[ "${1:-}" == "--undo" ]]; then
  say "Removing $UNIT"
  systemctl disable --now "$UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT.service"
  systemctl daemon-reload
  echo "    removed"
  exit 0
fi

[[ -n "$HOME_DIR" ]] || die "no such user: $TARGET_USER"
[[ -f "$HERE/badge_listen.py" ]]        || die "badge_listen.py is not next to this script"
[[ -f "$HERE/$UNIT.service" ]]          || die "$UNIT.service is not next to this script"

say "Installing the listener"

# The unit hardcodes /home/pi, so the script has to actually be there.
if [[ "$HERE/badge_listen.py" != "$HOME_DIR/badge_listen.py" ]]; then
  install -o "$TARGET_USER" -g "$TARGET_USER" -m 755 \
          "$HERE/badge_listen.py" "$HOME_DIR/badge_listen.py"
  echo "    copied badge_listen.py to $HOME_DIR"
else
  chmod 755 "$HOME_DIR/badge_listen.py"
fi

# Prove it runs before wiring it to boot. A unit that crash-loops on a Pi you
# have to walk over to is worse than no unit.
say "Checking it starts"
sudo -u "$TARGET_USER" python3 "$HOME_DIR/badge_listen.py" --selftest >/dev/null \
  || die "badge_listen.py --selftest failed; not installing the service"
echo "    self-test passed"

install -m 644 "$HERE/$UNIT.service" "/etc/systemd/system/$UNIT.service"
sed -i "s|^User=.*|User=$TARGET_USER|; s|^WorkingDirectory=.*|WorkingDirectory=$HOME_DIR|; \
        s|/home/pi/badge_listen.py|$HOME_DIR/badge_listen.py|" \
    "/etc/systemd/system/$UNIT.service"
echo "    installed /etc/systemd/system/$UNIT.service"

systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"
sleep 1

if systemctl is-active --quiet "$UNIT"; then
  say "Running"
else
  say "NOT running"
  systemctl status "$UNIT" --no-pager -l | tail -15
  die "see above"
fi

cat <<NEXT

  The Pi now listens on UDP 14555 from boot.

  Watch it:     journalctl -u $UNIT -f
  Stop it:      sudo systemctl stop $UNIT
  Remove it:    sudo ./install_listener.sh --undo

  Power the badge up on the same network. Its LEDs go steady green once this
  answers, and every button press shows up in the log above.

NEXT
