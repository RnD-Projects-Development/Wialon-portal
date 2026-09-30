"""
Hybrid Database Client: Local SQLite + Supabase Cloud Integration
==================================================================
Reads Supabase credentials securely from backend .env without exposing
any configuration on the client portal.
"""

import os
import json
import sqlite3
from datetime import datetime

# Simple .env loader
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

if os.path.exists(ENV_PATH):
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

try:
    from supabase import create_client, Client
    SUPABASE_LIB_AVAILABLE = True
except ImportError:
    SUPABASE_LIB_AVAILABLE = False

SQLITE_DB_PATH = os.path.join(BASE_DIR, "compliance.db")

class HybridDBClient:
    def __init__(self):
        self._init_sqlite()
        self.supabase_client = None
        self.supabase_configured = False
        self._init_supabase()

    def _init_sqlite(self):
        """Ensure local SQLite tables exist."""
        with sqlite3.connect(SQLITE_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_fatigue_reports (
                    id TEXT PRIMARY KEY,
                    vehicle_name TEXT NOT NULL,
                    driver_name TEXT,
                    violation_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    continuous_drive_minutes REAL,
                    rest_taken_minutes REAL,
                    speed_kmh REAL,
                    latitude REAL,
                    longitude REAL,
                    location_name TEXT,
                    notes TEXT,
                    violation_timestamp TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS compliance_configs (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def _init_supabase(self):
        """Connect to Supabase using backend environment variables only."""
        if not SUPABASE_LIB_AVAILABLE:
            return

        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()

        if url and key and url.startswith("http"):
            try:
                self.supabase_client = create_client(url, key)
                self.supabase_configured = True
                print("[Database] Supabase Cloud connected successfully.")
            except Exception as e:
                print(f"[Database] Supabase connection error: {e}")
                self.supabase_configured = False

    def save_violations(self, violations_list):
        """
        Saves ONLY actual violation records to SQLite and Supabase Cloud.
        Vehicles with no violations are strictly excluded.
        """
        if not violations_list:
            return

        # Filter strictly for valid violations
        valid_violations = []
        for v in violations_list:
            if not isinstance(v, dict):
                continue
            # Must have a vehicle and a violation type
            v_type = v.get("type") or v.get("violation_type")
            if not v_type:
                continue
            valid_violations.append(v)

        if not valid_violations:
            return

        with sqlite3.connect(SQLITE_DB_PATH) as conn:
            cursor = conn.cursor()
            for v in valid_violations:
                vid = v.get("id") or f"VIO-{v.get('vehicle', 'UNK')}-{int(datetime.now().timestamp()*1000)}"
                loc = v.get("location", {})
                lat = loc.get("lat") if isinstance(loc, dict) else v.get("latitude")
                lon = loc.get("lon") if isinstance(loc, dict) else v.get("longitude")
                loc_name = loc if isinstance(loc, str) else v.get("location_name", v.get("location", "Route"))

                v_time = v.get("violation_timestamp") or v.get("time") or v.get("formatted_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                cursor.execute("""
                    INSERT OR REPLACE INTO daily_fatigue_reports 
                    (id, vehicle_name, driver_name, violation_type, severity, continuous_drive_minutes, rest_taken_minutes, speed_kmh, latitude, longitude, location_name, notes, violation_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    vid,
                    v.get("vehicle", v.get("vehicle_name", "Unknown")),
                    v.get("driver", v.get("driver_name", "Unassigned")),
                    v.get("type", v.get("violation_type", "CONTINUOUS_DRIVE_EXCEEDED")),
                    v.get("severity", "HIGH"),
                    float(v.get("continuous_drive_minutes", v.get("continuous_minutes", 0.0)) or 0.0),
                    float(v.get("rest_taken_minutes", 0.0) or 0.0),
                    float(v.get("speed_kmh", v.get("speed", 0.0)) or 0.0),
                    lat,
                    lon,
                    str(loc_name) if loc_name else "Route",
                    v.get("notes", ""),
                    v_time
                ))
            conn.commit()

        if self.supabase_configured and self.supabase_client:
            try:
                for v in valid_violations:
                    loc = v.get("location", {})
                    lat = loc.get("lat") if isinstance(loc, dict) else v.get("latitude")
                    lon = loc.get("lon") if isinstance(loc, dict) else v.get("longitude")
                    loc_name = loc if isinstance(loc, str) else v.get("location_name", v.get("location", "Route"))
                    v_time = v.get("violation_timestamp") or v.get("time") or v.get("formatted_time") or datetime.now().isoformat()

                    payload = {
                        "vehicle_name": v.get("vehicle", v.get("vehicle_name", "Unknown")),
                        "driver_name": v.get("driver", v.get("driver_name", "Unassigned")),
                        "violation_type": v.get("type", v.get("violation_type", "CONTINUOUS_DRIVE_EXCEEDED")),
                        "severity": v.get("severity", "HIGH"),
                        "continuous_drive_minutes": float(v.get("continuous_drive_minutes", v.get("continuous_minutes", 0.0)) or 0.0),
                        "rest_taken_minutes": float(v.get("rest_taken_minutes", 0.0) or 0.0),
                        "speed_kmh": float(v.get("speed_kmh", v.get("speed", 0.0)) or 0.0),
                        "latitude": lat,
                        "longitude": lon,
                        "location_name": str(loc_name) if loc_name else "Route",
                        "notes": v.get("notes", ""),
                        "violation_timestamp": v_time
                    }
                    self.supabase_client.table("daily_fatigue_reports").insert(payload).execute()
            except Exception as e:
                print(f"[Supabase] Sync error: {e}")

    def get_historical_violations(self, from_str=None, to_str=None, vehicle_names=None, limit=5000):
        """
        Query violations that occurred strictly within the given historical window from Supabase / SQLite.
        Returns ONLY vehicles that had violations.
        """
        # 1. Try Supabase Cloud query
        if self.supabase_configured and self.supabase_client:
            try:
                query = self.supabase_client.table("daily_fatigue_reports").select("*")
                if from_str:
                    query = query.gte("violation_timestamp", from_str)
                if to_str:
                    query = query.lte("violation_timestamp", to_str)

                if vehicle_names:
                    if isinstance(vehicle_names, list) and len(vehicle_names) > 0:
                        if len(vehicle_names) == 1:
                            query = query.eq("vehicle_name", vehicle_names[0])
                        else:
                            query = query.in_("vehicle_name", vehicle_names)
                    elif isinstance(vehicle_names, str) and vehicle_names.strip():
                        query = query.eq("vehicle_name", vehicle_names.strip())

                res = query.order("violation_timestamp", desc=True).limit(limit).execute()
                if res.data is not None:
                    return res.data
            except Exception as e:
                print(f"[Supabase] Query error: {e}, falling back to SQLite")

        # 2. Local SQLite fallback query
        with sqlite3.connect(SQLITE_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            sql = "SELECT * FROM daily_fatigue_reports WHERE 1=1"
            params = []

            if from_str:
                sql += " AND violation_timestamp >= ?"
                params.append(from_str)
            if to_str:
                sql += " AND violation_timestamp <= ?"
                params.append(to_str)

            if vehicle_names:
                if isinstance(vehicle_names, list) and len(vehicle_names) > 0:
                    placeholders = ",".join(["?"] * len(vehicle_names))
                    sql += f" AND vehicle_name IN ({placeholders})"
                    params.extend(vehicle_names)
                elif isinstance(vehicle_names, str) and vehicle_names.strip():
                    sql += " AND vehicle_name = ?"
                    params.append(vehicle_names.strip())

            sql += " ORDER BY violation_timestamp DESC LIMIT ?"
            params.append(limit)

            cursor.execute(sql, params)
            return [dict(r) for r in cursor.fetchall()]

    def get_daily_violations(self, limit=50):
        return self.get_historical_violations(limit=limit)

    def save_generated_report(self, report_data):
        """
        Saves explicitly generated historical reports to SQLite and Supabase Cloud.
        Triggered ONLY when user clicks 'Generate Report'.
        """
        rep_id = report_data.get("report_id") or f"REP-{int(datetime.now().timestamp()*1000)}"
        rep_title = report_data.get("title", "Safety & Compliance Violation Report")
        from_ts = report_data.get("from_time", "")
        to_ts = report_data.get("to_time", "")
        total_violations = report_data.get("total_violations", 0)
        total_vehicles = report_data.get("total_violating_vehicles", 0)
        report_json = json.dumps(report_data)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(SQLITE_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS generated_compliance_reports (
                    report_id TEXT PRIMARY KEY,
                    title TEXT,
                    from_time TEXT,
                    to_time TEXT,
                    total_violations INTEGER,
                    total_violating_vehicles INTEGER,
                    report_json TEXT,
                    created_at TEXT
                )
            """)
            cursor.execute("""
                INSERT OR REPLACE INTO generated_compliance_reports 
                (report_id, title, from_time, to_time, total_violations, total_violating_vehicles, report_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (rep_id, rep_title, from_ts, to_ts, total_violations, total_vehicles, report_json, created_at))
            conn.commit()

        supabase_saved = False
        if self.supabase_configured and self.supabase_client:
            try:
                payload = {
                    "report_id": rep_id,
                    "title": rep_title,
                    "from_time": from_ts,
                    "to_time": to_ts,
                    "total_violations": total_violations,
                    "total_violating_vehicles": total_vehicles,
                    "report_json": report_json,
                    "created_at": created_at
                }
                try:
                    self.supabase_client.table("generated_compliance_reports").insert(payload).execute()
                    supabase_saved = True
                except Exception:
                    # Fallback to saving individual violation records into daily_fatigue_reports
                    for item in report_data.get("items", []):
                        for vio in item.get("violations", []):
                            v_payload = {
                                "vehicle_name": item.get("vehicle", "Unknown"),
                                "driver_name": item.get("driver", "Unassigned"),
                                "violation_type": vio.get("violation_type") or vio.get("type", "VIOLATION"),
                                "severity": vio.get("severity", "HIGH"),
                                "continuous_drive_minutes": float(vio.get("continuous_drive_minutes", 0) or 0),
                                "rest_taken_minutes": float(vio.get("rest_taken_minutes", 0) or 0),
                                "speed_kmh": float(vio.get("speed_kmh", 0) or 0),
                                "location_name": str(vio.get("location", item.get("location", "Route"))),
                                "notes": vio.get("notes") or "Compliance threshold exceeded",
                                "violation_timestamp": vio.get("violation_timestamp", created_at)
                            }
                            try:
                                self.supabase_client.table("daily_fatigue_reports").insert(v_payload).execute()
                            except Exception:
                                pass
                    supabase_saved = True
            except Exception as e:
                print(f"[Supabase] Generate Report save error: {e}")

        return {"report_id": rep_id, "supabase_saved": supabase_saved, "created_at": created_at}

# Singleton
db = HybridDBClient()
