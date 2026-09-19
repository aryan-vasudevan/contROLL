#!/usr/bin/env python3
"""
Set up the Pi's access point by editing its SD card, with no screen, no
keyboard and no network.

Run this ON A MAC (or any machine that can mount a FAT32 partition) with the
Pi's card inserted. It is the fallback for when you cannot reach the Pi any
other way: a Pi 5's USB-C port is power only, so unlike a microcontroller
there is no console to plug into.

    python3 sd_install_ap.py --pass 'choose-something'
    python3 sd_install_ap.py --undo

What it does, all on the card's FAT32 boot partition, which is the only part
a Mac can write:

  1. writes badge_firstrun.sh, a script that runs once as root on next boot
  2. appends systemd.run=... to cmdline.txt so the kernel runs it
  3. backs cmdline.txt up first, to cmdline.txt.bak-badge

On the Pi, that script writes a NetworkManager profile for the access point,
enables SSH, sets the Wi-Fi country (an AP will not start without a regulatory
domain), removes itself from cmdline.txt, and reboots. The Pi comes up
broadcasting the network and never runs the script again.

Why a boot script rather than just dropping the profile in place: the profile
has to live in /etc/NetworkManager/system-connections, which is on the ext4
root partition, and macOS cannot write ext4.

Defaults match firmware/include/config.h, so a badge pointed at f450-badge and
192.168.4.1 needs no change beyond the password.

If it does not work, the card is the recovery path too: put it back in this
machine and run --undo.
"""

import argparse
import os
import shutil
import sys
import uuid

AP_SSID_DEFAULT = "f450-badge"
AP_ADDR_DEFAULT = "192.168.4.1"
SCRIPT_NAME = "badge_firstrun.sh"
BACKUP_SUFFIX = ".bak-badge"
MARKER = "systemd.run=/boot/firmware/" + SCRIPT_NAME

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"

# Runs as root, very early, before most services. Everything is best-effort:
# a failure here must not stop the Pi booting, so nothing is allowed to be
# fatal and the script always exits 0.
FIRSTRUN = r"""#!/bin/sh
# Written by sd_install_ap.py. Runs once at boot, then removes itself from
# cmdline.txt. Safe to delete if you are reading this on a working Pi.
set +e

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot

mount -o remount,rw /        2>/dev/null
mount -o remount,rw "$BOOT"  2>/dev/null || mount "$BOOT" 2>/dev/null

LOG="$BOOT/badge_firstrun.log"
echo "badge firstrun $(date 2>/dev/null)" > "$LOG"

# A Wi-Fi radio with no regulatory domain will not start an access point at
# all, and the failure looks like a broken config rather than a missing
# country code.
raspi-config nonint do_wifi_country __COUNTRY__ >>"$LOG" 2>&1
iw reg set __COUNTRY__                          >>"$LOG" 2>&1
rfkill unblock wifi                             >>"$LOG" 2>&1

NMDIR=/etc/NetworkManager/system-connections
mkdir -p "$NMDIR"
cat > "$NMDIR/badge-ap.nmconnection" <<'PROFILE'
[connection]
id=badge-ap
uuid=__UUID__
type=wifi
interface-name=__IFACE__
autoconnect=true
autoconnect-priority=10

[wifi]
mode=ap
ssid=__SSID__
band=bg

[wifi-security]
key-mgmt=wpa-psk
psk=__PASS__

[ipv4]
method=shared
address1=__ADDR__/24

[ipv6]
addr-gen-mode=default
method=ignore
PROFILE
# NetworkManager refuses to load a profile that others can read.
chmod 600 "$NMDIR/badge-ap.nmconnection"
chown root:root "$NMDIR/badge-ap.nmconnection" 2>/dev/null
echo "wrote $NMDIR/badge-ap.nmconnection" >> "$LOG"

# Enable SSH without systemctl, which is not usable this early in boot.
ln -sf /lib/systemd/system/ssh.service \
       /etc/systemd/system/multi-user.target.wants/ssh.service 2>>"$LOG"
systemctl enable ssh >>"$LOG" 2>&1
touch "$BOOT/ssh"

# Take ourselves back out of cmdline.txt so this runs exactly once.
if [ -f "$BOOT/cmdline.txt" ]; then
  sed -i 's| systemd.run=[^ ]*||g; s| systemd.run_success_action=[^ ]*||g; s| systemd.unit=[^ ]*||g' \
      "$BOOT/cmdline.txt"
  echo "cleaned cmdline.txt" >> "$LOG"
fi

sync
exit 0
"""


def find_boot_volume(explicit=None):
    """The Pi's FAT32 boot partition, by the files that are always on it."""
    if explicit:
        return explicit if looks_like_boot(explicit) else None
    for name in os.listdir("/Volumes"):
        path = os.path.join("/Volumes", name)
        if looks_like_boot(path):
            return path
    return None


def looks_like_boot(path):
    return (os.path.isfile(os.path.join(path, "cmdline.txt"))
            and os.path.isfile(os.path.join(path, "config.txt")))


def undo(boot):
    cmdline = os.path.join(boot, "cmdline.txt")
    backup = cmdline + BACKUP_SUFFIX
    if os.path.isfile(backup):
        shutil.copyfile(backup, cmdline)
        os.remove(backup)
        print(f"  {GREEN}restored{RESET} cmdline.txt from the backup")
    else:
        text = open(cmdline).read()
        cleaned = " ".join(w for w in text.split()
                           if not w.startswith(("systemd.run", "systemd.unit")))
        open(cmdline, "w").write(cleaned + "\n")
        print(f"  {GREEN}cleaned{RESET} cmdline.txt (no backup found)")
    for leftover in (SCRIPT_NAME, "badge_firstrun.log"):
        p = os.path.join(boot, leftover)
        if os.path.isfile(p):
            os.remove(p)
            print(f"  removed {leftover}")
    print(f"\n  The card is back to how it was. Eject it before pulling it out.\n")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pass", dest="password", help="access point password, 8+ characters")
    ap.add_argument("--ssid", default=AP_SSID_DEFAULT)
    ap.add_argument("--addr", default=AP_ADDR_DEFAULT)
    ap.add_argument("--iface", default="wlan0")
    ap.add_argument("--country", default="CA", help="Wi-Fi regulatory domain (default CA)")
    ap.add_argument("--volume", help="path to the boot partition, if autodetect fails")
    ap.add_argument("--undo", action="store_true", help="put the card back how it was")
    args = ap.parse_args()

    boot = find_boot_volume(args.volume)
    if not boot:
        print(f"\n{RED}  Could not find the Pi's boot partition.{RESET}")
        print(f"{DIM}  Looked for a volume in /Volumes holding cmdline.txt and config.txt.")
        print(f"  Is the card inserted? Mounted volumes right now:{RESET}")
        for name in sorted(os.listdir("/Volumes")):
            print(f"    /Volumes/{name}")
        print(f"{DIM}  Pass --volume /Volumes/<name> if you know which it is.{RESET}\n")
        return 1

    print(f"\n{BOLD}  Pi boot partition:{RESET} {boot}")

    if args.undo:
        return undo(boot)

    if not args.password or len(args.password) < 8:
        print(f"\n{RED}  --pass is required and must be at least 8 characters "
              f"(a WPA2 rule, not ours).{RESET}\n")
        return 1

    cmdline_path = os.path.join(boot, "cmdline.txt")
    cmdline = open(cmdline_path).read().strip()

    if MARKER in cmdline:
        print(f"\n{YELLOW}  cmdline.txt already has the first-run hook.{RESET}")
        print(f"{DIM}  Run with --undo first if you want to redo it.{RESET}\n")
        return 1

    script = (FIRSTRUN
              .replace("__UUID__", str(uuid.uuid4()))
              .replace("__SSID__", args.ssid)
              .replace("__PASS__", args.password)
              .replace("__ADDR__", args.addr)
              .replace("__IFACE__", args.iface)
              .replace("__COUNTRY__", args.country))

    script_path = os.path.join(boot, SCRIPT_NAME)
    with open(script_path, "w", newline="\n") as f:
        f.write(script)
    os.chmod(script_path, 0o755)
    print(f"  wrote {SCRIPT_NAME}")

    shutil.copyfile(cmdline_path, cmdline_path + BACKUP_SUFFIX)
    print(f"  backed cmdline.txt up to cmdline.txt{BACKUP_SUFFIX}")

    # cmdline.txt must stay a single line; the kernel ignores anything after
    # the first newline, which silently drops every parameter past it.
    new_cmdline = (f"{cmdline} {MARKER} "
                   f"systemd.run_success_action=reboot "
                   f"systemd.unit=kernel-command-line.target")
    with open(cmdline_path, "w", newline="\n") as f:
        f.write(new_cmdline + "\n")
    print(f"  patched cmdline.txt")

    print(f"""
{GREEN}{BOLD}  Done.{RESET}

  Next:
    1. Eject the card properly, then put it back in the Pi.
    2. Power the Pi up. It boots, runs the script, and reboots itself once.
       Give it about two minutes.
    3. Join this Mac to "{args.ssid}" and: ssh <user>@{args.addr}

  The Pi writes badge_firstrun.log to the boot partition, so if it does not
  come up, put the card back in here and read that file. That is the only
  feedback you get from a headless run, which is why a screen is easier.

  To reverse:  python3 sd_install_ap.py --undo
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n")
        sys.exit(130)
