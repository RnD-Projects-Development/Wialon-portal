"""
Inspect the data behind a Fatigue Driving violation
===================================================
Dumps exactly what the calculation read for one vehicle, so the arithmetic can be checked by hand:

  Sheet "Messages"   every telemetry message the engine read: time, speed (pos.s), ignition (p.io_239),
                     position, odometer (p.io_16), the gap since the previous message, and beside each
                     row what the engine made of it - driving or rest, the running timer, and where the
                     limit was reached.
  Sheet "Violations" what the portal reports for this vehicle in the window.
  Sheet "API sample" the raw Wialon response for the first messages, field for field.

Usage:
    python inspect_fatigue.py --vehicle CCH-949
    python inspect_fatigue.py --vehicle CY-1720 --time "2026-09-17 09:00" --hours 4
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta

import fatigue_engine
import fleet_violations as fv
from wialon_alerts import WialonSession, load_env

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def open_session():
    env = load_env()
    return WialonSession(env.get("WIALON_BASE_URL", "https://hst-api.wialon.eu"), env["WIALON_TOKEN"])


def raw_messages(s, uid, time_from, time_to):
    """The same request the portal makes, returned exactly as Wialon sends it."""
    res = s.call("core/batch", {"params": fv.telemetry_call(uid, time_from, time_to), "flags": 0})
    loaded, msgs = res[0], res[1]
    if not isinstance(loaded, dict) or "count" not in loaded:
        raise RuntimeError(f"load_interval failed: {loaded}")
    return (msgs if loaded["count"] else []), loaded["count"]


def main():
    ap = argparse.ArgumentParser(description="Dump the telemetry behind a Fatigue Driving violation")
    ap.add_argument("--vehicle", required=True, help="vehicle plate, e.g. CCH-949")
    ap.add_argument("--time", help="violation time (PKT) 'YYYY-MM-DD HH:MM'; default: the latest in the window")
    ap.add_argument("--hours", type=float, default=6.0, help="hours of history to dump (default 6)")
    ap.add_argument("--out", help="output .xlsx path")
    args = ap.parse_args()

    s = open_session()
    try:
        units = {u["nm"]: u["id"] for u in s.search("avl_unit", 1)}
        if args.vehicle not in units:
            raise SystemExit(f"No vehicle called {args.vehicle}")
        uid = units[args.vehicle]

        if args.time:
            centre = int(datetime.strptime(args.time, "%Y-%m-%d %H:%M").replace(tzinfo=fv.PKT).timestamp())
        else:
            centre = int(time.time())
        time_to = min(int(time.time()), centre + 1800)
        time_from = int(centre - args.hours * 3600)

        print(f"{args.vehicle}: reading {fv.fmt_time(time_from)} -> {fv.fmt_time(time_to)} PKT")
        msgs, count = raw_messages(s, uid, time_from, time_to)
        print(f"Wialon returned {count} messages")
    finally:
        s.logout()

    rows = sorted((fv.message_row(m) for m in msgs), key=lambda r: r[0])
    stream = [{"t": t, "pos": {"s": sp, "y": y if y is not None else 0.0, "x": x if x is not None else 0.0},
               "p": {"io_239": ign}} for (t, sp, ign, y, x, _odo) in rows]
    result = fatigue_engine.analyze_telemetry_stream(stream, vehicle_name=args.vehicle)
    timeline = list(reversed(result["timeline"]))      # engine returns newest first
    violations = result["violations"]
    cfg = fatigue_engine.get_compliance_config()

    print(f"Engine on this window: {len(violations)} Fatigue Driving violation(s)")
    for v in violations:
        print(f"   {v['time']}  drove {v['continuous_drive_minutes']} min, rest taken {v['rest_taken_minutes']} min")

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    ws = wb.active
    ws.title = "Messages"
    headers = ["#", "Message Time (PKT)", "Unix time", "Speed pos.s (km/h)", "Ignition p.io_239",
               "Latitude pos.y", "Longitude pos.x", "Odometer p.io_16", "Gap since previous (s)",
               "Engine state", "Timer (drive or stop)", "Engine note"]
    widths = [6, 21, 13, 17, 16, 14, 14, 16, 19, 22, 20, 30]
    for i, (head, width) in enumerate(zip(headers, widths), 1):
        cell = ws.cell(row=1, column=i, value=head)
        cell.font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4C1D95")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    previous_t = None
    breach_fill = PatternFill("solid", fgColor="FCE7F3")
    rest_fill = PatternFill("solid", fgColor="EEF2FF")
    for i, ((t, speed, ign, lat, lon, odo), tl) in enumerate(zip(rows, timeline), start=2):
        gap = (t - previous_t) if previous_t else 0
        previous_t = t
        values = [i - 1, fv.fmt_time(t), t, speed, ign, lat, lon, fv.odometer_km(odo), gap,
                  tl["state"], tl["dwell_rest"], tl["status"]]
        for c, value in enumerate(values, 1):
            cell = ws.cell(row=i, column=c, value=value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(horizontal="center" if c != 10 else "left", vertical="center")
            if "BREACH" in str(tl["status"]):
                cell.fill = breach_fill
            elif tl["state"].startswith(("REST", "PARKED")):
                cell.fill = rest_fill
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(rows) + 1, 2)}"

    ws2 = wb.create_sheet("Violations")
    ws2.append(["Violation Time (PKT)", "Continuous Drive (min)", "Rest Taken (min)", "Speed (km/h)",
                "Location (lat, lon)", "Details"])
    for c, width in enumerate([21, 20, 17, 13, 26, 90], 1):
        ws2.cell(row=1, column=c).font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
        ws2.cell(row=1, column=c).fill = PatternFill("solid", fgColor="4C1D95")
        ws2.column_dimensions[get_column_letter(c)].width = width
    for v in violations:
        ws2.append([v["time"], v["continuous_drive_minutes"], v["rest_taken_minutes"], v["speed_kmh"],
                    v["location"], v["notes"]])
    ws2.append([])
    ws2.append(["Limits used:", f"day {cfg['max_continuous_drive_day']}", f"night {cfg['max_continuous_drive_night']}",
                f"reset stop {cfg['min_reset_stop']}", f"required break {cfg['min_break_day']}"])
    ws2.append(["Rule:", "driving = ignition ON and speed >= 5 km/h; rest = ignition OFF or speed < 5 km/h"])

    ws3 = wb.create_sheet("API sample")
    ws3.append(["Raw Wialon messages, exactly as returned (first 40)"])
    ws3.cell(row=1, column=1).font = Font(name="Arial", size=11, bold=True)
    ws3.column_dimensions["A"].width = 150
    for m in msgs[:40]:
        ws3.append([json.dumps(m, ensure_ascii=False)])
    for row in ws3.iter_rows(min_row=2):
        row[0].font = Font(name="Consolas", size=9)

    out = args.out or f"wialon_output/fatigue_check_{args.vehicle}_{datetime.now(fv.PKT):%Y%m%d_%H%M}.xlsx"
    wb.save(out)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
