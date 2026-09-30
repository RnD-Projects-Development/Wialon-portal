"""
Verify a portal report against Wialon
=====================================
Checks that what the portal shows is real and that nothing is missing, in three ways:

  1. Wialon alert types: compares the portal's violations with Wialon's own "Violations" report for the
     same period. Wialon builds that list itself, so matching counts mean the portal is not inventing
     or dropping alerts.
  2. Fatigue Driving, flagged ones: re-downloads the raw telemetry around each flagged violation and
     runs the rule again, confirming the violation really is in the data.
  3. Fatigue Driving, missed ones: takes a random sample of vehicles the report did NOT flag,
     downloads their full telemetry for the period and runs the rule. Anything found is a miss.

Usage:
    python verify_report.py --days 1
    python verify_report.py --days 7 --sample 15
"""

import argparse
import json
import random
import sys
import time
import urllib.request
from collections import Counter

import fatigue_engine
import fleet_violations as fv
import reports
from wialon_alerts import WialonSession, load_env

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORTAL = "http://localhost:5000"
GROUP_NAME = "Unilever"
WIALON_TYPES = [t["key"] for t in fv.VIOLATION_TYPES if t["evt_name"]]


def portal_report(days):
    """Build (or rebuild) the report the portal itself would show for this period."""
    req = urllib.request.Request(f"{PORTAL}/api/report/start", method="POST",
                                 data=json.dumps({"preset": f"{days}D"}).encode(),
                                 headers={"Content-Type": "application/json"})
    job_id = json.load(urllib.request.urlopen(req, timeout=60))["job_id"]
    while True:
        job = json.load(urllib.request.urlopen(f"{PORTAL}/api/report/job/{job_id}", timeout=60))
        if job["state"] == "done":
            break
        if job["state"] == "error":
            raise RuntimeError(job.get("error"))
        print(f"   building: {job.get('step', '')}", flush=True)
        time.sleep(5)
    return json.load(urllib.request.urlopen(f"{PORTAL}/api/report/{job['report_id']}", timeout=300))


def wialon_violations(s, time_from, time_to):
    """Wialon's own Violations report for the fleet: (vehicle, unix time) pairs."""
    resource = s.search("avl_resource", 1)[0]
    group = next(g for g in s.search("avl_unit_group", 1) if g["nm"] == GROUP_NAME)
    template = {
        "id": 0, "n": "portal verification", "ct": "avl_unit_group", "p": "{}",
        "tbl": [{"n": "unit_group_violations", "l": "Violations",
                 "c": "[\"time\",\"evt_text\",\"events_count\"]", "cl": "[\"Time\",\"Text\",\"Count\"]",
                 "cp": "[{\"calc_type\":5},{\"calc_type\":1},{\"calc_type\":1}]", "s": "", "sl": "", "sp": "",
                 "filter_order": [], "p": "{}",
                 "sch": {"f1": 0, "f2": 0, "t1": 0, "t2": 0, "m": 0, "y": 0, "w": 0, "fl": 0}, "f": 0}],
    }
    s.call("report/cleanup_result", {})
    s.call("report/exec_report", {"reportResourceId": resource["id"], "reportTemplateId": 0,
                                  "reportObjectId": group["id"], "reportObjectSecId": 0,
                                  "interval": {"from": time_from, "to": time_to, "flags": 0},
                                  "reportTemplate": template, "remoteExec": 1})
    while str(s.call("report/get_report_status", {}).get("status")) != "4":
        time.sleep(2)
    table = s.call("report/apply_report_result", {})["reportResult"]["tables"][0]
    rows = s.call("report/select_result_rows", {
        "tableIndex": 0, "config": {"type": "range", "data": {"from": 0, "to": table["rows"] - 1, "level": 1}}})

    # Wialon returns one row per vehicle with a violation count (individual rows are not retrievable)
    counts = {}
    for row in rows:
        cells = row.get("c") or []
        try:
            count = int(str(cells[3]).split()[0])
        except (IndexError, ValueError):
            count = 0
        if count:
            counts[cells[0]] = count
    s.call("report/cleanup_result", {})
    return counts


def telemetry(s, uid, time_from, time_to):
    res = s.call("core/batch", {"params": fv.telemetry_call(uid, time_from, time_to), "flags": 0})
    loaded, msgs = res[0], res[1]
    if not isinstance(loaded, dict) or not loaded.get("count") or not isinstance(msgs, list):
        return []
    return sorted((fv.message_row(m) for m in msgs), key=lambda r: r[0])


def run_engine(rows, name):
    stream = [{"t": t, "pos": {"s": sp, "y": y or 0.0, "x": x or 0.0}, "p": {"io_239": ign}}
              for (t, sp, ign, y, x, _odo) in rows]
    return fatigue_engine.analyze_telemetry_stream(stream, vehicle_name=name)["violations"]


def main():
    ap = argparse.ArgumentParser(description="Verify a portal report against Wialon")
    ap.add_argument("--days", type=int, default=1, help="period to verify (1, 7, 15, 30)")
    ap.add_argument("--sample", type=int, default=10, help="vehicles to re-check for missed Fatigue Driving")
    ap.add_argument("--full", action="store_true", help="re-check every vehicle instead of a sample (slower, complete proof)")
    args = ap.parse_args()

    print(f"Building the portal's {args.days}D report...")
    rep = portal_report(args.days)
    time_from, time_to = rep["from_unix"], rep["to_unix"]
    violations = [v for item in rep["vehicles"] for v in item["violations"]]
    print(f"Portal report: {rep['from']} -> {rep['to']} PKT, {len(violations)} violations "
          f"across {rep['summary']['violating_vehicles_count']} vehicles\n")

    env = load_env()
    s = WialonSession(env.get("WIALON_BASE_URL", "https://hst-api.wialon.eu"), env["WIALON_TOKEN"])
    try:
        # ---- 1. Wialon's own violations list
        print("1. Comparing the Wialon alert types with Wialon's own Violations report...")
        wialon_counts = Counter(wialon_violations(s, time_from, time_to))
        portal_counts = Counter(v["vehicle"] for v in violations if v["type"] in WIALON_TYPES)
        missing = sum((wialon_counts - portal_counts).values())
        extra = sum((portal_counts - wialon_counts).values())
        print(f"   Wialon reports {sum(wialon_counts.values())} violations on {len(wialon_counts)} vehicles")
        print(f"   the portal shows {sum(portal_counts.values())} on {len(portal_counts)} vehicles")
        print(f"   counted by Wialon but not shown by the portal: {missing}")
        print(f"   shown by the portal but not counted by Wialon: {extra}")
        for name in sorted(set(wialon_counts) | set(portal_counts)):
            if wialon_counts[name] != portal_counts[name]:
                print(f"      differs: {name}: portal {portal_counts[name]} vs Wialon {wialon_counts[name]}")

        # ---- 2. Flagged Fatigue Driving, re-derived from raw telemetry
        fatigue = [v for v in violations if v["type"] == "FATIGUE_DRIVING"]
        print(f"\n2. Re-checking {len(fatigue)} flagged Fatigue Driving violation(s) against raw telemetry...")
        confirmed = 0
        for v in fatigue:
            rows = telemetry(s, v["vehicle_id"], v["time_unix"] - 6 * 3600, v["time_unix"] + 1800)
            again = run_engine(rows, v["vehicle"])
            hit = any(abs(int(time.mktime(time.strptime(x["time"], "%Y-%m-%d %H:%M:%S"))) - v["time_unix"]) <= 300
                      for x in again)
            confirmed += hit
            if not hit:
                print(f"   NOT REPRODUCED: {v['vehicle']} {v['time']}")
        print(f"   confirmed {confirmed} of {len(fatigue)}")

        # ---- 3. Vehicles the report did not flag
        flagged_ids = {v["vehicle_id"] for v in fatigue}
        units = {u["id"]: u["nm"] for u in s.search("avl_unit", 1)}
        pool = [uid for uid in units if uid not in flagged_ids]
        sample = pool if args.full else random.sample(pool, min(args.sample, len(pool)))
        print(f"\n3. Downloading {len(sample)} unflagged vehicle(s) in full and running the rule on them...")
        missed = []
        if args.full:
            # every vehicle, fetched in parallel the way a report does
            service = reports.ReportService(cache_dir="wialon_output/report_cache")
            rows_by_unit = service._telemetry(sample, time_from - reports.LEAD_IN_SEC, time_to)
            checked = rows_by_unit.items()
        else:
            checked = ((uid, telemetry(s, uid, time_from - reports.LEAD_IN_SEC, time_to)) for uid in sample)
        for uid, rows in checked:
            for x in run_engine(rows, units[uid]):
                ts = int(time.mktime(time.strptime(x["time"], "%Y-%m-%d %H:%M:%S")))
                if time_from <= ts < time_to:
                    missed.append((units[uid], x["time"]))
        print(f"   missed violations found: {len(missed)}")
        for m in missed[:10]:
            print(f"      MISSED: {m[0]} at {m[1]}")

        print("\nVerdict")
        print(f"   Wialon alert types: {'MATCH' if missing == 0 and extra == 0 else 'MISMATCH'}")
        print(f"   Fatigue Driving shown: {'all confirmed' if confirmed == len(fatigue) else 'some not reproduced'}")
        print(f"   Fatigue Driving missed in sample: {len(missed)}")
    finally:
        s.logout()


if __name__ == "__main__":
    main()
