"""
Work-Rest Compliance Portal (Live Wialon Telemetry & Vehicle Drill-Down)
========================================================================
Features:
  - Live Wialon Remote API queries for 309 vehicles (with 6 Core Fields)
  - Full-page paginated vehicle overview table (Mileage, Speed, Ignition, Location, State)
  - Interactive single-vehicle drill-down with calendar date/time range selector
  - Exact LafargeHolcim Configurations section (Day/Night limits & penalty points)
  - Backend-only Supabase integration via .env (Hidden from UI)
  - Real-time Web Audio alarm chime & Desktop notifications
"""

import os
import sys
import gzip
import hashlib
import json
import time
import re
import urllib.request
import urllib.parse
import ssl
import threading
from collections import Counter
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, render_template_string, Response

# Force UTF-8 stdout
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from supabase_client import db
import notifier
import fatigue_engine
import fleet_violations
import reports
import regions
import review_modal
import theme
import dashboard_data
import dashboard_export
import dashboard_page
import violation_store

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "wialon_output")

# Read from .env only (supabase_client copies it into the environment on import). Never write a token
# here: code is committed, and a token in a commit stays in the history.
WIALON_TOKEN = os.environ.get("WIALON_TOKEN", "")
if not WIALON_TOKEN:
    print("[Wialon] WIALON_TOKEN is not set in .env: live Wialon data is unavailable.")
WIALON_BASE_URL = os.environ.get("WIALON_BASE_URL", "https://hst-api.wialon.eu")

SSL_CTX = ssl.create_default_context()
CURRENT_SESSION = {"sid": None, "gis_sid": None, "gis_host": "https://geocode-maps.wialon.com", "uid": 601786058, "last_login": 0}
GEOCODE_CACHE = {}

class InspectionReportCache:
    """Thread-safe in-memory LRU/TTL cache for vehicle inspection reports."""
    def __init__(self, max_size=500):
        self._cache = {}
        self._lock = threading.Lock()
        self._max_size = max_size

    def get(self, key):
        with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                if time.time() < entry["expires_at"]:
                    return entry["data"]
                else:
                    del self._cache[key]
            return None

    def set(self, key, data, ttl_seconds=120):
        with self._lock:
            now = time.time()
            if len(self._cache) >= self._max_size:
                expired = [k for k, v in self._cache.items() if now >= v["expires_at"]]
                for k in expired:
                    del self._cache[k]
                if len(self._cache) >= self._max_size:
                    oldest_key = next(iter(self._cache))
                    del self._cache[oldest_key]
            self._cache[key] = {
                "data": data,
                "expires_at": now + ttl_seconds
            }

    def clear(self):
        with self._lock:
            self._cache.clear()

INSPECTION_CACHE = InspectionReportCache(max_size=500)

# ---------------------------------------------------------------------------
# Wialon Live API Helpers & GIS Geocoding
# ---------------------------------------------------------------------------
def wialon_login():
    """Obtain or reuse a live Wialon session ID and GIS session."""
    now = time.time()
    if CURRENT_SESSION["sid"] and (now - CURRENT_SESSION["last_login"] < 1200):
        return CURRENT_SESSION["sid"]

    url = f"{WIALON_BASE_URL}/wialon/ajax.html?svc=token/login&params=" + urllib.parse.quote(json.dumps({"token": WIALON_TOKEN}))
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "eid" in data:
                CURRENT_SESSION["sid"] = data["eid"]
                CURRENT_SESSION["gis_sid"] = data.get("gis_sid")
                CURRENT_SESSION["gis_host"] = data.get("gis_geocode", "https://geocode-maps.wialon.com")
                CURRENT_SESSION["uid"] = data.get("user", {}).get("id", 601786058)
                CURRENT_SESSION["last_login"] = now
                print(f"[Wialon] Logged in live! SID: {data['eid'][:10]}... GIS SID: {str(data.get('gis_sid'))[:10]}...")
                return data["eid"]
    except Exception as e:
        print(f"[Wialon] Login error: {e}")
    return None

URDU_MAP = {
    'قومی شاہراہ': 'National Highway N-5',
    'موٹروے': 'Motorway M-2',
    'انڈس ہائی وے': 'Indus Highway N-55',
    'ملتان روڈ': 'Multan Road',
    'لاہور': 'Lahore',
    'پنجاب': 'Punjab',
    'سندھ': 'Sindh',
    'خیبر پختونخوا': 'KPK',
    'بلوچستان': 'Balochistan',
    'اسلام آباد': 'Islamabad',
    'راولپنڈی': 'Rawalpindi',
    'فیصل آباد': 'Faisalabad',
    'گوجرانوالہ': 'Gujranwala',
    'سیالکوٹ': 'Sialkot',
    'سرگودھا': 'Sargodha',
    'ساہیوال': 'Sahiwal',
    'بہاولپور': 'Bahawalpur',
    'رحیم یار خان': 'Rahim Yar Khan',
    'حیدرآباد': 'Hyderabad',
    'کراچی': 'Karachi',
    'پشاور': 'Peshawar',
    'سکھر': 'Sukkur',
    'کوئٹہ': 'Quetta'
}

def resolve_pakistan_corridor(lat, lon):
    if 31.0 <= lat <= 32.0 and 73.8 <= lon <= 74.6:
        return 'Lahore-Multan Transport Corridor, Punjab'
    if 31.8 <= lat <= 33.8 and 72.5 <= lon <= 73.5:
        return 'M-2 Motorway / Pindi Bhattian, Punjab'
    if 33.4 <= lat <= 33.8 and 72.8 <= lon <= 73.2:
        return 'GT Road / Rawalpindi-Islamabad Hub'
    if 31.3 <= lat <= 31.6 and 72.9 <= lon <= 73.3:
        return 'Faisalabad Industrial Logistics Route, Punjab'
    if 24.7 <= lat <= 25.1 and 66.8 <= lon <= 67.4:
        return 'National Highway / Port Qasim, Karachi'
    if 29.8 <= lat <= 30.4 and 71.3 <= lon <= 71.7:
        return 'Multan Bypass / N-5 Highway, Punjab'
    if 27.5 <= lat <= 28.0 and 68.6 <= lon <= 69.0:
        return 'Sukkur-Rohri N-5 Highway, Sindh'
    if 32.0 <= lat <= 32.6 and 74.0 <= lon <= 74.6:
        return 'Gujranwala-Sialkot Industrial Zone, Punjab'
    if 32.8 <= lat <= 33.2 and 72.7 <= lon <= 73.2:
        return 'Salt Range / Chakwal Route, Punjab'
    return 'National Highway Transport Route, Pakistan'

def clean_landmark_string(addr, lat, lon):
    if not addr or not isinstance(addr, str):
        return resolve_pakistan_corridor(lat, lon)
    clean = re.sub(r'^[0-9A-Z]{6,20}\s*', '', addr).strip()
    for u, e in URDU_MAP.items():
        clean = clean.replace(u, e)
    clean = clean.strip(', ')
    if not clean or re.match(r'^-?\d+\.\d+,\s*-?\d+\.\d+$', clean) or clean == "No GPS Fix":
        return resolve_pakistan_corridor(lat, lon)
    return clean

def batch_geocode_coords(coords_list):
    """
    Reverse geocode coordinates into actual human-readable landmarks, roads, and cities.
    Never leaves raw coordinates in the user interface.
    """
    if not coords_list:
        return []

    results = [None] * len(coords_list)
    unique_missing = {}

    for i, c in enumerate(coords_list):
        lat = float(c.get("lat") or 0.0)
        lon = float(c.get("lon") or 0.0)
        if abs(lat) < 0.001 and abs(lon) < 0.001:
            results[i] = resolve_pakistan_corridor(lat, lon)
            continue
        key = (round(lat, 3), round(lon, 3))
        if key in GEOCODE_CACHE:
            results[i] = GEOCODE_CACHE[key]
        else:
            if key not in unique_missing:
                unique_missing[key] = {"lat": lat, "lon": lon}

    if unique_missing:
        sid = wialon_login()
        gis_sid = CURRENT_SESSION.get("gis_sid")
        uid = CURRENT_SESSION.get("uid", 601786058)
        gis_host = CURRENT_SESSION.get("gis_host", "https://geocode-maps.wialon.com")

        missing_items = list(unique_missing.items())[:50]
        CHUNK_SIZE = 25

        for chunk_idx in range(0, len(missing_items), CHUNK_SIZE):
            chunk = missing_items[chunk_idx:chunk_idx + CHUNK_SIZE]
            chunk_coords = [item[1] for item in chunk]
            chunk_keys = [item[0] for item in chunk]

            if not gis_sid:
                for k, c in zip(chunk_keys, chunk_coords):
                    GEOCODE_CACHE[k] = clean_landmark_string(None, c['lat'], c['lon'])
                continue

            try:
                coords_json = urllib.parse.quote(json.dumps(chunk_coords))
                url = f"{gis_host}/gis_geocode?coords={coords_json}&flags=1255211&uid={uid}&gis_sid={gis_sid}"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=4, context=SSL_CTX) as resp:
                    raw_data = json.loads(resp.read().decode("utf-8"))

                if isinstance(raw_data, list):
                    for k, raw_addr, c in zip(chunk_keys, raw_data, chunk_coords):
                        GEOCODE_CACHE[k] = clean_landmark_string(raw_addr, c['lat'], c['lon'])
                else:
                    for k, c in zip(chunk_keys, chunk_coords):
                        GEOCODE_CACHE[k] = clean_landmark_string(None, c['lat'], c['lon'])
            except Exception as e:
                for k, c in zip(chunk_keys, chunk_coords):
                    GEOCODE_CACHE[k] = clean_landmark_string(None, c['lat'], c['lon'])

    # Fill in results for all coordinates in the list
    for i, c in enumerate(coords_list):
        if results[i] is None:
            lat = float(c.get("lat") or 0.0)
            lon = float(c.get("lon") or 0.0)
            key = (round(lat, 3), round(lon, 3))
            results[i] = GEOCODE_CACHE.get(key, clean_landmark_string(None, lat, lon))

    return results

def wialon_call(svc, params, retry=True):
    """Execute a Wialon API call with automatic session re-login on expired SID (error 1 or 4)."""
    sid = wialon_login()
    if not sid:
        return None

    url = f"{WIALON_BASE_URL}/wialon/ajax.html?svc={svc}&params=" + urllib.parse.quote(json.dumps(params)) + f"&sid={sid}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # Detect expired or invalidated session
        if isinstance(data, dict) and data.get("error") in [1, 4]:
            if retry:
                print(f"[Wialon] Session expired (error {data.get('error')}), re-authenticating...")
                CURRENT_SESSION["sid"] = None
                CURRENT_SESSION["last_login"] = 0
                return wialon_call(svc, params, retry=False)
        return data
    except Exception as e:
        print(f"[Wialon] API error calling {svc}: {e}")
        return None

DRIVER_CACHE = {"data": {}, "last_fetched": 0}

def fetch_live_drivers_from_wialon():
    """Fetch all 280 drivers and their assigned unit IDs from Wialon live API."""
    now = time.time()
    if DRIVER_CACHE["data"] and (now - DRIVER_CACHE["last_fetched"] < 600):
        return DRIVER_CACHE["data"]

    params = {
        "spec": {
            "itemsType": "avl_resource",
            "propName": "sys_name",
            "propValueMask": "*",
            "sortType": "sys_name"
        },
        "force": 1,
        "flags": 257,  # base + drivers
        "from": 0,
        "to": 0
    }
    data = wialon_call("core/search_items", params)
    mapping = {}
    if isinstance(data, dict) and "items" in data and len(data["items"]) > 0:
        drvrs = data["items"][0].get("drvrs", {})
        for did, d in drvrs.items():
            bu = d.get("bu")
            name = d.get("n", "").replace("\ufeff", "").strip()
            if bu and name:
                mapping[int(bu)] = name
        DRIVER_CACHE["data"] = mapping
        DRIVER_CACHE["last_fetched"] = now
        print(f"[Wialon] Successfully synced {len(mapping)} live driver-to-vehicle assignments from API.")
        return mapping
    return DRIVER_CACHE["data"] or {}

def fetch_live_vehicles_from_wialon():
    """Fetch all vehicles live from Wialon API with the 6 core fields and driver names."""
    driver_map = fetch_live_drivers_from_wialon()

    params = {
        "spec": {
            "itemsType": "avl_unit",
            "propName": "sys_name",
            "propValueMask": "*",
            "sortType": "sys_name"
        },
        "force": 1,
        "flags": 285995,  # base + pos + lmsg + sensors + counters
        "from": 0,
        "to": 0
    }
    data = wialon_call("core/search_items", params)
    if isinstance(data, dict) and "items" in data and len(data["items"]) > 0:
        return parse_raw_wialon_items(data["items"], driver_map=driver_map)

    print("[Wialon] Live search returned no items or error. Using cached snapshot.")
    return load_cached_vehicles(driver_map=driver_map)

def load_cached_vehicles(driver_map=None):
    cache_path = os.path.join(OUTPUT_DIR, "02_search_all_units_full_data.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return parse_raw_wialon_items(data.get("items", []), driver_map=driver_map)
    return []

def parse_raw_wialon_items(items, driver_map=None):
    if driver_map is None:
        driver_map = fetch_live_drivers_from_wialon()

    now_unix = int(time.time())
    parsed = []

    for item in items:
        if not isinstance(item, dict):
            continue
        uid = item.get("id")
        nm = item.get("nm", "Unknown")
        pos = item.get("pos") or {}
        lmsg = item.get("lmsg") or {}
        p = (lmsg.get("p") or {}) if isinstance(lmsg, dict) else {}

        # The Core Telemetry Fields:
        speed = float(pos.get("s") or 0.0)                               # 1. Speed (km/h)
        lat = float(pos.get("y") or 0.0)                                 # 2. Location (lat)
        lon = float(pos.get("x") or 0.0)                                 # 2. Location (lon)
        # 3. Mileage (Physical Odometer km from io_16 in meters, with cnm fallback)
        io_16 = p.get("io_16")
        cnm_val = item.get("cnm") or 0
        if io_16 is not None and float(io_16) > 0:
            raw_odo = float(io_16)
            mileage = round(raw_odo * 0.001, 1) if raw_odo > 10000 else round(raw_odo, 1)
        elif cnm_val and float(cnm_val) > 0:
            mileage = round(float(cnm_val) / 1000.0, 1)
        else:
            mileage = 0.0
        ignition = int(p.get("io_239") or 0)                             # 4. Physical Ignition Wire (io_239)
        engine_on = (ignition == 1)                                      # 5. Engine ON flag (strict physical ignition)
        t = pos.get("t") or lmsg.get("t") or now_unix                    # 6. Timestamp

        # State determination
        is_offline = (now_unix - t) > 1800 if t else True
        if is_offline:
            status = "OFFLINE"
        elif speed > 5.0 and engine_on:
            status = "DRIVING"
        elif engine_on and speed <= 5.0:
            status = "IDLING"
        else:
            status = "PARKED"

        # Driver assigned to this unit in Wialon (no guessed names: violations are attributed to this driver)
        driver = (driver_map.get(int(uid)) if (uid and driver_map) else None) or "Unassigned"

        parsed.append({
            "id": uid,
            "vehicle": nm,
            "driver": driver,
            "speed": round(speed, 1),
            "ignition": ignition,
            "engine_on": engine_on,
            "mileage": mileage,
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "location": f"{round(lat, 4)}, {round(lon, 4)}",
            "status": status,
            "last_heartbeat_unix": t or 0,
            "last_heartbeat": datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "N/A"
        })

    return parsed

# ---------------------------------------------------------------------------
# Fleet violations (every vehicle, refreshed in the background)
# ---------------------------------------------------------------------------
MONITOR = fleet_violations.FleetViolationMonitor(
    cache_path=os.path.join(OUTPUT_DIR, "fleet_telemetry_cache.json.gz"),
    driver_lookup=fetch_live_drivers_from_wialon,
)
VIOLATION_TYPE_KEYS = [t["key"] for t in fleet_violations.VIOLATION_TYPES]
ALERTS = notifier.FatigueAlerter(lambda: db.supabase_client)
REPORTS = reports.ReportService(
    cache_dir=os.path.join(OUTPUT_DIR, "report_cache"),
    driver_lookup=fetch_live_drivers_from_wialon,
    store=violation_store.ViolationStore(db.supabase_client),
    locate=lambda violations: with_locations(violations),
    notify=ALERTS.enqueue,
)


def fatigue_violations_by_key(keys):
    """{alert key: violation record} for queued alerts, read back out of the archive so the email
    describes exactly what the portal shows. Keys carry their own timestamp, so the days to read are
    known without touching the queue table again."""
    if not keys:
        return {}
    days = sorted({notifier.key_day(k) for k in keys})
    stored = REPORTS.store.load(days[0], days[-1], types=[notifier.ALERT_TYPE])
    return {notifier.alert_key(v): v for v in with_locations(stored)}


@app.before_request
def ensure_background_jobs_running():
    # Reports read the nightly archive, so the rolling fleet scan (MONITOR.start) is no longer run: it
    # re-downloaded 24 hours of telemetry for every vehicle every 5 minutes for a view the portal dropped.
    REPORTS.start_scheduler()
    ALERTS.start_worker(fatigue_violations_by_key)


def with_locations(violations):
    """Copies of violation records with a display location: Wialon's own address for notification
    violations, reverse geocoding for violations calculated from telemetry."""
    out = [dict(v) for v in violations]
    todo = [v for v in out if not v.get("location")]      # archived violations already carry one
    needs_geocode = [v for v in todo if not v.get("address")]
    if needs_geocode:
        geocoded = batch_geocode_coords([{"lat": v["lat"] or 0.0, "lon": v["lon"] or 0.0} for v in needs_geocode])
        for v, loc in zip(needs_geocode, geocoded):
            v["location"] = loc
    for v in todo:
        if v.get("address"):
            v["location"] = clean_landmark_string(v["address"], v["lat"] or 0.0, v["lon"] or 0.0)
    return out


def violation_types_payload():
    """Types in display order with a one-line explanation; Fatigue Driving uses the saved limits."""
    cfg = fatigue_engine.get_compliance_config()
    minutes = fatigue_engine.parse_time_str_to_minutes
    fatigue_text = (f"{cfg['max_continuous_drive_day']} h of driving ({cfg['max_continuous_drive_night']} h at night) "
                    f"with no {minutes(cfg['min_reset_stop'])}-min stop, then no {minutes(cfg['min_break_day'])}-min break")
    return [{"key": t["key"], "label": t["label"],
             "description": fatigue_text if t["key"] == "FATIGUE_DRIVING" else t["description"]}
            for t in fleet_violations.VIOLATION_TYPES]


def scan_status_payload(status):
    return {key: status.get(key) for key in
            ("state", "units_total", "units_loaded", "last_refresh", "window_from", "window_to", "messages", "error")}

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------
@app.route("/api/summary")
def api_summary():
    """Benign endpoint for legacy / cached browser pollers."""
    return jsonify({"status": "ok", "message": "Summary KPI cards retired in favor of single paginated vehicle table."})

def vehicle_matches_query(v, query):
    """
    Robust multi-criteria search matching:
    1. Substring search on vehicle plate and driver name (case-insensitive)
    2. Normalized alphanumeric search (ignores spaces, dashes, hyphens so 'CCJ018' matches 'CCJ-018')
    3. Multi-word token matching (e.g. 'Tayyab Arshad' matches 'Tayyab Ali Arshad')
    """
    if not query:
        return True

    q_raw = query.strip().lower()
    q_norm = re.sub(r'[^a-z0-9]', '', q_raw)

    v_name = (v.get("vehicle") or "").strip().lower()
    v_norm = re.sub(r'[^a-z0-9]', '', v_name)

    d_name = (v.get("driver") or "").strip().lower()
    d_norm = re.sub(r'[^a-z0-9]', '', d_name)

    # 1. Exact / Substring match on vehicle or driver
    if q_raw in v_name or q_raw in d_name:
        return True

    # 2. Normalized alphanumeric match (handles CCJ018, LET193386, etc.)
    if q_norm and (q_norm in v_norm or q_norm in d_norm):
        return True

    # 3. Multi-word token matching (e.g. 'Musa Khan' matches 'Muhammad Musa Khan')
    tokens = [t for t in q_raw.split() if t]
    if len(tokens) > 1:
        full_haystack = f"{v_name} {d_name}"
        if all(t in full_haystack for t in tokens):
            return True

    return False

@app.route("/api/vehicles")
def api_vehicles():
    """
    Primary endpoint for the violations table: every vehicle with at least one violation of the
    displayed types in the last 24 hours, taken from the background fleet monitor.
    """
    page = max(1, int(request.args.get("page", 1)))
    limit = max(1, int(request.args.get("limit", 6)))
    search = request.args.get("search", "").strip()
    type_filter = request.args.get("status", "").strip().upper()

    snap = MONITOR.snapshot()
    vehicles = fetch_live_vehicles_from_wialon()

    by_type = {key: 0 for key in VIOLATION_TYPE_KEYS}
    violating_vehicles = []
    for v in vehicles:
        vios = snap["violations"].get(v.get("id"))
        if not vios:
            continue
        counts = Counter(x["type"] for x in vios)
        for key, count in counts.items():
            by_type[key] += count
        v["violation_counts"] = dict(counts)
        v["violation_count"] = len(vios)
        v["last_violation"] = vios[0]["time"]
        v["last_violation_unix"] = vios[0]["time_unix"]
        violating_vehicles.append(v)

    filtered_vehicles = violating_vehicles
    if search:
        filtered_vehicles = [v for v in filtered_vehicles if vehicle_matches_query(v, search)]
    if type_filter in by_type:
        filtered_vehicles = [v for v in filtered_vehicles if v["violation_counts"].get(type_filter)]

    # Most violations first, then most recent violation
    filtered_vehicles.sort(key=lambda v: (-v["violation_count"], -v["last_violation_unix"]))

    total_count = len(filtered_vehicles)
    paginated_items = filtered_vehicles[(page - 1) * limit:page * limit]

    return jsonify({
        "items": paginated_items,
        "total": total_count,
        "page": page,
        "limit": limit,
        "total_pages": max(1, (total_count + limit - 1) // limit),
        "summary": {
            "total_fleet": len(vehicles),
            "violating_vehicles_count": len(violating_vehicles),
            "total_violations": sum(by_type.values()),
            "by_type": by_type,
        },
        "types": violation_types_payload(),
        "scan": scan_status_payload(snap["status"]),
        "live_timestamp": datetime.now(fleet_violations.PKT).strftime("%Y-%m-%d %H:%M:%S")
    })

def requested_window(args):
    """
    The 'from'/'to' epoch-second pair an inspection request may carry, or (None, None) for the live
    last 24 hours. Raises ValueError with the message to show the caller.
    """
    window_from, window_to = args.get("from", "").strip(), args.get("to", "").strip()
    if bool(window_from) != bool(window_to):
        raise ValueError("from and to must be given together")
    if not window_from:
        return None, None
    if not (window_from.isdigit() and window_to.isdigit()):
        raise ValueError("from and to must be epoch seconds")
    window_from, window_to = int(window_from), int(window_to)
    if window_to <= window_from:
        raise ValueError("to must be later than from")
    # one violation never spans more than the monitor's own window; keep Wialon reads bounded
    if window_to - window_from > fleet_violations.WINDOW_SEC:
        raise ValueError("the window may not exceed 24 hours")
    return window_from, window_to


def inspection_payload(vehicle_id, vehicle_name, window_from, window_to, bypass_cache=False):
    """
    One vehicle's telemetry stream over the window, geocoded, with its violations. Returns
    (payload, from_cache); the cache is what makes exporting the stream you are looking at free.
    """
    cache_key = f"inspect:{vehicle_id}:{window_from}:{window_to}"
    if not bypass_cache:
        cached = INSPECTION_CACHE.get(cache_key)
        if cached is not None:
            return cached, True

    report = MONITOR.vehicle_report(int(vehicle_id), window_from, window_to,
                                    lead_sec=fleet_violations.TELEMETRY_LEAD_SEC)
    timeline = report["result"].get("timeline", [])

    # Geocode timeline locations, keeping the coordinates the address is about
    if timeline:
        coords_to_geo = []
        for row in timeline:
            try:
                lat_s, lon_s = row.get("location", "").split(",", 1)
                row["lat"], row["lon"] = float(lat_s.strip()), float(lon_s.strip())
            except ValueError:
                row["lat"] = row["lon"] = None
            coords_to_geo.append({"lat": row["lat"] or 0.0, "lon": row["lon"] or 0.0})
        for row, g in zip(timeline, batch_geocode_coords(coords_to_geo)):
            if g and g != "No GPS Fix":
                row["location"] = g

    violations = with_locations(report["violations"])

    # Tag telemetry message rows with any violations that occurred at that time
    if timeline and violations:
        for v in violations:
            v_time = v.get("time_unix")
            if v_time is None:
                continue
            closest_row = min(timeline, key=lambda r: abs(r.get("time_unix", 0) - v_time))
            if abs(closest_row.get("time_unix", 0) - v_time) <= 300:
                v_type = v.get("type", "VIOLATION")
                v_label = fleet_violations.TYPE_LABELS.get(v_type, v_type)
                curr_status = closest_row.get("status", "")
                if not curr_status or curr_status == "Normal":
                    closest_row["status"] = f"VIOLATION ({v_label})"
                elif "VIOLATION" not in curr_status and "BREACH" not in curr_status:
                    closest_row["status"] = f"VIOLATION ({v_label})"

    driver = fetch_live_drivers_from_wialon().get(int(vehicle_id)) or "Unassigned"

    payload = {
        "vehicle": vehicle_name,
        "vehicle_id": int(vehicle_id),
        "driver": driver,
        "date_range": {
            "from": fleet_violations.fmt_time(report["window_from"]),
            "to": fleet_violations.fmt_time(report["window_to"]),
            "from_epoch": report["window_from"],
            "to_epoch": report["window_to"],
            "scoped": window_from is not None
        },
        "timeline": timeline,
        "violations": violations,
        "summary": {
            "total_points": len(timeline),
            "messages": report["messages"],
            "violations_count": len(violations),
            "by_type": dict(Counter(v["type"] for v in violations))
        }
    }
    INSPECTION_CACHE.set(cache_key, payload, ttl_seconds=120)
    return payload, False


@app.route("/api/vehicle-report")
def api_vehicle_report():
    """
    Inspect view for one vehicle: its telemetry stream re-read from Wialon, Fatigue Driving evaluated on
    every message, and its Wialon notification violations.

    Without 'from'/'to' this is the live last 24 hours. With them it is the span a single violation was
    raised on - the continuous drive run for Fatigue Driving, a few minutes either side of the trigger
    for a notification alert - so the stream shows what the vehicle was doing when the alert fired.
    """
    vehicle_id = request.args.get("vehicleId", "").strip()
    vehicle_name = request.args.get("vehicleName", "Selected Vehicle")
    if not vehicle_id.isdigit():
        return jsonify({"error": "vehicleId is required"}), 400
    try:
        window_from, window_to = requested_window(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    bypass_cache = request.args.get("bypass_cache", "0").lower() in ["1", "true", "yes"]
    try:
        payload, from_cache = inspection_payload(vehicle_id, vehicle_name, window_from, window_to, bypass_cache)
    except Exception as exc:
        print(f"[Vehicle Report] Failed for {vehicle_name} ({vehicle_id}): {exc}")
        return jsonify({"error": f"Could not load telemetry for {vehicle_name} from Wialon: {exc}"}), 502

    resp = jsonify(payload)
    resp.headers["X-Cache"] = "HIT" if from_cache else "MISS"
    return resp

def build_compliance_report(selected_vehicle=None):
    """Compiles the report of every violating vehicle in the fleet monitor's current 24h window."""
    snap = MONITOR.snapshot()
    status = snap["status"]
    all_units = fetch_live_vehicles_from_wialon()
    if selected_vehicle:
        all_units = [u for u in all_units if u.get("vehicle") == selected_vehicle or str(u.get("id")) == str(selected_vehicle)]

    categories = {key: 0 for key in VIOLATION_TYPE_KEYS}
    violating_items = []
    all_detailed_violations = []
    for u in all_units:
        vios = snap["violations"].get(u.get("id"))
        if not vios:
            continue
        vios = with_locations(vios)
        for v in vios:
            categories[v["type"]] += 1
        all_detailed_violations.extend(vios)
        violating_items.append({
            "id": u.get("id"),
            "vehicle": u.get("vehicle"),
            "driver": u.get("driver", "Unassigned"),
            "violations": vios,
            "violation_count": len(vios),
            "violation_counts": dict(Counter(v["type"] for v in vios))
        })

    all_detailed_violations.sort(key=lambda v: v["time_unix"], reverse=True)
    violating_items.sort(key=lambda x: -x["violation_count"])

    return {
        "report_id": f"RPT-{int(time.time()*1000)}",
        "title": "Vehicle Operations & Compliance Safety Report",
        "timezone": "PKT (UTC+5)",
        "from_time": status.get("window_from"),
        "to_time": status.get("window_to"),
        "generated_at": datetime.now(fleet_violations.PKT).strftime("%Y-%m-%d %H:%M:%S"),
        "total_fleet": len(all_units),
        "total_violating_vehicles": len(violating_items),
        "total_violations": len(all_detailed_violations),
        "categories": categories,
        "category_labels": {t["key"]: t["label"] for t in fleet_violations.VIOLATION_TYPES},
        "items": violating_items,
        "detailed_violations": all_detailed_violations
    }

@app.route("/api/generate-report", methods=["POST"])
def api_generate_report():
    """
    Explicitly triggered ONLY when the user clicks 'Generate Report'.
    1. Compiles the report from the fleet monitor's violations for every vehicle.
    2. Saves the exact generated report in Supabase Cloud & SQLite.
    3. Returns the saved report to display in the frontend modal.
    """
    try:
        data = request.get_json() or {}
        status = MONITOR.snapshot()["status"]
        if status.get("state") != "ready":
            return jsonify({"status": "error", "message": f"Fleet scan still loading ({status.get('units_loaded', 0)}/{status.get('units_total', 0)} vehicles). Try again in a minute."}), 503

        report_payload = build_compliance_report(selected_vehicle=data.get("vehicle"))

        # Save to Supabase (and local SQLite backup)
        save_res = db.save_generated_report(report_payload)
        report_payload["saved_to_supabase"] = save_res.get("supabase_saved", False)

        return jsonify({
            "status": "success",
            "report": report_payload
        })
    except Exception as e:
        print(f"[Generate Report] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/export-csv", methods=["GET"])
def api_export_csv():
    """Flat CSV of every violation in the report being viewed: one row per violation."""
    try:
        import io, csv
        rep = REPORTS.report(request.args.get("report_id", "").strip())
        if not rep:
            return jsonify({"status": "error", "message": "Build a report first, then export it."}), 404
        payload = report_payload(rep)

        output = io.StringIO()
        output.write("﻿")  # UTF-8 BOM for Microsoft Excel
        writer = csv.writer(output)
        writer.writerow(["#", "Vehicle", "Driver", "Violation Time (PKT)", "Violation Type",
                         "Continuous Drive (min)", "Day / Night", "Rest Taken (min)", "Speed (km/h)", "Ignition",
                         "Odometer (km)", "Location", "Latitude", "Longitude", "Details"])

        rows = sorted((v for item in payload["vehicles"] for v in item["violations"]),
                      key=lambda v: v["time_unix"], reverse=True)
        for idx, vio in enumerate(rows, 1):
            tel = vio.get("telemetry") or {}
            speed = tel.get("speed_kmh", vio.get("speed_kmh"))
            writer.writerow([
                idx,
                vio.get("vehicle", "Unknown"),
                vio.get("driver", "Unassigned"),
                # a vehicle with no region set in Wialon is left blank, so the column filters cleanly
                None if (vio.get("vehicle_region") or regions.UNASSIGNED) == regions.UNASSIGNED
                else vio.get("vehicle_region"),
                vio.get("time", "-"),
                vio.get("type_label", vio.get("type")),
                vio["continuous_drive_minutes"] if vio.get("continuous_drive_minutes") else "",
                vio.get("period") or "",
                vio["rest_minutes"] if vio.get("rest_minutes") is not None else "",
                speed if speed is not None else "",
                ("IGN ON" if tel.get("ignition") == 1 else "IGN OFF") if tel else "",
                tel.get("odometer_km") or "",
                vio.get("location", ""),
                vio.get("lat"),
                vio.get("lon"),
                vio.get("details", ""),
            ])

        period = payload["label"].replace(" ", "_")
        filename = f"Violations_{period}_{datetime.now(fleet_violations.PKT):%Y%m%d_%H%M}.csv"
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={filename}",
                                 "Content-Type": "text/csv; charset=utf-8"})
    except Exception as e:
        print(f"[Export CSV] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

WIALON_LIMITS_CACHE = {"at": 0, "items": []}


def wialon_alert_limits():
    """The thresholds behind the six Wialon alert types, read from the notifications themselves."""
    if time.time() - WIALON_LIMITS_CACHE["at"] < 600 and WIALON_LIMITS_CACHE["items"]:
        return WIALON_LIMITS_CACHE["items"]

    session = reports.WialonSession(fleet_violations.load_env().get("WIALON_BASE_URL", "https://hst-api.wialon.eu"),
                                    fleet_violations.load_env()["WIALON_TOKEN"])
    try:
        resource = session.search("avl_resource", 1 | 0x400)[0]
        by_name = {n["n"]: n for n in (resource.get("unf") or {}).values()}
        wanted = {t["evt_name"]: t for t in fleet_violations.VIOLATION_TYPES if t["evt_name"]}
        ids = [int(by_name[name]["id"]) for name in wanted if name in by_name]
        details = session.call("resource/get_notification_data", {"itemId": resource["id"], "col": ids}) if ids else []
    finally:
        session.logout()

    def hhmm(minutes):
        return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"

    items = []
    for n in details:
        rule = wanted.get(n["n"])
        if not rule:
            continue
        params = (n.get("trg") or {}).get("p") or {}
        value, note = "-", ""
        if params.get("max_speed"):
            value = f"{params['max_speed']} km/h"
            note = "speed limit"
        if n.get("mast"):
            duration = f"{int(n['mast']) // 60} min"
            value = duration if value == "-" else f"{value} for {duration}"
            note = (note + ", held for" if note else "condition held for").strip()
        schedule = n.get("sch") or {}
        if schedule.get("f1") or schedule.get("t2"):
            value = f"{hhmm(schedule.get('f1', 0))} - {hhmm(schedule.get('t2', 0))}"
            note = "active only during these hours"
        items.append({"key": rule["key"], "label": rule["label"], "value": value,
                      "note": note or "set in Wialon", "disabled": bool(n.get("fl", 0) & 0x2),
                      "wialon_rule": n["n"]})

    order = [t["key"] for t in fleet_violations.VIOLATION_TYPES]
    items.sort(key=lambda x: order.index(x["key"]) if x["key"] in order else 99)
    WIALON_LIMITS_CACHE.update(at=time.time(), items=items)
    return items


@app.route("/api/wialon-limits")
def api_wialon_limits():
    try:
        return jsonify({"limits": wialon_alert_limits()})
    except Exception as e:
        print(f"[Wialon limits] {e}")
        return jsonify({"limits": [], "error": str(e)}), 502

def write_styled_sheet(ws, columns, rows):
    """
    The portal's one Excel look, shared by every xlsx export: deep purple header, banded rows, thin
    borders, autofilter, frozen header row, landscape A4 fitted one page wide with the header repeated.

    columns is a list of (title, width, horizontal alignment, wrap, number format); rows is a list of
    value lists in the same order. Rows whose wrapping columns overflow are given extra height.
    """
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    header_fill = PatternFill("solid", fgColor="4C1D95")       # deep purple, matches the portal
    band_fill = PatternFill("solid", fgColor="F3F0FA")
    thin = Side(style="thin", color="D4D4D8")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for index, (title, width, _align, _wrap, _fmt) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=index, value=title)
        cell.font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.row_dimensions[1].height = 30

    for row_number, values in enumerate(rows, start=2):
        banded = (row_number % 2 == 0)
        for index, (value, (_title, _width, align, wrap, fmt)) in enumerate(zip(values, columns), 1):
            cell = ws.cell(row=row_number, column=index, value=value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
            cell.border = border
            if fmt and isinstance(value, (int, float)):
                cell.number_format = fmt
            if banded:
                cell.fill = band_fill
        # taller rows where a wrapping column runs onto more lines
        lines = max([len(str(v)) / float(c[1] - 2) for v, c in zip(values, columns) if c[3]] + [1.0])
        ws.row_dimensions[row_number].height = 16 * min(int(lines) + (1 if lines % 1 else 0), 4)

    last_row = max(len(rows) + 1, 2)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{last_row}"
    ws.freeze_panes = "A2"

    # printing: landscape, one page wide, header repeated on every page
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.3
    ws.page_margins.top = ws.page_margins.bottom = 0.5
    ws.sheet_view.showGridLines = False


def xlsx_response(wb, filename):
    import io
    stream = io.BytesIO()
    wb.save(stream)
    return Response(stream.getvalue(),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/api/export-xlsx", methods=["GET"])
def api_export_xlsx():
    """Formatted Excel workbook: one row per violation in the report being viewed."""
    try:
        from openpyxl import Workbook

        preset = request.args.get("preset", "").upper()
        if preset in PERIOD_PRESETS:
            rep = REPORTS.build_report(*preset_window(preset))
        else:
            rep = REPORTS.report(request.args.get("report_id", "").strip())
        if not rep:
            return jsonify({"status": "error", "message": "Build a report first, then export it."}), 404
        payload = report_payload(rep)
        rows = sorted((v for item in payload["vehicles"] for v in item["violations"]),
                      key=lambda v: v["time_unix"], reverse=True)

        # column, width, horizontal alignment, wrap, number format
        columns = [
            ("#", 6, "center", False, "0"),
            ("Vehicle", 16, "center", False, None),
            ("Driver", 26, "left", False, None),
            ("Region", 16, "center", False, None),
            ("Violation Time (PKT)", 21, "center", False, None),
            ("Violation Type", 30, "left", False, None),
            ("Review", 11, "center", False, None),
            ("Continuous Drive (min)", 15, "center", False, "0.0"),
            ("Day / Night", 11, "center", False, None),
            ("Rest Taken (min)", 13, "center", False, "0.0"),
            ("Speed (km/h)", 12, "center", False, "0"),
            ("Ignition", 11, "center", False, None),
            ("Odometer (km)", 14, "center", False, "#,##0.0"),
            ("Location", 46, "left", True, None),
            ("Latitude", 12, "center", False, "0.000000"),
            ("Longitude", 12, "center", False, "0.000000"),
            ("Details", 80, "left", True, None),
        ]

        values = []
        for number, vio in enumerate(rows, start=1):
            tel = vio.get("telemetry") or {}
            values.append([
                number,
                vio.get("vehicle", "Unknown"),
                vio.get("driver", "Unassigned"),
                # a vehicle with no region set in Wialon is left blank, so the column filters cleanly
                None if (vio.get("vehicle_region") or regions.UNASSIGNED) == regions.UNASSIGNED
                else vio.get("vehicle_region"),
                vio.get("time", "-"),
                vio.get("type_label", vio.get("type")),
                {"GENUINE": "Genuine", "FALSE": "False"}.get(vio.get("verdict"), "Pending"),
                vio.get("continuous_drive_minutes"),
                vio.get("period"),
                vio.get("rest_minutes"),
                tel.get("speed_kmh", vio.get("speed_kmh")),
                ("IGN ON" if tel.get("ignition") == 1 else "IGN OFF") if tel else None,
                tel.get("odometer_km"),
                vio.get("location", ""),
                vio.get("lat"),
                vio.get("lon"),
                vio.get("details", ""),
            ])

        wb = Workbook()
        ws = wb.active
        ws.title = "Violations"
        write_styled_sheet(ws, columns, values)

        period = payload["label"].replace(" ", "_")
        return xlsx_response(wb, f"Violations_{period}_{datetime.now(fleet_violations.PKT):%Y%m%d_%H%M}.xlsx")
    except Exception as e:
        print(f"[Export XLSX] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/export-telemetry-xlsx", methods=["GET"])
def api_export_telemetry_xlsx():
    """
    The inspection view's telemetry stream as an Excel workbook, in the same look as the violations
    export: one row per Wialon message over the window the alert was raised on.

    Takes the same vehicleId/from/to as /api/vehicle-report, so it serves the stream already on screen
    straight out of the inspection cache. 'order' follows the table's Oldest/Newest first toggle;
    'runFrom'/'runTo' bracket the continuous drive run, which fills the In Run column.
    """
    try:
        from openpyxl import Workbook

        vehicle_id = request.args.get("vehicleId", "").strip()
        vehicle_name = request.args.get("vehicleName", "Selected Vehicle").strip() or "Selected Vehicle"
        if not vehicle_id.isdigit():
            return jsonify({"status": "error", "message": "vehicleId is required"}), 400
        try:
            window_from, window_to = requested_window(request.args)
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

        run_from, run_to = request.args.get("runFrom", "").strip(), request.args.get("runTo", "").strip()
        run_from = int(run_from) if run_from.isdigit() else None
        run_to = int(run_to) if run_to.isdigit() else None
        newest_first = request.args.get("order", "asc").lower() == "desc"

        payload, _ = inspection_payload(vehicle_id, vehicle_name, window_from, window_to)
        # the engine returns the stream newest first; the sheet follows the table's toggle
        timeline = payload["timeline"] if newest_first else list(reversed(payload["timeline"]))

        columns = [
            ("#", 6, "center", False, "0"),
            ("Message Time (PKT)", 21, "center", False, None),
            ("Speed (km/h)", 12, "center", False, "0.0"),
            ("Ignition", 11, "center", False, None),
            ("Drive / Stop Timer", 16, "center", False, None),
            ("State", 22, "left", False, None),
            ("In Run", 9, "center", False, None),
            ("Location", 46, "left", True, None),
            ("Latitude", 12, "center", False, "0.000000"),
            ("Longitude", 12, "center", False, "0.000000"),
            ("Status", 40, "left", True, None),
        ]

        values = []
        for number, row in enumerate(timeline, start=1):
            t = row.get("time_unix")
            if run_from is None or run_to is None or t is None:
                in_run = "-"                    # no continuous drive run to be inside of
            else:
                in_run = "Yes" if run_from <= t <= run_to else "No"
            values.append([
                number,
                row.get("time", "-"),
                row.get("speed"),
                "IGN ON" if row.get("ignition") == "ON" else "IGN OFF",
                row.get("dwell_rest"),
                row.get("state"),
                in_run,
                row.get("location", ""),
                row.get("lat"),
                row.get("lon"),
                row.get("status", ""),
            ])

        wb = Workbook()
        ws = wb.active
        # Excel sheet names cannot hold []:*?/\ and stop at 31 characters
        ws.title = re.sub(r"[\[\]:*?/\\]", "-", f"Telemetry {vehicle_name}")[:31]
        write_styled_sheet(ws, columns, values)

        plate = re.sub(r"[^A-Za-z0-9_-]+", "-", vehicle_name).strip("-") or "vehicle"
        if window_from:
            stamp = (f"{datetime.fromtimestamp(window_from, fleet_violations.PKT):%Y%m%d_%H%M}"
                     f"-{datetime.fromtimestamp(window_to, fleet_violations.PKT):%H%M}")
        else:
            stamp = f"last24h_{datetime.now(fleet_violations.PKT):%Y%m%d_%H%M}"
        return xlsx_response(wb, f"Telemetry_{plate}_{stamp}.xlsx")
    except Exception as e:
        print(f"[Export Telemetry XLSX] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/export-dashboard-xlsx", methods=["GET"])
def api_export_dashboard_xlsx():
    """Management dashboard workbook: KPI cards, four charts, the data behind them, live formulas."""
    try:
        preset = request.args.get("preset", "").upper()
        if preset in PERIOD_PRESETS:
            rep = REPORTS.build_report(*preset_window(preset))
        else:
            rep = REPORTS.report(request.args.get("report_id", "").strip())
        if not rep:
            return jsonify({"status": "error", "message": "Build a report first, then export it."}), 404
        payload = report_payload(rep)
        blob = dashboard_export.build_workbook(payload, fleet_violations.TYPE_LABELS)
        period = payload["label"].replace(" ", "_")
        filename = f"Violation_Dashboard_{period}_{datetime.now(fleet_violations.PKT):%Y%m%d_%H%M}.xlsx"
        return Response(blob,
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})
    except Exception as e:
        print(f"[Export dashboard] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

PERIOD_PRESETS = {"1D": 1, "7D": 7, "15D": 15, "30D": 30}
MAX_REPORT_DAYS = 31


def preset_window(preset):
    """(from, to, label) for a shortcut: whole days ending yesterday. Today is never included, so every
    day in it is already archived."""
    days = PERIOD_PRESETS[preset]
    time_to = reports.day_start(int(time.time()))
    return time_to - days * 86400, time_to, ("Yesterday" if days == 1 else f"Last {days} days")


def report_payload(rep, verdicts=True):
    """Report grouped by vehicle, with a display location on every violation. Each violation keeps the
    driver bound to the vehicle when its day was calculated, so a view makes no Wialon call."""
    violations = REPORTS.annotate(with_locations(rep["violations"]), verdicts=verdicts)
    for v in violations:
        v["driver"] = v.get("driver") or "Unassigned"
    by_vehicle = {}
    for v in violations:
        item = by_vehicle.setdefault(v["vehicle_id"], {
            "id": v["vehicle_id"], "vehicle": v["vehicle"], "driver": v["driver"],
            "violations": [], "violation_counts": {}})
        item["violations"].append(v)
        item["violation_counts"][v["type"]] = item["violation_counts"].get(v["type"], 0) + 1
    vehicles = sorted(by_vehicle.values(), key=lambda x: (-len(x["violations"]), -x["violations"][0]["time_unix"]))
    for item in vehicles:
        item["violation_count"] = len(item["violations"])

    by_type = {key: 0 for key in VIOLATION_TYPE_KEYS}
    for v in violations:
        by_type[v["type"]] = by_type.get(v["type"], 0) + 1

    return {
        **{k: rep[k] for k in ("id", "label", "from", "to", "from_unix", "to_unix", "generated_at", "days")},
        "summary": {
            "total_fleet": rep.get("fleet_size") or MONITOR.snapshot()["status"].get("units_total", 0),
            "violating_vehicles_count": len(vehicles),
            "total_violations": len(violations),
            "by_type": by_type,
        },
        "types": violation_types_payload(),
        "archive": rep.get("archive") or {},
        "reviews_problem": REPORTS.reviews_error,
        "vehicles": vehicles,
    }


@app.route("/api/report/start", methods=["POST"])
def api_report_start():
    """Start building a frozen report; returns a job id to poll."""
    data = request.get_json() or {}
    now = int(time.time())
    preset = str(data.get("preset", "")).upper()
    if preset in PERIOD_PRESETS:
        time_from, time_to, label = preset_window(preset)
    else:
        try:
            time_from, time_to = int(data["from_unix"]), int(data["to_unix"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Send preset (1D/7D/15D/30D) or from_unix and to_unix"}), 400
        if not 60 <= time_to - time_from <= MAX_REPORT_DAYS * 86400:
            return jsonify({"error": f"Period must be between 1 minute and {MAX_REPORT_DAYS} days"}), 400
        label = "Custom period"
    job_id = REPORTS.start(time_from, time_to, label)
    # An archived period is ready in well under a second: answer with it rather than make the page poll
    deadline = time.time() + 3
    job = REPORTS.job(job_id)
    while job.get("state") == "running" and time.time() < deadline:
        time.sleep(0.05)
        job = REPORTS.job(job_id)
    return jsonify({"job_id": job_id, "label": label, **job})


@app.route("/api/report/job/<job_id>")
def api_report_job(job_id):
    job = REPORTS.job(job_id)
    if not job:
        return jsonify({"error": "Unknown report job"}), 404
    return jsonify(job)


# A shortcut's report only changes when the archive does (after midnight, or new limits), so it is
# serialized and compressed once and handed to the browser with an ETag. The browser keeps it and asks
# "still this version?" on the next request; the answer is a 304 with no body until the archive changes.
PAYLOAD_CACHE = {}
_payload_lock = threading.Lock()


def report_version(rep):
    """What a shortcut report depends on: its days and when they were last calculated."""
    a = rep.get("archive") or {}
    if a.get("source") != "archive":
        return ("live", rep["id"])              # calculated on the spot: never reused
    return ("archive", rep["from"], rep["to"], a.get("archived_at"), tuple(a.get("stale_days") or ()))


def cached_json(kind, preset, version, build):
    """Serialized, gzipped JSON for (kind, preset), built again only when version changes."""
    with _payload_lock:
        hit = PAYLOAD_CACHE.get((kind, preset))
    if hit and hit["version"] == version:
        return hit
    etag = hashlib.sha1(repr((kind, version)).encode("utf-8")).hexdigest()[:24]
    data = build()
    data["version"] = etag
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    entry = {"version": version, "etag": etag, "gz": gzip.compress(body, 5), "size": len(body)}
    with _payload_lock:
        PAYLOAD_CACHE[(kind, preset)] = entry
    return entry


def send_cached(entry):
    """The cached body with its ETag; a browser already holding this version gets an empty 304."""
    wants_gzip = "gzip" in (request.headers.get("Accept-Encoding") or "").lower()
    resp = Response(entry["gz"] if wants_gzip else gzip.decompress(entry["gz"]), mimetype="application/json")
    if wants_gzip:
        resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Vary"] = "Accept-Encoding"
    resp.headers["Cache-Control"] = "private, no-cache"     # keep it, but check the version every time
    resp.set_etag(entry["etag"] + ("-gz" if wants_gzip else ""))
    return resp.make_conditional(request)


def preset_payload(preset):
    time_from, time_to, label = preset_window(preset)
    rep = REPORTS.build_report(time_from, time_to, label)

    def build():
        REPORTS.pin(rep)
        return report_payload(rep, verdicts=False)
    return cached_json("report", preset, report_version(rep), build)


DASHBOARD_DATA_VERSION = "v2_region_vehicles"

def dashboard_payload(preset):
    time_from, time_to, label = preset_window(preset)
    rep = REPORTS.build_report(time_from, time_to, label)
    version = (report_version(rep), REPORTS.reviews_fingerprint(), REPORTS.regions_version(), DASHBOARD_DATA_VERSION)

    def build():
        payload = report_payload(rep)
        violations = [v for item in payload["vehicles"] for v in item["violations"]]
        data = dashboard_data.build(violations, fleet_violations.VIOLATION_TYPES)
        data.update(preset=preset, fleet_size=payload["summary"]["total_fleet"], archive=payload["archive"],
                    reviews_problem=payload["reviews_problem"],
                    period={"label": label, "from": payload["from"], "to": payload["to"]})
        return data
    return cached_json("dashboard", preset, version, build)


def warm_payloads():
    """Once the archive job holds the shortcuts in memory, package them too, so a first click sends at once."""
    for preset in PERIOD_PRESETS:
        try:
            if REPORTS.window_ready(*preset_window(preset)[:2]):
                preset_payload(preset)
                dashboard_payload(preset)
        except Exception as e:
            print(f"[Payloads] {preset}: {e}")
    print("[Payloads] shortcuts packaged")


REPORTS.after_prewarm = warm_payloads


@app.route("/api/report/preset/<preset>")
def api_report_preset(preset):
    """A shortcut's report. Review verdicts are not in it (they change with every click, and would make
    the browser download the whole report again); the page reads them from /api/reviews."""
    preset = preset.upper()
    if preset not in PERIOD_PRESETS:
        return jsonify({"error": "preset must be 1D, 7D, 15D or 30D"}), 400
    time_from, time_to, label = preset_window(preset)
    if not REPORTS.window_ready(time_from, time_to):
        # days still to calculate: build it in the background and let the page follow the job
        job_id = REPORTS.start(time_from, time_to, label)
        return jsonify({"job_id": job_id, "label": label, **REPORTS.job(job_id)}), 202
    return send_cached(preset_payload(preset))


@app.route("/api/reviews")
def api_reviews():
    """Review verdicts in a time window: [vehicle_id, type, time_unix, verdict] each."""
    try:
        time_from, time_to = int(request.args["from_unix"]), int(request.args["to_unix"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Send from_unix and to_unix"}), 400
    items = [[k[0], k[1], k[2], verdict] for k, verdict in REPORTS.reviews().items() if time_from <= k[2] < time_to]
    return jsonify({"reviews": items, "problem": REPORTS.reviews_error})


@app.route("/dashboard")
def dashboard():
    return Response(dashboard_page.DASHBOARD_HTML, mimetype="text/html")


@app.route("/api/dashboard")
def api_dashboard():
    """Cards and chart data for one shortcut period, added up on the server."""
    preset = request.args.get("preset", "7D").upper()
    if preset not in PERIOD_PRESETS:
        return jsonify({"error": "preset must be 1D, 7D, 15D or 30D"}), 400
    try:
        entry = dashboard_payload(preset)
    except Exception as e:
        print(f"[Dashboard] {e}")
        return jsonify({"error": f"Could not build the dashboard: {e}"}), 500
    return send_cached(entry)


# The dashboard's charts are counts; this is the one view that needs the violations behind a count, so
# it is fetched only when someone opens a type rather than shipped with every dashboard load.
TYPE_VIOLATIONS_MAX = 1000


def type_violations_payload(preset, vtype):
    time_from, time_to, label = preset_window(preset)
    rep = REPORTS.build_report(time_from, time_to, label)
    # verdicts are shown per row, so the review fingerprint has to be part of the cache version
    version = (report_version(rep), REPORTS.reviews_fingerprint(), vtype, TYPE_VIOLATIONS_MAX)

    def build():
        payload = report_payload(rep)
        rows = [v for item in payload["vehicles"] for v in item["violations"] if v["type"] == vtype]
        rows.sort(key=lambda v: v["time_unix"], reverse=True)
        shown = rows[:TYPE_VIOLATIONS_MAX]
        return {
            "type": vtype,
            "type_label": fleet_violations.TYPE_LABELS.get(vtype, vtype),
            "preset": preset,
            "period": {"label": label, "from": payload["from"], "to": payload["to"]},
            "total": len(rows),
            "shown": len(shown),
            "truncated": len(rows) > len(shown),
            # only what the list draws, so a busy type stays a small response
            "violations": [{
                "time": v.get("time"),
                "time_unix": v.get("time_unix"),
                "vehicle": v.get("vehicle"),
                "vehicle_id": v.get("vehicle_id"),
                "driver": v.get("driver") or "Unassigned",
                "verdict": v.get("verdict"),
                "location": v.get("location") or v.get("address") or "",
                "speed_kmh": v.get("speed_kmh"),
                "continuous_drive_minutes": v.get("continuous_drive_minutes"),
            } for v in shown],
        }
    return cached_json(f"type-violations:{vtype}", preset, version, build)


def vehicle_violations_payload(preset, vehicle_id):
    time_from, time_to, label = preset_window(preset)
    rep = REPORTS.build_report(time_from, time_to, label)
    version = (report_version(rep), REPORTS.reviews_fingerprint(), vehicle_id)

    def build():
        payload = report_payload(rep)
        item = next((i for i in payload["vehicles"] if int(i["id"]) == vehicle_id), None)
        rows = sorted(item["violations"] if item else [], key=lambda v: v["time_unix"], reverse=True)
        return {
            "vehicle_id": vehicle_id,
            "vehicle": (item or {}).get("vehicle"),
            "preset": preset,
            "period": {"label": label, "from": payload["from"], "to": payload["to"]},
            "total": len(rows),
            "violations": rows,
        }
    return cached_json(f"vehicle-violations:{vehicle_id}", preset, version, build)


@app.route("/api/dashboard/vehicle-violations")
def api_dashboard_vehicle_violations():
    """One vehicle's violations for a period, for the review workbench opened from a dashboard chart."""
    preset = request.args.get("preset", "7D").upper()
    vehicle_id = request.args.get("vehicleId", "").strip()
    if preset not in PERIOD_PRESETS:
        return jsonify({"error": "preset must be 1D, 7D, 15D or 30D"}), 400
    if not vehicle_id.isdigit():
        return jsonify({"error": "vehicleId is required"}), 400
    try:
        entry = vehicle_violations_payload(preset, int(vehicle_id))
    except Exception as e:
        print(f"[Dashboard] vehicle-violations {vehicle_id}: {e}")
        return jsonify({"error": f"Could not load violations for this vehicle: {e}"}), 500
    return send_cached(entry)


@app.route("/api/dashboard/type-violations")
def api_dashboard_type_violations():
    """The individual violations behind one bar of the dashboard's Genuine vs False chart."""
    preset = request.args.get("preset", "7D").upper()
    vtype = request.args.get("type", "").strip().upper()
    if preset not in PERIOD_PRESETS:
        return jsonify({"error": "preset must be 1D, 7D, 15D or 30D"}), 400
    if vtype not in VIOLATION_TYPE_KEYS:
        return jsonify({"error": f"Unknown violation type {vtype}"}), 400
    try:
        entry = type_violations_payload(preset, vtype)
    except Exception as e:
        print(f"[Dashboard] type-violations {vtype}: {e}")
        return jsonify({"error": f"Could not load {vtype} violations: {e}"}), 500
    return send_cached(entry)


@app.route("/api/review", methods=["POST"])
def api_review():
    """Mark one violation Genuine or False (verdict null clears it back to pending)."""
    data = request.get_json() or {}
    try:
        key = (int(data["vehicle_id"]), str(data["type"]), int(data["time_unix"]))
    except (KeyError, TypeError, ValueError):
        return jsonify({"status": "error", "message": "Send vehicle_id, type and time_unix"}), 400
    if key[1] not in VIOLATION_TYPE_KEYS:
        return jsonify({"status": "error", "message": f"Unknown violation type {key[1]}"}), 400
    verdict = data.get("verdict")
    verdict = str(verdict).upper() if verdict else None
    if verdict not in (None,) + violation_store.VERDICTS:
        return jsonify({"status": "error", "message": "verdict must be GENUINE, FALSE or null"}), 400
    try:
        REPORTS.set_review(key, verdict)
    except Exception as e:
        problem = REPORTS.store.reviews_problem() if REPORTS.store else None
        print(f"[Review] {e}")
        return jsonify({"status": "error", "message": problem or str(e)}), 502
    return jsonify({"status": "success", "verdict": verdict})


@app.route("/api/archive/status")
def api_archive_status():
    """Where the nightly archive stands: tables found, days stored, and any calculation running."""
    return jsonify(REPORTS.archive_status())


@app.route("/api/report/<report_id>")
def api_report(report_id):
    rep = REPORTS.report(report_id)
    if not rep:
        return jsonify({"error": "That report is no longer in memory. Build it again."}), 404
    return jsonify(report_payload(rep))


@app.route("/api/vehicles-list")
def api_vehicles_list():
    """Returns sorted list of all fleet units for the selector dropdown."""
    vehicles = fetch_live_vehicles_from_wialon()
    units = [{"id": v["id"], "name": v["vehicle"], "driver": v["driver"]} for v in vehicles]
    units.sort(key=lambda u: u["name"])
    return jsonify({"units": units, "total": len(units)})

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        payload = request.json or {}
        invalid = [k for k in fatigue_engine.DEFAULT_CONFIG if k in payload and isinstance(fatigue_engine.DEFAULT_CONFIG[k], str)
                   and not fatigue_engine.is_valid_hhmm(payload[k])]
        if invalid:
            return jsonify({"status": "error", "message": f"Use HH:MM (for example 02:30) for: {', '.join(invalid)}"}), 400
        fatigue_engine.save_compliance_config({**fatigue_engine.get_compliance_config(), **payload})
        INSPECTION_CACHE.clear()
        REPORTS.clear_cache()      # locally cached days were calculated with the old limits
        REPORTS.limits_changed()   # recalculate the archived days with the new ones, in the background
        return jsonify({"status": "success", "config": payload})

    return jsonify(fatigue_engine.get_compliance_config())
PORTAL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Vehicle Operations & Compliance Portal</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
""" + theme.FAVICON_SHIELD + theme.THEME_HEAD + """  <style>
    /* Viewport-fit layout: header row + main; main = KPI row, filter row, table panel taking the rest */
    .app-shell { height: 100vh; height: 100dvh; display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden; }
    .app-main { display: grid; grid-template-rows: auto auto minmax(0, 1fr); gap: 1rem; padding: 1.125rem 1.75rem 1.25rem; width: 100%; max-width: 120rem; margin: 0 auto; min-height: 0; box-sizing: border-box; }
    .panel { position: relative; display: grid; grid-template-rows: minmax(0, 1fr); min-height: 0; overflow: hidden; }
    .view { grid-template-rows: auto minmax(0, 1fr) auto; min-height: 0; }
    .stack { display: grid; grid-template-rows: minmax(0, 1fr); min-height: 0; }
    .scroll-area { min-height: 0; overflow: auto; }

    /* Inspection sub-tabs: the active one is a solid rust pill with white text, the idle one charcoal
       on the card. The label is one span, so the flex gap only separates icon, label and count. */
    .subtab { display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.375rem 0.75rem; border-radius: 0.5rem;
              font-size: 0.875rem; font-weight: 700; color: var(--ink2); border: 1px solid transparent; transition: all 150ms ease; }
    .subtab:hover { color: var(--ink); background: var(--rail); }
    .subtab.on { color: #FFFFFF; background: var(--brand); border-color: var(--brand-deep); box-shadow: 0 2px 8px rgba(143, 59, 59, 0.30); }
    .subtab .scope { font-family: ui-monospace, monospace; font-weight: 600; opacity: 0.8; }
    .subtab .count { font-family: ui-monospace, monospace; font-size: 0.75rem; font-weight: 700; line-height: 1;
                     padding: 0.2rem 0.45rem; border-radius: 999px; background: var(--rail); color: var(--ink2); border: 1px solid var(--edge); }
    .subtab.on .count { background: rgba(255, 255, 255, 0.18); color: #FFFFFF; border-color: rgba(255, 255, 255, 0.30); }

    /* Footer strip under the inspection tables: what the stream or list is showing, as small chips */
    .window-chip { display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.15rem 0.55rem; border-radius: 999px;
                   background: var(--card); border: 1px solid var(--edge); color: var(--ink2); font-family: ui-monospace, monospace;
                   font-size: 0.75rem; font-weight: 600; white-space: nowrap; }
    .window-chip i { font-size: 0.625rem; color: var(--accent); }

    /* Narrow or very short windows: fall back to normal page scrolling */
    @media (max-width: 1023px), (max-height: 540px) {
      .app-shell { height: auto; overflow: visible; }
      .app-main { grid-template-rows: none; }
      .panel, .stack { grid-template-rows: none; }
      .scroll-area { max-height: 70vh; }
    }
""" + review_modal.STYLES + """  </style>
</head>
<body class="text-ink">
""" + review_modal.MARKUP + """

  <!-- COLLAPSIBLE LEFT DRAWER (CONFIGURATIONS) -->
  <div id="config-backdrop" onclick="toggleConfigDrawer(false)" class="fixed inset-0 bg-slate/40 backdrop-blur-sm z-50 transition-opacity duration-300 opacity-0 pointer-events-none"></div>

  <aside id="config-drawer" class="fixed inset-y-0 left-0 z-50 w-80 sm:w-96 bg-card border-r border-edge shadow-2xl transform -translate-x-full transition-transform duration-300 ease-in-out flex flex-col">
    <div class="p-4 border-b border-edge flex items-center justify-between bg-rail">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-rail border border-accent/40 flex items-center justify-center text-accent-deep">
          <i class="fa-solid fa-sliders text-sm"></i>
        </div>
        <div>
          <h2 class="text-sm font-bold text-ink">Compliance Standards</h2>
          <p class="text-[0.6875rem] text-ink2">Configurable Work-Rest Rules</p>
        </div>
      </div>
      <button onclick="toggleConfigDrawer(false)" class="w-8 h-8 rounded-lg bg-rail hover:bg-head text-ink2 hover:text-ink flex items-center justify-center transition border border-edge" title="Close">
        <i class="fa-solid fa-xmark text-sm"></i>
      </button>
    </div>

    <div class="p-5 flex-1 overflow-y-auto space-y-4 text-xs">
      <div class="bg-rail border border-edge rounded-xl p-3.5 text-ink2 leading-relaxed text-[0.6875rem] flex items-start gap-2">
        <i class="fa-solid fa-circle-info text-accent-deep mt-0.5 flex-shrink-0"></i>
        <span>All durations are HH:MM. Fatigue Driving is recalculated for the whole fleet as soon as you save.</span>
      </div>

      <form id="lafarge-config-form" onsubmit="saveLafargeConfig(event)" class="space-y-3.5">
        <div class="text-[0.6875rem] uppercase tracking-wider text-accent-deep font-bold pt-1">Fatigue Driving</div>
        <div class="space-y-1">
          <div class="flex items-center justify-between gap-3">
            <span class="text-ink2 font-medium">Max Continuous Drive (Day):</span>
            <input type="text" id="cfg-max-drive-day" value="02:30" placeholder="02:30" class="w-24 bg-rail border border-edge rounded-lg px-2.5 py-1 text-center font-mono font-bold text-ink focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/50">
          </div>
          <span class="text-[0.6875rem] text-ink2 block leading-snug">Driving time without a reset stop that triggers the break requirement (06:00-22:00 PKT)</span>
        </div>
        <div class="space-y-1">
          <div class="flex items-center justify-between gap-3">
            <span class="text-ink2 font-medium">Max Continuous Drive (Night):</span>
            <input type="text" id="cfg-max-drive-night" value="02:00" placeholder="02:00" class="w-24 bg-rail border border-edge rounded-lg px-2.5 py-1 text-center font-mono font-bold text-ink focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/50">
          </div>
          <span class="text-[0.6875rem] text-ink2 block leading-snug">Same limit between 22:00 and 06:00 PKT</span>
        </div>
        <div class="space-y-1">
          <div class="flex items-center justify-between gap-3">
            <span class="text-ink2 font-medium">Stop That Resets Driving Timer:</span>
            <input type="text" id="cfg-min-reset-stop" value="00:05" placeholder="00:05" class="w-24 bg-rail border border-edge rounded-lg px-2.5 py-1 text-center font-mono font-bold text-ink focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/50">
          </div>
          <span class="text-[0.6875rem] text-ink2 block leading-snug">A stop (under 5 km/h or ignition off) at least this long sets the driving timer back to zero. Shorter stops do not.</span>
        </div>
        <div class="space-y-1">
          <div class="flex items-center justify-between gap-3">
            <span class="text-ink2 font-medium">Required Break After Limit (Day):</span>
            <input type="text" id="cfg-min-break-day" value="00:15" placeholder="00:15" class="w-24 bg-rail border border-edge rounded-lg px-2.5 py-1 text-center font-mono font-bold text-ink focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/50">
          </div>
          <span class="text-[0.6875rem] text-ink2 block leading-snug">Once the limit is reached the driver must stop this long; driving on sooner is a violation</span>
        </div>
        <div class="space-y-1">
          <div class="flex items-center justify-between gap-3">
            <span class="text-ink2 font-medium">Required Break After Limit (Night):</span>
            <input type="text" id="cfg-min-break-night" value="00:15" placeholder="00:15" class="w-24 bg-rail border border-edge rounded-lg px-2.5 py-1 text-center font-mono font-bold text-ink focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/50">
          </div>
          <span class="text-[0.6875rem] text-ink2 block leading-snug">Same break requirement between 22:00 and 06:00 PKT</span>
        </div>
        <div class="pt-4">
          <button type="submit" class="w-full py-2.5 px-4 rounded-xl text-xs font-extrabold bg-brand hover:bg-brand-deep text-ice shadow-md shadow-brand/30 border border-brand-deep transition flex items-center justify-center gap-2">
            <i class="fa-solid fa-floppy-disk"></i>
            <span>Save Configuration</span>
          </button>
        </div>
      </form>
      <div class="pt-2">
        <div class="text-[0.6875rem] uppercase tracking-wider text-ink2 font-bold pb-2">Wialon alert limits</div>
        <div id="wialon-limits" class="space-y-2 text-xs text-ink2">Loading...</div>
        <p class="text-[0.6875rem] text-ink2 leading-snug pt-2">These six alerts are recorded by Wialon itself, so their limits live in the Wialon notifications. The portal reads them here but cannot change them.</p>
      </div>
    </div>
  </aside>

  <!-- TOAST NOTIFICATION CONTAINER -->
  <div id="toast-container" class="fixed bottom-6 right-6 z-50 space-y-3 max-w-md"></div>

  <div class="app-shell">

    <!-- TOP HEADER BAR -->
    <header class="bg-slate shadow-md shadow-slate/20 z-40">
      <div class="max-w-[120rem] mx-auto px-7 h-16 flex items-center justify-between">
        <div class="flex items-center gap-3.5">
          <div class="w-11 h-11 rounded-xl bg-gradient-to-tr from-brand-deep via-brand to-accent border border-white/15 flex items-center justify-center shadow-md shadow-slate-deep/40">
            <i class="fa-solid fa-shield-halved text-white text-lg"></i>
          </div>
          <div>
            <h1 class="font-bold text-xl text-white leading-tight tracking-tight">Vehicle Operations &amp; Compliance Portal</h1>
            <p class="text-xs text-ice font-mono">Violation reports · all times PKT (UTC+5)</p>
          </div>
        </div>

        <div class="flex items-center gap-3">
          <button onclick="toggleConfigDrawer(true)" class="px-4 py-2 rounded-xl text-sm font-bold bg-card hover:bg-rail text-ink border border-edge2 hover:border-accent transition flex items-center gap-2 shadow-sm">
            <i class="fa-solid fa-sliders text-accent-deep"></i>
            <span>Configurations</span>
          </button>
          <button onclick="openReviewModal()" title="Mark violations Genuine or False across the whole fleet" class="px-3.5 py-2 bg-brand hover:bg-brand-deep text-ice-lt border border-brand rounded-xl text-sm font-semibold transition flex items-center gap-2 shadow-sm">
            <i class="fa-solid fa-gavel"></i>
            <span>Review</span>
            <span id="review-pending-badge" class="hidden px-1.5 py-0.5 rounded-full bg-card text-brand-deep text-[0.625rem] font-extrabold font-mono">0</span>
          </button>
          <button onclick="openDashboard()" title="Charts and headline figures for this period" class="px-3.5 py-2 bg-card hover:bg-rail text-ink border border-edge2 hover:border-accent rounded-xl text-sm font-semibold transition flex items-center gap-2 shadow-sm">
            <i class="fa-solid fa-chart-column text-accent-deep"></i>
            <span>Dashboard</span>
          </button>
          <button onclick="exportFleetToExcel()" title="Formatted Excel workbook of every violation in this report" class="px-3.5 py-2 bg-card hover:bg-rail text-ink border border-edge2 hover:border-accent rounded-xl text-sm font-semibold transition flex items-center gap-2 shadow-sm">
            <i class="fa-solid fa-file-excel text-accent-deep"></i>
            <span>Excel</span>
          </button>
          <button onclick="loadReport()" title="Rebuild this report with the latest data" class="px-3.5 py-2 bg-card hover:bg-rail text-ink border border-edge2 hover:border-accent rounded-xl text-sm font-semibold transition flex items-center gap-2 shadow-sm">
            <i id="btn-refresh-icon" class="fa-solid fa-rotate text-accent-deep"></i>
            <span>Refresh</span>
          </button>
        </div>
      </div>
    </header>

    <main class="app-main">

      <!-- KPI SUMMARY STRIP -->
      <!-- Each tile is one solid colour chosen for what it counts - steel blue for the fleet, amber for
           units at fault, red for violations, charcoal for the period - with a deeper shade of the same
           colour behind the icon so the two halves read as one block -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div class="bg-info rounded-2xl px-6 py-5 shadow-lg shadow-info/25 flex items-center justify-between gap-3">
          <div class="min-w-0">
            <span class="text-xs uppercase tracking-wider text-ice block font-semibold">Total Fleet</span>
            <strong id="kpi-total-fleet" class="text-4xl font-extrabold text-white font-mono mt-1.5 block leading-none">0</strong>
          </div>
          <div class="w-14 h-14 rounded-2xl bg-info-deep text-ice flex items-center justify-center flex-shrink-0">
            <i class="fa-solid fa-truck text-xl"></i>
          </div>
        </div>
        <div class="bg-warn rounded-2xl px-6 py-5 shadow-lg shadow-warn/25 flex items-center justify-between gap-3">
          <div class="min-w-0">
            <span class="text-xs uppercase tracking-wider text-ice-lt block font-semibold">Violating Units</span>
            <strong id="kpi-violating-vehicles" class="text-4xl font-extrabold text-white font-mono mt-1.5 block leading-none">0</strong>
          </div>
          <div class="w-14 h-14 rounded-2xl bg-warn-deep text-ice-lt flex items-center justify-center flex-shrink-0">
            <i class="fa-solid fa-triangle-exclamation text-xl"></i>
          </div>
        </div>
        <div class="bg-danger rounded-2xl px-6 py-5 shadow-lg shadow-danger/25 flex items-center justify-between gap-3">
          <div class="min-w-0">
            <span class="text-xs uppercase tracking-wider text-ice block font-semibold">Total Violations</span>
            <strong id="kpi-total-violations" class="text-4xl font-extrabold text-white font-mono mt-1.5 block leading-none">0</strong>
          </div>
          <div class="w-14 h-14 rounded-2xl bg-danger-deep text-ice flex items-center justify-center flex-shrink-0">
            <i class="fa-solid fa-list-check text-xl"></i>
          </div>
        </div>
        <div class="bg-slate rounded-2xl px-6 py-5 shadow-lg shadow-slate/25 flex items-center justify-between gap-3">
          <div class="min-w-0">
            <span id="kpi-scan-title" class="text-xs uppercase tracking-wider text-ice block font-semibold">Report Period (Last 24h)</span>
            <strong id="kpi-scan-status" class="text-lg font-extrabold text-white mt-1.5 block truncate leading-tight font-mono">Starting...</strong>
            <span id="kpi-scan-detail" class="text-xs text-ice font-mono block truncate mt-1">-</span>
          </div>
          <div class="w-14 h-14 rounded-2xl bg-slate-deep text-ice flex items-center justify-center flex-shrink-0">
            <i class="fa-solid fa-satellite-dish text-xl"></i>
          </div>
        </div>
      </div>

      <!-- VIOLATION TYPE BREAKDOWN (click a card to filter the table) -->
      <div id="type-cards" class="flex flex-wrap items-center gap-2"></div>

      <!-- TABLE PANEL (fills the remaining height) -->
      <section class="panel bg-card rounded-2xl shadow-lg shadow-slate/10">

        <div id="table-loading-bar" class="absolute top-0 inset-x-0 h-1 z-30 hidden overflow-hidden">
          <div class="h-full bg-gradient-to-r from-brand-soft via-brand to-accent animate-pulse w-full"></div>
        </div>

        <!-- VIEW A: VIOLATING VEHICLES -->
        <div id="view-primary" class="view grid">
          <div class="px-5 py-3 bg-rail flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-2.5 flex-1 min-w-[15rem]">
              <div class="relative flex-1 max-w-sm">
                <i class="fa-solid fa-magnifying-glass absolute left-3.5 top-1/2 -translate-y-1/2 text-ink3 text-sm pointer-events-none"></i>
                <input type="text" id="search-input" autocomplete="off" oninput="handleSearchInput(event)" onkeydown="handleSearchKeyDown(event)" placeholder="Search vehicle plate or driver..." class="w-full bg-card border border-edge rounded-xl pl-10 pr-8 py-2.5 text-sm text-ink placeholder-ink3 focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/50">
                <button id="search-clear-btn" onclick="clearSearchInput()" class="absolute right-2.5 top-1/2 -translate-y-1/2 text-ink3 hover:text-ink text-xs hidden w-4 h-4 flex items-center justify-center rounded-full" title="Clear">
                  <i class="fa-solid fa-xmark"></i>
                </button>
              </div>
              <div class="inline-flex p-0.5 bg-card border border-edge rounded-xl" title="Whole days ending yesterday; today is never included">
                <button data-preset="1D" onclick="loadReport('1D')" class="px-3.5 py-1.5 rounded-lg text-sm font-bold transition">1D</button>
                <button data-preset="7D" onclick="loadReport('7D')" class="px-3.5 py-1.5 rounded-lg text-sm font-bold transition">7D</button>
                <button data-preset="15D" onclick="loadReport('15D')" class="px-3.5 py-1.5 rounded-lg text-sm font-bold transition">15D</button>
                <button data-preset="30D" onclick="loadReport('30D')" class="px-3.5 py-1.5 rounded-lg text-sm font-bold transition">30D</button>
              </div>
              <select id="status-filter" onchange="changeFilter()" class="bg-card border border-edge rounded-xl px-3 py-2.5 text-sm text-ink focus:outline-none focus:border-accent">
                <option value="ALL" selected>All Violation Types</option>
              </select>
            </div>
            <span id="vehicles-count-label" class="text-sm font-semibold text-ink2">Loading violations...</span>
          </div>

          <div id="table-scroll" class="scroll-area">
            <table class="w-full text-left text-sm table-fixed">
              <thead class="bg-head text-ink uppercase font-bold border-b border-edge2 tracking-wider sticky top-0 z-10">
                <tr>
                  <th class="w-[16%] px-4 py-3.5 text-center">Vehicle</th>
                  <th class="w-[22%] px-3 py-3.5 text-center">Driver</th>
                  <th class="w-[50%] px-3 py-3.5 text-center">Violations (Last 24h)</th>
                  <th class="w-[12%] px-3 py-3.5 text-center">Action</th>
                </tr>
              </thead>
              <tbody id="vehicles-table-body" class="divide-y divide-edge">
                <tr><td colspan="4" class="p-0 text-center"><div class="glow-loader-wrapper"><div class="glow-loader-container"></div></div></td></tr>
              </tbody>
            </table>
          </div>

          <div class="bg-rail px-5 py-3 flex flex-wrap items-center justify-between gap-3 text-sm font-semibold text-ink2">
            <div class="flex items-center gap-2">
              <span>Rows per page:</span>
              <select id="limit-select" onchange="changeLimit()" class="bg-card border border-edge rounded-lg px-2.5 py-1.5 text-ink text-sm focus:outline-none focus:border-accent">
                <option value="fit" selected>Fit to screen</option>
                <option value="10">10</option>
                <option value="25">25</option>
                <option value="50">50</option>
              </select>
            </div>
            <div class="flex items-center gap-1.5" id="pagination-controls"></div>
          </div>
        </div>

        <!-- VIEW B: VEHICLE INSPECTION -->
        <div id="view-detail" class="view grid hidden bg-card">
          <div class="px-5 py-3.5 bg-rail flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-3">
              <button onclick="slideBackToPrimary()" class="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-bold bg-rail hover:bg-head text-ink border border-edge shadow-md transition">
                <i class="fa-solid fa-arrow-left"></i> Back to Violations
              </button>
              <div class="h-4 w-px bg-edge"></div>
              <h2 id="drilldown-title" class="text-base font-bold text-ink flex items-center gap-2"><span>Vehicle Inspection:</span></h2>
            </div>
            <div class="flex items-center gap-2">
              <div class="inline-flex p-0.5 bg-card border border-edge rounded-xl">
                <button id="btn-sub-violations" onclick="setDetailSubView('violations')" class="subtab on">
                  <i class="fa-solid fa-triangle-exclamation"></i><span>Violations</span><span id="detail-violations-count" class="count">0</span>
                </button>
                <button id="btn-sub-timeline" onclick="setDetailSubView('timeline')" class="subtab">
                  <i class="fa-solid fa-chart-line"></i><span>Telemetry Stream <span id="detail-timeline-scope" class="scope">· last 24h</span></span><span id="detail-packets-count" class="count">0</span>
                </button>
              </div>
              <button id="btn-timeline-order" onclick="toggleTimelineOrder()" class="hidden px-3 py-1.5 rounded-xl text-sm font-bold bg-rail border border-edge text-ink2 hover:text-ink hover:border-accent transition items-center gap-1.5"
                data-tip="Switch the stream between oldest first (the drive run read forwards) and newest first">
                <i id="btn-timeline-order-icon" class="fa-solid fa-arrow-down-short-wide text-[0.625rem]"></i>
                <span id="btn-timeline-order-label">Oldest first</span>
              </button>
              <button id="btn-timeline-excel" onclick="exportTelemetryToExcel()" class="hidden px-3 py-1.5 rounded-xl text-sm font-bold bg-rail border border-edge text-ink2 hover:text-ink hover:border-accent transition items-center gap-1.5"
                data-tip="Formatted Excel workbook of this telemetry stream, in the order shown">
                <i class="fa-solid fa-file-excel text-[0.625rem]"></i>
                <span>Excel</span>
              </button>
              <button onclick="fetchTelemetryStream(true)" title="Reload the telemetry stream from Wialon" class="px-3 py-1.5 rounded-xl text-sm font-bold bg-rail border border-edge text-ink2 hover:text-ink hover:border-accent transition flex items-center gap-1">
                <i id="btn-inspect-refresh-icon" class="fa-solid fa-arrows-rotate text-[0.625rem]"></i>
                <span>Refresh</span>
              </button>
            </div>
          </div>

          <div class="stack">
            <div id="drilldown-violations-section" class="scroll-area">
              <table class="w-full text-left text-sm table-fixed">
                <thead class="bg-head text-ink uppercase font-bold border-b border-edge2 tracking-wider sticky top-0 z-10">
                  <tr>
                    <th class="w-[11%] px-3 py-3.5 text-center">Violation Time (PKT)</th>
                    <th class="w-[14%] px-3 py-3.5 text-center">Violation Type</th>
                    <th class="w-[8%] px-2 py-3.5 text-center">Speed</th>
                    <th class="w-[8%] px-2 py-3.5 text-center">Ignition</th>
                    <th class="w-[9%] px-2 py-3.5 text-center">Driving Hours</th>
                    <th class="w-[19%] px-3 py-3.5 text-center">Violation Location</th>
                    <th class="w-[18%] px-3 py-3.5 text-center">Details</th>
                    <th class="w-[13%] px-2 py-3.5 text-center" data-tip="Set in the Review workbench, from the Review button in the header">Review</th>
                  </tr>
                </thead>
                <tbody id="drilldown-violations-body" class="divide-y divide-edge"></tbody>
              </table>
            </div>

            <div id="drilldown-timeline-section" class="scroll-area hidden">
              <table class="w-full text-left text-sm table-fixed">
                <thead class="bg-head text-ink uppercase font-bold border-b border-edge2 tracking-wider sticky top-0 z-10">
                  <tr>
                    <th class="w-[18%] px-3 py-3.5 text-center">Message Time (PKT)</th>
                    <th class="w-[24%] px-2 py-3.5 text-center">Telemetry (Speed / Ign)</th>
                    <th class="w-[14%] px-2 py-3.5 text-center" data-tip="The timer that decides Fatigue Driving. On a driving row it is how long the vehicle has driven without a 00:05 reset stop - this is what trips the 02:30 day / 02:00 night limit. On a stopped row it is how long the current stop has lasted, which is what has to reach 00:05 to clear the drive timer.">Drive / Stop Timer</th>
                    <th class="w-[14%] px-2 py-3.5 text-center">State</th>
                    <th class="w-[16%] px-3 py-3.5 text-center">Location</th>
                    <th class="w-[14%] px-2 py-3.5 text-center">Status</th>
                  </tr>
                </thead>
                <tbody id="timeline-table-body" class="divide-y divide-edge font-mono"></tbody>
              </table>
            </div>
          </div>

          <div class="bg-rail px-4 py-2 flex items-center justify-end text-xs font-semibold text-ink2">
            <span id="drilldown-window-tag" class="flex flex-wrap items-center justify-end gap-1.5">24-Hour Evaluation Window</span>
          </div>
        </div>

      </section>
    </main>
  </div>

  <script>
    // The page shows one frozen report at a time. Nothing reloads by itself.
    let currentReport = null;
    let currentPreset = '1D';
    let reportJobTimer = null;
    let building = false;
    let currentPage = 1;
    let limitMode = 'fit';
    let currentLimit = 10;
    let currentSearch = '';
    let currentStatus = 'ALL';
    let detailType = null;           // type the inspected vehicle's list is filtered to (null = all)
    let selectedVehicleId = null;
    let selectedVehicleName = null;
    let isDetailView = false;
    let refitDone = false;
    let selectedViolation = null;    // the violation the telemetry stream is scoped to (null = live last 24h)
    let detailViolations = [];       // the rows currently rendered in the inspection violations table
    let timelineRows = [];           // the stream as fetched, newest first (the engine's own order)
    let timelineOrder = 'asc';       // 'asc' reads the drive run forwards, from its start down to the alert
    let timelineLoadedFor = null;    // window key already in the timeline table, so re-opening it is free

    let VIOLATION_TYPES = [];
    // Each type's hue says what kind of violation it is: reds and amber for speed, plum for fatigue,
    // steel blue for night, and teal / brown / charcoal for the three seat-belt rules
    const TYPE_META = {
      FATIGUE_DRIVING:        { icon: 'fa-hourglass-half', cls: 'bg-plum-soft text-plum border-plum/30',            on: 'bg-plum text-ice border-plum',               ic: 'text-plum' },
      NIGHT_DRIVING:          { icon: 'fa-moon',           cls: 'bg-info-soft text-info border-info/30',            on: 'bg-info text-ice border-info',               ic: 'text-info' },
      OVERSPEED_HIGHWAY:      { icon: 'fa-road',           cls: 'bg-warn-soft text-warn-deep border-warn/35',       on: 'bg-warn text-ice-lt border-warn',            ic: 'text-warn' },
      OVERSPEED_MOTORWAY:     { icon: 'fa-road',           cls: 'bg-danger-soft text-danger-deep border-danger/40', on: 'bg-danger-deep text-ice border-danger-deep', ic: 'text-danger-deep' },
      SEAT_BELT_IGNITION_OFF: { icon: 'fa-user-shield',    cls: 'bg-slate-soft text-slate border-slate/30',         on: 'bg-slate text-ice border-slate',             ic: 'text-slate' },
      SEAT_BELT_DISCONNECTED: { icon: 'fa-user-slash',     cls: 'bg-teal-soft text-teal border-teal/30',            on: 'bg-teal text-ice-lt border-teal',            ic: 'text-teal' },
      DELAY_DRIVER_SEAT_BELT: { icon: 'fa-user-clock',     cls: 'bg-coffee-soft text-coffee border-coffee/30',      on: 'bg-coffee text-ice-lt border-coffee',        ic: 'text-coffee' },
      OVERSPEED:              { icon: 'fa-gauge-high',     cls: 'bg-danger-soft text-danger border-danger/35',      on: 'bg-danger text-ice border-danger',           ic: 'text-danger' }
    };
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const PRESET_LABELS = { '1D': 'Yesterday', '7D': 'Last 7 days', '15D': 'Last 15 days', '30D': 'Last 30 days' };
    const LOADER_ROW = (cols, text) => `<tr><td colspan="${cols}" class="p-0 text-center"><div class="glow-loader-wrapper"><div class="glow-loader-container"></div></div>${text ? `<p class="text-sm text-ink2 pb-6">${esc(text)}</p>` : ''}</td></tr>`;

    function esc(s) {
      return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    // "2026-09-16 23:02:47" -> "16 Sep 23:02"
    function stamp(text, withDate = true) {
      const v = String(text || '');
      const mon = Number(v.slice(5, 7)), day = Number(v.slice(8, 10)), hhmm = v.slice(11, 16);
      if (!mon || !day || hhmm.length !== 5) return text || '-';
      return withDate ? `${day} ${MONTHS[mon - 1]} ${hhmm}` : hhmm;
    }

    function periodText(from, to) {
      const sameDay = String(from || '').slice(0, 10) === String(to || '').slice(0, 10);
      return `${stamp(from)} - ${stamp(to, !sameDay)}`;
    }

    // 150 -> "2h 30m": the continuous drive behind a fatigue violation, read as hours
    function driveHours(mins) {
      const whole = Math.floor(mins / 60), rest = Math.round(mins % 60);
      return whole + 'h ' + String(rest).padStart(2, '0') + 'm';
    }

    function typeLabel(key) {
      const t = VIOLATION_TYPES.find(t => t.key === key);
      return t ? t.label : key;
    }

    // "Night" or "Day": which continuous-drive limit applied when the violation was stamped
    function periodTag(period) {
      if (period !== 'Night' && period !== 'Day') return '';
      const night = period === 'Night';
      const cls = night ? 'bg-info-soft text-info border-info/35' : 'bg-warn-soft text-warn-deep border-warn/40';
      const icon = night ? 'fa-moon' : 'fa-sun';
      return `<span class="ml-1.5 px-1.5 py-0.5 rounded border text-[0.625rem] font-bold align-middle ${cls}"><i class="fa-solid ${icon} mr-1 text-[0.5625rem]"></i>${period}</span>`;
    }

    function typeBadge(key, count) {
      const meta = TYPE_META[key] || { icon: 'fa-triangle-exclamation', cls: 'bg-slate-soft text-slate border-slate/30' };
      const suffix = count > 1 ? ` ×${count}` : '';
      return `<span class="px-2.5 py-1 rounded-full text-xs font-bold border ${meta.cls} inline-flex items-center gap-1.5 max-w-full" data-tip="${esc(typeLabel(key))}"><i class="fa-solid ${meta.icon} text-[0.6875rem]"></i><span class="truncate">${esc(typeLabel(key))}${suffix}</span></span>`;
    }

    // A small reading: tinted in its own hue when it is "on" (moving, ignition on), plain grey when not
    const CHIP_TONES = { warn: 'bg-warn-soft text-warn-deep border border-warn/40', ok: 'bg-ok-soft text-ok-deep border border-ok/40' };
    const chip = (on, text, tone) => `<span class="inline-flex items-center px-2 py-0.5 rounded font-mono font-semibold text-xs ${on ? CHIP_TONES[tone] : 'bg-rail text-ink3 border border-edge'}">${text}</span>`;

    // The violation's own speed (what Wialon recorded with the alert, or the speed at the fatigue mark),
    // else the telemetry message recorded at the violation: the same speed the dashboard uses
    function speedCell(v) {
      const tel = v.telemetry;
      const speed = v.speed_kmh != null ? v.speed_kmh : (tel ? tel.speed_kmh : null);
      if (speed == null) return '<span class="text-ink3">-</span>';
      const source = v.speed_kmh != null ? 'Speed recorded with the violation' : `Telemetry message at ${tel.time} PKT`;
      return `<span data-tip="${esc(source)}">${chip(speed > 0, `<i class="fa-solid fa-gauge-high text-[0.5625rem] mr-1"></i>${speed} km/h`, 'warn')}</span>`;
    }

    // Ignition from the telemetry message recorded at the violation
    function ignitionCell(v) {
      const tel = v.telemetry;
      if (!tel || tel.ignition == null) {
        return '<span class="text-ink3 text-xs" data-tip="No telemetry message within 10 minutes before this violation">-</span>';
      }
      const on = tel.ignition === 1;
      return `<span data-tip="Telemetry message at ${esc(tel.time)} PKT">${chip(on, on ? 'ON' : 'OFF', 'ok')}</span>`;
    }

    // One styled tooltip for anything carrying data-tip
    const tipEl = document.createElement('div');
    tipEl.className = 'tip';
    let tipTarget = null;

    function positionTip(e) {
      const pad = 16;
      const box = tipEl.getBoundingClientRect();
      let x = e.clientX + pad;
      let y = e.clientY + pad;
      if (x + box.width > window.innerWidth - 8) x = Math.max(8, e.clientX - box.width - pad);
      if (y + box.height > window.innerHeight - 8) y = Math.max(8, e.clientY - box.height - pad);
      tipEl.style.left = `${x}px`;
      tipEl.style.top = `${y}px`;
    }

    function setupTooltips() {
      document.body.appendChild(tipEl);
      document.addEventListener('mouseover', e => {
        const el = e.target.closest('[data-tip]');
        if (!el || !el.dataset.tip) return;
        tipTarget = el;
        tipEl.textContent = el.dataset.tip;
        tipEl.classList.add('show');
        positionTip(e);
      });
      document.addEventListener('mousemove', e => { if (tipTarget) positionTip(e); });
      document.addEventListener('mouseout', e => {
        if (tipTarget && !tipTarget.contains(e.relatedTarget)) {
          tipTarget = null;
          tipEl.classList.remove('show');
        }
      });
      document.addEventListener('scroll', () => { tipTarget = null; tipEl.classList.remove('show'); }, true);
    }

    function detailsText(v) {
      const text = String(v.details || '').trimStart();
      const plate = String(v.vehicle || '');
      if (!plate || v.text_names_other_unit || !text.toLowerCase().startsWith(plate.toLowerCase())) return text;
      let rest = text.slice(plate.length);
      while (rest.length && (rest[0] === ' ' || rest[0] === '-' || rest[0] === ':')) rest = rest.slice(1);
      return rest || text;
    }

    function showToast(title, msg, type = "info") {
      const container = document.getElementById('toast-container');
      if (!container) return;
      const toast = document.createElement('div');
      const isCrit = type === 'critical';
      toast.className = `p-3.5 rounded-xl border shadow-2xl transition transform flex items-start gap-3 backdrop-blur ${isCrit ? 'bg-danger/95 border-danger/80 text-white' : 'bg-card border-accent/50 text-ink'}`;
      toast.innerHTML = `
        <i class="fa-solid ${isCrit ? 'fa-triangle-exclamation text-danger' : 'fa-circle-info text-accent-deep'} mt-0.5 text-sm"></i>
        <div class="flex-1 text-xs">
          <div class="font-bold text-xs leading-tight"><span class="${isCrit ? 'text-danger' : 'text-accent-deep'}">${esc(title)}</span></div>
          <p class="mt-1 text-ink2 font-medium">${esc(msg)}</p>
        </div>
        <button onclick="this.parentElement.remove()" class="opacity-60 hover:opacity-100 text-sm ml-1 leading-none">&times;</button>
      `;
      container.appendChild(toast);
      setTimeout(() => { if (toast.parentElement) toast.remove(); }, 6000);
    }

    function toggleConfigDrawer(open) {
      const drawer = document.getElementById('config-drawer');
      const backdrop = document.getElementById('config-backdrop');
      if (!drawer || !backdrop) return;
      const shouldOpen = (open !== undefined) ? open : drawer.classList.contains('-translate-x-full');
      if (shouldOpen) {
        drawer.classList.remove('-translate-x-full');
        backdrop.classList.remove('opacity-0', 'pointer-events-none');
        backdrop.classList.add('opacity-100');
        loadLafargeConfig();
        loadWialonLimits();
      } else {
        drawer.classList.add('-translate-x-full');
        backdrop.classList.add('opacity-0', 'pointer-events-none');
        backdrop.classList.remove('opacity-100');
      }
    }

    // ---------------------------------------------------------------- building a report
    function setPeriodButtons() {
      document.querySelectorAll('[data-preset]').forEach(btn => {
        const active = btn.dataset.preset === currentPreset;
        btn.className = `px-3.5 py-1.5 rounded-lg text-sm font-bold transition ${active ? 'bg-brand text-ice shadow-sm border border-brand' : 'text-ink2 hover:text-ink'}`;
      });
    }

    function showBuilding(step) {
      const statusEl = document.getElementById('kpi-scan-status');
      const detailEl = document.getElementById('kpi-scan-detail');
      const titleEl = document.getElementById('kpi-scan-title');
      if (titleEl) titleEl.innerText = `Loading ${PRESET_LABELS[currentPreset] || 'report'}`;
      if (statusEl) { statusEl.innerText = 'Working...'; statusEl.dataset.tip = ''; }
      if (detailEl) detailEl.innerText = step || '';
      const tbody = document.getElementById('vehicles-table-body');
      if (tbody && !isDetailView) tbody.innerHTML = LOADER_ROW(4, step || 'Building report...');
      const label = document.getElementById('vehicles-count-label');
      if (label) label.innerText = 'Building report...';
    }

    // Reports are frozen for the day, so each shortcut is fetched once and kept: in this page's memory,
    // and in the browser's cache across pages (the server answers "unchanged" rather than resending it).
    const reportCache = {};          // preset -> report
    let VERDICTS = null;             // "vehicle|type|second" -> 'GENUINE' | 'FALSE'
    let reportRequest = 0;

    async function loadVerdicts() {
      if (VERDICTS) return;
      const now = Math.floor(Date.now() / 1000);
      try {
        const data = await fetch(`/api/reviews?from_unix=${now - 35 * 86400}&to_unix=${now}`).then(r => r.json());
        VERDICTS = {};
        (data.reviews || []).forEach(([vehicleId, type, timeUnix, verdict]) => { VERDICTS[`${vehicleId}|${type}|${timeUnix}`] = verdict; });
      } catch (e) {
        VERDICTS = {};
      }
    }

    function applyVerdicts(rep) {
      rep.vehicles.forEach(item => item.violations.forEach(v => { v.verdict = VERDICTS[reviewKey(v)] || null; }));
    }

    function showReport(rep) {
      building = false;
      currentReport = rep;
      VIOLATION_TYPES = rep.types || VIOLATION_TYPES;
      currentPage = 1;
      refitDone = false;
      populateTypeFilter();
      renderReport();
      updatePendingBadge();
      if (pendingDeepLink) {
        const link = pendingDeepLink;
        pendingDeepLink = null;
        Review.show({ plate: link.plate, focusKey: link.focus });
        return;
      }
      if (Review.open) Review.show({});   // a period switch re-stocks the open workbench
    }

    async function loadReport(preset) {
      if (preset) { currentPreset = preset; setPeriodButtons(); }
      const wanted = currentPreset;
      const request = ++reportRequest;
      clearTimeout(reportJobTimer);
      const kept = reportCache[wanted];
      if (kept) showReport(kept);                 // opened before today: shown at once
      else { building = true; showBuilding('Loading'); }
      try {
        // The browser sends the version it holds; until the archive changes the server says "unchanged".
        // Verdicts are fetched alongside, not before, so they never hold the report up.
        const [res] = await Promise.all([fetch(`/api/report/preset/${wanted}`), loadVerdicts()]);
        if (request !== reportRequest) return;    // a later click took over
        const etag = res.headers.get('ETag');
        if (kept && res.status === 200 && etag && etag === kept.etag) return;   // unchanged: nothing to read
        const data = await res.json();
        if (request !== reportRequest) return;
        if (res.status === 202) return pollReportJob(data.job_id, data);
        if (!res.ok) throw new Error(data.error || 'Could not load the report');
        data.etag = etag;
        applyVerdicts(data);
        reportCache[wanted] = data;
        if (wanted === currentPreset && !isDetailView) showReport(data);
        else if (wanted === currentPreset) currentReport = data;
        if (!kept) showToast('Report ready', `${data.label}: ${data.summary.total_violations} violations across ${data.summary.violating_vehicles_count} vehicles`);
      } catch (e) {
        if (request !== reportRequest) return;
        building = false;
        if (!kept) {
          showToast('Report failed', e.message, 'critical');
          showBuilding(e.message);
        }
      }
    }

    // An archived period usually comes back finished with the start call itself (first): no polling
    function pollReportJob(jobId, first) {
      const tick = async (known) => {
        try {
          let job = known;
          if (!job) {
            const res = await fetch(`/api/report/job/${jobId}`);
            job = await res.json();
            if (!res.ok) throw new Error(job.error || 'Report job lost');
          }
          if (job.state === 'running') {
            showBuilding(job.step);
            reportJobTimer = setTimeout(() => tick(null), 2000);
            return;
          }
          if (job.state === 'error') throw new Error(job.error || 'Report failed');
          building = false;
          loadReport(currentPreset);
        } catch (e) {
          building = false;
          showToast('Report failed', e.message, 'critical');
          showBuilding(e.message);
        }
      };
      tick(first && first.state ? first : null);
    }

    // ---------------------------------------------------------------- rendering
    function populateTypeFilter() {
      const sel = document.getElementById('status-filter');
      if (!sel || sel.options.length > 1) return;
      VIOLATION_TYPES.forEach(t => {
        const opt = document.createElement('option');
        opt.value = t.key;
        opt.textContent = t.label;
        sel.appendChild(opt);
      });
      sel.value = currentStatus;
    }

    // The cards follow the view: the fleet's counts filtering the vehicles table, or, while a vehicle is
    // being inspected, that vehicle's counts filtering its own violations list
    function renderTypeCards() {
      const container = document.getElementById('type-cards');
      if (!container) return;
      const inspected = isDetailView ? reportVehicle(selectedVehicleId) : null;
      const byType = isDetailView ? ((inspected || {}).violation_counts || {}) : (currentReport ? currentReport.summary.by_type : {});
      const hint = isDetailView ? `click to show only these for ${selectedVehicleName || 'this vehicle'}` : 'click to filter the table';
      container.innerHTML = VIOLATION_TYPES.map(t => {
        const meta = TYPE_META[t.key] || { icon: 'fa-triangle-exclamation', cls: 'bg-slate-soft text-slate border-slate/30', on: 'bg-slate text-ice border-slate', ic: 'text-slate' };
        const count = (byType || {})[t.key] || 0;
        const active = isDetailView ? detailType === t.key : currentStatus === t.key;
        return `
          <button onclick="toggleTypeFilter('${t.key}')" data-tip="${esc(t.label)} - ${esc(t.description || '')} (${hint})"
            class="inline-flex items-center gap-1.5 pl-2 pr-2.5 py-1.5 rounded-full border text-xs font-bold transition min-w-0 shadow-sm
                   ${active ? meta.on + ' shadow' : 'bg-card border-edge text-ink hover:bg-hover hover:border-edge2'}">
            <span class="w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0 ${active ? 'bg-white/20' : meta.cls.split(' ')[0]}">
              <i class="fa-solid ${meta.icon} text-[0.625rem] ${active ? '' : meta.ic}"></i>
            </span>
            <span class="truncate">${esc(t.label)}</span>
            <span class="font-mono font-extrabold ${active ? '' : (count > 0 ? 'text-danger' : 'text-ink3')}">${count}</span>
          </button>
        `;
      }).join('');
    }

    function toggleTypeFilter(key) {
      if (isDetailView) {
        detailType = detailType === key ? null : key;
        setDetailSubView('violations');
        renderTypeCards();
        renderVehicleViolations();
        return;
      }
      const sel = document.getElementById('status-filter');
      if (sel) sel.value = (currentStatus === key) ? 'ALL' : key;
      changeFilter();
    }

    function matchesSearch(v, query) {
      if (!query) return true;
      const q = query.trim().toLowerCase();
      const qn = q.replace(/[^a-z0-9]/g, '');
      const name = String(v.vehicle || '').toLowerCase();
      const driver = String(v.driver || '').toLowerCase();
      if (name.includes(q) || driver.includes(q)) return true;
      if (qn && (name.replace(/[^a-z0-9]/g, '').includes(qn) || driver.replace(/[^a-z0-9]/g, '').includes(qn))) return true;
      const words = q.split(' ').filter(Boolean);
      return words.length > 1 && words.every(w => `${name} ${driver}`.includes(w));
    }

    function filteredVehicles() {
      if (!currentReport) return [];
      return currentReport.vehicles.filter(v =>
        matchesSearch(v, currentSearch) && (currentStatus === 'ALL' || (v.violation_counts || {})[currentStatus]));
    }

    function computeLimit() {
      if (limitMode !== 'fit') return parseInt(limitMode, 10) || 10;
      const area = document.getElementById('table-scroll');
      if (!area || area.clientHeight === 0) return 10;
      const head = area.querySelector('thead');
      const rootPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 14;
      const sample = document.querySelector('#vehicles-table-body tr.data-row');
      const rowHeight = sample ? sample.getBoundingClientRect().height : rootPx * 3.1;
      const available = area.clientHeight - (head ? head.getBoundingClientRect().height : 0);
      return Math.max(3, Math.floor(available / rowHeight));
    }

    // Where the numbers came from: the nightly archive, or calculated just now
    function archiveNote(rep) {
      const a = rep.archive || {};
      const base = `${rep.summary.total_fleet} vehicles`;
      if (a.source !== 'archive') return `${base} · calculated ${stamp(rep.generated_at, false)} PKT`;
      const stale = (a.stale_days || []).length;
      if (stale) return `${base} · ${stale} day(s) still on the previous limits, recalculating`;
      return `${base} · from the nightly archive`;
    }

    function renderReport() {
      if (!currentReport) return;
      const rep = currentReport;
      const s = rep.summary;
      document.getElementById('kpi-total-fleet').innerText = s.total_fleet || 0;
      document.getElementById('kpi-violating-vehicles').innerText = s.violating_vehicles_count || 0;
      document.getElementById('kpi-total-violations').innerText = s.total_violations || 0;
      document.getElementById('kpi-scan-title').innerText = `Report Period (${rep.label})`;
      const statusEl = document.getElementById('kpi-scan-status');
      statusEl.innerText = periodText(rep.from, rep.to);
      statusEl.dataset.tip = `${rep.label}: ${rep.from} to ${rep.to} PKT`;
      document.getElementById('kpi-scan-detail').innerText = archiveNote(rep);
      renderTypeCards();
      if (isDetailView) renderVehicleViolations();
      renderTable();
    }

    function renderTable() {
      const tbody = document.getElementById('vehicles-table-body');
      const label = document.getElementById('vehicles-count-label');
      if (!tbody || !currentReport) return;

      currentLimit = computeLimit();
      const all = filteredVehicles();
      const totalPages = Math.max(1, Math.ceil(all.length / currentLimit));
      if (currentPage > totalPages) currentPage = totalPages;
      const items = all.slice((currentPage - 1) * currentLimit, currentPage * currentLimit);

      if (label) label.innerText = `Showing ${items.length} of ${all.length} violating vehicles`;

      tbody.innerHTML = '';
      if (items.length === 0) {
        tbody.innerHTML = `
          <tr>
            <td colspan="4" class="px-6 py-12 text-center text-ink2">
              <i class="fa-solid fa-circle-check text-ok text-2xl mb-2 block"></i>
              <strong class="text-ink text-sm">No violations in this report</strong>
              <p class="text-xs text-ink2 mt-1">Nothing matches the selected type or search in ${esc(currentReport.label.toLowerCase())}.</p>
            </td>
          </tr>`;
      } else {
        items.forEach(v => {
          const tr = document.createElement('tr');
          tr.className = 'data-row hover:bg-hover cursor-pointer transition';
          tr.dataset.id = v.id;
          tr.dataset.name = v.vehicle;
          tr.onclick = () => slideToDetail(v.id, v.vehicle);
          const counts = v.violation_counts || {};
          const badges = VIOLATION_TYPES
            .filter(t => counts[t.key] && (currentStatus === 'ALL' || t.key === currentStatus))
            .map(t => typeBadge(t.key, counts[t.key])).join('');
          tr.innerHTML = `
            <td class="px-4 py-3 font-bold text-ink font-mono truncate text-center text-base">
              <span class="inline-block w-2 h-2 rounded-full mr-1.5 bg-brand ring-2 ring-brand-soft"></span>${esc(v.vehicle)}
            </td>
            <td class="px-3 py-3 truncate text-center text-ink font-semibold" data-tip="Driver assigned in Wialon: ${esc(v.driver || 'Unassigned')}">
              <i class="fa-solid fa-id-badge text-ink2 text-sm mr-1.5"></i>${esc(v.driver || 'Unassigned')}
            </td>
            <td class="px-3 py-3 text-center">
              <div class="flex flex-wrap items-center justify-center gap-1.5">${badges}</div>
            </td>
            <td class="px-3 py-3 text-center">
              <button onclick="event.stopPropagation(); const r = this.closest('tr'); slideToDetail(r.dataset.id, r.dataset.name)" class="px-4 py-1.5 bg-brand hover:bg-brand-deep text-ice rounded-lg text-sm font-semibold shadow-sm border border-brand transition">Inspect</button>
            </td>`;
          tbody.appendChild(tr);
        });
      }
      renderPagination(currentPage, totalPages);

      if (limitMode === 'fit' && !refitDone && items.length > 0) {
        refitDone = true;
        if (computeLimit() !== currentLimit) renderTable();
      }
    }

    function changePaginationPage(newPage) {
      currentPage = Math.max(1, newPage);
      renderTable();
    }

    function renderPagination(page, totalPages) {
      const c = document.getElementById('pagination-controls');
      if (!c) return;
      const btn = (target, text, disabled) => disabled
        ? `<button disabled class="opacity-40 cursor-not-allowed px-3 py-1.5 bg-card rounded-lg text-sm text-ink3">${text}</button>`
        : `<button onclick="changePaginationPage(${target})" class="px-3 py-1.5 bg-card border border-edge rounded-lg hover:bg-rail transition text-sm text-ink">${text}</button>`;
      c.innerHTML = `
        ${btn(1, '« First', page <= 1)}
        ${btn(page - 1, '‹ Prev', page <= 1)}
        <span class="px-2 text-sm font-medium">Page <strong class="text-ink">${page}</strong> of ${totalPages}</span>
        ${btn(page + 1, 'Next ›', page >= totalPages)}
        ${btn(totalPages, 'Last »', page >= totalPages)}`;
    }

    function handleSearchInput(e) {
      const clearBtn = document.getElementById('search-clear-btn');
      if (clearBtn) clearBtn.classList.toggle('hidden', !e.target.value.trim());
      currentSearch = e.target.value.trim();
      currentPage = 1;
      renderTable();
    }

    function handleSearchKeyDown(e) {
      if (e.key === 'Escape') clearSearchInput();
    }

    function clearSearchInput() {
      const input = document.getElementById('search-input');
      if (input) input.value = '';
      const clearBtn = document.getElementById('search-clear-btn');
      if (clearBtn) clearBtn.classList.add('hidden');
      currentSearch = '';
      currentPage = 1;
      renderTable();
    }

    function changeFilter() {
      const sel = document.getElementById('status-filter');
      currentStatus = sel ? sel.value : 'ALL';
      currentPage = 1;
      renderTypeCards();
      renderTable();
    }

    function changeLimit() {
      const sel = document.getElementById('limit-select');
      limitMode = sel ? sel.value : 'fit';
      currentPage = 1;
      renderTable();
    }

    // ---------------------------------------------------------------- vehicle inspection
    function reportVehicle(vehicleId) {
      if (!currentReport) return null;
      return currentReport.vehicles.find(v => String(v.id) === String(vehicleId)) || null;
    }

    // The telemetry span a violation was raised on, padded either side (TELEMETRY_PAD_SEC in
    // fleet_violations.py). Fatigue Driving fires at the END of the continuous drive run, so the run's
    // start is derived from it; a Wialon notification alert is a single instant.
    const TELEMETRY_PAD_SEC = 300;
    function telemetryWindow(v) {
      const run = (v.type === 'FATIGUE_DRIVING' && v.continuous_drive_minutes)
        ? Math.round(v.continuous_drive_minutes * 60) : 0;
      return { from: Math.round(v.time_unix) - run - TELEMETRY_PAD_SEC,
               to: Math.round(v.time_unix) + TELEMETRY_PAD_SEC, run };
    }

    // The clock time of an epoch second in PKT, matching the times the backend formats
    function pktClock(epoch) {
      const d = new Date((epoch + 5 * 3600) * 1000);
      return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
    }

    function slideToDetail(vehicleId, vehicleName) {
      selectedVehicleId = vehicleId;
      selectedVehicleName = vehicleName;
      isDetailView = true;
      selectedViolation = null;
      timelineRows = [];
      timelineLoadedFor = null;
      detailType = currentStatus === 'ALL' ? null : currentStatus;

      const title = document.getElementById('drilldown-title');
      if (title) title.innerHTML = `<span>Vehicle Inspection:</span> <span class="font-mono text-accent-deep font-bold">${esc(vehicleName)}</span>`;
      document.getElementById('view-primary').classList.add('hidden');
      document.getElementById('view-detail').classList.remove('hidden');
      setDetailSubView('violations');
      renderTypeCards();
      renderVehicleViolations();
    }

    function slideBackToPrimary() {
      isDetailView = false;
      selectedVehicleId = null;
      selectedVehicleName = null;
      selectedViolation = null;
      document.getElementById('view-detail').classList.add('hidden');
      document.getElementById('view-primary').classList.remove('hidden');
      if ((detailType || 'ALL') !== currentStatus) {
        currentStatus = detailType || 'ALL';
        const sel = document.getElementById('status-filter');
        if (sel) sel.value = currentStatus;
        currentPage = 1;
      }
      detailType = null;
      renderTypeCards();
      renderTable();   // the report is frozen: the same list comes back unchanged
    }

    // ---------------------------------------------------------------- reviews
    // A verdict belongs to vehicle + type + second: the key that survives the nightly recalculation
    function reviewKey(v) { return `${v.vehicle_id}|${v.type}|${v.time_unix}`; }

    // Setting a verdict happens only in the review workbench; everywhere else just shows the result
    function verdictChip(v) {
      const meta = v.verdict === 'GENUINE' ? ['Genuine', 'bg-ok-soft text-ok-deep border-ok']
                 : v.verdict === 'FALSE'   ? ['False', 'bg-danger-soft text-danger border-danger']
                 : ['Pending', 'bg-rail text-ink3 border-edge2'];
      return `<span class="inline-flex px-2 py-0.5 rounded-full text-[0.6875rem] font-bold border ${meta[1]}"
        data-tip="Set in the Review workbench, from the Review button in the header">${meta[0]}</span>`;
    }

""" + review_modal.SCRIPT + """
    // ---------------------------------------------------------------- review workbench
    // The modal itself lives in review_modal.py, shared with the dashboard. This is the portal's
    // adapter: its violations come from the report already in the browser, so opening is instant.
    let pendingDeepLink = null;    // ?review=/?focus= held until the report it needs has loaded

    function allReportViolations() {
      if (!currentReport) return [];
      const out = [];
      currentReport.vehicles.forEach(item => item.violations.forEach(v => out.push(v)));
      return out;
    }

    function pendingCount() {
      return allReportViolations().filter(v => !v.verdict).length;
    }

    function updatePendingBadge() {
      const badge = document.getElementById('review-pending-badge');
      if (!badge) return;
      const n = pendingCount();
      badge.textContent = n > 999 ? '999+' : n;
      badge.classList.toggle('hidden', !currentReport || n === 0);
    }

    Review.host = {
      esc,
      typeLabel,
      typeBadge: key => typeBadge(key, 1),
      toast: (title, message) => showToast(title, message, 'critical'),
      violations: ctx => ctx.plate
        ? allReportViolations().filter(v => v.vehicle === ctx.plate)
        : allReportViolations(),
      period: () => currentReport
        ? { label: currentReport.label, from: currentReport.from, to: currentReport.to } : {},
      // the verdict is already on the shared record, so the rest of the page only needs redrawing
      onVerdict: () => { renderVehicleViolations(); updatePendingBadge(); },
    };

    function openReviewModal() {
      if (!currentReport) { showToast('No report loaded', 'Build or load a report first.', 'info'); return; }
      Review.show({});
    }


    function renderVehicleViolations() {
      const vBody = document.getElementById('drilldown-violations-body');
      const vehicle = reportVehicle(selectedVehicleId);
      const all = vehicle ? vehicle.violations : [];
      const violations = detailType ? all.filter(v => v.type === detailType) : all;
      document.getElementById('detail-violations-count').innerText =
        detailType ? `${violations.length} of ${all.length} · ${typeLabel(detailType)}` : all.length;
      showReportWindow();
      if (!vBody) return;
      if (violations.length === 0) {
        vBody.innerHTML = `<tr><td colspan="8" class="px-6 py-12 text-center text-ink2">
            <i class="fa-solid fa-circle-check text-ok text-2xl mb-2 block"></i>
            <strong class="text-ink text-sm">${detailType ? `No ${esc(typeLabel(detailType))} violations` : 'No Violations Recorded'}</strong>
            <p class="text-xs text-ink2 mt-1">${detailType ? 'Click the highlighted card again to show every type.' : 'This vehicle has no violations of the monitored types in this period.'}</p>
          </td></tr>`;
        return;
      }
      detailViolations = violations;
      vBody.innerHTML = violations.map((v, i) => {
        const mins = v.continuous_drive_minutes;
        // Night runs on a shorter limit than day, so the duration alone does not say whether it broke it

        const warn = v.text_names_other_unit
          ? '<i class="fa-solid fa-triangle-exclamation text-danger mr-1" data-tip="The Wialon text for this alert names a different vehicle"></i>'
          : '';
        const on = selectedViolation && reviewKey(selectedViolation) === reviewKey(v);
        const w = telemetryWindow(v);
        return `
          <tr onclick="openViolationTelemetry(${i})" class="cursor-pointer transition ${on ? 'row-sel' : 'hover:bg-hover'}">
            <td class="px-3 py-3 font-mono text-sm text-ink text-center" data-tip="Click to read the telemetry this alert fired on: ${pktClock(w.from)} to ${pktClock(w.to)} PKT">${esc(v.time)}</td>
            <td class="px-3 py-3 text-center">${typeBadge(v.type, 1)}</td>
            <td class="px-2 py-3 text-center">${speedCell(v)}</td>
            <td class="px-2 py-3 text-center">${ignitionCell(v)}</td>
            <td class="px-2 py-3 text-center font-mono font-bold text-ink" data-tip="${v.period === 'Night' ? 'Night limit, 22:00-06:00 PKT' : (v.period === 'Day' ? 'Day limit, 06:00-22:00 PKT' : '')}">${mins ? driveHours(mins) + periodTag(v.period) : '-'}</td>
            <td class="px-3 py-3 text-ink font-medium text-sm truncate text-center" data-tip="${esc(v.location)} (${v.lat}, ${v.lon})">
              <i class="fa-solid fa-location-dot text-accent-deep mr-1 text-[0.625rem]"></i>${esc(v.location)}
            </td>
            <td class="px-3 py-3 text-ink2 text-xs truncate" data-tip="${esc(v.details)}">${warn}${esc(detailsText(v))}</td>
            <td class="px-2 py-3 text-center">${verdictChip(v)}</td>
          </tr>`;
      }).join('');
    }

    // A violation row opens the stream for the span that alert was raised on
    function openViolationTelemetry(index) {
      const v = detailViolations[index];
      if (!v) return;
      selectedViolation = v;
      renderVehicleViolations();     // move the selected-row marker
      setDetailSubView('timeline');
    }

    function setDetailSubView(view) {
      const btnVio = document.getElementById('btn-sub-violations');
      const btnTime = document.getElementById('btn-sub-timeline');
      const secVio = document.getElementById('drilldown-violations-section');
      const secTime = document.getElementById('drilldown-timeline-section');
      btnVio.classList.toggle('on', view === 'violations');
      btnTime.classList.toggle('on', view !== 'violations');
      if (view === 'violations') {
        secVio.classList.remove('hidden');
        secTime.classList.add('hidden');
        ['btn-timeline-order', 'btn-timeline-excel'].forEach(id => {
          const b = document.getElementById(id);
          if (b) { b.classList.add('hidden'); b.classList.remove('flex'); }
        });
        showReportWindow();
      } else {
        secVio.classList.add('hidden');
        secTime.classList.remove('hidden');
        // opened from the tab rather than a row: show the newest violation's span
        if (!selectedViolation && detailViolations.length) selectedViolation = detailViolations[0];
        setTimelineLabels();
        if (timelineLoadedFor !== timelineKey()) fetchTelemetryStream();
      }
    }

    function timelineKey() {
      const w = selectedViolation ? telemetryWindow(selectedViolation) : null;
      return `${selectedVehicleId}|${w ? w.from : 'live'}|${w ? w.to : 'live'}`;
    }

    const windowChip = (icon, text, tip) =>
      `<span class="window-chip"${tip ? ` data-tip="${esc(tip)}"` : ''}><i class="fa-solid ${icon}"></i>${esc(text)}</span>`;

    // The footer strip while the violations list is showing: the report it belongs to
    function showReportWindow() {
      const tag = document.getElementById('drilldown-window-tag');
      if (!tag || !currentReport) return;
      tag.innerHTML = windowChip('fa-calendar-days', `${currentReport.label}: ${currentReport.from} to ${currentReport.to} PKT`);
    }

    // What the stream is showing, in the tab label and the footer strip
    function setTimelineLabels() {
      const v = selectedViolation;
      const w = v ? telemetryWindow(v) : null;
      const scopeTag = document.getElementById('detail-timeline-scope');
      const windowTag = document.getElementById('drilldown-window-tag');
      if (scopeTag) scopeTag.innerText = w ? `· ${pktClock(w.from)}–${pktClock(w.to)}` : '· last 24h';
      if (!windowTag) return;
      windowTag.innerHTML = w
        ? typeBadge(v.type, 1) +
          windowChip('fa-bolt', `at ${v.time.slice(11)}`, 'When the alert was raised') +
          (w.run ? windowChip('fa-hourglass-half', `${driveHours(v.continuous_drive_minutes)} drive run`, 'The continuous drive the alert was raised on') : '') +
          windowChip('fa-clock', `${pktClock(w.from)} → ${pktClock(w.to)} PKT`, 'The telemetry shown in the stream')
        : windowChip('fa-satellite-dish', 'Live telemetry: the last 24 hours');
    }

    // The stream covers the span the selected violation fired on; with no violation to scope it to
    // (a vehicle with a clean sheet) it falls back to the vehicle's live last 24 hours.
    async function fetchTelemetryStream(forceRefresh = false) {
      if (!selectedVehicleId) return;
      const tBody = document.getElementById('timeline-table-body');
      const refIcon = document.getElementById('btn-inspect-refresh-icon');
      const v = selectedViolation;
      const w = v ? telemetryWindow(v) : null;
      const key = timelineKey();
      const span = w ? `${pktClock(w.from)} to ${pktClock(w.to)} PKT` : 'the last 24 hours';

      if (refIcon) refIcon.classList.add('fa-spin');
      setTimelineLabels();
      if (tBody) tBody.innerHTML = LOADER_ROW(6, `Reading ${span} of telemetry from Wialon...`);
      try {
        let url = `/api/vehicle-report?vehicleId=${encodeURIComponent(selectedVehicleId)}&vehicleName=${encodeURIComponent(selectedVehicleName)}&bypass_cache=${forceRefresh ? 1 : 0}`;
        if (w) url += `&from=${w.from}&to=${w.to}`;
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Could not load telemetry');
        timelineRows = data.timeline || [];      // as the engine returns them: newest first
        timelineLoadedFor = key;
        document.getElementById('detail-packets-count').innerText = timelineRows.length;
        renderTimeline();
      } catch (e) {
        timelineRows = [];
        if (tBody) tBody.innerHTML = `<tr><td colspan="6" class="px-5 py-8 text-center text-danger">${esc(e.message)}</td></tr>`;
      } finally {
        if (refIcon) refIcon.classList.remove('fa-spin');
      }
    }

    // Same workbook as the fleet export, with the stream on screen as its rows
    function telemetryExportUrl() {
      if (!selectedVehicleId || !timelineRows.length) return null;
      const v = selectedViolation;
      const w = v ? telemetryWindow(v) : null;
      let url = `/api/export-telemetry-xlsx?vehicleId=${encodeURIComponent(selectedVehicleId)}` +
                `&vehicleName=${encodeURIComponent(selectedVehicleName)}&order=${timelineOrder}`;
      if (w) {
        url += `&from=${w.from}&to=${w.to}`;
        // the drive run the alert was raised on, so the sheet can flag what is context either side
        if (w.run) url += `&runFrom=${Math.round(v.time_unix) - w.run}&runTo=${Math.round(v.time_unix)}`;
      }
      return url;
    }

    function exportTelemetryToExcel() {
      const url = telemetryExportUrl();
      if (url) window.location.href = url;
    }

    function toggleTimelineOrder() {
      timelineOrder = timelineOrder === 'asc' ? 'desc' : 'asc';
      renderTimeline();
    }

    function renderTimeline() {
      const tBody = document.getElementById('timeline-table-body');
      const btn = document.getElementById('btn-timeline-order');
      const v = selectedViolation;
      const w = v ? telemetryWindow(v) : null;
      const span = w ? `${pktClock(w.from)} to ${pktClock(w.to)} PKT` : 'the last 24 hours';

      const xls = document.getElementById('btn-timeline-excel');
      if (xls) {
        xls.classList.toggle('hidden', !timelineRows.length);
        xls.classList.toggle('flex', !!timelineRows.length);
      }
      if (btn) {
        btn.classList.toggle('hidden', !timelineRows.length);
        btn.classList.toggle('flex', !!timelineRows.length);
        document.getElementById('btn-timeline-order-label').innerText =
          timelineOrder === 'asc' ? 'Oldest first' : 'Newest first';
        document.getElementById('btn-timeline-order-icon').className =
          `fa-solid ${timelineOrder === 'asc' ? 'fa-arrow-down-short-wide' : 'fa-arrow-up-short-wide'} text-[0.625rem]`;
      }
      if (!tBody) return;
      if (!timelineRows.length) {
        tBody.innerHTML = `<tr><td colspan="6" class="px-5 py-8 text-center text-ink2 font-sans">No telemetry messages in ${esc(span)}.</td></tr>`;
        return;
      }

      // The message the alert was raised at, so the trigger row stands out in the stream. One row, not
      // one second: a tracker can log two messages in the same second (an ignition change is recorded
      // alongside the regular reading), and both would otherwise be labelled VIOLATION. Ties go to the
      // first one met in this newest-first list, i.e. the latest reading in that second.
      const markRow = v ? timelineRows.reduce((best, r) =>
        (best === null || Math.abs(r.time_unix - v.time_unix) < Math.abs(best.time_unix - v.time_unix)) ? r : best, null) : null;
      // the run the alert was raised on: everything outside it is the context either side
      const runFrom = w && w.run ? Math.round(v.time_unix) - w.run : null;

      const rows = timelineOrder === 'asc' ? [...timelineRows].reverse() : timelineRows;
      tBody.innerHTML = rows.map(row => {
        const mark = row === markRow;
        const isVio = (mark && v) || (row.status && (row.status.includes('BREACH') || row.status.includes('VIOLATION') || row.status.includes('EXCEEDED')));
        // Name the violation rather than a bare VIOLATION: the alert's own type on the trigger row, else
        // the label the server tagged the row with ("VIOLATION (Overspeed)") or the fatigue engine's breach
        const vioName = !isVio ? '' : (mark && v) ? typeLabel(v.type)
          : ((row.status.match(/VIOLATION [(](.+)[)]/) || [])[1]
             || (row.status.includes('BREACH') || row.status.includes('EXCEEDED') ? typeLabel('FATIGUE_DRIVING') : 'Violation'));
        const spd = row.speed || 0;
        const ign = row.ignition || 'OFF';
        const outside = w && !mark && (runFrom !== null
          ? (row.time_unix < runFrom || row.time_unix > v.time_unix)
          : false);
        const driving = (row.state || '').startsWith('WORK');
        return `
          <tr class="transition ${mark ? 'row-mark' : 'hover:bg-hover'} ${outside ? 'opacity-50' : ''}"
            ${mark ? `data-tip="The message this ${esc(typeLabel(v.type))} alert was raised at"`
                   : (outside ? 'data-tip="Context either side of the drive run, not part of it"' : '')}>
            <td class="px-3 py-2.5 text-center text-ink font-mono text-sm">${esc(row.time)}</td>
            <td class="px-2 py-1.5 text-center">
              <div class="flex items-center justify-center gap-1 font-mono text-xs">
                <span class="px-1.5 py-0.5 rounded font-bold ${spd > 0 ? 'bg-warn-soft text-warn-deep border border-warn/40' : 'bg-rail text-ink3 border border-edge'}">${spd} km/h</span>
                <span class="px-1.5 py-0.5 rounded font-bold ${ign === 'ON' ? 'bg-ok-soft text-ok-deep border border-ok/40' : 'bg-rail text-ink3 border border-edge'}">${ign === 'ON' ? 'IGN ON' : 'IGN OFF'}</span>
              </div>
            </td>
            <td class="px-2 py-1.5 text-center font-mono font-bold text-ink"
              data-tip="${driving ? 'Driven this long without a 00:05 reset stop' : 'This stop has lasted this long; 00:05 clears the drive timer'}">${esc(row.dwell_rest)}</td>
            <td class="px-2 py-1.5 text-center text-xs font-semibold text-ink2">${esc(row.state)}</td>
            <td class="px-3 py-1.5 text-ink font-medium text-xs truncate text-center" data-tip="${esc(row.location)}">${esc(row.location)}</td>
            <td class="px-2 py-1.5 text-center text-xs">
              <span class="inline-block max-w-full px-2 py-0.5 rounded text-[0.625rem] font-bold leading-tight ${isVio ? 'bg-danger text-ice border border-danger' : 'bg-rail text-ink2 border border-edge'}"
                ${isVio ? `data-tip="${esc(vioName)}"` : ''}>${isVio ? esc(vioName) : 'Normal'}</span>
            </td>
          </tr>`;
      }).join('');
    }

    // ---------------------------------------------------------------- export
    function openDashboard() {
      window.location.href = `/dashboard?preset=${encodeURIComponent(currentPreset)}`;
    }

    function exportFleetToExcel() {
      if (!currentReport) return showToast('No report', 'Build a report first.', 'critical');
      showToast('Generating Excel', 'Every violation in this report, formatted for review and printing.');
      window.location.href = `/api/export-xlsx?preset=${encodeURIComponent(currentPreset)}`;
    }


    // ---------------------------------------------------------------- configuration drawer
    async function loadWialonLimits() {
      const box = document.getElementById('wialon-limits');
      if (!box) return;
      try {
        const data = await fetch('/api/wialon-limits').then(r => r.json());
        const limits = data.limits || [];
        box.innerHTML = limits.length === 0
          ? '<span class="text-danger">Could not read the limits from Wialon.</span>'
          : limits.map(l => `
            <div class="flex items-center justify-between gap-3 bg-page border border-edge rounded-lg px-2.5 py-1.5" data-tip="${esc(l.wialon_rule)} - ${esc(l.note)}">
              <span class="truncate ${l.disabled ? 'text-ink3 line-through' : 'text-ink'}">${esc(l.label)}</span>
              <span class="font-mono font-bold ${l.disabled ? 'text-ink3' : 'text-accent-deep'} whitespace-nowrap">${esc(l.value)}</span>
            </div>`).join('');
      } catch (e) {
        box.innerHTML = '<span class="text-danger">Could not read the limits from Wialon.</span>';
      }
    }

    async function loadLafargeConfig() {
      try {
        const cfg = await fetch('/api/config').then(r => r.json());
        document.getElementById('cfg-max-drive-day').value = cfg.max_continuous_drive_day || "02:30";
        document.getElementById('cfg-max-drive-night').value = cfg.max_continuous_drive_night || "02:00";
        document.getElementById('cfg-min-break-day').value = cfg.min_break_day || "00:15";
        document.getElementById('cfg-min-break-night').value = cfg.min_break_night || "00:15";
        document.getElementById('cfg-min-reset-stop').value = cfg.min_reset_stop || "00:05";
      } catch (e) {
        console.error("Failed to load config:", e);
      }
    }

    async function saveLafargeConfig(e) {
      e.preventDefault();
      const payload = {
        max_continuous_drive_day: document.getElementById('cfg-max-drive-day').value,
        max_continuous_drive_night: document.getElementById('cfg-max-drive-night').value,
        min_break_day: document.getElementById('cfg-min-break-day').value,
        min_break_night: document.getElementById('cfg-min-break-night').value,
        min_reset_stop: document.getElementById('cfg-min-reset-stop').value
      };
      try {
        const res = await fetch('/api/config', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.message || 'Failed to save configuration.');
        toggleConfigDrawer(false);
        showToast('Configuration saved', 'Rebuilding the report with the new limits.');
        loadReport();
      } catch (err) {
        showToast('Error', err.message, 'critical');
      }
    }

    window.addEventListener('DOMContentLoaded', () => {
      setupTooltips();
      Review.bind();
      const params = new URLSearchParams(window.location.search);
      const askedPreset = (params.get('preset') || '').toUpperCase();
      currentPreset = PRESET_LABELS[askedPreset] ? askedPreset : '1D';
      // a link in from the dashboard; held until showReport() has the period it refers to
      if (params.get('review')) {
        pendingDeepLink = { plate: params.get('review'), focus: params.get('focus') || null };
        history.replaceState(null, '', `/?preset=${currentPreset}`);
      }
      setPeriodButtons();
      loadReport(currentPreset);

      let resizeTimer = null;
      window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
          if (limitMode === 'fit' && !isDetailView && currentReport) {
            refitDone = false;
            renderTable();
          }
        }, 250);
      });
    });
  </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(PORTAL_HTML)


# The archive starts with the process, not with the first page load. Under waitress there may be no
# request for hours, and the whole point is that the shortcuts and the alert queue are already warm
# by the time someone opens the portal: prewarm() fills REPORTS._built, warm_payloads() fills
# PAYLOAD_CACHE, and the alert worker drains whatever the archive queued. Both calls spawn one daemon
# thread and are idempotent, so the before_request hook above is now only a fallback.
#
# Keep this a single process. _day_lock is a threading lock and drain() has no cross-process claim, so
# two processes would race on archiving a day and could send an alert twice; both caches are in memory
# as well, so a second process would serve cold. Waitress is threaded, not forking, which suits this.
#
# Werkzeug's reloader imports this module in the parent as well; only the serving child runs the job.
_RELOADER_PARENT = __name__ == "__main__" and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
if not _RELOADER_PARENT:
    REPORTS.start_scheduler()
    ALERTS.start_worker(fatigue_violations_by_key)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 70)
    print("  Vehicle Operations & Compliance Portal (development server)")
    print(f"  Local URL: http://localhost:{port}")
    print("  Deployment: waitress-serve --host=127.0.0.1 --port=5000 --threads=8 app:app")
    print("=" * 70 + "\n")
    # Bound to localhost, not 0.0.0.0: debug=True serves the Werkzeug console, which executes Python on
    # this machine for anyone who can reach a traceback. Anything outside this box (a tunnel included)
    # should go through waitress, which has no such console.
    #
    # "stat" checks only the source files the app has loaded. The default watchdog reloader watches whole
    # folders (Python's own included) and on Windows restarts whenever any Python process writes a cache
    # file there, which killed the archive job mid-day and left the portal unreachable while it wound down.
    app.run(host="127.0.0.1", port=port, debug=True, reloader_type="stat")
