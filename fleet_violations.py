"""
Fleet Violations Monitor
========================
Keeps the last 24 hours of telemetry for every vehicle in memory and derives the
portal's violation types from it:

  Fatigue Driving        fatigue_engine.analyze_telemetry_stream (continuous driving
                         limit), run on every telemetry message of every vehicle
  Wialon alert types     violations registered by the Wialon notifications, read from
                         each unit's event history (p.evt_name)

A background thread refreshes everything every few minutes. Telemetry is fetched with
messages/get_messages filtered to the fields the engine reads (speed, lat, lon, ignition).
After each refresh the per-vehicle message count is compared with Wialon's own count for
the window, and any vehicle that differs (e.g. a tracker uploading buffered messages
late) is downloaded again, so every message is part of the calculation.
"""

import bisect
import gzip
import json
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import fatigue_engine
from wialon_alerts import SessionPool, load_env, speed_from_text

PKT = timezone(timedelta(hours=5), "PKT")
WINDOW_SEC = 24 * 3600
REFRESH_SEC = 5 * 60
REFETCH_OVERLAP_SEC = 15 * 60   # re-read this much recent history each cycle
BATCH_UNITS = 10                # vehicles per core/batch telemetry request
WORKERS = 4
TELEMETRY_FILTER = "pos.s,pos.x,pos.y,p.io_239,p.io_16"
TELEMETRY_MATCH_SEC = 10 * 60   # telemetry shown for a violation must be at most this much older
CACHE_VERSION = 2               # bump when the cached message row layout changes

# Display order matches the portal's violation chart. Descriptions of the Wialon types restate the
# notification rules as configured in the tpl_unilever resource; Fatigue Driving's text is built from
# compliance_config.json at request time.
VIOLATION_TYPES = [
    {"key": "FATIGUE_DRIVING", "label": "Fatigue Driving", "evt_name": None,
     "description": "Continuous driving past the limit without a long enough break"},
    {"key": "NIGHT_DRIVING", "label": "Night Driving", "evt_name": "Night Time Driving",
     "description": "Ignition on inside an Interchange Exit geofence, 22:00-05:59"},
    {"key": "OVERSPEED", "label": "Overspeed", "evt_name": "Overspeed",
     "description": "Above 85 km/h off highways and motorways"},
    {"key": "OVERSPEED_HIGHWAY", "label": "Overspeed-Highway", "evt_name": "Overspeed-Highway",
     "description": "Above 105 km/h on a highway"},
    {"key": "OVERSPEED_MOTORWAY", "label": "Overspeed-Motorway", "evt_name": "Overspeed-Motorway",
     "description": "Above 125 km/h on a motorway"},
    {"key": "DELAY_DRIVER_SEAT_BELT", "label": "Delay Driver Seat Belt", "evt_name": "Delay Driver Seat Belt",
     "description": "Seat belt not fastened for 5 minutes while above 20 km/h"},
    {"key": "SEAT_BELT_DISCONNECTED", "label": "Seatbelt Disconnected", "evt_name": "Driver Seat Belt Disconnected",
     "description": "Driver unbuckled the belt after it had been fastened (sensor went fastened -> open)"},
    {"key": "SEAT_BELT_IGNITION_OFF", "label": "Seatbelt On - Ignition Off",
     "evt_name": "Seat Belt ('Ignition Off - Seatbelt On')",
     "description": "Belt reads buckled with ignition off for 1 hour (bypass or faulty sensor)"},
]
TYPE_LABELS = {t["key"]: t["label"] for t in VIOLATION_TYPES}
EVT_NAME_TO_TYPE = {t["evt_name"]: t["key"] for t in VIOLATION_TYPES if t["evt_name"]}


def fmt_time(ts):
    """Unix time -> Pakistan time string (independent of the server's timezone)."""
    return datetime.fromtimestamp(ts, PKT).strftime("%Y-%m-%d %H:%M:%S")


def odometer_km(raw):
    """Same conversion as the portal's vehicle list: io_16 above 10,000 is metres."""
    if raw is None or float(raw) <= 0:
        return None
    raw = float(raw)
    return round(raw * 0.001, 1) if raw > 10000 else round(raw, 1)


def message_row(m):
    """Compact telemetry row: (t, speed, ignition, lat, lon, odometer_raw)."""
    pos = m.get("pos") or {}
    p = m.get("p") or {}
    return (m["t"], pos.get("s") or 0, p.get("io_239"), pos.get("y"), pos.get("x"), p.get("io_16"))


def telemetry_at(rows, ts):
    """The telemetry message at the violation time, or the last one before it (within TELEMETRY_MATCH_SEC),
    skipping readings the vehicle drove through (tracker ignition dropouts, see fatigue_engine)."""
    ignored = fatigue_engine.false_stop_indices(rows)
    i = bisect.bisect_right(rows, (ts, float("inf"))) - 1
    while i >= 0 and i in ignored:
        i -= 1
    if i < 0 or ts - rows[i][0] > TELEMETRY_MATCH_SEC:
        return None
    t, speed, ign, lat, lon, odo = rows[i]
    return {"time": fmt_time(t), "time_unix": t, "speed_kmh": speed, "ignition": ign, "engine_on": ign == 1,
            "odometer_km": odometer_km(odo), "lat": lat, "lon": lon}


def telemetry_call(uid, time_from, time_to):
    return [
        {"svc": "messages/load_interval", "params": {"itemId": uid, "timeFrom": time_from, "timeTo": time_to,
                                                     "flags": 0x0001, "flagsMask": 0xFF01, "loadCount": 0}},
        {"svc": "messages/get_messages", "params": {"indexFrom": 0, "indexTo": 0xFFFFFFFF, "filter": TELEMETRY_FILTER}},
        {"svc": "messages/unload", "params": {}},
    ]


def event_violation(units, uid, m, index, drivers):
    text = m.get("et") or ""
    name = units.get(uid, str(uid))
    vtype = EVT_NAME_TO_TYPE[m["p"]["evt_name"]]
    near = re.search(r"near '([^']*)", text)
    return {
        "id": f"EVT-{uid}-{m['t']}-{index}",
        "type": vtype,
        "type_label": TYPE_LABELS[vtype],
        "source": "wialon_notification",
        "time_unix": m["t"],
        "time": fmt_time(m["t"]),
        "vehicle": name,
        "vehicle_id": uid,
        "driver": drivers.get(uid, "Unassigned"),
        "speed_kmh": speed_from_text(text),
        "continuous_drive_minutes": None,
        "rest_minutes": None,
        "lat": m.get("y"),
        "lon": m.get("x"),
        "address": near.group(1).strip() if near else None,
        "details": text,
        # Wialon fills the plate into the text at trigger time; a different plate needs checking
        "text_names_other_unit": bool(text) and name.replace(" ", "") not in text.replace(" ", ""),
    }

def fatigue_violation(uid, v):
    lat, lon = (float(x) for x in v["location"].split(","))
    # the engine formats times in the server's local zone; go back to unix, then show PKT
    ts = int(time.mktime(time.strptime(v["time"], "%Y-%m-%d %H:%M:%S")))
    return {
        "id": v["id"],
        "type": "FATIGUE_DRIVING",
        "type_label": TYPE_LABELS["FATIGUE_DRIVING"],
        "source": "telemetry",
        "time_unix": ts,
        "time": fmt_time(ts),
        "vehicle": v["vehicle"],
        "vehicle_id": uid,
        "driver": v["driver"],
        "speed_kmh": v["speed_kmh"],
        "continuous_drive_minutes": v["continuous_drive_minutes"],
        # the stop taken after the limit was reached (0 when the driver never stopped)
        "rest_minutes": v.get("rest_taken_minutes"),
        # which limit applied at the mark: Night (22:00-06:00) is shorter than Day
        "period": v.get("period"),
        "lat": lat,
        "lon": lon,
        "address": None,
        "details": v["notes"],
        "text_names_other_unit": False,
    }


class FleetViolationMonitor:
    def __init__(self, cache_path, driver_lookup=None):
        env = load_env()
        self.base_url = env.get("WIALON_BASE_URL", "https://hst-api.wialon.eu")
        self.token = env["WIALON_TOKEN"]
        self.cache_path = cache_path
        self.driver_lookup = driver_lookup or (lambda: {})
        self.sessions = SessionPool(self.base_url)

        self.units = {}        # unit id -> name
        self.messages = {}     # unit id -> [(t, speed, ignition, lat, lon)] sorted by t, every message in the window
        self.violations = {}   # unit id -> [violation dict]
        self.fetched_to = 0
        self.status = {"state": "starting", "units_total": 0, "units_loaded": 0, "last_refresh": None,
                       "window_from": None, "window_to": None, "refetched_units": 0, "error": None}

        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._thread = None

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="fleet-violations", daemon=True)
            self._thread.start()

    def _run(self):
        self._load_cache()
        while True:
            started = time.time()
            try:
                self.refresh()
            except Exception as e:
                traceback.print_exc()
                self.status["error"] = str(e)
                if self.status["state"] != "ready":
                    self.status["state"] = "error"
            time.sleep(max(30, REFRESH_SEC - (time.time() - started)))

    def _session(self):
        """One Wialon session, reused (see wialon_alerts.SessionPool)."""
        return self.sessions.use()

    # ------------------------------------------------------------------ Wialon fetches
    def _fetch_telemetry(self, unit_ids, time_from, time_to, progress=False):
        """Every data message with position for the given units, in [time_from, time_to]."""
        chunks = [unit_ids[i:i + BATCH_UNITS] for i in range(0, len(unit_ids), BATCH_UNITS)]
        result = {}

        def fetch_chunk(s, chunk):
            calls = [c for uid in chunk for c in telemetry_call(uid, time_from, time_to)]
            res = s.call("core/batch", {"params": calls, "flags": 0})
            out = {}
            for k, uid in enumerate(chunk):
                loaded, msgs = res[3 * k], res[3 * k + 1]
                if not isinstance(loaded, dict) or "count" not in loaded:
                    raise RuntimeError(f"load_interval failed for unit {uid}: {loaded}")
                if loaded["count"] == 0:
                    out[uid] = []
                    continue
                if not isinstance(msgs, list) or len(msgs) != loaded["count"]:
                    got = len(msgs) if isinstance(msgs, list) else msgs
                    raise RuntimeError(f"unit {uid}: Wialon loaded {loaded['count']} messages but returned {got}")
                rows = sorted((message_row(m) for m in msgs), key=lambda r: r[0])
                out[uid] = rows
            return out

        def work(my_chunks):
            with self._session() as s:
                for chunk in my_chunks:
                    for attempt in range(3):
                        try:
                            result.update(fetch_chunk(s, chunk))
                            break
                        except Exception:
                            if attempt == 2:
                                raise
                            time.sleep(5)      # a timed-out session re-opens itself on the next call
                    if progress:
                        self.status["units_loaded"] += len(chunk)

        with ThreadPoolExecutor(WORKERS) as ex:
            futures = [ex.submit(work, chunks[i::WORKERS]) for i in range(WORKERS)]
            for f in futures:
                f.result()
        return result

    def _server_counts(self, s, unit_ids, time_from, time_to):
        counts = {}
        for i in range(0, len(unit_ids), 100):
            chunk = unit_ids[i:i + 100]
            calls = [{"svc": "messages/load_interval", "params": {"itemId": uid, "timeFrom": time_from, "timeTo": time_to,
                                                                   "flags": 0x0001, "flagsMask": 0xFF01, "loadCount": 0}}
                     for uid in chunk]
            for uid, r in zip(chunk, s.call("core/batch", {"params": calls, "flags": 0})):
                counts[uid] = r.get("count") if isinstance(r, dict) else None
        s.call("messages/unload", {})
        return counts

    def _fetch_events(self, s, unit_ids, time_from, time_to, drivers):
        """Violation events (flags 0x0601) registered by the Wialon notifications we display."""
        events = {}
        for i in range(0, len(unit_ids), 100):
            chunk = unit_ids[i:i + 100]
            calls = [{"svc": "messages/load_interval", "params": {"itemId": uid, "timeFrom": time_from, "timeTo": time_to,
                                                                   "flags": 0x0601, "flagsMask": 0xFF01, "loadCount": 0xFFFFFFFF}}
                     for uid in chunk]
            for uid, r in zip(chunk, s.call("core/batch", {"params": calls, "flags": 0})):
                if not isinstance(r, dict) or "messages" not in r:
                    raise RuntimeError(f"event history failed for unit {uid}: {r}")
                events[uid] = [self._event_violation(uid, m, k, drivers) for k, m in enumerate(r["messages"])
                               if (m.get("p") or {}).get("evt_name") in EVT_NAME_TO_TYPE]
        s.call("messages/unload", {})
        return events

    # ------------------------------------------------------------------ records
    def _event_violation(self, uid, m, index, drivers):
        return event_violation(self.units, uid, m, index, drivers)

    def _fatigue_violation(self, uid, v):
        return fatigue_violation(uid, v)

    def _analyze(self, uid, rows, window_from, drivers):
        stream = [{"t": t, "pos": {"s": s, "y": y if y is not None else 0.0, "x": x if x is not None else 0.0},
                   "p": {"io_239": ign}}
                  for (t, s, ign, y, x, _odo) in rows if t >= window_from]
        result = fatigue_engine.analyze_telemetry_stream(stream, vehicle_name=self.units.get(uid, str(uid)),
                                                         driver_name=drivers.get(uid, "Unassigned"))
        # one timeline row per message, newest first: show each row's time in PKT
        for row, m in zip(result["timeline"], reversed(stream)):
            row["time"] = fmt_time(m["t"])
        fatigue = [self._fatigue_violation(uid, v) for v in result["violations"]
                   if v.get("violation_type") == "CONTINUOUS_DRIVE_EXCEEDED"]
        return result, fatigue

    @staticmethod
    def _with_telemetry(violations, rows):
        for v in violations:
            v["telemetry"] = telemetry_at(rows, v["time_unix"])
        return sorted(violations, key=lambda v: v["time_unix"], reverse=True)

    # ------------------------------------------------------------------ store
    def _merge(self, fetched, replace_from, window_from):
        with self._lock:
            for uid, rows in fetched.items():
                old = self.messages.get(uid, [])
                keep_from = bisect.bisect_left(old, (window_from,))
                cut = bisect.bisect_left(old, (replace_from,))
                self.messages[uid] = old[keep_from:max(keep_from, cut)] + rows

    def _count_in(self, uid, time_from, time_to):
        rows = self.messages.get(uid, [])
        return bisect.bisect_left(rows, (time_to + 1,)) - bisect.bisect_left(rows, (time_from,))

    def _drivers(self):
        try:
            return self.driver_lookup() or {}
        except Exception:
            return {}

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        with self._refresh_lock:
            now = int(time.time())
            window_from = now - WINDOW_SEC

            with self._session() as s:
                self.units = {u["id"]: u["nm"] for u in s.search("avl_unit", 1)}
            unit_ids = list(self.units)
            self.status["units_total"] = len(unit_ids)
            drivers = self._drivers()

            warming = not self.fetched_to or self.fetched_to < window_from
            if warming:
                self.status.update(state="warming_up", units_loaded=0)
                replace_from = window_from
            else:
                replace_from = self.fetched_to - REFETCH_OVERLAP_SEC
            self._merge(self._fetch_telemetry(unit_ids, replace_from, now, progress=warming), replace_from, window_from)

            with self._session() as s:
                server = self._server_counts(s, unit_ids, window_from, now)
                with self._lock:
                    stale = [uid for uid in unit_ids
                             if server.get(uid) is not None and server[uid] != self._count_in(uid, window_from, now)]
                if stale:
                    self._merge(self._fetch_telemetry(stale, window_from, now), window_from, window_from)
                events = self._fetch_events(s, unit_ids, window_from, now, drivers)

            with self._lock:
                rows_by_unit = {uid: self.messages.get(uid, []) for uid in unit_ids}
            violations = {}
            for uid in unit_ids:
                _, fatigue = self._analyze(uid, rows_by_unit[uid], window_from, drivers)
                vios = fatigue + events.get(uid, [])
                if vios:
                    violations[uid] = self._with_telemetry(vios, rows_by_unit[uid])

            with self._lock:
                self.violations = violations
                self.fetched_to = now
            self.status.update(state="ready", units_loaded=len(unit_ids), last_refresh=fmt_time(now),
                               window_from=fmt_time(window_from), window_to=fmt_time(now),
                               refetched_units=len(stale), messages=sum(len(r) for r in rows_by_unit.values()),
                               error=None)
            self._save_cache()

    def vehicle_report(self, uid):
        """Re-read one vehicle's full window from Wialon, recompute it, and return engine output + violations."""
        now = int(time.time())
        window_from = now - WINDOW_SEC
        drivers = self._drivers()
        if uid not in self.units:
            with self._session() as s:
                self.units = {u["id"]: u["nm"] for u in s.search("avl_unit", 1)}

        with self._session() as s:
            res = s.call("core/batch", {"params": telemetry_call(uid, window_from, now), "flags": 0})
            events = self._fetch_events(s, [uid], window_from, now, drivers)[uid]
        loaded, msgs = res[0], res[1]
        if not isinstance(loaded, dict) or "count" not in loaded:
            raise RuntimeError(f"load_interval failed for unit {uid}: {loaded}")
        if loaded["count"] and (not isinstance(msgs, list) or len(msgs) != loaded["count"]):
            raise RuntimeError(f"unit {uid}: Wialon loaded {loaded['count']} messages but returned a different amount")
        rows = sorted((message_row(m) for m in (msgs if loaded["count"] else [])), key=lambda r: r[0])

        result, fatigue = self._analyze(uid, rows, window_from, drivers)
        vios = self._with_telemetry(fatigue + events, rows)
        with self._lock:
            self.messages[uid] = rows
            if vios:
                self.violations[uid] = vios
            else:
                self.violations.pop(uid, None)
        return {"window_from": window_from, "window_to": now, "result": result, "violations": vios,
                "messages": len(rows)}

    def snapshot(self):
        with self._lock:
            return {"violations": {uid: list(v) for uid, v in self.violations.items()},
                    "units": dict(self.units), "status": dict(self.status)}

    # ------------------------------------------------------------------ disk cache (fast restarts)
    def _save_cache(self):
        if not self.cache_path:
            return
        with self._lock:
            data = {"version": CACHE_VERSION, "saved_at": int(time.time()), "fetched_to": self.fetched_to, "units": self.units,
                    "messages": {str(uid): rows for uid, rows in self.messages.items()}}
        tmp = self.cache_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=1) as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, self.cache_path)
        except Exception as e:
            print(f"[FleetViolations] cache save failed: {e}")

    def _load_cache(self):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return
        try:
            with gzip.open(self.cache_path, "rt", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != CACHE_VERSION or data.get("fetched_to", 0) < time.time() - WINDOW_SEC:
                return
            with self._lock:
                self.units = {int(k): v for k, v in data["units"].items()}
                self.messages = {int(uid): [tuple(r) for r in rows] for uid, rows in data["messages"].items()}
                self.fetched_to = data["fetched_to"]
            print(f"[FleetViolations] loaded telemetry cache from {fmt_time(data['saved_at'])}")
        except Exception as e:
            print(f"[FleetViolations] cache load failed, doing a full load: {e}")
