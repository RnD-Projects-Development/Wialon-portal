"""
Fatigue & Work-Rest Compliance Engine (LafargeHolcim Configuration Replica)
==========================================================================
Infers work, rest, and fatigue states from vehicle telematics:
  - Driving: Ignition ON (io_239 == 1) and speed >= 5 km/h
  - Rest: Ignition OFF (io_239 == 0), or speed < 5 km/h
"""

import os
import json
import math
import time
from datetime import datetime

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compliance_config.json")

# Bumped whenever the calculation changes, so stored results made with an older one are recalculated.
# 2: stops the vehicle drove through are ignored (see false_stop_indices).
# 3: ...only when the vehicle kept at least half its speed across them, so short real stops are kept.
ENGINE_VERSION = 3

FALSE_STOP_MAX_SEC = 300      # only stops up to this long can be false; longer ones are always real
FALSE_STOP_MIN_KMH = 5.0      # ...and only if the vehicle still covered ground at least this fast
FALSE_STOP_MIN_RATIO = 0.5    # ...and at least this share of its speed either side (a real stop collapses it)
FALSE_STOP_MAX_KMH = 160.0    # faster than this is a GPS jump, not evidence of movement


def _km_between(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[3], a[4], b[3], b[4]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def false_stop_indices(points):
    """
    Readings inside stops the vehicle drove through. points: time-ordered (t, speed, ignition, lat, lon, ...).

    Many trackers in this fleet report the ignition dropping off for a second or two while the vehicle is
    driving, and speed 0 with it (recorded as ignition-change events). Taken at face value those readings
    turn driving time into stop time and hide Fatigue Driving. A run of non-driving readings is treated as
    false when it sits between two driving readings, lasts at most FALSE_STOP_MAX_SEC, and the vehicle
    covered ground across it at FALSE_STOP_MIN_KMH or more AND at least FALSE_STOP_MIN_RATIO of its speed
    on either side. Through a real stop the average collapses (the vehicle only covers its approach and
    departure), so real stops are kept, and a stop longer than the limit is never touched.
    """
    def driving(p):
        return (p[2] == 1) and (p[1] or 0) >= 5.0

    ignored, before, i, n = set(), None, 0, len(points)
    while i < n:
        if driving(points[i]):
            before, i = i, i + 1
            continue
        j = i
        while j < n and not driving(points[j]):
            j += 1
        if before is not None and j < n:
            a, b = points[before], points[j]
            span = b[0] - a[0]
            if 0 < span <= FALSE_STOP_MAX_SEC and None not in (a[3], a[4], b[3], b[4]):
                kmh = _km_between(a, b) / (span / 3600.0)
                around = ((a[1] or 0) + (b[1] or 0)) / 2.0
                if max(FALSE_STOP_MIN_KMH, FALSE_STOP_MIN_RATIO * around) <= kmh <= FALSE_STOP_MAX_KMH:
                    ignored.update(range(i, j))
        i = j
    return ignored


# Default exact matching LafargeHolcim settings screen:
DEFAULT_CONFIG = {
    "max_continuous_drive_day": "02:30",
    "max_continuous_drive_night": "02:00",
    "min_break_day": "00:15",
    "min_break_night": "00:15",
    "min_reset_stop": "00:05",
    "max_drive_24h": "10:30",
    "min_rest_24h": "08:00",
    "max_onduty_24h": "12:00",
    "harsh_braking_penalty": 1,
    "harsh_accel_penalty": 1,
    "overspeed_penalty": 1,
    "hos_penalty": 1
}

def parse_time_str_to_minutes(s):
    try:
        parts = s.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return 150

def is_valid_hhmm(s):
    """True for 'HH:MM' strings such as '02:30' or '00:05'."""
    parts = str(s).split(":")
    return len(parts) == 2 and all(p.isdigit() for p in parts) and int(parts[1]) < 60

def format_seconds_to_hms(seconds):
    """Format duration seconds into HH:MM:SS format."""
    total_sec = int(max(0, seconds))
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def format_seconds_to_hm(seconds):
    """Format duration seconds into HH:MM format."""
    total_sec = int(max(0, seconds))
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    return f"{h:02d}:{m:02d}"


def get_compliance_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_compliance_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def analyze_telemetry_stream(messages, vehicle_name="Unknown", driver_name="Unassigned"):
    """
    Fatigue Driving (continuous driving) rule, with every limit taken from the compliance configuration:

    - Driving: ignition ON and speed >= 5 km/h. Rest: ignition OFF, or speed < 5 km/h.
    - The continuous-driving timer counts driving time. A stop lasting at least the reset stop
      (min_reset_stop, default 00:05) sets it back to zero; shorter stops do not.
    - When the timer reaches the limit (max_continuous_drive_day, default 02:30; max_continuous_drive_night,
      default 02:00, from 22:00 to 06:00) the driver must take a break of at least min_break_day / min_break_night
      (default 00:15). Driving on without that break is a violation, recorded at the moment the limit was reached.
    - Movement lasting less than 1 minute (GPS speed noise while parked, pulling over) does not end a stop.
    - A tracker that sends nothing for at least the reset stop is treated as stopped for that time.
    - Stops the vehicle drove through (tracker ignition dropouts, see false_stop_indices) are ignored: their
      time counts as driving, and they appear in the timeline marked IGNORED.
    """
    cfg = get_compliance_config()
    max_continuous_day_sec = parse_time_str_to_minutes(cfg.get("max_continuous_drive_day", "02:30")) * 60
    max_continuous_night_sec = parse_time_str_to_minutes(cfg.get("max_continuous_drive_night", "02:00")) * 60
    min_break_day_sec = parse_time_str_to_minutes(cfg.get("min_break_day", "00:15")) * 60
    min_break_night_sec = parse_time_str_to_minutes(cfg.get("min_break_night", "00:15")) * 60
    reset_stop_sec = parse_time_str_to_minutes(cfg.get("min_reset_stop", "00:05")) * 60
    move_tolerance_sec = 60

    if not messages:
        return {"timeline": [], "violations": [], "summary": {}}

    sorted_msgs = sorted(messages, key=lambda m: m.get("t", 0))
    ignored = false_stop_indices([(m.get("t", 0), (m.get("pos") or {}).get("s") or 0, (m.get("p") or {}).get("io_239"),
                                   (m.get("pos") or {}).get("y"), (m.get("pos") or {}).get("x")) for m in sorted_msgs])

    timeline = []
    violations = []

    current_drive_sec = 0     # continuous driving timer
    current_stop_sec = 0      # length of the current stop
    moving_since = None       # when the current movement started (None while stopped)
    limit_reached = None      # set when the timer hits the limit; waiting to see if an adequate break follows
    last_t = None

    def apply_stop_rules():
        nonlocal current_drive_sec, limit_reached
        if limit_reached is None and current_stop_sec >= reset_stop_sec:
            # a stop of at least the reset length ends the continuous-driving run
            current_drive_sec = 0
        if limit_reached is not None and current_stop_sec >= limit_reached["break_sec"]:
            # adequate break taken after reaching the limit: no violation
            limit_reached = None
            current_drive_sec = 0

    for index, msg in enumerate(sorted_msgs):
        t = msg.get("t", 0)
        pos = msg.get("pos", {})
        speed = pos.get("s", 0.0)
        p = msg.get("p", {})

        if index in ignored:
            # the vehicle kept moving through this reading: skip it, so the gap counts with the next one
            timeline.append({
                "time": datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S"),
                "speed": round(speed or 0, 1),
                "ignition": "ON" if int(p.get("io_239") or 0) == 1 else "OFF",
                "dwell_rest": format_seconds_to_hms(current_drive_sec),
                "state": "IGNORED (false stop)",
                "location": f"{pos.get('y', 0) or 0:.4f}, {pos.get('x', 0) or 0:.4f}",
                "status": "Ignored: the vehicle kept moving through this reading",
            })
            continue

        # Strict physical ignition sensor flag (io_239)
        ignition = int(p.get("io_239") or 0)
        engine_on = (ignition == 1)

        dt = (t - last_t) if last_t else 0
        last_t = t

        msg_dt = datetime.fromtimestamp(t) if t else datetime.now()
        is_night = (msg_dt.hour >= 22 or msg_dt.hour < 6)
        max_cont_limit_sec = max_continuous_night_sec if is_night else max_continuous_day_sec
        min_break_limit_sec = min_break_night_sec if is_night else min_break_day_sec
        limit_label = cfg.get("max_continuous_drive_night" if is_night else "max_continuous_drive_day")
        break_label = cfg.get("min_break_night" if is_night else "min_break_day")
        location = f"{pos.get('y', 0):.4f}, {pos.get('x', 0):.4f}"

        # Tracker silent for at least the reset stop (it sleeps while parked): count the gap as stopped time
        if dt >= reset_stop_sec:
            moving_since = None
            current_stop_sec += dt
            apply_stop_rules()
            dt = 0

        driving = engine_on and speed >= 5.0

        if driving:
            if moving_since is None:
                moving_since = t
            current_drive_sec += dt
            status_flag = "Normal"

            if t - moving_since >= move_tolerance_sec:
                stop_taken_sec = current_stop_sec
                # sustained movement ends the stop
                current_stop_sec = 0

                # Drove on after reaching the limit without an adequate break -> violation at the limit mark
                if limit_reached is not None and t - max(moving_since, limit_reached["t"]) >= move_tolerance_sec:
                    mark = limit_reached
                    stop_text = (f"stopped only {round(stop_taken_sec / 60, 1)} min" if stop_taken_sec > 0
                                 else "kept driving without stopping")
                    violations.append({
                        "id": f"VIO-CONT-{vehicle_name}-{mark['t']}",
                        "time": mark["time"],
                        "violation_timestamp": mark["time"],
                        "vehicle": vehicle_name,
                        "driver": driver_name,
                        "type": "CONTINUOUS_DRIVE_EXCEEDED",
                        "violation_type": "CONTINUOUS_DRIVE_EXCEEDED",
                        "severity": "CRITICAL",
                        "continuous_minutes": round(mark["drive_sec"] / 60, 1),
                        "continuous_drive_minutes": round(mark["drive_sec"] / 60, 1),
                        "rest_taken_minutes": round(stop_taken_sec / 60, 1),
                        "period": mark["period"],
                        "speed": mark["speed"],
                        "speed_kmh": mark["speed"],
                        "location": mark["location"],
                        "notes": (f"Drove {mark['limit_label']} ({mark['period'].lower()} limit) without a "
                                  f"{mark['reset_label']} stop, then {stop_text} instead of a "
                                  f"{mark['break_label']} break (drove on at {msg_dt.strftime('%H:%M')})")
                    })
                    timeline[mark["timeline_index"]]["status"] = "FATIGUE BREACH (limit reached)"
                    # the next run is timed from the limit mark
                    current_drive_sec = max(0, current_drive_sec - mark["drive_sec"])
                    limit_reached = None

            if limit_reached is None and current_drive_sec >= max_cont_limit_sec:
                limit_reached = {
                    "t": t,
                    "time": msg_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "drive_sec": current_drive_sec,
                    "speed": round(speed, 1),
                    "location": location,
                    "break_sec": min_break_limit_sec,
                    "period": "Night" if is_night else "Day",   # which limit applied at the mark
                    "limit_label": limit_label,
                    "break_label": break_label,
                    "reset_label": cfg.get("min_reset_stop", "00:05"),
                    "timeline_index": len(timeline),
                }

            timeline.append({
                "time": msg_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "speed": round(speed, 1),
                "ignition": "ON",
                "dwell_rest": format_seconds_to_hms(current_drive_sec),
                "state": "WORK (DRIVING)",
                "location": location,
                "status": status_flag
            })

        else:
            # === STOPPED: ignition OFF, or speed under 5 km/h ===
            moving_since = None
            current_stop_sec += dt
            apply_stop_rules()

            timeline.append({
                "time": msg_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "speed": round(speed, 1),
                "ignition": "ON" if engine_on else "OFF",
                "dwell_rest": format_seconds_to_hms(current_stop_sec),
                "state": "REST (ENGINE ON)" if engine_on else "PARKED (ENGINE OFF)",
                "location": location,
                "status": "Normal"
            })

    # Reverse timeline so that the most recent packets appear on top
    timeline.reverse()

    return {
        "timeline": timeline,
        "violations": violations,
        "summary": {
            "vehicle": vehicle_name,
            "driver": driver_name,
            "total_points": len(timeline),
            "violations_count": len(violations)
        }
    }


def get_vehicle_daily_compliance(vehicle_id, vehicle_name=None, vehicle_data=None, messages=None, window_hours=24.0, from_ts=None, to_ts=None, known_violations=None):
    """
    Computes actual metrics and hours for each vehicle across compliance categories:
    1. Max Continuous Driving (flagged if speed > 5 & ignition ON > configured threshold, default 2.5h)
    2. Max Driving Hours in 24 Hour Cycle
    3. Minimum continuous rest period in 24 hour Cycle
    4. Max on Duty Hours in 24 Hour Cycle
    """
    cfg = get_compliance_config()
    max_continuous_day_sec = parse_time_str_to_minutes(cfg.get("max_continuous_drive_day", "02:30")) * 60
    min_break_day_sec = parse_time_str_to_minutes(cfg.get("min_break_day", "00:15")) * 60
    max_drive_24h_sec = parse_time_str_to_minutes(cfg.get("max_drive_24h", "10:30")) * 60
    min_rest_24h_sec = parse_time_str_to_minutes(cfg.get("min_rest_24h", "08:00")) * 60
    max_onduty_24h_sec = parse_time_str_to_minutes(cfg.get("max_onduty_24h", "12:00")) * 60

    # 1. When raw message packets are provided
    if messages and len(messages) > 0:
        sorted_msgs = sorted(messages, key=lambda m: m.get("t", 0))
        max_cont_drive_sec = 0
        max_rest_engine_on_sec = 0
        total_drive_sec = 0
        total_onduty_sec = 0
        max_continuous_rest_sec = 0

        current_drive_sec = 0
        current_rest_engine_on_sec = 0
        current_break_sec = 0
        moving_since = None
        stationary_sec = 0
        last_t = None

        for msg in sorted_msgs:
            t = msg.get("t", 0)
            pos = msg.get("pos", {})
            speed = pos.get("s", 0.0)
            p = msg.get("p", {})
            ignition = int(p.get("io_239") or 0)
            engine_on = (ignition == 1)

            dt = (t - last_t) if last_t else 0
            last_t = t

            # Tracker silent for a full break (it sleeps while parked): count the gap as a break, not as driving
            if dt >= min(min_break_day_sec, 3600):
                current_drive_sec = 0
                current_break_sec = 0
                moving_since = None
                dt = 0

            if engine_on:
                total_onduty_sec += dt
                if speed > 5.0:
                    # Driving state
                    if moving_since is None:
                        moving_since = t
                    total_drive_sec += dt
                    current_drive_sec += dt
                    if current_drive_sec > max_cont_drive_sec:
                        max_cont_drive_sec = current_drive_sec
                    current_rest_engine_on_sec = 0
                    # A one-off GPS speed reading during a stop does not end the break; only sustained movement does
                    if t - moving_since >= 60:
                        current_break_sec = 0
                    stationary_sec = 0
                else:
                    # Resting state with engine on
                    moving_since = None
                    current_rest_engine_on_sec += dt
                    current_break_sec += dt
                    if current_rest_engine_on_sec > max_rest_engine_on_sec:
                        max_rest_engine_on_sec = current_rest_engine_on_sec
                    if current_break_sec >= min_break_day_sec:
                        current_drive_sec = 0
            else:
                # Short engine-off stops do not reset driving time; only a full minimum break does
                moving_since = None
                current_break_sec += dt
                if current_break_sec >= min_break_day_sec:
                    current_drive_sec = 0
                current_rest_engine_on_sec = 0
                stationary_sec += dt
                if stationary_sec > max_continuous_rest_sec:
                    max_continuous_rest_sec = stationary_sec

        b_cont = max_cont_drive_sec >= max_continuous_day_sec
        b_break = False

        breach_cnt = 1 if b_cont else 0

        return {
            "max_continuous_driving": format_seconds_to_hm(max_cont_drive_sec),
            "max_continuous_driving_breach": b_cont,
            "min_break": format_seconds_to_hm(max_rest_engine_on_sec),
            "min_break_breach": False,
            "max_driving_24h": format_seconds_to_hm(total_drive_sec),
            "max_driving_24h_breach": False,
            "min_rest_24h": format_seconds_to_hm(max_continuous_rest_sec),
            "min_rest_24h_breach": False,
            "max_onduty_24h": format_seconds_to_hm(total_onduty_sec),
            "max_onduty_24h_breach": False,
            "is_compliant": (breach_cnt == 0),
            "breach_count": breach_cnt
        }

    # 2. When known_violations are provided from DB / prior evaluation
    if known_violations:
        b_cont = any((v.get("violation_type") or v.get("type")) == "CONTINUOUS_DRIVE_EXCEEDED" for v in known_violations)
        
        cont_mins = 0.0
        for v in known_violations:
            vt = v.get("violation_type") or v.get("type")
            if vt == "CONTINUOUS_DRIVE_EXCEEDED":
                cont_mins = max(cont_mins, float(v.get("continuous_drive_minutes") or v.get("continuous_minutes") or 150.0))

        breach_cnt = 1 if b_cont else 0
        return {
            "max_continuous_driving": format_seconds_to_hm(int(cont_mins * 60)),
            "max_continuous_driving_breach": b_cont,
            "min_break": "00:00",
            "min_break_breach": False,
            "max_driving_24h": "00:00",
            "max_driving_24h_breach": False,
            "min_rest_24h": "00:00",
            "min_rest_24h_breach": False,
            "max_onduty_24h": "00:00",
            "max_onduty_24h_breach": False,
            "is_compliant": (breach_cnt == 0),
            "breach_count": breach_cnt
        }

    # 3. Default when neither messages nor known violations are present
    return {
        "max_continuous_driving": "00:00",
        "max_continuous_driving_breach": False,
        "min_break": "00:00",
        "min_break_breach": False,
        "max_driving_24h": "00:00",
        "max_driving_24h_breach": False,
        "min_rest_24h": "00:00",
        "min_rest_24h_breach": False,
        "max_onduty_24h": "00:00",
        "max_onduty_24h_breach": False,
        "is_compliant": True,
        "breach_count": 0
    }


def evaluate_vehicle_violations(vehicle_id, vehicle_name, vehicle_data=None, messages=None, timestamp_str=None, window_hours=24.0, from_ts=None, to_ts=None, known_violations=None):
    """
    Evaluates a vehicle against the active compliance configuration.
    Returns a list containing ONLY genuine continuous drive violations:
    1. CONTINUOUS_DRIVE_EXCEEDED (speed > 5 km/h & ignition ON >= 2.5h)
    """
    if known_violations:
        return [v for v in known_violations if (v.get("violation_type") or v.get("type")) == "CONTINUOUS_DRIVE_EXCEEDED"]

    comp = get_vehicle_daily_compliance(
        vehicle_id, vehicle_name, vehicle_data=vehicle_data,
        messages=messages, window_hours=window_hours,
        from_ts=from_ts, to_ts=to_ts, known_violations=known_violations
    )
    
    if comp.get("is_compliant", True) and comp.get("breach_count", 0) == 0:
        return []

    cfg = get_compliance_config()
    hos_pts = int(cfg.get("hos_penalty", 1))
    driver = (vehicle_data or {}).get("driver", "Unassigned")
    speed = float((vehicle_data or {}).get("speed", 0.0))
    mileage = float((vehicle_data or {}).get("mileage", 0.0))
    location = (vehicle_data or {}).get("location", "Route")
    t_str = timestamp_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    violations = []
    
    if comp.get("max_continuous_driving_breach"):
        driving_spd = speed if speed > 5.0 else 48.0
        violations.append({
            "id": f"VIO-CONT-{vehicle_name}-{int(time.time()*1000)}",
            "vehicle_name": vehicle_name,
            "vehicle": vehicle_name,
            "driver_name": driver,
            "driver": driver,
            "violation_type": "CONTINUOUS_DRIVE_EXCEEDED",
            "type": "CONTINUOUS_DRIVE_EXCEEDED",
            "severity": "CRITICAL",
            "penalty_points": hos_pts,
            "continuous_drive_minutes": round(parse_time_str_to_minutes(comp.get("max_continuous_driving", "02:30")), 1),
            "rest_taken_minutes": 0.0,
            "speed_kmh": driving_spd,
            "ignition": 1,
            "engine_on": True,
            "mileage": mileage,
            "location_name": location,
            "location": location,
            "violation_timestamp": t_str,
            "notes": f"Continuous driving exceeded limit ({cfg.get('max_continuous_drive_day', '02:30')})"
        })

    return violations

