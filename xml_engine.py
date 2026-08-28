"""
xml_engine.py - reads, edits, and writes .tcx and .gpx activity files.

Unlike .fit, these are plain XML with no checksum, so "not corrupting" mostly
just means staying well-formed XML - much lower risk than binary FIT editing.

Exposes the same function-style API as fit_adapter.py so activity_io.py can
dispatch between formats uniformly:
  parse_file, get_summary, extract_profile, raw_to_local_dt, local_dt_to_raw,
  apply_distance_scale, apply_time_shift, apply_altitude_shift,
  apply_heart_rate_scale, validate, serialize

IMPORTANT DIFFERENCE FROM FIT: GPX has no stored distance field anywhere in
its spec - every platform computes distance from the GPS coordinates. So
apply_distance_scale() for GPX actually rescales the track geometry (each
point's displacement from the previous point, scaled by the ratio, anchored
at the start point) rather than editing a number. TCX, like FIT, does store
explicit distance fields, so its distance edit is a direct field edit.
"""

import math
import re
import datetime
import xml.etree.ElementTree as ET

EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


class XmlParseError(Exception):
    pass


def _localname(tag):
    return tag.split('}')[-1] if '}' in tag else tag


def _find_all(root, name):
    return [el for el in root.iter() if _localname(el.tag) == name]


def _find_child(el, name):
    for c in el:
        if _localname(c.tag) == name:
            return c
    return None


def _parse_iso(s):
    if s is None:
        return None
    s = s.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return datetime.datetime.fromisoformat(s)


def _format_iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


# --------------------------------------------------------------- raw <-> dt
# "raw" here is just seconds since the Unix epoch (UTC). tz_offset_s is
# always 0 for these formats - TCX/GPX times are always UTC, with no
# device-local-time concept like FIT has.

def raw_to_local_dt(raw, tz_offset_s):
    return EPOCH + datetime.timedelta(seconds=raw + tz_offset_s)


def local_dt_to_raw(dt_naive_local, tz_offset_s):
    dt_utc_form = dt_naive_local.replace(tzinfo=datetime.timezone.utc)
    return int(round((dt_utc_form - EPOCH).total_seconds() - tz_offset_s))


# ------------------------------------------------------------------ parse

class Context:
    def __init__(self, fmt, tree, root):
        self.fmt = fmt          # 'tcx' or 'gpx'
        self.tree = tree
        self.root = root


def clone_context(ctx):
    """Returns an independent working copy (deep-copies the XML element tree)
    - edits to the clone never touch the original ctx."""
    import copy
    return Context(ctx.fmt, None, copy.deepcopy(ctx.root))


def parse_string(text):
    """Core parse logic, operating on XML text directly (no file I/O) - used
    by parse_file() below, and directly by the browser build (web_bridge.py)
    which gets file content from the browser's File API instead of a path."""
    head = text[:4096]
    for prefix, uri in re.findall(r'xmlns:([\w.-]+)="([^"]+)"', head):
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            pass  # e.g. "ns0" - reserved for ElementTree's own auto-generated prefixes
    default_uri = re.search(r'xmlns="([^"]+)"', head)
    if default_uri:
        try:
            ET.register_namespace('', default_uri.group(1))
        except ValueError:
            pass

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise XmlParseError(f"Not a valid XML file: {e}")

    tag = _localname(root.tag).lower()
    if tag == 'trainingcenterdatabase':
        fmt = 'tcx'
    elif tag == 'gpx':
        fmt = 'gpx'
    else:
        raise XmlParseError(f"Unrecognized root element <{_localname(root.tag)}> - not a TCX or GPX file.")
    return Context(fmt, None, root)


def parse_file(path):
    # Pre-register the file's own namespace prefixes so serialization
    # preserves them (e.g. "ns2:", "gpxtpx:") instead of ElementTree
    # inventing generic ns0:/ns1: prefixes. Purely cosmetic - XML
    # namespace-aware parsers work off the URI either way - but keeps
    # the output closer to what the original device/app wrote.
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        text = f.read()
    return parse_string(text)


# ------------------------------------------------------------- trackpoints

def _tcx_trackpoints(ctx):
    """Returns list of dicts: {el, time(datetime), lat, lon, alt, dist, hr, cad, pwr}."""
    pts = []
    for tp in _find_all(ctx.root, 'Trackpoint'):
        time_el = _find_child(tp, 'Time')
        pos_el = _find_child(tp, 'Position')
        alt_el = _find_child(tp, 'AltitudeMeters')
        dist_el = _find_child(tp, 'DistanceMeters')
        hr_el = _find_child(tp, 'HeartRateBpm')
        hr_val_el = _find_child(hr_el, 'Value') if hr_el is not None else None
        lat_el = _find_child(pos_el, 'LatitudeDegrees') if pos_el is not None else None
        lon_el = _find_child(pos_el, 'LongitudeDegrees') if pos_el is not None else None

        cad_el = _find_child(tp, 'Cadence')
        pwr_el = None
        ext_el = _find_child(tp, 'Extensions')
        if ext_el is not None:
            for cand in ext_el.iter():
                name = _localname(cand.tag)
                if cad_el is None and name == 'RunCadence':
                    cad_el = cand
                if name == 'Watts':
                    pwr_el = cand

        pts.append({
            'el': tp,
            'time_el': time_el,
            'time': _parse_iso(time_el.text) if time_el is not None and time_el.text else None,
            'lat_el': lat_el, 'lon_el': lon_el,
            'lat': float(lat_el.text) if lat_el is not None and lat_el.text else None,
            'lon': float(lon_el.text) if lon_el is not None and lon_el.text else None,
            'alt_el': alt_el,
            'alt': float(alt_el.text) if alt_el is not None and alt_el.text else None,
            'dist_el': dist_el,
            'dist': float(dist_el.text) if dist_el is not None and dist_el.text else None,
            'hr_el': hr_val_el,
            'hr': int(float(hr_val_el.text)) if hr_val_el is not None and hr_val_el.text else None,
            'cad_el': cad_el,
            'cad': int(float(cad_el.text)) if cad_el is not None and cad_el.text else None,
            'pwr_el': pwr_el,
            'pwr': int(float(pwr_el.text)) if pwr_el is not None and pwr_el.text else None,
        })
    return pts


def _gpx_trackpoints(ctx):
    pts = []
    for tp in _find_all(ctx.root, 'trkpt'):
        time_el = _find_child(tp, 'time')
        ele_el = _find_child(tp, 'ele')
        hr_el = None
        cad_el = None
        pwr_el = None
        ext_el = _find_child(tp, 'extensions')
        if ext_el is not None:
            for cand in ext_el.iter():
                name = _localname(cand.tag).lower()
                if name == 'hr':
                    hr_el = cand
                elif name == 'cad':
                    cad_el = cand
                elif name in ('power', 'watts'):
                    pwr_el = cand
        lat = float(tp.get('lat')) if tp.get('lat') is not None else None
        lon = float(tp.get('lon')) if tp.get('lon') is not None else None
        pts.append({
            'el': tp,
            'time_el': time_el,
            'time': _parse_iso(time_el.text) if time_el is not None and time_el.text else None,
            'lat': lat, 'lon': lon,
            'alt_el': ele_el,
            'alt': float(ele_el.text) if ele_el is not None and ele_el.text else None,
            'hr_el': hr_el,
            'hr': int(float(hr_el.text)) if hr_el is not None and hr_el.text else None,
            'cad_el': cad_el,
            'cad': int(float(cad_el.text)) if cad_el is not None and cad_el.text else None,
            'pwr_el': pwr_el,
            'pwr': int(float(pwr_el.text)) if pwr_el is not None and pwr_el.text else None,
        })
    return pts


def _trackpoints(ctx):
    return _tcx_trackpoints(ctx) if ctx.fmt == 'tcx' else _gpx_trackpoints(ctx)


# ------------------------------------------------------------------ laps

def _tcx_laps(ctx):
    return _find_all(ctx.root, 'Lap')


# ---------------------------------------------------------------- summary

def get_summary(ctx):
    pts = _trackpoints(ctx)
    times = [p['time'] for p in pts if p['time'] is not None]
    start_dt = min(times) if times else None
    start_raw = int(round((start_dt - EPOCH).total_seconds())) if start_dt else None

    duration_s = None
    calories = None
    if ctx.fmt == 'tcx':
        dists = [p['dist'] for p in pts if p['dist'] is not None]
        total_distance_m = max(dists) if dists else None
        num_laps = len(_tcx_laps(ctx))
        lap_durations = []
        lap_calories = []
        for lap in _tcx_laps(ctx):
            tts_el = _find_child(lap, 'TotalTimeSeconds')
            if tts_el is not None and tts_el.text:
                try:
                    lap_durations.append(float(tts_el.text))
                except ValueError:
                    pass
            cal_el = _find_child(lap, 'Calories')
            if cal_el is not None and cal_el.text:
                try:
                    lap_calories.append(float(cal_el.text))
                except ValueError:
                    pass
        if lap_durations:
            duration_s = sum(lap_durations)
        if lap_calories:
            calories = sum(lap_calories)
    else:
        coords = [(p['lat'], p['lon']) for p in pts if p['lat'] is not None and p['lon'] is not None]
        total_distance_m = 0.0
        for i in range(1, len(coords)):
            total_distance_m += _haversine_m(*coords[i - 1], *coords[i])
        total_distance_m = total_distance_m if coords else None
        num_laps = len(_find_all(ctx.root, 'trkseg'))

    if duration_s is None and times:
        duration_s = (max(times) - min(times)).total_seconds()

    return {
        'total_distance_m': total_distance_m,
        'start_raw': start_raw,
        'tz_offset_s': 0,
        'num_records': len(pts),
        'num_laps': num_laps,
        'duration_s': duration_s,
        'calories': calories,
    }


def extract_profile(ctx):
    pts = _trackpoints(ctx)
    out = {'distance_m': [], 'altitude_m': [], 'heart_rate': [], 'lat_deg': [], 'lon_deg': [],
           'cadence': [], 'power': []}

    if ctx.fmt == 'tcx':
        for p in pts:
            if p['dist'] is None:
                continue
            out['distance_m'].append(p['dist'])
            out['altitude_m'].append(p['alt'])
            out['heart_rate'].append(p['hr'])
            out['lat_deg'].append(p['lat'])
            out['lon_deg'].append(p['lon'])
            out['cadence'].append(p['cad'])
            out['power'].append(p['pwr'])
    else:
        cum = 0.0
        prev = None
        for p in pts:
            if p['lat'] is None or p['lon'] is None:
                continue
            if prev is not None:
                cum += _haversine_m(prev[0], prev[1], p['lat'], p['lon'])
            prev = (p['lat'], p['lon'])
            out['distance_m'].append(cum)
            out['altitude_m'].append(p['alt'])
            out['heart_rate'].append(p['hr'])
            out['lat_deg'].append(p['lat'])
            out['lon_deg'].append(p['lon'])
            out['cadence'].append(p['cad'])
            out['power'].append(p['pwr'])

    return out


# ------------------------------------------------------------------ edits

def apply_distance_scale(ctx, ratio):
    """TCX: scales every stored DistanceMeters field directly (same approach
    as FIT). GPX: has no distance field to edit, so instead rescales the
    track geometry itself - each point's displacement (in metres, via an
    equirectangular local approximation) from the previous point is scaled
    by `ratio`, anchored at the first point. This exactly scales total path
    length by `ratio` while preserving the route's shape."""
    changed = 0
    if ctx.fmt == 'tcx':
        for p in _trackpoints(ctx):
            if p['dist_el'] is not None and p['dist_el'].text:
                try:
                    val = float(p['dist_el'].text)
                except ValueError:
                    continue
                p['dist_el'].text = f"{val * ratio:.2f}"
                changed += 1
        for lap in _tcx_laps(ctx):
            dist_el = _find_child(lap, 'DistanceMeters')
            if dist_el is not None and dist_el.text:
                try:
                    val = float(dist_el.text)
                except ValueError:
                    continue
                dist_el.text = f"{val * ratio:.2f}"
                changed += 1
        return changed

    # gpx: rescale geometry
    pts = [p for p in _gpx_trackpoints(ctx) if p['lat'] is not None and p['lon'] is not None]
    if len(pts) < 2:
        return 0
    lat0, lon0 = pts[0]['lat'], pts[0]['lon']
    cur_lat, cur_lon = lat0, lon0
    for i in range(1, len(pts)):
        prev = pts[i - 1]
        cur = pts[i]
        mean_lat_rad = math.radians((prev['lat'] + cur['lat']) / 2.0)
        dx_m = (cur['lon'] - prev['lon']) * math.cos(mean_lat_rad) * 111320.0
        dy_m = (cur['lat'] - prev['lat']) * 111320.0
        dx_m *= ratio
        dy_m *= ratio
        new_lat = cur_lat + (dy_m / 111320.0)
        new_lon = cur_lon + (dx_m / (math.cos(mean_lat_rad) * 111320.0))
        cur['el'].set('lat', f"{new_lat:.7f}")
        cur['el'].set('lon', f"{new_lon:.7f}")
        cur_lat, cur_lon = new_lat, new_lon
        changed += 1
    return changed


def apply_time_shift(ctx, shift_seconds):
    changed = 0
    for p in _trackpoints(ctx):
        if p['time_el'] is not None and p['time'] is not None:
            new_dt = p['time'] + datetime.timedelta(seconds=shift_seconds)
            p['time_el'].text = _format_iso(new_dt)
            changed += 1
    if ctx.fmt == 'tcx':
        for lap in _tcx_laps(ctx):
            st = lap.get('StartTime')
            if st:
                dt = _parse_iso(st)
                if dt is not None:
                    lap.set('StartTime', _format_iso(dt + datetime.timedelta(seconds=shift_seconds)))
                    changed += 1
        for act_id in _find_all(ctx.root, 'Id'):
            if act_id.text:
                dt = _parse_iso(act_id.text)
                if dt is not None:
                    act_id.text = _format_iso(dt + datetime.timedelta(seconds=shift_seconds))
                    changed += 1
    return changed


def apply_altitude_shift(ctx, delta_m):
    changed = 0
    for p in _trackpoints(ctx):
        if p['alt_el'] is not None and p['alt_el'].text:
            try:
                val = float(p['alt_el'].text)
            except ValueError:
                continue
            p['alt_el'].text = f"{val + delta_m:.1f}"
            changed += 1
    return changed


def apply_heart_rate_scale(ctx, ratio):
    changed = 0
    pts = _trackpoints(ctx)
    scaled = []  # (time, new_hr) for lap aggregate recompute
    for p in pts:
        if p['hr_el'] is not None and p['hr_el'].text:
            try:
                val = float(p['hr_el'].text)
            except ValueError:
                continue
            new_hr = max(1, min(254, int(round(val * ratio))))
            p['hr_el'].text = str(new_hr)
            changed += 1
            if p['time'] is not None:
                scaled.append((p['time'], new_hr))

    if ctx.fmt == 'tcx' and scaled:
        for lap in _tcx_laps(ctx):
            st = lap.get('StartTime')
            tts_el = _find_child(lap, 'TotalTimeSeconds')
            if not st or tts_el is None or not tts_el.text:
                continue
            start_dt = _parse_iso(st)
            try:
                elapsed = float(tts_el.text)
            except ValueError:
                continue
            end_dt = start_dt + datetime.timedelta(seconds=elapsed + 1)
            lap_hrs = [hr for (t, hr) in scaled if start_dt <= t <= end_dt]
            if not lap_hrs:
                continue
            avg_el = _find_child(lap, 'AverageHeartRateBpm')
            max_el = _find_child(lap, 'MaximumHeartRateBpm')
            avg_val_el = _find_child(avg_el, 'Value') if avg_el is not None else None
            max_val_el = _find_child(max_el, 'Value') if max_el is not None else None
            if avg_val_el is not None:
                avg_val_el.text = str(int(round(sum(lap_hrs) / len(lap_hrs))))
            if max_val_el is not None:
                max_val_el.text = str(max(lap_hrs))

    return changed


def apply_cadence_scale(ctx, ratio):
    """Scales every trackpoint's cadence value. TCX also scales a lap-level
    <Cadence> average field directly, if present, by the same ratio."""
    changed = 0
    for p in _trackpoints(ctx):
        if p['cad_el'] is not None and p['cad_el'].text:
            try:
                val = float(p['cad_el'].text)
            except ValueError:
                continue
            new_val = max(0, min(254, int(round(val * ratio))))
            p['cad_el'].text = str(new_val)
            changed += 1

    if ctx.fmt == 'tcx':
        for lap in _tcx_laps(ctx):
            cad_el = _find_child(lap, 'Cadence')
            if cad_el is not None and cad_el.text:
                try:
                    val = float(cad_el.text)
                except ValueError:
                    continue
                cad_el.text = str(max(0, min(254, int(round(val * ratio)))))
                changed += 1
    return changed


def apply_power_scale(ctx, ratio):
    """Scales every trackpoint's power (watts) value. TCX also scales
    lap-level AvgWatts/MaxWatts extension fields directly, if present."""
    changed = 0
    for p in _trackpoints(ctx):
        if p['pwr_el'] is not None and p['pwr_el'].text:
            try:
                val = float(p['pwr_el'].text)
            except ValueError:
                continue
            new_val = max(0, int(round(val * ratio)))
            p['pwr_el'].text = str(new_val)
            changed += 1

    if ctx.fmt == 'tcx':
        for lap in _tcx_laps(ctx):
            for el in lap.iter():
                name = _localname(el.tag)
                if name in ('AvgWatts', 'MaxWatts') and el.text:
                    try:
                        val = float(el.text)
                    except ValueError:
                        continue
                    el.text = str(max(0, int(round(val * ratio))))
                    changed += 1
    return changed


def apply_calories_scale(ctx, ratio):
    """TCX only - scales each Lap's <Calories> field. GPX has no calories
    concept anywhere in its spec, so this is a no-op (returns 0) for GPX."""
    if ctx.fmt != 'tcx':
        return 0
    changed = 0
    for lap in _tcx_laps(ctx):
        cal_el = _find_child(lap, 'Calories')
        if cal_el is not None and cal_el.text:
            try:
                val = float(cal_el.text)
            except ValueError:
                continue
            cal_el.text = str(max(0, int(round(val * ratio))))
            changed += 1
    return changed


def apply_duration_scale(ctx, ratio, start_raw):
    """Stretches or compresses the whole activity's timing by `ratio`,
    anchored at `start_raw` (seconds since Unix epoch - the activity's start
    instant never moves). Scales every trackpoint's time by its
    distance-from-start, and (TCX) each Lap's TotalTimeSeconds/StartTime the
    same way. Distance is untouched (for TCX, the stored DistanceMeters
    values simply aren't touched; for GPX, distance is derived from GPS
    position, which duration scaling doesn't touch either), so average
    pace changes automatically and consistently - no separate speed
    field needs adjusting for these formats."""
    changed = 0
    start_dt = EPOCH + datetime.timedelta(seconds=start_raw)

    for p in _trackpoints(ctx):
        if p['time_el'] is not None and p['time'] is not None:
            delta = (p['time'] - start_dt).total_seconds()
            new_dt = start_dt + datetime.timedelta(seconds=delta * ratio)
            p['time_el'].text = _format_iso(new_dt)
            changed += 1

    if ctx.fmt == 'tcx':
        for lap in _tcx_laps(ctx):
            st = lap.get('StartTime')
            tts_el = _find_child(lap, 'TotalTimeSeconds')
            if st:
                lap_start_dt = _parse_iso(st)
                if lap_start_dt is not None:
                    delta = (lap_start_dt - start_dt).total_seconds()
                    new_dt = start_dt + datetime.timedelta(seconds=delta * ratio)
                    lap.set('StartTime', _format_iso(new_dt))
                    changed += 1
            if tts_el is not None and tts_el.text:
                try:
                    val = float(tts_el.text)
                except ValueError:
                    val = None
                if val is not None:
                    tts_el.text = f"{val * ratio:.1f}"
                    changed += 1
        for act_id in _find_all(ctx.root, 'Id'):
            if act_id.text:
                dt = _parse_iso(act_id.text)
                if dt is not None:
                    delta = (dt - start_dt).total_seconds()
                    act_id.text = _format_iso(start_dt + datetime.timedelta(seconds=delta * ratio))
                    changed += 1
    return changed


def validate(ctx):
    # Re-serialize and re-parse as a cheap well-formedness check.
    data = serialize(ctx)
    try:
        ET.fromstring(data)
    except ET.ParseError as e:
        raise XmlParseError(f"Edited file failed to re-validate: {e}")
    return True


def serialize(ctx):
    return ET.tostring(ctx.root, encoding='utf-8', xml_declaration=True)


def save_bytes(path, data):
    with open(path, 'wb') as f:
        f.write(data)
