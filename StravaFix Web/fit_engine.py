"""
fit_engine.py
Minimal, dependency-free .FIT file reader/editor.

Supports:
  - Parsing definition/data messages (incl. compressed-timestamp headers)
  - Reading distance fields (record.distance, lap.total_distance, session.total_distance)
  - Proportionally rescaling ALL distance fields to hit a new total distance
  - Shifting ALL timestamp fields (record/lap/session/activity/file_id) by N seconds
  - Recomputing the FIT file CRC-16 so the result is not "corrupted"

This does NOT change the size of the file - only field values are overwritten
in place, so message layout/offsets never move.
"""

import struct
import math
import datetime

FIT_EPOCH = datetime.datetime(1989, 12, 31, tzinfo=datetime.timezone.utc)

BASE_TYPES = {
    0x00: ('enum', 1, 'B'),
    0x01: ('sint8', 1, 'b'),
    0x02: ('uint8', 1, 'B'),
    0x83: ('sint16', 2, 'h'),
    0x84: ('uint16', 2, 'H'),
    0x85: ('sint32', 4, 'i'),
    0x86: ('uint32', 4, 'I'),
    0x07: ('string', 1, None),
    0x88: ('float32', 4, 'f'),
    0x89: ('float64', 8, 'd'),
    0x0A: ('uint8z', 1, 'B'),
    0x8B: ('uint16z', 2, 'H'),
    0x8C: ('uint32z', 4, 'I'),
    0x0D: ('byte', 1, None),
    0x8E: ('sint64', 8, 'q'),
    0x8F: ('uint64', 8, 'Q'),
    0x90: ('uint64z', 8, 'Q'),
}

GLOBAL_MSG_NAMES = {0: 'file_id', 18: 'session', 19: 'lap', 20: 'record', 21: 'event', 34: 'activity'}

CRC_TABLE = [
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
]


class FitParseError(Exception):
    pass


def fit_crc(data):
    crc = 0
    for b in data:
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[b & 0xF]
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[(b >> 4) & 0xF]
    return crc & 0xFFFF


def parse(data):
    """data: bytes-like. Returns (records, header_size, data_size)."""
    if len(data) < 14 or data[8:12] != b'.FIT':
        raise FitParseError("Not a valid .FIT file (missing '.FIT' signature).")

    header_size = data[0]
    data_size = struct.unpack_from('<I', data, 4)[0]

    if header_size + data_size + 2 > len(data):
        raise FitParseError("File appears truncated or has an inconsistent header.")

    pos = header_size
    end_of_data = header_size + data_size
    local_defs = {}
    records = []

    while pos < end_of_data:
        header_byte = data[pos]
        rec_start = pos
        pos += 1

        compressed_ts = bool(header_byte & 0x80)
        if compressed_ts:
            local_type = (header_byte >> 5) & 0x03
            is_def = False
        else:
            is_def = bool(header_byte & 0x40)
            dev_flag = bool(header_byte & 0x20)
            local_type = header_byte & 0x0F

        if is_def:
            pos += 1  # reserved
            arch = data[pos]; pos += 1
            endian = '<' if arch == 0 else '>'
            global_num = struct.unpack_from(endian + 'H', data, pos)[0]; pos += 2
            num_fields = data[pos]; pos += 1
            fields = []
            for _ in range(num_fields):
                fnum, fsize, ftype = data[pos], data[pos + 1], data[pos + 2]
                pos += 3
                fields.append((fnum, fsize, ftype))
            dev_fields = []
            if dev_flag:
                num_dev = data[pos]; pos += 1
                for _ in range(num_dev):
                    fnum, fsize, dindex = data[pos], data[pos + 1], data[pos + 2]
                    pos += 3
                    dev_fields.append((fnum, fsize, dindex))
            local_defs[local_type] = {
                'global_num': global_num, 'fields': fields,
                'dev_fields': dev_fields, 'endian': endian,
            }
        else:
            d = local_defs.get(local_type)
            if d is None:
                raise FitParseError(f"Corrupt file: no definition for local type {local_type} at offset {rec_start}.")
            field_offsets = []
            for (fnum, fsize, ftype) in d['fields']:
                field_offsets.append((fnum, pos, fsize, ftype))
                pos += fsize
            for (fnum, fsize, dindex) in d['dev_fields']:
                pos += fsize  # skip developer fields, not needed here
            records.append({
                'offset': rec_start, 'global_num': d['global_num'],
                'field_offsets': field_offsets, 'endian': d['endian'],
            })

    return records, header_size, data_size


def decode_field(data, offset, size, base_type, endian):
    info = BASE_TYPES.get(base_type)
    if info and info[2] and info[1] == size:
        return struct.unpack_from(endian + info[2], data, offset)[0]
    return None


def get_summary(data, records):
    """Returns a dict describing the activity: distance, start time, tz offset, counts."""
    summary = {
        'total_distance_m': None, 'start_raw': None, 'tz_offset_s': 0,
        'num_records': 0, 'num_laps': 0, 'sport': None, 'duration_s': None, 'calories': None,
    }
    sessions = [r for r in records if r['global_num'] == 18]
    laps = [r for r in records if r['global_num'] == 19]
    recs = [r for r in records if r['global_num'] == 20]
    acts = [r for r in records if r['global_num'] == 34]

    summary['num_laps'] = len(laps)
    summary['num_records'] = len(recs)

    if sessions:
        s = sessions[0]
        for (fnum, foff, fsize, ftype) in s['field_offsets']:
            if fnum == 9:
                summary['total_distance_m'] = decode_field(data, foff, fsize, ftype, s['endian']) / 100.0
            if fnum == 2:
                summary['start_raw'] = decode_field(data, foff, fsize, ftype, s['endian'])
            if fnum == 7:
                summary['duration_s'] = decode_field(data, foff, fsize, ftype, s['endian']) / 1000.0
            if fnum == 11 and fsize == 2:
                summary['calories'] = decode_field(data, foff, fsize, ftype, s['endian'])

    if acts:
        a = acts[0]
        ts_val, local_val = None, None
        for (fnum, foff, fsize, ftype) in a['field_offsets']:
            if fnum == 253:
                ts_val = decode_field(data, foff, fsize, ftype, a['endian'])
            if fnum == 5:
                local_val = decode_field(data, foff, fsize, ftype, a['endian'])
        if ts_val is not None and local_val is not None:
            summary['tz_offset_s'] = local_val - ts_val

    if summary['start_raw'] is None and recs:
        r0 = recs[0]
        for (fnum, foff, fsize, ftype) in r0['field_offsets']:
            if fnum == 253:
                summary['start_raw'] = decode_field(data, foff, fsize, ftype, r0['endian'])

    return summary


def raw_to_local_dt(raw, tz_offset_s):
    return FIT_EPOCH + datetime.timedelta(seconds=raw + tz_offset_s)


def local_dt_to_raw(dt_naive_local, tz_offset_s):
    """dt_naive_local: a naive datetime representing the wall-clock time to set."""
    dt_utc_form = dt_naive_local.replace(tzinfo=datetime.timezone.utc)
    total_local_epoch = (dt_utc_form - FIT_EPOCH).total_seconds()
    return int(round(total_local_epoch - tz_offset_s))


def apply_distance_scale(data, records, ratio):
    """Multiply every distance-bearing field (record.distance, lap/session.total_distance)
    by `ratio`, in place. Returns number of fields changed."""
    changed = 0
    for r in records:
        endian = r['endian']
        is_record = r['global_num'] == 20
        is_lap_or_session = r['global_num'] in (18, 19)
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            target = (is_record and fnum == 5) or (is_lap_or_session and fnum == 9)
            if not target or ftype != 0x86 or fsize != 4:
                continue
            current = struct.unpack_from(endian + 'I', data, foff)[0]
            if current in (0, 0xFFFFFFFF):
                continue
            new_val = int(round(current * ratio))
            new_val = max(0, min(new_val, 0xFFFFFFFE))
            struct.pack_into(endian + 'I', data, foff, new_val)
            changed += 1
    return changed


def apply_time_shift(data, records, shift_seconds):
    """Shift every timestamp-like field by shift_seconds, in place. Returns count changed."""
    changed = 0
    for r in records:
        endian = r['endian']
        gnum = r['global_num']
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            is_timestamp = fnum == 253
            is_start_time = gnum in (18, 19) and fnum == 2
            is_local_timestamp = gnum == 34 and fnum == 5
            is_time_created = gnum == 0 and fnum == 4
            if not (is_timestamp or is_start_time or is_local_timestamp or is_time_created):
                continue
            if ftype != 0x86 or fsize != 4:
                continue
            current = struct.unpack_from(endian + 'I', data, foff)[0]
            if current in (0, 0xFFFFFFFF):
                continue
            new_val = current + shift_seconds
            if new_val < 0:
                continue
            struct.pack_into(endian + 'I', data, foff, new_val)
            changed += 1
    return changed


def apply_altitude_shift(data, records, delta_m):
    """Shift every altitude/enhanced_altitude record value by delta_m metres, in place.
    A constant shift never changes total ascent/descent (those come from differences
    between consecutive points), so no other fields need touching. Returns count changed."""
    changed = 0
    raw_delta = int(round(delta_m * 5))  # scale = 5 for both altitude(2) and enhanced_altitude(78)
    for r in records:
        if r['global_num'] != 20:
            continue
        endian = r['endian']
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            if fnum not in (2, 78):
                continue
            if fnum == 2 and (ftype != 0x84 or fsize != 2):
                continue
            if fnum == 78 and (ftype != 0x86 or fsize != 4):
                continue
            fmt = endian + ('H' if fnum == 2 else 'I')
            current = struct.unpack_from(fmt, data, foff)[0]
            invalid = 0xFFFF if fnum == 2 else 0xFFFFFFFF
            if current == invalid:
                continue
            new_val = current + raw_delta
            maxval = 0xFFFE if fnum == 2 else 0xFFFFFFFE
            new_val = max(0, min(new_val, maxval))
            struct.pack_into(fmt, data, foff, new_val)
            changed += 1
    return changed


def apply_heart_rate_scale(data, records, ratio):
    """Scale every record's heart_rate field by `ratio`, then recompute avg/max
    heart_rate on the session and on every lap from the actual scaled record
    values (grouped by each lap's timestamp window). Returns count of record
    fields changed."""
    changed = 0
    for r in records:
        if r['global_num'] != 20:
            continue
        endian = r['endian']
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            if fnum != 3 or ftype != 0x02 or fsize != 1:
                continue
            current = struct.unpack_from('B', data, foff)[0]
            if current in (0, 0xFF):
                continue
            new_val = int(round(current * ratio))
            new_val = max(1, min(new_val, 254))
            struct.pack_into('B', data, foff, new_val)
            changed += 1

    _recompute_hr_aggregates(data, records)
    return changed


def _record_hr_and_ts(data, r):
    endian = r['endian']
    hr, ts = None, None
    for (fnum, foff, fsize, ftype) in r['field_offsets']:
        if fnum == 3 and ftype == 0x02 and fsize == 1:
            v = struct.unpack_from('B', data, foff)[0]
            if v != 0xFF:
                hr = v
        if fnum == 253 and ftype == 0x86 and fsize == 4:
            ts = struct.unpack_from(endian + 'I', data, foff)[0]
    return hr, ts


def _recompute_hr_aggregates(data, records):
    recs = [r for r in records if r['global_num'] == 20]
    laps = [r for r in records if r['global_num'] == 19]

    rec_hr_ts = [_record_hr_and_ts(data, r) for r in recs]
    rec_hr_ts = [(hr, ts) for (hr, ts) in rec_hr_ts if hr is not None and ts is not None]

    if not rec_hr_ts:
        return

    def write_avg_max(r, avg_fnum, max_fnum, hrs):
        if not hrs:
            return
        avg_v = int(round(sum(hrs) / len(hrs)))
        max_v = max(hrs)
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            if ftype != 0x02 or fsize != 1:
                continue
            if fnum == avg_fnum:
                struct.pack_into('B', data, foff, min(254, max(1, avg_v)))
            elif fnum == max_fnum:
                struct.pack_into('B', data, foff, min(254, max(1, max_v)))

    # session: avg_heart_rate=16, max_heart_rate=17
    sessions = [r for r in records if r['global_num'] == 18]
    if sessions:
        all_hrs = [hr for (hr, ts) in rec_hr_ts]
        write_avg_max(sessions[0], 16, 17, all_hrs)

    # laps: avg_heart_rate=15, max_heart_rate=16, grouped by [start_time, start_time+elapsed]
    for lap in laps:
        endian = lap['endian']
        start_time, elapsed = None, None
        for (fnum, foff, fsize, ftype) in lap['field_offsets']:
            if fnum == 2 and ftype == 0x86 and fsize == 4:
                start_time = struct.unpack_from(endian + 'I', data, foff)[0]
            if fnum == 7 and ftype == 0x86 and fsize == 4:
                elapsed = struct.unpack_from(endian + 'I', data, foff)[0] / 1000.0
        if start_time is None or elapsed is None:
            continue
        end_time = start_time + elapsed + 1
        lap_hrs = [hr for (hr, ts) in rec_hr_ts if start_time <= ts <= end_time]
        write_avg_max(lap, 15, 16, lap_hrs)


def apply_speed_scale(data, records, ratio):
    """Scale every record's speed / enhanced_speed field by `ratio`. Does NOT touch
    lap/session summary stats - Strava (and most tools) derive average pace/speed
    from distance and elapsed time, which are left untouched, so nothing there
    needs reconciling. Returns count of fields changed."""
    changed = 0
    for r in records:
        if r['global_num'] != 20:
            continue
        endian = r['endian']
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            if fnum == 6 and ftype == 0x84 and fsize == 2:
                fmt = endian + 'H'
                invalid, maxval = 0xFFFF, 0xFFFE
            elif fnum == 73 and ftype == 0x86 and fsize == 4:
                fmt = endian + 'I'
                invalid, maxval = 0xFFFFFFFF, 0xFFFFFFFE
            else:
                continue
            current = struct.unpack_from(fmt, data, foff)[0]
            if current == invalid:
                continue
            new_val = int(round(current * ratio))
            new_val = max(0, min(new_val, maxval))
            struct.pack_into(fmt, data, foff, new_val)
            changed += 1
    return changed


def lonlat_to_webmercator(lon, lat):
    """Converts WGS84 lon/lat (degrees) to Web Mercator x/y (metres, EPSG:3857) -
    the projection OpenStreetMap/most web map tiles use. Pure math, no dependencies."""
    x = lon * 20037508.34 / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    y = y * 20037508.34 / 180.0
    return x, y


def extract_route_and_profile(data, records):
    """Returns dict of parallel lists for plotting: distance_m, altitude_m, heart_rate,
    cadence, power, lat_deg, lon_deg - one entry per GPS-valid record (skips records
    missing a distance field)."""
    out = {'distance_m': [], 'altitude_m': [], 'heart_rate': [], 'lat_deg': [], 'lon_deg': [],
           'cadence': [], 'power': []}
    recs = [r for r in records if r['global_num'] == 20]
    for r in recs:
        endian = r['endian']
        vals = {}
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            if fnum == 5 and ftype == 0x86 and fsize == 4:
                vals['distance_m'] = struct.unpack_from(endian + 'I', data, foff)[0] / 100.0
            elif fnum == 78 and ftype == 0x86 and fsize == 4:
                v = struct.unpack_from(endian + 'I', data, foff)[0]
                if v != 0xFFFFFFFF:
                    vals['altitude_m'] = v / 5.0 - 500.0
            elif fnum == 2 and ftype == 0x84 and fsize == 2 and 'altitude_m' not in vals:
                v = struct.unpack_from(endian + 'H', data, foff)[0]
                if v != 0xFFFF:
                    vals['altitude_m'] = v / 5.0 - 500.0
            elif fnum == 3 and ftype == 0x02 and fsize == 1:
                v = struct.unpack_from('B', data, foff)[0]
                if v != 0xFF:
                    vals['heart_rate'] = v
            elif fnum == 4 and ftype == 0x02 and fsize == 1:
                v = struct.unpack_from('B', data, foff)[0]
                if v != 0xFF:
                    vals['cadence'] = v
            elif fnum == 7 and ftype == 0x84 and fsize == 2:
                v = struct.unpack_from(endian + 'H', data, foff)[0]
                if v != 0xFFFF:
                    vals['power'] = v
            elif fnum == 0 and ftype == 0x85 and fsize == 4:
                v = struct.unpack_from(endian + 'i', data, foff)[0]
                if v != 0x7FFFFFFF:
                    vals['lat_deg'] = v * (180.0 / 2**31)
            elif fnum == 1 and ftype == 0x85 and fsize == 4:
                v = struct.unpack_from(endian + 'i', data, foff)[0]
                if v != 0x7FFFFFFF:
                    vals['lon_deg'] = v * (180.0 / 2**31)
        if 'distance_m' in vals:
            for key in out:
                out[key].append(vals.get(key))
    return out


def apply_cadence_scale(data, records, ratio):
    """Scale every record's cadence field by `ratio`, then recompute avg/max
    cadence on the session and on every lap from the actual scaled record
    values (grouped by each lap's timestamp window), same approach as
    apply_heart_rate_scale. Returns count of record fields changed."""
    changed = 0
    for r in records:
        if r['global_num'] != 20:
            continue
        endian = r['endian']
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            if fnum != 4 or ftype != 0x02 or fsize != 1:
                continue
            current = struct.unpack_from('B', data, foff)[0]
            if current in (0, 0xFF):
                continue
            new_val = int(round(current * ratio))
            new_val = max(1, min(new_val, 254))
            struct.pack_into('B', data, foff, new_val)
            changed += 1

    _recompute_uint8_aggregates(data, records, record_fnum=4,
                                 session_avg=18, session_max=19, lap_avg=17, lap_max=18)
    return changed


def apply_power_scale(data, records, ratio):
    """Scale every record's power field by `ratio`, then recompute avg/max
    power on the session and on every lap the same way as heart rate/cadence.
    Power is uint16 (watts), unlike HR/cadence which are uint8."""
    changed = 0
    for r in records:
        if r['global_num'] != 20:
            continue
        endian = r['endian']
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            if fnum != 7 or ftype != 0x84 or fsize != 2:
                continue
            current = struct.unpack_from(endian + 'H', data, foff)[0]
            if current == 0xFFFF:
                continue
            new_val = int(round(current * ratio))
            new_val = max(0, min(new_val, 0xFFFE))
            struct.pack_into(endian + 'H', data, foff, new_val)
            changed += 1

    _recompute_uint16_aggregates(data, records, record_fnum=7,
                                  session_avg=20, session_max=21, lap_avg=19, lap_max=20)
    return changed


def _record_field_and_ts(data, r, record_fnum, base_type, size):
    endian = r['endian']
    val, ts = None, None
    fmt = 'B' if base_type == 0x02 else (endian + 'H')
    for (fnum, foff, fsize, ftype) in r['field_offsets']:
        if fnum == record_fnum and ftype == base_type and fsize == size:
            v = struct.unpack_from(fmt, data, foff)[0]
            invalid = 0xFF if base_type == 0x02 else 0xFFFF
            if v != invalid:
                val = v
        if fnum == 253 and ftype == 0x86 and fsize == 4:
            ts = struct.unpack_from(endian + 'I', data, foff)[0]
    return val, ts


def _write_avg_max(data, r, avg_fnum, max_fnum, base_type, values):
    if not values:
        return
    avg_v = int(round(sum(values) / len(values)))
    max_v = max(values)
    lo, hi = (1, 254) if base_type == 0x02 else (0, 0xFFFE)
    endian = r['endian']
    for (fnum, foff, fsize, ftype) in r['field_offsets']:
        if ftype != base_type:
            continue
        this_fmt = 'B' if base_type == 0x02 else (endian + 'H')
        if fnum == avg_fnum:
            struct.pack_into(this_fmt, data, foff, min(hi, max(lo, avg_v)))
        elif fnum == max_fnum:
            struct.pack_into(this_fmt, data, foff, min(hi, max(lo, max_v)))


def _recompute_aggregates(data, records, record_fnum, base_type, size,
                           session_avg, session_max, lap_avg, lap_max):
    recs = [r for r in records if r['global_num'] == 20]
    laps = [r for r in records if r['global_num'] == 19]

    rec_val_ts = [_record_field_and_ts(data, r, record_fnum, base_type, size) for r in recs]
    rec_val_ts = [(v, ts) for (v, ts) in rec_val_ts if v is not None and ts is not None]
    if not rec_val_ts:
        return

    sessions = [r for r in records if r['global_num'] == 18]
    if sessions:
        all_vals = [v for (v, ts) in rec_val_ts]
        _write_avg_max(data, sessions[0], session_avg, session_max, base_type, all_vals)

    for lap in laps:
        endian = lap['endian']
        start_time, elapsed = None, None
        for (fnum, foff, fsize, ftype) in lap['field_offsets']:
            if fnum == 2 and ftype == 0x86 and fsize == 4:
                start_time = struct.unpack_from(endian + 'I', data, foff)[0]
            if fnum == 7 and ftype == 0x86 and fsize == 4:
                elapsed = struct.unpack_from(endian + 'I', data, foff)[0] / 1000.0
        if start_time is None or elapsed is None:
            continue
        end_time = start_time + elapsed + 1
        lap_vals = [v for (v, ts) in rec_val_ts if start_time <= ts <= end_time]
        _write_avg_max(data, lap, lap_avg, lap_max, base_type, lap_vals)


def _recompute_uint8_aggregates(data, records, record_fnum, session_avg, session_max, lap_avg, lap_max):
    _recompute_aggregates(data, records, record_fnum, 0x02, 1, session_avg, session_max, lap_avg, lap_max)


def _recompute_uint16_aggregates(data, records, record_fnum, session_avg, session_max, lap_avg, lap_max):
    _recompute_aggregates(data, records, record_fnum, 0x84, 2, session_avg, session_max, lap_avg, lap_max)


def apply_calories_scale(data, records, ratio):
    """Scales every lap's total_calories, then sets the session total to the
    exact sum of the (rounded) scaled lap values - matching the real
    relationship in device-written files (session total == sum of laps)
    more precisely than independently scaling the session field too."""
    changed = 0
    lap_total = 0
    laps = [r for r in records if r['global_num'] == 19]
    for lap in laps:
        endian = lap['endian']
        for (fnum, foff, fsize, ftype) in lap['field_offsets']:
            if fnum == 11 and ftype == 0x84 and fsize == 2:
                current = struct.unpack_from(endian + 'H', data, foff)[0]
                if current == 0xFFFF:
                    continue
                new_val = max(0, min(0xFFFE, int(round(current * ratio))))
                struct.pack_into(endian + 'H', data, foff, new_val)
                lap_total += new_val
                changed += 1

    sessions = [r for r in records if r['global_num'] == 18]
    if sessions and changed:
        s = sessions[0]
        endian = s['endian']
        for (fnum, foff, fsize, ftype) in s['field_offsets']:
            if fnum == 11 and ftype == 0x84 and fsize == 2:
                struct.pack_into(endian + 'H', data, foff, min(0xFFFE, lap_total))
                changed += 1
    return changed


def apply_duration_scale(data, records, ratio, start_raw):
    """Stretches or compresses the whole activity's timing by `ratio`,
    anchored at `start_raw` (the activity's own start instant never moves).
    Scales total_elapsed_time/total_timer_time on session+lap directly, and
    scales every timestamp's distance-from-start by the same ratio so
    lap splits and the GPS timeline stay internally consistent. Also scales
    every record's stored speed by 1/ratio, since distance is unchanged but
    time isn't - keeping the speed data physically consistent with the new
    pacing. Returns count of fields changed (across all of the above)."""
    changed = 0
    for r in records:
        endian = r['endian']
        gnum = r['global_num']
        for (fnum, foff, fsize, ftype) in r['field_offsets']:
            if ftype != 0x86 or fsize != 4:
                continue
            is_timestamp = fnum == 253
            is_start_time = gnum in (18, 19) and fnum == 2
            is_local_timestamp = gnum == 34 and fnum == 5
            is_time_created = gnum == 0 and fnum == 4
            is_elapsed_or_timer = gnum in (18, 19) and fnum in (7, 8)

            current = struct.unpack_from(endian + 'I', data, foff)[0]
            if current in (0, 0xFFFFFFFF):
                continue

            if is_elapsed_or_timer:
                new_val = max(0, int(round(current * ratio)))
                struct.pack_into(endian + 'I', data, foff, new_val)
                changed += 1
            elif is_timestamp or is_start_time or is_local_timestamp or is_time_created:
                delta = current - start_raw
                new_val = start_raw + int(round(delta * ratio))
                if new_val < 0:
                    continue
                struct.pack_into(endian + 'I', data, foff, new_val)
                changed += 1

    if ratio > 0:
        changed += apply_speed_scale(data, records, 1.0 / ratio)
    return changed


def rewrite_crc(data, header_size, data_size):
    """Recompute and write the trailing file CRC. `data` must be a bytearray."""
    body = data[:header_size + data_size]
    new_crc = fit_crc(body)
    struct.pack_into('<H', data, header_size + data_size, new_crc)
    return new_crc


def load_fit_bytes(path):
    with open(path, 'rb') as f:
        return bytearray(f.read())


def save_fit_bytes(path, data):
    with open(path, 'wb') as f:
        f.write(data)
