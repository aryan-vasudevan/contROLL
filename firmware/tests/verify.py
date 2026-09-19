#!/usr/bin/env python3
"""Check the frames the firmware's encoder produced against ref.py, which is an
independent MAVLink v2 implementation. It derives each message's CRC_EXTRA from
the message definition rather than copying the firmware's table, so a wrong
constant in the firmware cannot hide here."""
import struct, sys
import ref

frames = {}
for line in open(sys.argv[1]):
    p = line.split()
    if len(p) == 3 and all(c in '0123456789ABCDEF' for c in p[2]):
        frames[p[0]] = (int(p[1]), bytes.fromhex(p[2]))

fails = []
def check(cond, msg):
    print(('  ok   ' if cond else '  FAIL ') + msg)
    if not cond: fails.append(msg)

def field(msgid, payload, name, fmt):
    off, full = ref.wire_offsets(msgid)
    payload = payload + b'\x00' * (full - len(payload))   # undo v2 truncation
    return struct.unpack_from('<' + fmt, payload, off[name])[0]

for name, (ln, fr) in frames.items():
    d = ref.parse_v2(fr)
    check(d['crc_ok'], f'{name}: checksum validates against the reference')
    check(d['total'] == ln, f'{name}: framing consistent')
    check(d['sysid'] == 255 and d['compid'] == 190, f'{name}: GCS identity 255/190')

d = ref.parse_v2(frames['HEARTBEAT'][1])
check(field(0, d['payload'], 'type', 'B') == 6, 'HEARTBEAT announces MAV_TYPE_GCS')
check(field(0, d['payload'], 'autopilot', 'B') == 8, 'HEARTBEAT autopilot INVALID, correct for a GCS')
check(field(0, d['payload'], 'mavlink_version', 'B') == 3, 'HEARTBEAT mavlink_version 3')

d = ref.parse_v2(frames['MANUAL_CONTROL'][1])
for f, want, fmt in [('x',300,'h'),('y',-200,'h'),('z',500,'h'),('r',350,'h'),
                     ('buttons',0x42,'H'),('target',1,'B')]:
    check(field(69, d['payload'], f, fmt) == want, f'MANUAL_CONTROL {f} survives the wire')

d = ref.parse_v2(frames['SET_MODE'][1])
check(field(11, d['payload'], 'custom_mode', 'I') == 5, 'SET_MODE carries custom_mode 5')
check(field(11, d['payload'], 'base_mode', 'B') == 1, 'SET_MODE sets CUSTOM_MODE_ENABLED')

d = ref.parse_v2(frames['ARM'][1])
check(field(76, d['payload'], 'command', 'H') == 400, 'ARM is COMPONENT_ARM_DISARM')
check(field(76, d['payload'], 'param1', 'f') == 1.0, 'ARM param1 requests arm')
check(field(76, d['payload'], 'param2', 'f') == 0.0, 'ARM does not pass the force magic number')
check(field(76, d['payload'], 'confirmation', 'B') == 0, 'ARM confirmation recovers from truncation')

d = ref.parse_v2(frames['TAKEOFF'][1])
check(field(76, d['payload'], 'command', 'H') == 22, 'TAKEOFF is NAV_TAKEOFF')
check(abs(field(76, d['payload'], 'param7', 'f') - 3.0) < 1e-6, 'TAKEOFF altitude in param7')

d = ref.parse_v2(frames['SETPOS'][1])
check(field(86, d['payload'], 'lat_int', 'i') == 434730000, 'SETPOS latitude exact at 1e-7 degrees')
check(field(86, d['payload'], 'lon_int', 'i') == -803010000, 'SETPOS longitude exact, sign preserved')
check(field(86, d['payload'], 'type_mask', 'H') == 0x0DF8, 'SETPOS asks for position only')
check(field(86, d['payload'], 'coordinate_frame', 'B') == 6, 'SETPOS frame is GLOBAL_RELATIVE_ALT_INT')
for v in ['vx','vy','vz','afx','afy','afz','yaw','yaw_rate']:
    check(field(86, d['payload'], v, 'f') == 0.0, f'SETPOS {v} left at zero')

# The firmware's CRC_EXTRA table, read straight out of its switch statement and
# compared against values derived from the message definitions themselves.
import re, io
src = io.open('mavlink_min.cpp', encoding='utf-8').read()
# The message ids are enumerated in the header, the CRC_EXTRA table is in the
# implementation, so both files are needed to tie a name to a number.
hdr = io.open('mavlink_min.h', encoding='utf-8').read()
ids = {m.group(1): int(m.group(2))
       for m in re.finditer(r'MSG_(\w+)\s*=\s*(\d+),', hdr)}
assert ids, 'no message ids found in mavlink_min.h'
for m in re.finditer(r'case MSG_(\w+):\s+return (\d+);', src):
    name, got = m.group(1), int(m.group(2))
    msgid = ids[name]
    want = ref.crc_extra(msgid)
    check(got == want, f'CRC_EXTRA for {name} is {want}, derived not copied')

# The receive-side readers index into payloads with hardcoded offsets. Check
# each one against the offset the spec's field-sorting rule produces.
FIRMWARE_OFFSETS = {
  0:   {'custom_mode':0, 'type':4, 'autopilot':5, 'base_mode':6, 'system_status':7},
  1:   {'voltage_battery':14, 'current_battery':16, 'battery_remaining':30},
  24:  {'fix_type':28, 'satellites_visible':29},
  33:  {'lat':4, 'lon':8, 'alt':12, 'relative_alt':16,
        'vx':20, 'vy':22, 'vz':24, 'hdg':26},
  74:  {'airspeed':0, 'groundspeed':4, 'alt':8, 'climb':12,
        'heading':16, 'throttle':18},
  77:  {'command':0, 'result':2},
  253: {'severity':0, 'text':1},
}
for msgid, fields in FIRMWARE_OFFSETS.items():
    truth, _ = ref.wire_offsets(msgid)
    for fname, off in fields.items():
        check(truth.get(fname) == off,
              f'{ref.MSGS[msgid][0]}.{fname} read at byte {off}')

print(f'\n  {len(fails)} failure(s)')
sys.exit(1 if fails else 0)
