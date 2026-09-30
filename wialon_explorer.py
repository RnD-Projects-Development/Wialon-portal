"""
Wialon API Explorer — Hit Every Useful Endpoint & Dump JSON Responses
=====================================================================

This script systematically calls every analytically-relevant Wialon API
endpoint and saves the JSON response to individual files.

Usage:
    python wialon_explorer.py --token YOUR_TOKEN_HERE
    python wialon_explorer.py --token YOUR_TOKEN_HERE --base-url https://hst-api.wialon.eu

Output:
    ./wialon_output/
        01_login.json
        02_search_all_units.json
        03_search_all_resources.json
        ...etc
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import ssl
from datetime import datetime, timedelta

# Force UTF-8 output on Windows terminals
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_BASE = "https://hst-api.wialon.eu"
OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "wialon_api_explorer", "wialon_output")

# How many units to pull detailed data for (to avoid hammering the API)
MAX_UNITS_DETAIL = 5
# How many days of history to load messages / trips for
HISTORY_DAYS = 3
# Max messages to load per unit
MAX_MESSAGES = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class WialonAPI:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/wialon/ajax.html"
        self.token = token
        self.sid = None  # session id (eid)
        self.step = 0
        # Allow unverified SSL if needed (some corp proxies)
        self.ssl_ctx = ssl.create_default_context()

    def call(self, svc: str, params: dict, label: str = None) -> dict:
        """Make an API call and return the parsed JSON response."""
        self.step += 1
        label = label or svc
        filename = f"{self.step:02d}_{label.replace('/', '_').replace(' ', '_')}.json"

        query = {"svc": svc, "params": json.dumps(params)}
        if self.sid:
            query["sid"] = self.sid

        url = f"{self.api_url}?{urllib.parse.urlencode(query)}"

        print(f"  [{self.step:02d}] {svc:<45} ", end="", flush=True)

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60, context=self.ssl_ctx) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"_raw_response": raw}

            # Check for Wialon error
            if isinstance(data, dict) and "error" in data and data["error"] != 0:
                error_msg = data.get("reason", f"error code {data['error']}")
                print(f"⚠️  API Error: {error_msg}")
            else:
                # Estimate size for display
                size = len(raw)
                if size > 1024 * 1024:
                    size_str = f"{size / 1024 / 1024:.1f} MB"
                elif size > 1024:
                    size_str = f"{size / 1024:.1f} KB"
                else:
                    size_str = f"{size} B"
                print(f"✅  ({size_str})")

        except Exception as e:
            print(f"❌  {e}")
            data = {"_error": str(e)}

        # Save to file
        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return data

    def login(self) -> dict:
        """Login with token and store session ID."""
        data = self.call("token/login", {"token": self.token}, "login")
        if isinstance(data, dict) and "eid" in data:
            self.sid = data["eid"]
            print(f"\n  ✅ Logged in as: {data.get('au', '?')} | Session: {self.sid[:16]}...\n")
        else:
            print("\n  ❌ LOGIN FAILED — check your token\n")
            sys.exit(1)
        return data

    def logout(self):
        """End the session."""
        if self.sid:
            self.call("core/logout", {}, "logout")


def now_unix():
    return int(time.time())


def days_ago_unix(days):
    return int((datetime.now() - timedelta(days=days)).timestamp())


def safe_get(d, *keys, default=None):
    """Safely traverse nested dicts."""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, default)
        else:
            return default
    return d


# ---------------------------------------------------------------------------
# Main exploration logic
# ---------------------------------------------------------------------------
def explore(token: str, base_url: str):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    api = WialonAPI(base_url, token)

    ts_now = now_unix()
    ts_from = days_ago_unix(HISTORY_DAYS)

    # ======================================================================
    # PHASE 1: Authentication
    # ======================================================================
    print("=" * 70)
    print("PHASE 1: Authentication")
    print("=" * 70)
    login_data = api.login()

    # ======================================================================
    # PHASE 2: Discovery — find all units, resources, unit groups
    # ======================================================================
    print("=" * 70)
    print("PHASE 2: Discovery — Units, Resources, Groups")
    print("=" * 70)

    # --- All units with FULL analytics data ---
    # flags breakdown:
    #   0x1     = base info (name, id)
    #   0x2     = custom properties
    #   0x8     = custom fields
    #   0x20    = admin fields
    #   0x100   = sensors list
    #   0x400   = last message + position
    #   0x800   = last known position
    #   0x1000  = counters (mileage, engine hours)
    #   0x4000  = connection status
    #   0x40000 = profile fields
    all_flags = 0x1 | 0x2 | 0x8 | 0x20 | 0x100 | 0x400 | 0x800 | 0x1000 | 0x4000 | 0x40000
    units_data = api.call("core/search_items", {
        "spec": {
            "itemsType": "avl_unit",
            "propName": "sys_name",
            "propValueMask": "*",
            "sortType": "sys_name"
        },
        "force": 1,
        "flags": all_flags,
        "from": 0,
        "to": 0  # 0 = return all
    }, "search_all_units_full_data")

    # Extract unit list
    units = []
    if isinstance(units_data, dict) and "items" in units_data:
        units = units_data["items"]
    print(f"  📦 Found {len(units)} units\n")

    # --- All resources (contain report templates, geofences, drivers, notifications) ---
    # flags: 0x1 (base) | 0x2001 (reports + notifications + zones + drivers)
    res_flags = 0x1 | 0x2 | 0x8 | 0x100 | 0x200 | 0x400 | 0x800 | 0x1000 | 0x2000 | 0x4000 | 0x8000 | 0x10000 | 0x20000
    resources_data = api.call("core/search_items", {
        "spec": {
            "itemsType": "avl_resource",
            "propName": "sys_name",
            "propValueMask": "*",
            "sortType": "sys_name"
        },
        "force": 1,
        "flags": res_flags,
        "from": 0,
        "to": 0
    }, "search_all_resources")

    resources = []
    if isinstance(resources_data, dict) and "items" in resources_data:
        resources = resources_data["items"]
    print(f"  📦 Found {len(resources)} resources\n")

    # --- All unit groups ---
    groups_data = api.call("core/search_items", {
        "spec": {
            "itemsType": "avl_unit_group",
            "propName": "sys_name",
            "propValueMask": "*",
            "sortType": "sys_name"
        },
        "force": 1,
        "flags": 0x1 | 0x2 | 0x8 | 0x20,
        "from": 0,
        "to": 0
    }, "search_all_unit_groups")

    # --- All users ---
    users_data = api.call("core/search_items", {
        "spec": {
            "itemsType": "avl_user",
            "propName": "sys_name",
            "propValueMask": "*",
            "sortType": "sys_name"
        },
        "force": 1,
        "flags": 0x1 | 0x2 | 0x4 | 0x8,
        "from": 0,
        "to": 0
    }, "search_all_users")

    # ======================================================================
    # PHASE 3: Unit-Level Analytics (for top N units)
    # ======================================================================
    sample_units = units[:MAX_UNITS_DETAIL]
    if sample_units:
        print("=" * 70)
        print(f"PHASE 3: Unit-Level Analytics (top {len(sample_units)} units)")
        print("=" * 70)

        for i, unit in enumerate(sample_units):
            uid = unit.get("id")
            uname = unit.get("nm", f"unit_{uid}")
            safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in uname).strip()
            print(f"\n  --- Unit {i+1}/{len(sample_units)}: {uname} (ID: {uid}) ---")

            # 3a. Get trips for this unit
            api.call("unit/get_trips", {
                "itemId": uid,
                "days": HISTORY_DAYS
            }, f"unit_trips_{safe_name}")

            # 3b. Trip detector settings
            api.call("unit/get_trip_detector", {
                "itemId": uid
            }, f"unit_trip_detector_{safe_name}")

            # 3c. Fuel settings
            api.call("unit/get_fuel_settings", {
                "itemId": uid
            }, f"unit_fuel_settings_{safe_name}")

            # 3d. Speed settings
            api.call("unit/get_speed_settings", {
                "itemId": uid
            }, f"unit_speed_settings_{safe_name}")

            # 3e. Eco-driving / drive rank settings
            api.call("unit/get_drive_rank_settings", {
                "itemId": uid
            }, f"unit_drive_rank_settings_{safe_name}")

            # 3f. Report settings
            api.call("unit/get_report_settings", {
                "itemId": uid
            }, f"unit_report_settings_{safe_name}")

            # 3g. Calc last message (latest computed values)
            api.call("unit/calc_last_message", {
                "itemId": uid
            }, f"unit_calc_last_message_{safe_name}")

            # 3h. VIN info
            api.call("unit/get_vin_info", {
                "itemId": uid
            }, f"unit_vin_info_{safe_name}")

            # 3i. Activity settings
            api.call("unit/get_activity_settings", {
                "itemId": uid
            }, f"unit_activity_settings_{safe_name}")

            # 3j. Calculate current sensor values
            sensors = unit.get("sens", {})
            if sensors:
                sensor_ids = list(sensors.keys())
                api.call("unit/calc_sensors", {
                    "itemId": uid,
                    "sensors": sensor_ids
                }, f"unit_calc_sensors_{safe_name}")

            # 3k. Load historical messages (telemetry)
            print(f"\n  Loading {HISTORY_DAYS}-day message history for {uname}...")
            msg_load = api.call("messages/load_interval", {
                "itemId": uid,
                "timeFrom": ts_from,
                "timeTo": ts_now,
                "flags": 1,  # 1 = data messages (GPS + params)
                "flagsMask": 65281,
                "loadCount": MAX_MESSAGES
            }, f"messages_load_{safe_name}")

            # Get the loaded messages
            msg_count = 0
            if isinstance(msg_load, dict) and "count" in msg_load:
                msg_count = min(msg_load["count"], MAX_MESSAGES)

            if msg_count > 0:
                api.call("messages/get_messages", {
                    "indexFrom": 0,
                    "indexTo": min(msg_count, 100) - 1  # first 100 messages
                }, f"messages_data_sample_{safe_name}")

            # Unload messages to free server memory
            api.call("messages/unload", {}, f"messages_unload_{safe_name}")

    # ======================================================================
    # PHASE 4: Resource-Level Analytics (geofences, drivers, notifications)
    # ======================================================================
    if resources:
        print("\n" + "=" * 70)
        print("PHASE 4: Resource Analytics (Geofences, Drivers, Notifications)")
        print("=" * 70)

        for res in resources[:3]:  # top 3 resources
            rid = res.get("id")
            rname = res.get("nm", f"resource_{rid}")
            safe_rname = "".join(c if c.isalnum() or c in "-_ " else "_" for c in rname).strip()
            print(f"\n  --- Resource: {rname} (ID: {rid}) ---")

            # 4a. Geofence data — get all zones listed in the resource
            zones = res.get("zl", {})
            if zones:
                # Get data for up to 5 zones
                zone_ids = list(zones.keys())[:5]
                for zid in zone_ids:
                    zone_name = zones.get(zid, {}).get("n", zid) if isinstance(zones.get(zid), dict) else zid
                    api.call("resource/get_zone_data", {
                        "itemId": rid,
                        "col": [int(zid)],
                        "flags": 0
                    }, f"geofence_data_{safe_rname}_{zone_name}")

            # 4b. Driver bindings history
            drivers = res.get("drvrs", {}) or res.get("drv", {})
            if drivers:
                driver_ids = list(drivers.keys())[:5]
                for did in driver_ids:
                    api.call("resource/get_driver_bindings", {
                        "itemId": rid,
                        "driverId": int(did),
                        "timeFrom": ts_from,
                        "timeTo": ts_now
                    }, f"driver_bindings_{safe_rname}_{did}")

            # 4c. Notification details
            notifs = res.get("unf", {})
            if notifs:
                notif_ids = list(notifs.keys())[:5]
                for nid in notif_ids:
                    api.call("resource/get_notification_data", {
                        "itemId": rid,
                        "col": [int(nid)]
                    }, f"notification_data_{safe_rname}_{nid}")

            # 4d. Job details
            jobs = res.get("ujb", {})
            if jobs:
                job_ids = list(jobs.keys())[:5]
                for jid in job_ids:
                    api.call("resource/get_job_data", {
                        "itemId": rid,
                        "col": [int(jid)]
                    }, f"job_data_{safe_rname}_{jid}")

            # 4e. Which geofences is each sample unit in right now?
            if zones and sample_units:
                for unit in sample_units[:2]:
                    uid = unit.get("id")
                    uname = unit.get("nm", str(uid))
                    api.call("resource/get_zones_by_unit", {
                        "itemId": rid,
                        "unitId": uid
                    }, f"zones_by_unit_{safe_rname}_{uname}")

            # 4f. Get unit-driver assignments
            if sample_units:
                for unit in sample_units[:2]:
                    uid = unit.get("id")
                    api.call("resource/get_unit_drivers", {
                        "itemId": rid,
                        "unitId": uid,
                        "timeFrom": ts_from,
                        "timeTo": ts_now
                    }, f"unit_drivers_{safe_rname}_{uid}")

    # ======================================================================
    # PHASE 5: Report Execution (THE BIG ONE)
    # ======================================================================
    print("\n" + "=" * 70)
    print("PHASE 5: Report Execution")
    print("=" * 70)

    # Find report templates from resources
    report_templates = []
    for res in resources:
        rid = res.get("id")
        rname = res.get("nm", "")
        rep = res.get("rep", {})
        if rep:
            for tid, tinfo in rep.items():
                tname = tinfo.get("n", tid) if isinstance(tinfo, dict) else tid
                report_templates.append({
                    "resource_id": rid,
                    "resource_name": rname,
                    "template_id": int(tid),
                    "template_name": tname
                })

    if report_templates:
        print(f"  📋 Found {len(report_templates)} report template(s)")
        for rt in report_templates[:10]:  # list first 10
            print(f"     - [{rt['template_id']}] {rt['template_name']} (resource: {rt['resource_name']})")

        # Save the template list
        tpl_path = os.path.join(OUTPUT_DIR, f"{api.step + 1:02d}_report_templates_list.json")
        with open(tpl_path, "w", encoding="utf-8") as f:
            json.dump(report_templates, f, indent=2, ensure_ascii=False)
        api.step += 1
        print(f"\n  [{api.step:02d}] Saved template list                              ✅")

        # Execute up to 3 report templates on the first unit
        if sample_units:
            target_unit = sample_units[0]
            uid = target_unit.get("id")
            uname = target_unit.get("nm", str(uid))
            print(f"\n  🎯 Executing reports on unit: {uname} (ID: {uid})")
            print(f"     Time range: {datetime.fromtimestamp(ts_from)} → {datetime.fromtimestamp(ts_now)}\n")

            for rt in report_templates[:3]:
                tname = rt["template_name"] if isinstance(rt["template_name"], str) else str(rt["template_id"])
                safe_tname = "".join(c if c.isalnum() or c in "-_ " else "_" for c in tname).strip()

                print(f"\n  📊 Report: {tname}")

                # Step 1: Cleanup any previous report
                api.call("report/cleanup_result", {}, f"report_cleanup_{safe_tname}")

                # Step 2: Execute
                exec_result = api.call("report/exec_report", {
                    "reportResourceId": rt["resource_id"],
                    "reportTemplateId": rt["template_id"],
                    "reportObjectId": uid,
                    "reportObjectSecId": 0,
                    "interval": {
                        "flags": 0,
                        "from": ts_from,
                        "to": ts_now
                    }
                }, f"report_exec_{safe_tname}")

                # Step 3: Check status (poll if needed)
                report_ready = False
                if isinstance(exec_result, dict) and "reportResult" in exec_result:
                    report_ready = True
                else:
                    # Poll for up to 30 seconds
                    for attempt in range(15):
                        time.sleep(2)
                        status = api.call("report/get_report_status", {}, f"report_status_{safe_tname}_poll{attempt}")
                        if isinstance(status, dict):
                            st = status.get("status", 0)
                            if st == 4:  # done
                                report_ready = True
                                break
                            elif st in (0, 1, 2, 3):  # still processing
                                continue
                            else:
                                break

                if not report_ready:
                    # Try apply_report_result anyway
                    api.call("report/apply_report_result", {}, f"report_apply_{safe_tname}")
                    report_ready = True

                if report_ready:
                    # Step 4: Get tables
                    tables = api.call("report/get_report_tables", {}, f"report_tables_{safe_tname}")

                    # Step 5: Get data for each table
                    if isinstance(tables, list):
                        for t_idx, table in enumerate(tables):
                            table_label = table.get("label", f"table_{t_idx}")
                            row_count = table.get("rows", 0)
                            safe_tlabel = "".join(c if c.isalnum() or c in "-_ " else "_" for c in table_label).strip()

                            if row_count > 0:
                                # Get summary/statistics row
                                api.call("report/get_report_data", {}, f"report_summary_{safe_tname}_{safe_tlabel}")

                                # Get data rows (up to 100)
                                fetch_count = min(row_count, 100)
                                rows_result = api.call("report/get_result_rows", {
                                    "tableIndex": t_idx,
                                    "indexFrom": 0,
                                    "indexTo": fetch_count - 1
                                }, f"report_rows_{safe_tname}_{safe_tlabel}")

                                # Get sub-rows for first row if available
                                if isinstance(rows_result, list) and len(rows_result) > 0:
                                    first_row = rows_result[0]
                                    if isinstance(first_row, dict) and first_row.get("c"):
                                        api.call("report/get_result_subrows", {
                                            "tableIndex": t_idx,
                                            "rowIndex": 0
                                        }, f"report_subrows_{safe_tname}_{safe_tlabel}_row0")

                    # Try to get chart data
                    api.call("report/get_result_chart", {
                        "tableIndex": 0,
                        "rowIndex": 0,
                        "width": 800,
                        "height": 300
                    }, f"report_chart_{safe_tname}")

                # Cleanup
                api.call("report/cleanup_result", {}, f"report_cleanup_after_{safe_tname}")
    else:
        print("  ⚠️  No report templates found in any resource")
        print("     You may need to create report templates in the Wialon UI first")

    # ======================================================================
    # PHASE 6: Events / Notification History
    # ======================================================================
    print("\n" + "=" * 70)
    print("PHASE 6: Events & Notification History")
    print("=" * 70)

    # Load events for each resource
    for res in resources[:2]:
        rid = res.get("id")
        rname = res.get("nm", str(rid))
        safe_rname = "".join(c if c.isalnum() or c in "-_ " else "_" for c in rname).strip()

        api.call("events/load", {
            "itemId": rid,
            "timeFrom": ts_from,
            "timeTo": ts_now
        }, f"events_load_{safe_rname}")

        api.call("events/get_last", {
            "itemId": rid,
            "count": 50
        }, f"events_last_{safe_rname}")

    # ======================================================================
    # PHASE 7: Platform Stats & Misc
    # ======================================================================
    print("\n" + "=" * 70)
    print("PHASE 7: Platform Stats & Misc")
    print("=" * 70)

    # Platform statistics
    api.call("core/get_statistics", {}, "platform_statistics")

    # Account data (for logged-in user)
    login_user_id = login_data.get("user", {}).get("id")
    if login_user_id:
        api.call("core/get_account_data", {
            "itemId": login_user_id,
            "type": 1
        }, "account_data")

    # ======================================================================
    # PHASE 8: Order / Logistics data (may not be available)
    # ======================================================================
    print("\n" + "=" * 70)
    print("PHASE 8: Logistics / Orders (if available)")
    print("=" * 70)

    if resources:
        rid = resources[0].get("id")
        api.call("order/get_orders_history", {
            "itemId": rid,
            "timeFrom": ts_from,
            "timeTo": ts_now,
            "tz": 134228528
        }, "orders_history")

        api.call("order/get_routes_history", {
            "itemId": rid,
            "timeFrom": ts_from,
            "timeTo": ts_now
        }, "routes_history")

    # ======================================================================
    # Cleanup & Summary
    # ======================================================================
    print("\n" + "=" * 70)
    print("PHASE 9: Logout & Summary")
    print("=" * 70)

    api.logout()

    # Print summary
    files = sorted(os.listdir(OUTPUT_DIR))
    json_files = [f for f in files if f.endswith(".json")]
    total_size = sum(os.path.getsize(os.path.join(OUTPUT_DIR, f)) for f in json_files)

    print(f"\n{'=' * 70}")
    print(f"  DONE! 🎉")
    print(f"{'=' * 70}")
    print(f"  📁 Output directory : {OUTPUT_DIR}")
    print(f"  📄 JSON files saved : {len(json_files)}")
    print(f"  💾 Total size       : {total_size / 1024:.1f} KB")
    print(f"{'=' * 70}")
    print(f"\n  Files created:")
    for f in json_files:
        fsize = os.path.getsize(os.path.join(OUTPUT_DIR, f))
        print(f"    {f:<65} {fsize:>8,} bytes")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Wialon API Explorer — calls every useful endpoint and dumps JSON responses"
    )
    parser.add_argument(
        "--token", "-t",
        required=True,
        help="Your Wialon API token"
    )
    parser.add_argument(
        "--base-url", "-b",
        default=DEFAULT_BASE,
        help=f"Wialon API base URL (default: {DEFAULT_BASE})"
    )
    parser.add_argument(
        "--max-units", "-u",
        type=int,
        default=MAX_UNITS_DETAIL,
        help=f"Max units to pull detailed data for (default: {MAX_UNITS_DETAIL})"
    )
    parser.add_argument(
        "--days", "-d",
        type=int,
        default=HISTORY_DAYS,
        help=f"Days of history to load (default: {HISTORY_DAYS})"
    )

    args = parser.parse_args()

    # Override globals with CLI args
    MAX_UNITS_DETAIL = args.max_units
    HISTORY_DAYS = args.days

    print(f"\n{'=' * 70}")
    print(f"  Wialon API Explorer")
    print(f"  Base URL     : {args.base_url}")
    print(f"  Max units    : {MAX_UNITS_DETAIL}")
    print(f"  History days : {HISTORY_DAYS}")
    print(f"  Output dir   : {OUTPUT_DIR}")
    print(f"{'=' * 70}\n")

    explore(args.token, args.base_url)
