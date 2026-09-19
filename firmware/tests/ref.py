# Independent MAVLink v2 reference. CRC_EXTRA is DERIVED from the message
# definition (name + sorted field types/names), exactly as the MAVLink
# generator does, rather than copied from the firmware's table.
import struct

def crc_accum(b, crc):
    tmp = b ^ (crc & 0xFF)
    tmp = (tmp ^ (tmp << 4)) & 0xFF
    return ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF

def crc(data, seed=0xFFFF):
    c = seed
    for b in data:
        c = crc_accum(b, c)
    return c

SIZE = {'uint64_t':8,'int64_t':8,'double':8,'uint32_t':4,'int32_t':4,'float':4,
        'uint16_t':2,'int16_t':2,'uint8_t':1,'int8_t':1,'char':1,
        'uint8_t_mavlink_version':1}

# (name, [(type, field, arraylen_or_0)]) in XML DECLARATION order.
MSGS = {
 0:  ('HEARTBEAT', [('uint8_t','type',0),('uint8_t','autopilot',0),
      ('uint8_t','base_mode',0),('uint32_t','custom_mode',0),
      ('uint8_t','system_status',0),('uint8_t_mavlink_version','mavlink_version',0)]),
 1:  ('SYS_STATUS', [('uint32_t','onboard_control_sensors_present',0),
      ('uint32_t','onboard_control_sensors_enabled',0),
      ('uint32_t','onboard_control_sensors_health',0),('uint16_t','load',0),
      ('uint16_t','voltage_battery',0),('int16_t','current_battery',0),
      ('int8_t','battery_remaining',0),('uint16_t','drop_rate_comm',0),
      ('uint16_t','errors_comm',0),('uint16_t','errors_count1',0),
      ('uint16_t','errors_count2',0),('uint16_t','errors_count3',0),
      ('uint16_t','errors_count4',0)]),
 11: ('SET_MODE', [('uint8_t','target_system',0),('uint8_t','base_mode',0),
      ('uint32_t','custom_mode',0)]),
 24: ('GPS_RAW_INT', [('uint64_t','time_usec',0),('uint8_t','fix_type',0),
      ('int32_t','lat',0),('int32_t','lon',0),('int32_t','alt',0),
      ('uint16_t','eph',0),('uint16_t','epv',0),('uint16_t','vel',0),
      ('uint16_t','cog',0),('uint8_t','satellites_visible',0)]),
 30: ('ATTITUDE', [('uint32_t','time_boot_ms',0),('float','roll',0),
      ('float','pitch',0),('float','yaw',0),('float','rollspeed',0),
      ('float','pitchspeed',0),('float','yawspeed',0)]),
 33: ('GLOBAL_POSITION_INT', [('uint32_t','time_boot_ms',0),('int32_t','lat',0),
      ('int32_t','lon',0),('int32_t','alt',0),('int32_t','relative_alt',0),
      ('int16_t','vx',0),('int16_t','vy',0),('int16_t','vz',0),('uint16_t','hdg',0)]),
 69: ('MANUAL_CONTROL', [('uint8_t','target',0),('int16_t','x',0),('int16_t','y',0),
      ('int16_t','z',0),('int16_t','r',0),('uint16_t','buttons',0)]),
 74: ('VFR_HUD', [('float','airspeed',0),('float','groundspeed',0),
      ('int16_t','heading',0),('uint16_t','throttle',0),('float','alt',0),
      ('float','climb',0)]),
 76: ('COMMAND_LONG', [('uint8_t','target_system',0),('uint8_t','target_component',0),
      ('uint16_t','command',0),('uint8_t','confirmation',0),('float','param1',0),
      ('float','param2',0),('float','param3',0),('float','param4',0),
      ('float','param5',0),('float','param6',0),('float','param7',0)]),
 77: ('COMMAND_ACK', [('uint16_t','command',0),('uint8_t','result',0)]),
 86: ('SET_POSITION_TARGET_GLOBAL_INT', [('uint32_t','time_boot_ms',0),
      ('uint8_t','target_system',0),('uint8_t','target_component',0),
      ('uint8_t','coordinate_frame',0),('uint16_t','type_mask',0),
      ('int32_t','lat_int',0),('int32_t','lon_int',0),('float','alt',0),
      ('float','vx',0),('float','vy',0),('float','vz',0),('float','afx',0),
      ('float','afy',0),('float','afz',0),('float','yaw',0),('float','yaw_rate',0)]),
 253:('STATUSTEXT', [('uint8_t','severity',0),('char','text',50)]),
}

def sorted_fields(fields):
    # MAVLink sorts by field size descending, stable within equal sizes.
    return sorted(fields, key=lambda f: -SIZE[f[0]])

def crc_extra(msgid):
    name, fields = MSGS[msgid]
    c = crc((name + ' ').encode())
    for ftype, fname, alen in sorted_fields(fields):
        t = 'uint8_t' if ftype == 'uint8_t_mavlink_version' else ftype
        c = crc((t + ' ').encode(), c)
        c = crc((fname + ' ').encode(), c)
        if alen:
            c = crc_accum(alen, c)
    return (c & 0xFF) ^ (c >> 8)

def wire_offsets(msgid):
    off, out = 0, {}
    for ftype, fname, alen in sorted_fields(MSGS[msgid][1]):
        out[fname] = off
        off += SIZE[ftype] * (alen or 1)
    return out, off

def parse_v2(frame):
    assert frame[0] == 0xFD, "not a v2 frame"
    ln, incompat, compat, seq, sysid, compid = frame[1:7]
    msgid = frame[7] | frame[8] << 8 | frame[9] << 16
    payload = frame[10:10+ln]
    rx = frame[10+ln] | frame[11+ln] << 8
    c = crc(frame[1:10+ln])
    c = crc_accum(crc_extra(msgid), c)
    return dict(len=ln, seq=seq, sysid=sysid, compid=compid, msgid=msgid,
                payload=payload, crc_ok=(c == rx), total=12+ln)
