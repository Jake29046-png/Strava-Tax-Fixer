"""
web_bridge.py - the JS <-> Python interface for the browser build.

Runs inside Pyodide (Python compiled to WebAssembly, executing in-browser).
Every function here takes and returns plain strings (JSON, base64, or plain
text) so the JS side never needs to know anything about Python objects -
just call these functions and parse/build simple values.

Reuses fit_engine.py and xml_engine.py completely unchanged (aside from the
parse_string() addition to xml_engine.py, which the desktop app also uses
via parse_file() - same code path, just entered differently). No engine
logic is duplicated here - this is purely plumbing: base64/JSON in, base64/
JSON out, and a handle registry so multi-step JS calls (load, then edit,
then save) can refer back to the same in-memory activity.
"""

import base64
import datetime
import json
import os
import uuid

import fit_engine as fe
import xml_engine as xe

# ---------------------------------------------------------------- registry
# Loaded/working activities live here, keyed by an opaque handle string.
# Nothing here is written to disk - it's browser tab memory only.
_registry = {}


def _new_handle(fmt, ctx):
    h = uuid.uuid4().hex[:12]
    _registry[h] = (fmt, ctx)
    return h


def _get(handle):
    if handle not in _registry:
        raise KeyError(f"Unknown or expired handle: {handle}")
    return _registry[handle]


# ------------------------------------------------------------- duration fmt

def _format_duration(seconds):
    if seconds is None:
        return ""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _parse_duration(text):
    parts = text.strip().split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    elif len(parts) == 1:
        return float(parts[0])
    raise ValueError(f"Can't parse duration: {text!r}")


# --------------------------------------------------------- summary/profile

def _build_summary_dict(fmt, ctx, engine):
    summary = engine.get_summary(ctx)
    profile = engine.extract_profile(ctx)

    alts = [a for a in profile['altitude_m'] if a is not None]
    hrs = [h for h in profile['heart_rate'] if h is not None]
    cads = [c for c in profile['cadence'] if c is not None]
    pwrs = [p for p in profile['power'] if p is not None]
    elev_gain = sum(max(0, b - a) for a, b in zip(alts, alts[1:])) if len(alts) > 1 else None

    start_dt = None
    if summary['start_raw'] is not None:
        start_dt = engine.raw_to_local_dt(summary['start_raw'], summary['tz_offset_s'])

    return {
        'format': fmt,
        'distance_km': round((summary['total_distance_m'] or 0) / 1000.0, 3),
        'start_time': start_dt.strftime('%Y-%m-%d %H:%M:%S') if start_dt else None,
        'duration': _format_duration(summary['duration_s']),
        'elevation_gain_m': round(elev_gain, 1) if elev_gain is not None else None,
        'avg_heart_rate': round(sum(hrs) / len(hrs), 1) if hrs else None,
        'avg_cadence': round(sum(cads) / len(cads), 1) if cads else None,
        'avg_power': round(sum(pwrs) / len(pwrs), 1) if pwrs else None,
        'calories': summary['calories'],
        'num_laps': summary['num_laps'],
        'num_records': summary['num_records'],
    }


def _downsample(items, max_points=800):
    n = len(items)
    if n <= max_points:
        return items
    step = n / max_points
    return [items[int(i * step)] for i in range(max_points)]


def _build_profile_json(engine, ctx):
    profile = engine.extract_profile(ctx)
    out = {}
    for key in ('distance_m', 'altitude_m', 'heart_rate', 'lat_deg', 'lon_deg'):
        out[key] = _downsample(profile[key])
    return out


# ---------------------------------------------------------------- load/save

def load(filename, content):
    """content: for .fit, a base64 string of the raw bytes.
    For .tcx/.gpx, the plain XML text.
    Returns JSON: {ok, handle, summary} or {ok:false, error}."""
    try:
        ext = os.path.splitext(filename)[1].lower()
        if ext == '.fit':
            data = bytearray(base64.b64decode(content))
            records, hsize, dsize = fe.parse(data)
            ctx = _FitCtx(data, records, hsize, dsize)
            fmt = 'fit'
            engine = _FitEngineShim
        elif ext in ('.tcx', '.gpx'):
            ctx = xe.parse_string(content)
            fmt = ctx.fmt
            engine = xe
        else:
            return json.dumps({'ok': False, 'error': f"Unsupported file type '{ext}'. Use .fit, .tcx, or .gpx."})

        handle = _new_handle(fmt, ctx)
        summary = _build_summary_dict(fmt, ctx, engine)
        profile = _build_profile_json(engine, ctx)
        return json.dumps({'ok': True, 'handle': handle, 'summary': summary, 'profile': profile})
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})


class _FitCtx:
    def __init__(self, data, records, hsize, dsize):
        self.data = data
        self.records = records
        self.hsize = hsize
        self.dsize = dsize


class _FitEngineShim:
    """Adapts fit_engine's (data, records) function signatures to the same
    ctx-based calling convention xml_engine uses, so the rest of this module
    can treat both formats identically."""

    @staticmethod
    def get_summary(ctx):
        return fe.get_summary(ctx.data, ctx.records)

    @staticmethod
    def extract_profile(ctx):
        return fe.extract_route_and_profile(ctx.data, ctx.records)

    @staticmethod
    def raw_to_local_dt(raw, tz_offset_s):
        return fe.raw_to_local_dt(raw, tz_offset_s)

    @staticmethod
    def local_dt_to_raw(dt, tz_offset_s):
        return fe.local_dt_to_raw(dt, tz_offset_s)

    @staticmethod
    def clone_context(ctx):
        return _FitCtx(bytearray(ctx.data), ctx.records, ctx.hsize, ctx.dsize)

    @staticmethod
    def apply_distance_scale(ctx, ratio):
        return fe.apply_distance_scale(ctx.data, ctx.records, ratio)

    @staticmethod
    def apply_time_shift(ctx, shift_seconds):
        return fe.apply_time_shift(ctx.data, ctx.records, shift_seconds)

    @staticmethod
    def apply_altitude_shift(ctx, delta_m):
        return fe.apply_altitude_shift(ctx.data, ctx.records, delta_m)

    @staticmethod
    def apply_heart_rate_scale(ctx, ratio):
        return fe.apply_heart_rate_scale(ctx.data, ctx.records, ratio)

    @staticmethod
    def apply_cadence_scale(ctx, ratio):
        return fe.apply_cadence_scale(ctx.data, ctx.records, ratio)

    @staticmethod
    def apply_power_scale(ctx, ratio):
        return fe.apply_power_scale(ctx.data, ctx.records, ratio)

    @staticmethod
    def apply_calories_scale(ctx, ratio):
        return fe.apply_calories_scale(ctx.data, ctx.records, ratio)

    @staticmethod
    def apply_duration_scale(ctx, ratio, start_raw):
        return fe.apply_duration_scale(ctx.data, ctx.records, ratio, start_raw)

    @staticmethod
    def validate(ctx):
        fe.parse(ctx.data)
        return True

    @staticmethod
    def serialize(ctx):
        fe.rewrite_crc(ctx.data, ctx.hsize, ctx.dsize)
        return bytes(ctx.data)


def _engine_for(fmt):
    return _FitEngineShim if fmt == 'fit' else xe


def apply_edits(handle, edits_json):
    """edits_json: JSON object with any of the keys below (as strings,
    matching form input values - empty/missing means "no change", exactly
    like the desktop app's behavior):
      distance_km, start_time (YYYY-MM-DD HH:MM:SS), elevation_m,
      heart_rate, duration (HH:MM:SS/MM:SS/seconds), cadence, power, calories

    Returns JSON: {ok, working_handle, changes: [...], profile} or
    {ok:false, error}."""
    try:
        fmt, ctx = _get(handle)
        engine = _engine_for(fmt)
        summary = engine.get_summary(ctx)
        edits = json.loads(edits_json)
        working = engine.clone_context(ctx)
        changes = []

        distance_km = edits.get('distance_km', '').strip()
        if distance_km:
            new_km = float(distance_km)
            old_m = summary['total_distance_m']
            if old_m and old_m > 0 and abs(new_km * 1000 - old_m) > 0.001:
                ratio = (new_km * 1000.0) / old_m
                n = engine.apply_distance_scale(working, ratio)
                note = " (rescaled GPS track)" if fmt == 'gpx' else ""
                changes.append(f"distance: {old_m/1000:.2f} km -> {new_km:.2f} km ({n} fields){note}")

        start_time = edits.get('start_time', '').strip()
        if start_time:
            new_dt = datetime.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            if summary['start_raw'] is not None:
                new_raw = engine.local_dt_to_raw(new_dt, summary['tz_offset_s'])
                shift = new_raw - summary['start_raw']
                if shift != 0:
                    n = engine.apply_time_shift(working, shift)
                    changes.append(f"start time shifted {shift:+d}s ({n} fields)")

        elevation_m = edits.get('elevation_m', '').strip()
        if elevation_m:
            delta_m = float(elevation_m)
            if abs(delta_m) > 0.001:
                n = engine.apply_altitude_shift(working, delta_m)
                changes.append(f"elevation: shifted {delta_m:+.1f} m ({n} fields)")

        heart_rate = edits.get('heart_rate', '').strip()
        if heart_rate:
            target_hr = float(heart_rate)
            profile = engine.extract_profile(ctx)
            hrs = [h for h in profile['heart_rate'] if h is not None]
            current_avg = sum(hrs) / len(hrs) if hrs else None
            if current_avg and abs(target_hr - current_avg) > 0.5:
                ratio = target_hr / current_avg
                n = engine.apply_heart_rate_scale(working, ratio)
                changes.append(f"heart rate: avg {current_avg:.0f} -> {target_hr:.0f} bpm ({n} fields)")

        duration = edits.get('duration', '').strip()
        if duration:
            target_dur = _parse_duration(duration)
            old_dur = summary['duration_s']
            if old_dur and old_dur > 0 and abs(target_dur - old_dur) > 0.5:
                ratio = target_dur / old_dur
                n = engine.apply_duration_scale(working, ratio, summary['start_raw'])
                changes.append(
                    f"duration: {_format_duration(old_dur)} -> {_format_duration(target_dur)} ({n} fields)"
                )

        cadence = edits.get('cadence', '').strip()
        if cadence:
            target_cad = float(cadence)
            profile = engine.extract_profile(ctx)
            cads = [c for c in profile['cadence'] if c is not None]
            current_avg = sum(cads) / len(cads) if cads else None
            if current_avg and abs(target_cad - current_avg) > 0.5:
                ratio = target_cad / current_avg
                n = engine.apply_cadence_scale(working, ratio)
                changes.append(f"cadence: avg {current_avg:.0f} -> {target_cad:.0f} ({n} fields)")

        power = edits.get('power', '').strip()
        if power:
            target_pwr = float(power)
            profile = engine.extract_profile(ctx)
            pwrs = [p for p in profile['power'] if p is not None]
            current_avg = sum(pwrs) / len(pwrs) if pwrs else None
            if current_avg and abs(target_pwr - current_avg) > 0.5:
                ratio = target_pwr / current_avg
                n = engine.apply_power_scale(working, ratio)
                changes.append(f"power: avg {current_avg:.0f} -> {target_pwr:.0f} W ({n} fields)")

        calories = edits.get('calories', '').strip()
        if calories:
            target_cal = float(calories)
            current_cal = summary['calories']
            if current_cal and current_cal > 0 and abs(target_cal - current_cal) > 0.5:
                ratio = target_cal / current_cal
                n = engine.apply_calories_scale(working, ratio)
                changes.append(f"calories: {current_cal:.0f} -> {target_cal:.0f} kcal ({n} fields)")

        if not changes:
            return json.dumps({'ok': True, 'no_changes': True})

        engine.validate(working)

        working_handle = _new_handle(fmt, working)
        profile = _build_profile_json(engine, working)
        return json.dumps({'ok': True, 'working_handle': working_handle, 'changes': changes, 'profile': profile})
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})


_MIME = {'fit': 'application/octet-stream', 'tcx': 'application/xml', 'gpx': 'application/gpx+xml'}


def finalize(working_handle, orig_filename):
    """Serializes the working (edited) activity. Returns JSON:
    {ok, filename, content_b64, mime} or {ok:false, error}."""
    try:
        fmt, ctx = _get(working_handle)
        engine = _engine_for(fmt)
        data = engine.serialize(ctx)
        if isinstance(data, str):
            data = data.encode('utf-8')
        b64 = base64.b64encode(data).decode('ascii')

        base, ext = os.path.splitext(orig_filename)
        out_name = f"{base}_edited{ext}"
        return json.dumps({'ok': True, 'filename': out_name, 'content_b64': b64, 'mime': _MIME.get(fmt, 'application/octet-stream')})
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})


def release(handle):
    """Frees a handle's memory. Best-effort, never raises."""
    _registry.pop(handle, None)
    return json.dumps({'ok': True})
