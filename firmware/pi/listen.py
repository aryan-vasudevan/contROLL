#!/usr/bin/env python3
"""Show what the flight controller is saying, so you can prove the serial link
works before the badge is anywhere near it.

    python3 listen.py                 # via mavlink-router's TCP server
    python3 listen.py --udp           # join the badge's UDP endpoint instead
    python3 listen.py --serial /dev/ttyAMA0 --baud 921600   # straight serial

Self-contained on purpose: this gets copied onto the Pi, where the rest of the
project is not present. It decodes only the handful of messages worth seeing
during bring-up.
"""
import argparse, socket, struct, sys, time

# --- MAVLink v2 framing -----------------------------------------------------
CRC_EXTRA = {0: 50, 1: 124, 24: 24, 30: 39, 33: 104, 74: 20, 77: 143, 253: 83}
NAMES = {0: 'HEARTBEAT', 1: 'SYS_STATUS', 24: 'GPS_RAW_INT', 30: 'ATTITUDE',
         33: 'GLOBAL_POSITION_INT', 74: 'VFR_HUD', 77: 'COMMAND_ACK',
         253: 'STATUSTEXT'}

def crc_accum(b, crc):
    tmp = b ^ (crc & 0xFF)
    tmp = (tmp ^ (tmp << 4)) & 0xFF
    return ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF

def crc16(data, seed=0xFFFF):
    c = seed
    for b in data:
        c = crc_accum(b, c)
    return c

def frames(stream):
    """Yield (sysid, msgid, payload) for every checksum-valid frame."""
    buf = bytearray()
    while True:
        chunk = stream()
        if not chunk:
            return
        buf += chunk
        while True:
            i = buf.find(b'\xFD')
            if i < 0:
                del buf[:]
                break
            if i:
                del buf[:i]
            if len(buf) < 12:
                break
            ln = buf[1]
            total = 12 + ln
            if len(buf) < total:
                break
            frame = bytes(buf[:total])
            msgid = frame[7] | frame[8] << 8 | frame[9] << 16
            rx = frame[10 + ln] | frame[11 + ln] << 8
            extra = CRC_EXTRA.get(msgid)
            ok = False
            if extra is not None:
                c = crc_accum(extra, crc16(frame[1:10 + ln]))
                ok = (c == rx)
            if ok or extra is None:
                if ok:
                    yield frame[5], msgid, frame[10:10 + ln]
                del buf[:total]
            else:
                # Bad checksum, or a 0xFD that was really payload. Step past it.
                del buf[:1]

def pad(p, n):
    return p + b'\x00' * max(0, n - len(p))

MODES = {0: 'STABILIZE', 2: 'ALT_HOLD', 3: 'AUTO', 4: 'GUIDED', 5: 'LOITER',
         6: 'RTL', 9: 'LAND', 16: 'POSHOLD'}
FIX = {0: 'none', 1: 'none', 2: '2D', 3: '3D', 4: 'DGPS', 5: 'RTK-float', 6: 'RTK-fixed'}

def die(problem, *fixes):
    """Explain the problem and stop. No stack trace: this is a tool for
    working out what is wrong, so a traceback is the least useful thing it
    could print."""
    print(f"\n  {problem}\n", file=sys.stderr)
    for line in fixes:
        print(f"  {line}" if line else "", file=sys.stderr)
    print(file=sys.stderr)
    raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=5760)
    ap.add_argument('--udp', action='store_true', help='listen on UDP 14550 instead')
    ap.add_argument('--serial', help='read a serial port directly, bypassing the router')
    ap.add_argument('--baud', type=int, default=921600)
    args = ap.parse_args()

    if args.serial:
        try:
            import serial
        except ImportError:
            die("pyserial is needed for --serial",
                "sudo apt install python3-serial")
        try:
            port = serial.Serial(args.serial, args.baud, timeout=1)
        except Exception as exc:
            die(f"cannot open {args.serial}: {exc}",
                "Check the port exists:  ls /dev/tty*",
                "On a Pi 5 the GPIO header UART is /dev/ttyAMA0.",
                "/dev/serial0 is the separate debug connector, not the header.",
                "If it says permission denied:  sudo usermod -aG dialout $USER",
                "then log out and back in.")
        src = lambda: port.read(512)
        where = f"{args.serial} at {args.baud}"

    elif args.udp:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", 14550))
        except OSError as exc:
            die(f"cannot bind UDP 14550: {exc}",
                "Something else already has that port. Usually mavlink-router.",
                "Either stop it, or use the default TCP mode instead of --udp.")
        s.settimeout(1.0)

        def src():
            try:
                return s.recv(2048)
            except socket.timeout:
                return b""

        where = "UDP 14550"

    else:
        try:
            s = socket.create_connection((args.host, args.port), timeout=5)
        except ConnectionRefusedError:
            die(f"nothing is listening on {args.host}:{args.port}",
                "This mode reads mavlink-router's TCP server, so the router "
                "has to be running:",
                "    sudo systemctl start mavlink-router",
                "    systemctl status mavlink-router",
                "",
                "Run this ON THE PI, not on your laptop. Nothing on a laptop "
                "serves this port.",
                "",
                "To read the flight controller's serial port directly and skip "
                "the router entirely:",
                f"    python3 listen.py --serial /dev/ttyAMA0 --baud {args.baud}")
        except socket.timeout:
            die(f"timed out connecting to {args.host}:{args.port}",
                "The address answered nothing. Check you are on the same "
                "network as the Pi and that the address is right.")
        except OSError as exc:
            die(f"cannot reach {args.host}:{args.port}: {exc}")
        s.settimeout(1.0)

        def src():
            try:
                return s.recv(2048)
            except socket.timeout:
                return b""

        where = f"TCP {args.host}:{args.port}"

    print(f'listening on {where}; ctrl-c to stop\n')

    seen = {}
    state = {}
    last = 0
    started = time.time()

    for sysid, msgid, p in frames(src):
        seen[msgid] = seen.get(msgid, 0) + 1

        if msgid == 0:
            p = pad(p, 9)
            custom, typ, ap_, base, status = struct.unpack('<IBBBB', p[:8])
            state['armed'] = bool(base & 0x80)
            state['mode'] = MODES.get(custom, str(custom))
            state['sysid'] = sysid
        elif msgid == 1:
            p = pad(p, 31)
            state['volts'] = struct.unpack_from('<H', p, 14)[0] / 1000.0
            state['batt'] = struct.unpack_from('<b', p, 30)[0]
        elif msgid == 24:
            p = pad(p, 30)
            state['fix'] = FIX.get(p[28], p[28])
            state['sats'] = p[29]
        elif msgid == 33:
            p = pad(p, 28)
            lat, lon = struct.unpack_from('<ii', p, 4)
            state['lat'] = lat / 1e7
            state['lon'] = lon / 1e7
            state['alt'] = struct.unpack_from('<i', p, 16)[0] / 1000.0
        elif msgid == 253:
            p = pad(p, 51)
            text = p[1:51].split(b'\x00')[0].decode('ascii', 'replace')
            # The status line below is redrawn in place with a carriage return,
            # so clear it before printing or the two overwrite each other.
            print(f'\r{" " * 78}\r  [fc] {text}')
            last = 0

        now = time.time()
        if now - last >= 1.0:
            last = now
            if 'sysid' not in state:
                print(f'\r{now - started:5.0f}s  frames seen but no heartbeat yet', end='')
            else:
                print(f"\rsys {state['sysid']}  {state.get('mode','?'):9} "
                      f"{'ARMED' if state.get('armed') else 'disarmed':9} "
                      f"gps {state.get('fix','?')}/{state.get('sats',0)} sats  "
                      f"alt {state.get('alt',0):5.1f} m  "
                      f"batt {state.get('volts',0):4.1f} V  ", end='')
            sys.stdout.flush()

    print('\nstream ended')

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n')
