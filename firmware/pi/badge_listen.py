#!/usr/bin/env python3
"""
Listen for button state from the badge and print it.

This is the Pi half of the first link in the chain:

    badge buttons  --Wi-Fi/UDP-->  THIS  --MAVLink-->  Pixhawk
                                   ^^^^
                                   you are here

It does not touch a drone, a flight controller or pymavlink. It answers one
question: is the badge reaching the Pi, and are the buttons right? Get a clean
readout here before wiring anything to the aircraft, because debugging a badge
and a flight controller at the same time is debugging two unknowns at once.

    python3 badge_listen.py

Standard library only, so it runs on the Pi, on a laptop, or anywhere else.

Every packet gets a reply. The badge only lights its LEDs green once it hears
one, so the reply is what turns this from "packets are arriving" into "both
directions work". Use --no-ack to stay silent and watch the badge go red.

Self-test with no badge and no Pi:

    python3 badge_listen.py --selftest
"""

import argparse
import re
import socket
import sys
import time

# Must match PI_UDP_PORT in firmware/include/config.h. Deliberately not 14550:
# that belongs to MAVLink, and keeping them apart means mavlink-router and this
# can both run on the Pi without eating each other's packets.
DEFAULT_PORT = 14555

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"

# BADGE1 seq=42 ms=12345 raw=0xFB down=[UP A]
LINE = re.compile(
    r"^BADGE1\s+seq=(\d+)\s+ms=(\d+)\s+raw=0x([0-9A-Fa-f]{2})\s+down=\[([^\]]*)\]"
)


class Stats:
    """Sequence tracking, so dropped packets are visible rather than silent."""

    def __init__(self):
        self.packets = 0
        self.dropped = 0
        self.bad = 0
        self.last_seq = None
        self.first_ms = None

    def note(self, seq):
        self.packets += 1
        if self.last_seq is not None:
            gap = seq - self.last_seq - 1
            # A restarted badge counts its sequence from zero again. That is a
            # reboot, not a hundred lost packets, so do not score it as loss.
            if gap > 0:
                self.dropped += gap
            elif seq <= self.last_seq:
                self.last_seq = None
                return "badge restarted"
        self.last_seq = seq
        return None


def parse(payload):
    """-> (seq, ms, raw, [names]) or None if it is not a badge line."""
    m = LINE.match(payload.strip())
    if not m:
        return None
    seq, ms, raw, held = m.groups()
    return int(seq), int(ms), int(raw, 16), held.split()


def render(seq, ms, raw, names, addr, stats, quiet):
    if quiet and not names:
        return
    held = " ".join(names) if names else f"{DIM}--{RESET}"
    colour = GREEN if names else DIM
    print(f"  {DIM}{addr[0]:<15}{RESET} seq={seq:<6} "
          f"{DIM}{ms / 1000:7.1f}s{RESET} raw={raw:#04x} "
          f"{colour}{BOLD}{held}{RESET}")


def serve(port, ack, quiet, bind):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind, port))
    except OSError as exc:
        print(f"{RED}cannot bind {bind}:{port}: {exc}{RESET}")
        print(f"{DIM}something else may already be listening there.{RESET}")
        return 1
    sock.settimeout(1.0)

    print(f"\n{BOLD}  badge listener{RESET} {DIM}on {bind}:{port}{RESET}")
    print(f"{DIM}  waiting for the badge. It should be on the same Wi-Fi as this "
          f"machine.{RESET}")
    print(f"{DIM}  replies are {'ON -- badge LEDs should go green' if ack else 'OFF -- badge LEDs will show red'}"
          f"{RESET}")
    print(f"{DIM}  ctrl-c to stop{RESET}\n")

    stats = Stats()

    # One handler around the whole loop, not just the receive: ctrl-c can land
    # during a print or a reply too, and the closing summary is the point of
    # stopping cleanly.
    try:
        _loop(sock, stats, ack, quiet)
    except KeyboardInterrupt:
        pass

    loss = ""
    if stats.packets:
        total = stats.packets + stats.dropped
        loss = f", {stats.dropped} dropped ({100 * stats.dropped / total:.1f}%)"
    print(f"\n  {stats.packets} packets{loss}"
          f"{f', {stats.bad} unparsed' if stats.bad else ''}\n")
    return 0


def _loop(sock, stats, ack, quiet):
    last_rx = None
    warned = False
    while True:
        try:
            data, addr = sock.recvfrom(512)
        except socket.timeout:
            # Say something when a live badge goes quiet, rather than just
            # sitting there looking identical to "never started".
            if last_rx and time.monotonic() - last_rx > 3 and not warned:
                print(f"  {YELLOW}no packets for 3s -- badge out of range, "
                      f"asleep or rebooting{RESET}")
                warned = True
            continue

        last_rx = time.monotonic()
        warned = False

        payload = data.decode("utf-8", errors="replace")
        fields = parse(payload)
        if not fields:
            stats.bad += 1
            print(f"  {YELLOW}unparsed from {addr[0]}:{RESET} {payload.strip()!r}")
            continue

        seq, ms, raw, names = fields
        note = stats.note(seq)
        if note:
            print(f"  {CYAN}{note}{RESET}")
        render(seq, ms, raw, names, addr, stats, quiet)

        if ack:
            try:
                sock.sendto(f"OK {seq}".encode(), addr)
            except OSError as exc:
                print(f"  {YELLOW}reply to {addr[0]} failed: {exc}{RESET}")


def selftest(port):
    """Prove the parser and the socket path without a badge."""
    print(f"\n{BOLD}  self-test{RESET} {DIM}no badge required{RESET}\n")

    cases = [
        ("BADGE1 seq=1 ms=1000 raw=0xFF down=[]",            (1, 1000, 0xFF, [])),
        ("BADGE1 seq=2 ms=1050 raw=0xFB down=[UP]",          (2, 1050, 0xFB, ["UP"])),
        ("BADGE1 seq=3 ms=1100 raw=0x7B down=[UP A]",        (3, 1100, 0x7B, ["UP", "A"])),
        ("BADGE1 seq=4 ms=1150 raw=0x00 down=[UP DOWN LEFT RIGHT A B SELECT SLIDE]",
         (4, 1150, 0x00, ["UP", "DOWN", "LEFT", "RIGHT", "A", "B", "SELECT", "SLIDE"])),
        ("garbage",                                          None),
        ("BADGE1 seq=x ms=1 raw=0xFF down=[]",               None),
    ]
    fails = 0
    for payload, want in cases:
        got = parse(payload)
        ok = got == want
        fails += not ok
        print(f"  {GREEN + 'ok  ' + RESET if ok else RED + 'FAIL' + RESET} "
              f"{payload[:58]:<58} -> {got}")

    # Sequence accounting, including the reboot case that must not read as loss.
    s = Stats()
    for n in (1, 2, 3, 7, 8):
        s.note(n)
    ok = s.dropped == 3
    fails += not ok
    print(f"\n  {GREEN + 'ok  ' + RESET if ok else RED + 'FAIL' + RESET} "
          f"gap 3->7 counts as 3 dropped (got {s.dropped})")

    s = Stats()
    for n in (10, 11, 1, 2):
        s.note(n)
    ok = s.dropped == 0
    fails += not ok
    print(f"  {GREEN + 'ok  ' + RESET if ok else RED + 'FAIL' + RESET} "
          f"a restart is not counted as loss (got {s.dropped})")

    # Real loopback traffic through a real socket, including the reply.
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(2.0)
    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli.settimeout(2.0)
    cli.sendto(b"BADGE1 seq=99 ms=5000 raw=0xDF down=[B]", srv.getsockname())
    data, addr = srv.recvfrom(512)
    srv.sendto(b"OK 99", addr)
    reply, _ = cli.recvfrom(64)
    got = parse(data.decode())
    ok = got == (99, 5000, 0xDF, ["B"]) and reply == b"OK 99"
    fails += not ok
    print(f"  {GREEN + 'ok  ' + RESET if ok else RED + 'FAIL' + RESET} "
          f"round trip over a real socket, reply {reply!r}")
    srv.close(); cli.close()

    print(f"\n  {GREEN + 'ALL PASSED' + RESET if not fails else RED + f'{fails} FAILED' + RESET}\n")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"UDP port to listen on (default {DEFAULT_PORT})")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="address to bind (default all interfaces)")
    ap.add_argument("--no-ack", action="store_true",
                    help="do not reply; the badge will show a dead link")
    ap.add_argument("--quiet", action="store_true",
                    help="only print packets where a button is actually held")
    ap.add_argument("--selftest", action="store_true",
                    help="check the parser and socket path, no badge needed")
    args = ap.parse_args()

    # Python block-buffers stdout when it is not a terminal, so piping this to
    # a file or through tee would show nothing until the buffer filled. For a
    # tool whose entire job is a live log, that reads as "no packets arriving".
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:      # Python < 3.7
        pass

    if args.selftest:
        return selftest(args.port)
    return serve(args.port, not args.no_ack, args.quiet, args.bind)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n")
        sys.exit(130)
