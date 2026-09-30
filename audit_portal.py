"""
Portal audit
============
Checks that what the portal renders is exactly what Wialon holds, using code that shares nothing with
the portal's own pipeline (no reports.py, no archive, no cache): it reads the report the page itself
receives, then goes back to Wialon directly.

  1. Wialon alerts, event by event: every violation event Wialon holds for the period, matched one to
     one against the portal's list. Also lists violation events of other types Wialon flags that the
     portal does not show.
  2. Wialon alerts, Wialon's own count: the "Violations" report Wialon builds itself, per vehicle.
  3. Fields: time, speed, position and text of every matched alert against Wialon's raw message.
  4. Fatigue Driving, whole fleet: every vehicle's telemetry downloaded again and the rule re-run over
     the whole period as one continuous stretch with a 12-hour lead-in (the portal works day by day
     with 3 hours, and only on a shortlist). Anything the portal lacks is a miss; anything extra is a
     false flag.

Usage:
    python audit_portal.py                       # alerts over 30 days, fatigue over 7 days
    python audit_portal.py --alerts 7D --fatigue 30D
    python audit_portal.py --source archive      # portal not running: read the Supabase archive it serves
"""

import argparse
import json
import re
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.dont_write_bytecode = True
import fatigue_engine                                  # noqa: E402  (the rule itself)
from wialon_alerts import WialonSession, load_env      # noqa: E402  (just the HTTP session)

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORTAL = "http://localhost:5000"
PKT = timezone(timedelta(hours=5))
EVT_TYPES = {                       # Wialon notification name -> portal type
    "Night Time Driving": "NIGHT_DRIVING",
    "Overspeed-Highway": "OVERSPEED_HIGHWAY",
    "Overspeed-Motorway": "OVERSPEED_MOTORWAY",
    "Seat Belt ('Ignition Off - Seatbelt On')": "SEAT_BELT_IGNITION_OFF",
    "Driver Seat Belt Disconnected": "SEAT_BELT_DISCONNECTED",
    "Delay Driver Seat Belt": "DELAY_DRIVER_SEAT_BELT",
    "Overspeed": "OVERSPEED",
}
LEAD_IN = 12 * 3600
SPEED_IN_TEXT = re.compile(r"(?:recorded|speed(?: of)?)\s+(\d+(?:\.\d+)?)\s*km/h", re.I)


def pkt(ts):
    return datetime.fromtimestamp(ts, PKT).strftime("%Y-%m-%d %H:%M:%S")


def session():
    env = load_env()
    return WialonSession(env.get("WIALON_BASE_URL", "https://hst-api.wialon.eu"), env["WIALON_TOKEN"])


def call_retrying(s, svc, params, what):
    """One Wialon call, retried with a fresh session when the connection drops (a long audit downloads
    hundreds of megabytes, and a single cut connection should not throw the whole run away)."""
    for wait in (0, 10, 30, 60, 120):
        if wait:
            print(f"   {what}: connection dropped, retrying in {wait} s", flush=True)
            time.sleep(wait)
            try:
                s.logout()
            except Exception:
                pass
            try:
                s = session()
            except Exception:
                continue
        try:
            return s, s.call(svc, params)
        except Exception as e:
            last = e
    raise RuntimeError(f"{what} failed after retries: {last}")


SOURCE = "portal"     # or "archive": the stored rows the portal serves, read straight from Supabase


def archive_report(preset):
    """The shortcut's period read straight from the Supabase archive the portal serves, merged the same way
    (one violation per vehicle, type and second)."""
    import violation_store
    from supabase_client import db
    days = int(preset.rstrip("Dd"))
    today = int(datetime.now(PKT).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    t0, t1 = today - days * 86400, today
    day = lambda ts: datetime.fromtimestamp(ts, PKT).strftime("%Y-%m-%d")
    seen, rows = set(), []
    for v in sorted(violation_store.ViolationStore(db.supabase_client).load(day(t0), day(t1 - 1)),
                    key=lambda v: v["time_unix"], reverse=True):
        key = (v["vehicle_id"], v["type"], v["time_unix"])
        if key not in seen:
            seen.add(key)
            rows.append(v)
    return {"from_unix": t0, "to_unix": t1, "from": pkt(t0), "to": pkt(t1 - 1), "vehicles": [{"violations": rows}],
            "summary": {"total_violations": len(rows), "violating_vehicles_count": len({v["vehicle_id"] for v in rows}),
                        "total_fleet": "-"}}


def portal_report(preset):
    """The exact report the portal page receives for a shortcut (or the archive it is built from)."""
    if SOURCE == "archive":
        return archive_report(preset)
    while True:
        with urllib.request.urlopen(f"{PORTAL}/api/report/preset/{preset}", timeout=600) as r:
            body = json.load(r)
            if r.status == 200:
                return body
        print(f"   portal is still calculating: {body.get('step')}", flush=True)
        time.sleep(10)


def portal_violations(rep):
    return [v for item in rep["vehicles"] for v in item["violations"]]


# ------------------------------------------------------------------ 1-3: Wialon alerts
def wialon_events(units, time_from, time_to):
    """Every violation-flagged event message of every unit in [from, to): (uid, name, t, text, lat, lon)."""
    out = []
    s = session()
    try:
        ids = list(units)
        for i in range(0, len(ids), 60):
            chunk = ids[i:i + 60]
            calls = [{"svc": "messages/load_interval",
                      "params": {"itemId": uid, "timeFrom": time_from, "timeTo": time_to - 1,
                                 "flags": 0x0601, "flagsMask": 0xFF01, "loadCount": 0xFFFFFFFF}} for uid in chunk]
            for uid, res in zip(chunk, s.call("core/batch", {"params": calls, "flags": 0})):
                if not isinstance(res, dict) or "messages" not in res:
                    raise RuntimeError(f"Wialon refused the events of unit {uid}: {res}")
                for m in res["messages"]:
                    out.append((uid, (m.get("p") or {}).get("evt_name") or "(no name)", m["t"],
                                m.get("et") or "", m.get("y"), m.get("x")))
    finally:
        s.logout()
    return out


def wialon_report_counts(time_from, time_to):
    """Wialon's own "Violations" report over the Unilever group: {vehicle name: count}."""
    s = session()
    try:
        resource = s.search("avl_resource", 1)[0]
        group = next(g for g in s.search("avl_unit_group", 1) if g["nm"] == "Unilever")
        template = {"id": 0, "n": "portal audit", "ct": "avl_unit_group", "p": "{}",
                    "tbl": [{"n": "unit_group_violations", "l": "Violations",
                             "c": "[\"time\",\"evt_text\",\"events_count\"]", "cl": "[\"Time\",\"Text\",\"Count\"]",
                             "cp": "[{\"calc_type\":5},{\"calc_type\":1},{\"calc_type\":1}]", "s": "", "sl": "", "sp": "",
                             "filter_order": [], "p": "{}",
                             "sch": {"f1": 0, "f2": 0, "t1": 0, "t2": 0, "m": 0, "y": 0, "w": 0, "fl": 0}, "f": 0}]}
        s.call("report/cleanup_result", {})
        s.call("report/exec_report", {"reportResourceId": resource["id"], "reportTemplateId": 0,
                                      "reportObjectId": group["id"], "reportObjectSecId": 0,
                                      "interval": {"from": time_from, "to": time_to - 1, "flags": 0},
                                      "reportTemplate": template, "remoteExec": 1})
        while str(s.call("report/get_report_status", {}).get("status")) != "4":
            time.sleep(2)
        tables = s.call("report/apply_report_result", {})["reportResult"]["tables"]
        counts = {}
        if tables and tables[0]["rows"]:
            rows = s.call("report/select_result_rows", {
                "tableIndex": 0, "config": {"type": "range", "data": {"from": 0, "to": tables[0]["rows"] - 1, "level": 1}}})
            for row in rows:
                cells = row.get("c") or []
                try:
                    counts[cells[0]] = int(str(cells[3]).split()[0])
                except (IndexError, ValueError):
                    pass
        s.call("report/cleanup_result", {})
        return counts, set(group.get("u") or [])
    finally:
        s.logout()


def audit_alerts(preset, units):
    print(f"\n=== Wialon alerts, {preset} ===")
    rep = portal_report(preset)
    t0, t1 = rep["from_unix"], rep["to_unix"]
    print(f"portal renders: {rep['from']} -> {rep['to']} PKT, {rep['summary']['total_violations']} violations "
          f"on {rep['summary']['violating_vehicles_count']} vehicles; fleet {rep['summary']['total_fleet']} "
          f"(Wialon has {len(units)} vehicles)")
    shown = [v for v in portal_violations(rep) if v["type"] in EVT_TYPES.values()]
    portal_keys = {(v["vehicle_id"], v["type"], v["time_unix"]): v for v in shown}

    events = wialon_events(units, t0, t1)
    ours = [e for e in events if e[1] in EVT_TYPES]
    others = Counter(e[1] for e in events if e[1] not in EVT_TYPES)
    wialon_keys = {}
    for e in ours:
        wialon_keys.setdefault((e[0], EVT_TYPES[e[1]], e[2]), []).append(e)

    print(f"\n1. Event by event")
    print(f"   Wialon holds {len(ours)} violation events of the 6 alert types "
          f"({len(wialon_keys)} after merging same-vehicle, same-type, same-second repeats)")
    # Wialon fires a notification twice when it reaches a vehicle through two groups; the copies are the
    # same violation (same second and place) and differ at most in the group list printed in the text
    repeats = [group for group in wialon_keys.values() if len(group) > 1]
    moved = [g for g in repeats if len({(e[4], e[5]) for e in g}) > 1]
    speeds = [g for g in repeats if len({(SPEED_IN_TEXT.search(e[3]) or [None, None])[1] for e in g}) > 1]
    print(f"   same-second repeats: {sum(len(g) - 1 for g in repeats)} extra copies in {len(repeats)} places "
          f"(the portal shows each once); copies at a different position: {len(moved)}, "
          f"with a different speed: {len(speeds)}")
    missing = sorted(set(wialon_keys) - set(portal_keys))
    extra = sorted(set(portal_keys) - set(wialon_keys))
    print(f"   in Wialon but NOT on the portal: {len(missing)}")
    for k in missing[:10]:
        e = wialon_keys[k][0]
        print(f"      MISSING {units.get(k[0], k[0])} {k[1]} {pkt(k[2])} :: {e[3][:120]}")
    print(f"   on the portal but NOT in Wialon: {len(extra)}")
    for k in extra[:10]:
        print(f"      EXTRA {units.get(k[0], k[0])} {k[1]} {pkt(k[2])}")
    by_type_w = Counter(k[1] for k in wialon_keys)
    by_type_p = Counter(k[1] for k in portal_keys)
    for t in sorted(set(by_type_w) | set(by_type_p)):
        print(f"      {t:24s} Wialon {by_type_w[t]:5d}   portal {by_type_p[t]:5d}   {'ok' if by_type_w[t] == by_type_p[t] else 'DIFFERS'}")
    if others:
        print(f"   other violation-flagged events in Wialon (not one of the portal's types): {dict(others)}")

    print(f"\n2. Wialon's own Violations report (Unilever group)")
    counts, group_ids = wialon_report_counts(t0, t1)
    raw_in_group = Counter(units[e[0]] for e in events if e[0] in group_ids)
    print(f"   Wialon's report counts {sum(counts.values())} violations on {len(counts)} vehicles; "
          f"the raw events for the same vehicles number {sum(raw_in_group.values())} "
          f"({sum(1 for e in events if e[0] not in group_ids)} more on vehicles outside the group)")
    differ = {n: (counts.get(n, 0), raw_in_group.get(n, 0)) for n in set(counts) | set(raw_in_group)
              if counts.get(n, 0) != raw_in_group.get(n, 0)}
    print(f"   vehicles where Wialon's report and its raw events disagree: {len(differ)}"
          + (f" e.g. {dict(list(differ.items())[:5])}" if differ else ""))

    print(f"\n3. Fields of every matched alert")
    bad = Counter()
    examples = []
    for k, v in portal_keys.items():
        if k not in wialon_keys:
            continue
        copies = wialon_keys[k]
        e = next((c for c in copies if c[3] == (v.get("details") or "")), copies[0])   # the copy the portal kept
        checks = {
            "time": v["time"] == pkt(e[2]),
            "text": (v.get("details") or "") == e[3],
            "position": v.get("lat") == e[4] and v.get("lon") == e[5],
        }
        m = SPEED_IN_TEXT.search(e[3])
        if m:
            checks["speed"] = v.get("speed_kmh") is not None and abs(float(v["speed_kmh"]) - float(m.group(1))) < 0.51
        for name, ok in checks.items():
            if not ok:
                bad[name] += 1
                if len(examples) < 5:
                    examples.append((name, units.get(k[0]), v["time"]))
    matched = len(set(portal_keys) & set(wialon_keys))
    print(f"   {matched} alerts compared field by field: "
          + ("all fields identical to Wialon" if not bad else f"differences {dict(bad)} e.g. {examples}"))
    return not missing and not extra and not bad and not moved and not speeds


# ------------------------------------------------------------------ 4: Fatigue Driving
def audit_fatigue(preset, units, workers):
    print(f"\n=== Fatigue Driving, {preset}, every vehicle ===")
    rep = portal_report(preset)
    t0, t1 = rep["from_unix"], rep["to_unix"]
    portal = {(v["vehicle_id"], v["time_unix"]): v for v in portal_violations(rep) if v["type"] == "FATIGUE_DRIVING"}
    print(f"portal renders {len(portal)} Fatigue Driving violations, {rep['from']} -> {rep['to']} PKT")
    print(f"limits in use: {json.dumps({k: fatigue_engine.get_compliance_config()[k] for k in ('max_continuous_drive_day', 'max_continuous_drive_night', 'min_break_day', 'min_reset_stop')})}")

    found, lock, done = {}, threading.Lock(), [0]
    messages = [0]
    ids = list(units)

    def work(my_ids):
        s = session()
        try:
            for uid in my_ids:
                s, res = call_retrying(s, "core/batch", {"params": [
                    {"svc": "messages/load_interval", "params": {"itemId": uid, "timeFrom": t0 - LEAD_IN, "timeTo": t1 - 1,
                                                                 "flags": 1, "flagsMask": 0xFF01, "loadCount": 0}},
                    {"svc": "messages/get_messages", "params": {"indexFrom": 0, "indexTo": 0xFFFFFFFF,
                                                                "filter": "pos.s,pos.x,pos.y,p.io_239"}},
                    {"svc": "messages/unload", "params": {}}], "flags": 0}, f"unit {units[uid]}")
                loaded, msgs = res[0], res[1]
                if not isinstance(loaded, dict) or "count" not in loaded:
                    raise RuntimeError(f"Wialon refused the telemetry of unit {uid}: {loaded}")
                msgs = msgs if (loaded["count"] and isinstance(msgs, list)) else []
                stream = sorted(({"t": m["t"], "pos": {"s": (m.get("pos") or {}).get("s") or 0,
                                                       "y": (m.get("pos") or {}).get("y") or 0.0,
                                                       "x": (m.get("pos") or {}).get("x") or 0.0},
                                  "p": {"io_239": (m.get("p") or {}).get("io_239") or 0}} for m in msgs if m.get("pos")),
                                key=lambda m: m["t"])
                result = fatigue_engine.analyze_telemetry_stream(stream, vehicle_name=units[uid])
                hits = []
                for x in result["violations"]:
                    if x.get("violation_type") != "CONTINUOUS_DRIVE_EXCEEDED":
                        continue
                    ts = int(time.mktime(time.strptime(x["time"], "%Y-%m-%d %H:%M:%S")))
                    if t0 <= ts < t1:
                        hits.append((ts, x))
                with lock:
                    for ts, x in hits:
                        found[(uid, ts)] = x
                    messages[0] += len(stream)
                    done[0] += 1
                    if done[0] % 50 == 0:
                        print(f"   {done[0]}/{len(ids)} vehicles re-checked", flush=True)
        finally:
            s.logout()

    started = time.time()
    with ThreadPoolExecutor(workers) as ex:
        for f in [ex.submit(work, ids[i::workers]) for i in range(workers)]:
            f.result()
    print(f"re-checked {len(ids)} vehicles, {messages[0]:,} telemetry messages, in {time.time() - started:.0f} s")

    missing = sorted(set(found) - set(portal))
    extra = sorted(set(portal) - set(found))
    print(f"   found by the full re-check: {len(found)}   rendered by the portal: {len(portal)}")
    print(f"   missed by the portal: {len(missing)}")
    for k in missing[:15]:
        x = found[k]
        near = [p for p in portal if p[0] == k[0] and abs(p[1] - k[1]) <= 600]
        print(f"      MISSED {units[k[0]]} {pkt(k[1])} drove {x['continuous_drive_minutes']} min {x.get('period')}"
              + (f"  (portal has one {abs(near[0][1] - k[1])} s away)" if near else ""))
    print(f"   on the portal but not found by the re-check: {len(extra)}")
    for k in extra[:15]:
        print(f"      EXTRA {units[k[0]]} {pkt(k[1])} drove {portal[k]['continuous_drive_minutes']} min")
    diffs = [(k, portal[k], found[k]) for k in set(found) & set(portal)
             if (portal[k]["continuous_drive_minutes"], portal[k].get("period"), portal[k].get("rest_minutes"))
             != (found[k]["continuous_drive_minutes"], found[k].get("period"), found[k].get("rest_taken_minutes"))]
    print(f"   matched violations whose drive time, Day/Night or rest differ: {len(diffs)}")
    for k, p, f in diffs[:10]:
        print(f"      {units[k[0]]} {pkt(k[1])}: portal {p['continuous_drive_minutes']}/{p.get('period')}/{p.get('rest_minutes')}"
              f" vs re-check {f['continuous_drive_minutes']}/{f.get('period')}/{f.get('rest_taken_minutes')}")
    return not missing and not extra and not diffs


def main():
    ap = argparse.ArgumentParser(description="Audit the portal against Wialon")
    ap.add_argument("--alerts", default="30D", help="shortcut whose Wialon alerts to audit (or 'none')")
    ap.add_argument("--fatigue", default="7D", help="shortcut whose Fatigue Driving to audit (or 'none')")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--source", choices=("portal", "archive"), default="portal",
                    help="read what the page receives (portal must be running) or the Supabase archive it serves")
    args = ap.parse_args()
    global SOURCE
    SOURCE = args.source

    s = session()
    try:
        units = {u["id"]: u["nm"] for u in s.search("avl_unit", 1)}
    finally:
        s.logout()

    ok = True
    if args.alerts.lower() != "none":
        ok &= audit_alerts(args.alerts.upper(), units)
    if args.fatigue.lower() != "none":
        ok &= audit_fatigue(args.fatigue.upper(), units, args.workers)
    print("\nRESULT:", "everything the portal renders matches Wialon, nothing left out" if ok else "differences found (see above)")


if __name__ == "__main__":
    main()
