"""
Ask Wialon one question, get one clean JSON answer.

Built for studying the Remote API: every command is a few lines around one or two Wialon calls, and
--explain prints the exact svc and params each call sends, so you can see the request behind every answer.

Usage:
    python wialon_query.py positions                   # latest location of every vehicle, one array
    python wialon_query.py positions --out pos.csv     # same, as a spreadsheet-friendly CSV
    python wialon_query.py units                       # id + name of every vehicle
    python wialon_query.py drivers                     # every driver and the vehicle they are bound to
    python wialon_query.py groups                      # unit groups and their member vehicle ids
    python wialon_query.py sensors CCH-891             # sensors configured on one vehicle (name or id)
    python wialon_query.py messages CCH-891 --hours 2  # raw telemetry points for one vehicle
    python wialon_query.py snapshot                    # rebuild the offline fallback app.py reads

    python wialon_query.py call core/search_items @request.json   # any endpoint, params from a file
    python wialon_query.py call core/search_item '{"id": 12345, "flags": 1025}'   # ...or inline (bash)

Options (any command):
    --explain   print each request (svc + params) to stderr before it is sent
    --raw       print Wialon's untouched response instead of the tidied one
    --out FILE  write to FILE instead of stdout (.csv for list answers, anything else is JSON)

Reads WIALON_TOKEN / WIALON_BASE_URL from .env, like the rest of the portal.
Reference: https://sdk.wialon.com/wiki/en/sidebar/remoteapi/apiref/apiref
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

from wialon_alerts import OUTPUT_DIR, WialonSession, load_env

# Item data flags for core/search_items on avl_unit. Ask only for what the answer needs: each flag
# adds a block to every item, and over 300 vehicles the difference is megabytes.
UNIT_BASE = 0x1             # id, nm (name), cls, mu
UNIT_LAST_MESSAGE = 0x400   # pos (latest position) and lmsg (latest message, with its p parameters)
UNIT_SENSORS = 0x1000       # sens: the sensors configured on the unit
UNIT_PORTAL_SNAPSHOT = 285995   # what app.py asks for (fetch_live_vehicles_from_wialon)

RESOURCE_BASE = 0x1
RESOURCE_DRIVERS = 0x100    # drvrs: drivers, with bu = the unit a driver is bound to


class ExplainedSession(WialonSession):
    """A WialonSession that can show each request on stderr as it goes out."""

    def __init__(self, base_url, token, explain=False):
        self.explain = explain
        super().__init__(base_url, token)

    def call(self, svc, params, timeout=300):
        if self.explain:
            print(f"--> svc={svc}\n    params={json.dumps(params, ensure_ascii=False)}", file=sys.stderr)
        return super().call(svc, params, timeout)


def search(s, items_type, flags, mask="*"):
    return s.call("core/search_items", {
        "spec": {"itemsType": items_type, "propName": "sys_name", "propValueMask": mask, "sortType": "sys_name"},
        "force": 1, "flags": flags, "from": 0, "to": 0,
    })


def stamp(t):
    """Unix seconds -> local ISO time with its offset, or None."""
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None


def find_unit(s, ref, flags=UNIT_BASE):
    """One vehicle by id or by (part of) its name."""
    if str(ref).isdigit():
        item = s.call("core/search_item", {"id": int(ref), "flags": flags}).get("item")
        if item:
            return item
    items = search(s, "avl_unit", flags, f"*{ref}*").get("items", [])
    exact = [u for u in items if u.get("nm", "").lower() == str(ref).lower()]
    if exact or len(items) == 1:
        return (exact or items)[0]
    if not items:
        sys.exit(f"No vehicle matches {ref!r}.")
    sys.exit(f"{ref!r} matches {len(items)} vehicles: " + ", ".join(u["nm"] for u in items[:15]))


# ---------------------------------------------------------------------------
# Commands: each takes (session, args) and returns (raw Wialon response, tidied answer)
# ---------------------------------------------------------------------------
def cmd_positions(s, a):
    raw = search(s, "avl_unit", UNIT_BASE | UNIT_LAST_MESSAGE)
    now = time.time()
    rows = []
    for u in raw.get("items", []):
        pos = u.get("pos") or {}
        params = (u.get("lmsg") or {}).get("p") or {}
        t = pos.get("t")
        rows.append({
            "id": u["id"],
            "vehicle": u.get("nm"),
            "lat": pos.get("y"),
            "lon": pos.get("x"),
            "speed_kmh": pos.get("s"),
            "course_deg": pos.get("c"),
            "satellites": pos.get("sc"),
            "ignition": params.get("io_239"),   # the Teltonika ignition wire the portal uses
            "time": stamp(t),
            "minutes_ago": round((now - t) / 60) if t else None,
        })
    return raw, rows


def cmd_units(s, a):
    raw = search(s, "avl_unit", UNIT_BASE)
    return raw, [{"id": u["id"], "vehicle": u.get("nm")} for u in raw.get("items", [])]


def cmd_drivers(s, a):
    raw = search(s, "avl_resource", RESOURCE_BASE | RESOURCE_DRIVERS)
    units = {u["id"]: u.get("nm") for u in search(s, "avl_unit", UNIT_BASE).get("items", [])}
    rows = []
    for res in raw.get("items", []):
        for d in (res.get("drvrs") or {}).values():
            rows.append({
                "id": d.get("id"),
                "driver": (d.get("n") or "").replace("﻿", "").strip(),
                "phone": d.get("p") or None,
                "code": d.get("c") or None,
                "unit_id": d.get("bu") or None,
                "vehicle": units.get(d.get("bu")),
                "resource": res.get("nm"),
            })
    return raw, rows


def cmd_groups(s, a):
    raw = search(s, "avl_unit_group", UNIT_BASE)
    return raw, [{"id": g["id"], "group": g.get("nm"), "units": len(g.get("u", [])), "unit_ids": g.get("u", [])}
                 for g in raw.get("items", [])]


def cmd_sensors(s, a):
    unit = find_unit(s, a.unit, UNIT_BASE | UNIT_SENSORS)
    rows = [{"id": x.get("id"), "name": x.get("n"), "type": x.get("t"), "parameter": x.get("p"),
             "unit_of_measure": x.get("m")} for x in (unit.get("sens") or {}).values()]
    return unit, {"id": unit["id"], "vehicle": unit.get("nm"), "sensors": rows}


def cmd_messages(s, a):
    unit = find_unit(s, a.unit)
    to_ts = int(time.time())
    raw = s.call("messages/load_interval", {
        "itemId": unit["id"], "timeFrom": to_ts - int(a.hours * 3600), "timeTo": to_ts,
        "flags": 0x1, "flagsMask": 0xFF01,   # 0x1 = messages carrying data (not SMS, events...)
        "loadCount": a.limit,
    })
    s.call("messages/unload", {})            # free the server-side buffer load_interval filled
    rows = []
    for m in raw.get("messages", []):
        pos = m.get("pos") or {}
        rows.append({"time": stamp(m.get("t")), "lat": pos.get("y"), "lon": pos.get("x"),
                     "speed_kmh": pos.get("s"), "course_deg": pos.get("c"), "params": m.get("p") or {}})
    return raw, {"id": unit["id"], "vehicle": unit.get("nm"), "hours": a.hours,
                 "total_in_window": raw.get("totalCount"), "returned": len(rows), "messages": rows}


def cmd_snapshot(s, a):
    raw = search(s, "avl_unit", UNIT_PORTAL_SNAPSHOT)
    path = os.path.join(OUTPUT_DIR, "02_search_all_units_full_data.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    return raw, {"written": path, "units": len(raw.get("items", []))}


def cmd_call(s, a):
    params = a.params
    if params.startswith("@"):               # @file.json: saves fighting PowerShell over JSON quoting
        with open(params[1:], encoding="utf-8") as f:
            params = f.read()
    raw = s.call(a.svc, json.loads(params))
    return raw, raw


# ---------------------------------------------------------------------------
def write(answer, out):
    if out and out.lower().endswith(".csv"):
        if not (isinstance(answer, list) and answer and isinstance(answer[0], dict)):
            sys.exit("CSV needs a list answer (positions, units, drivers, groups). Use .json for this one.")
        with open(out, "w", newline="", encoding="utf-8-sig") as f:   # -sig: Excel reads Urdu names right
            w = csv.DictWriter(f, fieldnames=list(answer[0]))
            w.writeheader()
            w.writerows({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in r.items()}
                        for r in answer)
    else:
        text = json.dumps(answer, indent=2, ensure_ascii=False)
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        else:
            print(text)
    if out:
        count = f"{len(answer)} rows" if isinstance(answer, list) else "done"
        print(f"Wrote {out} ({count})", file=sys.stderr)


def main():
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--explain", action="store_true", help="print each request to stderr")
    common.add_argument("--raw", action="store_true", help="print Wialon's untouched response")
    common.add_argument("--out", help="write to this file (.csv or .json)")

    # Options live on the commands only: on both, the command's defaults would overwrite them.
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip(),
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("positions", parents=[common], help="latest location of every vehicle").set_defaults(run=cmd_positions)
    sub.add_parser("units", parents=[common], help="id and name of every vehicle").set_defaults(run=cmd_units)
    sub.add_parser("drivers", parents=[common], help="drivers and their bound vehicle").set_defaults(run=cmd_drivers)
    sub.add_parser("groups", parents=[common], help="unit groups and members").set_defaults(run=cmd_groups)
    sp = sub.add_parser("sensors", parents=[common], help="sensors on one vehicle")
    sp.add_argument("unit", help="vehicle name (or part of it) or id")
    sp.set_defaults(run=cmd_sensors)
    sp = sub.add_parser("messages", parents=[common], help="recent telemetry points for one vehicle")
    sp.add_argument("unit", help="vehicle name (or part of it) or id")
    sp.add_argument("--hours", type=float, default=1, help="how far back (default 1)")
    sp.add_argument("--limit", type=int, default=1000, help="most messages to return (default 1000)")
    sp.set_defaults(run=cmd_messages)
    sub.add_parser("snapshot", parents=[common],
                   help="rebuild wialon_output/02_search_all_units_full_data.json").set_defaults(run=cmd_snapshot)
    sp = sub.add_parser("call", parents=[common], help="call any Remote API service")
    sp.add_argument("svc", help="e.g. core/search_items")
    sp.add_argument("params", nargs="?", default="{}", help="params as JSON, or @file.json (default {})")
    sp.set_defaults(run=cmd_call)
    a = p.parse_args()

    env = load_env()
    s = ExplainedSession(env.get("WIALON_BASE_URL", "https://hst-api.wialon.eu"), env["WIALON_TOKEN"], a.explain)
    try:
        raw, answer = a.run(s, a)
    finally:
        s.logout()
    write(raw if a.raw else answer, a.out)


if __name__ == "__main__":
    main()
