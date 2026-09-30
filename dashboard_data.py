"""
Dashboard numbers
=================
Everything the dashboard page shows, added up on the server from a report's violations, so the browser
receives a few hundred numbers instead of thousands of rows. The violations must already carry their
review verdict and vehicle region (ReportService.annotate).

Region is the vehicle's depot region from its Wialon record: where it works, not where the violation
happened. Speeds are the violation's own reading (the speed Wialon recorded with the alert, or the
speed at the fatigue mark), falling back to the telemetry message nearest to it.
"""

from collections import Counter

from regions import UNASSIGNED

OVERSPEED_TYPES = ("OVERSPEED", "OVERSPEED_HIGHWAY", "OVERSPEED_MOTORWAY")


def speed_of(v):
    own = v.get("speed_kmh")
    if own is not None:
        return own
    return (v.get("telemetry") or {}).get("speed_kmh")


def _split(items):
    """{'total', 'genuine', 'false', 'pending'} for a list of violations."""
    genuine = sum(1 for v in items if v.get("verdict") == "GENUINE")
    false = sum(1 for v in items if v.get("verdict") == "FALSE")
    return {"total": len(items), "genuine": genuine, "false": false, "pending": len(items) - genuine - false}


def build(violations, types):
    """types: fleet_violations.VIOLATION_TYPES, in display order."""
    overall = _split(violations)
    reviewed = overall["genuine"] + overall["false"]

    by_region = {}
    for v in violations:
        by_region.setdefault(v.get("vehicle_region") or UNASSIGNED, []).append(v)

    region_rows = []
    for r, items in by_region.items():
        veh_map = {}
        for v in items:
            vid = v.get("vehicle_id")
            if vid not in veh_map:
                veh_map[vid] = {
                    "vehicle_id": vid,
                    "vehicle": v.get("vehicle") or f"Vehicle {vid}",
                    "driver": v.get("driver") or "Unassigned",
                    "total": 0,
                    "genuine": 0,
                    "false": 0,
                    "pending": 0,
                    "by_type": Counter(),
                    "latest_time": v.get("time"),
                }
            entry = veh_map[vid]
            entry["total"] += 1
            verdict = v.get("verdict")
            if verdict == "GENUINE":
                entry["genuine"] += 1
            elif verdict == "FALSE":
                entry["false"] += 1
            else:
                entry["pending"] += 1
            entry["by_type"][v.get("type")] += 1
            if v.get("time") and (not entry["latest_time"] or v.get("time") > entry["latest_time"]):
                entry["latest_time"] = v.get("time")

        vehicles_list = [
            {
                "vehicle_id": entry["vehicle_id"],
                "vehicle": entry["vehicle"],
                "driver": entry["driver"],
                "total": entry["total"],
                "genuine": entry["genuine"],
                "false": entry["false"],
                "pending": entry["pending"],
                "by_type": dict(entry["by_type"]),
                "latest_time": entry["latest_time"],
            }
            for entry in veh_map.values()
        ]
        vehicles_list.sort(key=lambda x: (-x["total"], x["vehicle"]))

        region_rows.append({
            "region": r,
            **_split(items),
            "vehicle_count": len(vehicles_list),
            "vehicles": vehicles_list
        })

    region_rows.sort(key=lambda row: (row["region"] == UNASSIGNED, -row["total"], row["region"]))

    by_type = {}
    for v in violations:
        by_type.setdefault(v["type"], []).append(v)
    type_rows = [{"key": t["key"], "label": t["label"], **_split(by_type.get(t["key"], []))} for t in types]

    mix = {row["region"]: Counter(v["type"] for v in by_region[row["region"]]) for row in region_rows}

    hours = [0] * 24
    for v in violations:
        hours[int(str(v["time"])[11:13])] += 1

    speeds = [(speed_of(v), v) for v in violations if speed_of(v) is not None]
    top = max(speeds, key=lambda pair: pair[0]) if speeds else None
    overspeeds = [s for s, v in speeds if v["type"] in OVERSPEED_TYPES]

    return {
        "cards": {
            "total": overall["total"],
            "genuine": overall["genuine"],
            "false": overall["false"],
            "pending": overall["pending"],
            "reviewed": reviewed,
            "genuine_pct": round(100.0 * overall["genuine"] / reviewed, 1) if reviewed else None,
            "vehicles": len({v["vehicle_id"] for v in violations}),
            "regions": sum(1 for r in by_region if r != UNASSIGNED),
            "max_speed": round(top[0]) if top else None,
            "max_speed_at": {"vehicle": top[1]["vehicle"], "time": top[1]["time"],
                             "type": top[1].get("type_label")} if top else None,
            "avg_overspeed": round(sum(overspeeds) / len(overspeeds), 1) if overspeeds else None,
            "overspeed_events": len(overspeeds),
        },
        "by_region": region_rows,
        "by_type": type_rows,
        "region_mix": {
            "regions": [row["region"] for row in region_rows],
            "series": [{"key": t["key"], "label": t["label"],
                        "values": [mix[row["region"]].get(t["key"], 0) for row in region_rows]}
                       for t in types],
        },
        "by_hour": hours,
    }
