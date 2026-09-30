"""
Wialon Alerts — read-only pull of the portal's violation alerts
================================================================

Reads the alerts configured as notifications in the Wialon resource, from the
two places the Remote API exposes them:

  1. Stored violations   messages/load_interval on every unit (flags 0x0601 /
                         mask 0xFF01 = event messages marked as violation),
                         sent as one core/batch request. Rule name is in p.evt_name.
  2. Notification report report/exec_report with the "Notification triggers"
                         table, for rules that only show a popup and never
                         register a violation (Road Based Speeding, Driver Seat
                         Belt Disconnected).

Nothing in Wialon is modified.

Usage:
    python wialon_alerts.py --hours 24            # one-shot, prints + saves JSON
    python wialon_alerts.py --watch 5             # poll every 5 minutes, append new alerts to JSONL
"""

import argparse
import contextlib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "wialon_output")

# Wialon notification name -> label. Stored rules register "event (violation)" in unit history.
STORED_RULES = {
    "Overspeed": "Overspeeding",
    "Overspeed-Highway": "Overspeed (Highway)",
    "Overspeed-Motorway": "Overspeed (Motorway)",
    "Night Time Driving": "Night driving",
    "Delay Driver Seat Belt": "Delay driver seat belt",
    "Seat Belt ('Ignition Off - Seatbelt On')": "Seat belt (ignition off, belt on)",
    # the rule gained its "register event" action on 22 Sep 2026; nothing is stored before that date
    "Driver Seat Belt Disconnected": "Driver seat belt disconnected",
}
# Popup-only rules: never stored as events, only visible in the Notification triggers report.
REPORT_RULES = {
    "Road Based Speeding": "Road-based speeding",
}
REPORT_TEMPLATE_NAME = "Notification_Triggered"
REPORT_GROUP_NAME = "Unilever"

# Stored events are timestamped when the condition started, but written when the rule fires
# (up to 60 min later for the seat-belt rule), so every poll re-reads this much history.
POLL_LOOKBACK_SEC = 2 * 3600

SSL_CTX = ssl.create_default_context()


def load_env():
    """Settings, with .env winning over the process environment.

    It has to win: another module copies .env into os.environ at import, so a long-lived parent process
    passes those values to every child it starts. With setdefault, a token replaced in .env was ignored
    for as long as that parent lived, and the portal kept logging in with the old one."""
    env = dict(os.environ)
    path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


LOGIN_RETRY_WAITS = (5, 20, 60)   # a refused login is usually the account being pushed back, not a dead token
SESSION_IDLE_SEC = 240            # Wialon drops a session after about five minutes without a request


def login_problem(answer):
    """A plain sentence for a refused login, instead of the raw Wialon error."""
    if isinstance(answer, dict) and answer.get("error") == 8:
        return ("Wialon refused the login (INVALID_AUTH_TOKEN). The token in .env may have been deleted or "
                "expired, or the account is being rate limited after too many logins. Check WIALON_TOKEN.")
    return f"Wialon login failed: {answer}"


class WialonSession:
    def __init__(self, base_url, token):
        self.url = base_url.rstrip("/") + "/wialon/ajax.html"
        self.base_url = base_url
        self.token = token
        self.sid = None
        self.used_at = 0.0
        self.login()

    def login(self):
        """Open a session, retrying a refusal: Wialon pushes back when an account logs in very often."""
        self.sid = None
        answer = None
        for wait in (0,) + LOGIN_RETRY_WAITS:
            if wait:
                time.sleep(wait)
            try:
                answer = self._post("token/login", {"token": self.token})
            except Exception as e:                       # a dropped connection, worth another go
                answer = {"error": str(e)}
                continue
            if isinstance(answer, dict) and answer.get("eid"):
                self.sid = answer["eid"]
                self.used_at = time.time()
                return self
            if not (isinstance(answer, dict) and answer.get("error") == 8):
                break                                    # anything other than a refusal will not fix itself
        raise RuntimeError(login_problem(answer))

    def _post(self, svc, params, timeout=300):
        body = {"svc": svc, "params": json.dumps(params)}
        if self.sid:
            body["sid"] = self.sid
        req = urllib.request.Request(self.url, data=urllib.parse.urlencode(body).encode())
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def call(self, svc, params, timeout=300):
        data = self._post(svc, params, timeout)
        if isinstance(data, dict) and data.get("error") == 1 and self.sid and svc != "token/login":
            self.login()                                 # the session timed out: open a new one and repeat
            data = self._post(svc, params, timeout)
        self.used_at = time.time()
        if isinstance(data, dict) and data.get("error") and len(data) <= 2:
            raise RuntimeError(f"{svc} failed: {data}")
        return data

    def search(self, items_type, flags):
        return self.call("core/search_items", {
            "spec": {"itemsType": items_type, "propName": "sys_name", "propValueMask": "*", "sortType": "sys_name"},
            "force": 1, "flags": flags, "from": 0, "to": 0,
        }).get("items", [])

    def logout(self):
        try:
            self.call("core/logout", {})
        except Exception:
            pass


class SessionPool:
    """Wialon sessions, handed out and put back rather than opened per operation.

    Logging in for every request is what gets an account pushed back: a full archive rebuild used to open
    several hundred sessions within minutes, and Wialon answered some of those logins with
    INVALID_AUTH_TOKEN even though the token was valid for weeks. Sessions here are reused while they are
    fresh, and one that has sat idle too long is replaced.
    """

    def __init__(self, base_url, token=None):
        self.base_url = base_url
        self._token = token       # None: read WIALON_TOKEN from .env at each login, so a replaced
        self.logins = 0           # token takes effect without restarting the portal
        self._free = []
        self._lock = threading.Lock()

    @property
    def token(self):
        return self._token or load_env()["WIALON_TOKEN"]

    @contextlib.contextmanager
    def use(self):
        session = None
        with self._lock:
            while self._free:
                candidate = self._free.pop()
                if time.time() - candidate.used_at < SESSION_IDLE_SEC:
                    session = candidate
                    break
                candidate.logout()
        if session is None:
            session = WialonSession(self.base_url, self.token)
            with self._lock:
                self.logins += 1
        try:
            yield session
        except Exception:
            session.logout()                             # do not hand a broken session to the next caller
            raise
        else:
            session.used_at = time.time()
            with self._lock:
                self._free.append(session)

    def close(self):
        with self._lock:
            sessions, self._free = self._free, []
        for session in sessions:
            session.logout()


def speed_from_text(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*km/h", text or "")
    return float(m.group(1)) if m else None


def tz_offset_sec(wialon_tz):
    """Wialon timezone value: low 16 bits hold the UTC offset in seconds (signed)."""
    offset = wialon_tz & 0xFFFF
    return offset - 0x10000 if offset > 0x7FFF else offset


def position_time_from_text(text, offset_sec):
    """%POS_TIME% as rendered in the notification text (dd.mm.yyyy HH:MM:SS, notification timezone) -> unix."""
    m = re.search(r"(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})", text or "")
    if not m or offset_sec is None:
        return None
    naive = datetime.strptime(m.group(1), "%d.%m.%Y %H:%M:%S")
    return int((naive - datetime(1970, 1, 1)).total_seconds()) - offset_sec


def make_alert(source, rule, labels, unit_name, unit_id, ts, lat, lon, text, trigger_ts=None):
    compact_text = (text or "").replace(" ", "")
    return {
        "source": source,
        "rule": rule,
        "label": labels[rule],
        "unit": unit_name,
        "unit_id": unit_id,
        "time_unix": ts,
        "time_local": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
        # report rows only: when Wialon processed the trigger (shared by everything in one incoming data batch)
        "trigger_time_unix": trigger_ts,
        "lat": lat,
        "lon": lon,
        "speed_kmh": speed_from_text(text),
        "text": text,
        # Wialon fills %UNIT% at trigger time; a different plate here needs checking (rename or moved tracker)
        "text_names_other_unit": bool(text and text.strip() and unit_name.replace(" ", "") not in compact_text),
    }


class AlertReader:
    def __init__(self, session):
        self.s = session
        self.units = {u["id"]: u["nm"] for u in session.search("avl_unit", 1)}
        resource = session.search("avl_resource", 1 | 0x400 | 0x2000)[0]
        self.resource_id = resource["id"]

        notifications = {n["n"]: n for n in (resource.get("unf") or {}).values()}
        for name in list(STORED_RULES) + list(REPORT_RULES):
            n = notifications.get(name)
            if not n:
                print(f"  ⚠️  Notification '{name}' not found in resource — it will never match")
            elif n.get("fl", 0) & 0x2:
                print(f"  ⚠️  Notification '{name}' is disabled in Wialon")
        wanted = [int(notifications[name]["id"]) for name in REPORT_RULES if name in notifications]
        details = session.call("resource/get_notification_data", {"itemId": self.resource_id, "col": wanted}) if wanted else []
        self.rule_tz_offset = {n["n"]: tz_offset_sec(n["tz"]) for n in details}

        templates = {t["n"]: t for t in (resource.get("rep") or {}).values()}
        if REPORT_TEMPLATE_NAME not in templates:
            raise RuntimeError(f"Report template '{REPORT_TEMPLATE_NAME}' not found in resource")
        self.template_id = templates[REPORT_TEMPLATE_NAME]["id"]

        group = next((g for g in session.search("avl_unit_group", 1) if g["nm"] == REPORT_GROUP_NAME), None)
        if not group:
            raise RuntimeError(f"Unit group '{REPORT_GROUP_NAME}' not found")
        self.group_id = group["id"]
        outside = [nm for uid, nm in self.units.items() if uid not in set(group["u"])]
        if outside:
            print(f"  ℹ️  Not in group '{REPORT_GROUP_NAME}' (report-only alerts not read for them): {', '.join(outside)}")

    def stored_violations(self, time_from, time_to):
        unit_ids = list(self.units)
        batch = [{"svc": "messages/load_interval",
                  "params": {"itemId": uid, "timeFrom": time_from, "timeTo": time_to,
                             "flags": 0x0601, "flagsMask": 0xFF01, "loadCount": 0xFFFFFFFF}}
                 for uid in unit_ids]
        results = self.s.call("core/batch", {"params": batch, "flags": 0})
        self.s.call("messages/unload", {})

        alerts = []
        for uid, res in zip(unit_ids, results):
            if not isinstance(res, dict) or "messages" not in res:
                print(f"  ⚠️  {self.units[uid]}: {res}")
                continue
            for m in res["messages"]:
                rule = (m.get("p") or {}).get("evt_name")
                if rule in STORED_RULES:
                    alerts.append(make_alert("stored_violation", rule, STORED_RULES, self.units[uid], uid,
                                             m["t"], m.get("y"), m.get("x"), m.get("et")))
        return alerts

    def report_triggers(self, time_from, time_to):
        self.s.call("report/cleanup_result", {})
        self.s.call("report/exec_report", {
            "reportResourceId": self.resource_id, "reportTemplateId": self.template_id,
            "reportObjectId": self.group_id, "reportObjectSecId": 0,
            "interval": {"from": time_from, "to": time_to, "flags": 0}, "remoteExec": 1,
        })
        for _ in range(200):
            if str(self.s.call("report/get_report_status", {}).get("status")) == "4":
                break
            time.sleep(3)
        else:
            raise RuntimeError("Notification triggers report did not finish in time")
        result = self.s.call("report/apply_report_result", {}).get("reportResult", {})

        alerts = []
        for ti, table in enumerate(result.get("tables", [])):
            if not table["name"].endswith("notify_triggers") or not table["rows"]:
                continue
            rows = self.s.call("report/select_result_rows", {
                "tableIndex": ti,
                "config": {"type": "range", "data": {"from": 0, "to": table["rows"] - 1, "level": 1}},
            })
            name_to_id = {nm: uid for uid, nm in self.units.items()}
            for row in rows:
                for sub in row.get("r", []):
                    c = sub["c"]
                    rule = c[2]
                    if rule not in REPORT_RULES or not isinstance(c[3], dict):
                        continue
                    unit_name = c[7] or c[1]
                    trigger_ts = c[3]["v"]
                    pos_ts = position_time_from_text(c[6], self.rule_tz_offset.get(rule))
                    # the text's position time can't be later than the trigger; if it is, the parse is wrong
                    ts = pos_ts if pos_ts and pos_ts <= trigger_ts + 60 else trigger_ts
                    alerts.append(make_alert("notification_report", rule, REPORT_RULES, unit_name,
                                             name_to_id.get(unit_name), ts, c[3].get("y"), c[3].get("x"), c[6], trigger_ts))
        self.s.call("report/cleanup_result", {})
        return alerts

    def read(self, time_from, time_to):
        alerts = self.stored_violations(time_from, time_to) + self.report_triggers(time_from, time_to)
        alerts.sort(key=lambda a: a["time_unix"])
        return alerts


def alert_key(a):
    return (a["source"], a["unit"], a["rule"], a["time_unix"], a["lat"], a["lon"])


def print_alerts(alerts, limit=None):
    shown = alerts if limit is None else alerts[-limit:]
    for a in shown:
        speed = f"{a['speed_kmh']:.0f} km/h" if a["speed_kmh"] is not None else ""
        flag = "  ⚠️ text names another unit" if a["text_names_other_unit"] else ""
        print(f"  {a['time_local']}  {a['unit']:<12} {a['label']:<34} {speed:>8}  ({a['lat']}, {a['lon']}){flag}")


def one_shot(reader, hours):
    now = int(time.time())
    alerts = reader.read(now - int(hours * 3600), now)
    print(f"\n{len(alerts)} alerts in the last {hours:g}h")
    for label, count in Counter(a["label"] for a in alerts).most_common():
        print(f"  {count:>5}  {label}")
    print("\nMost recent 25:")
    print_alerts(alerts, limit=25)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"alerts_{int(hours)}h_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)
    print(f"\n📁 Saved: {path}")


def watch(reader, minutes, out_path):
    seen = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(alert_key(json.loads(line)))
                except ValueError:
                    pass
    print(f"\n👀 Polling every {minutes:g} min (lookback {POLL_LOOKBACK_SEC // 60} min), appending new alerts to {out_path}")
    while True:
        now = int(time.time())
        new = []
        try:
            for a in reader.read(now - POLL_LOOKBACK_SEC, now):
                # the report can list the same trigger twice, so dedupe within a poll too
                if alert_key(a) not in seen:
                    seen.add(alert_key(a))
                    new.append(a)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] poll failed: {e}")
        if new:
            with open(out_path, "a", encoding="utf-8") as f:
                for a in new:
                    f.write(json.dumps(a, ensure_ascii=False) + "\n")
            print(f"[{datetime.now():%H:%M:%S}] {len(new)} new alert(s)")
            print_alerts(new)
        else:
            print(f"[{datetime.now():%H:%M:%S}] no new alerts")
        time.sleep(minutes * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only pull of Wialon violation alerts")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--hours", type=float, help="one-shot: alerts from the last N hours")
    mode.add_argument("--watch", type=float, metavar="MINUTES", help="poll every N minutes")
    parser.add_argument("--out", default=os.path.join(OUTPUT_DIR, "alerts_live.jsonl"), help="JSONL file for --watch")
    args = parser.parse_args()

    env = load_env()
    session = WialonSession(env.get("WIALON_BASE_URL", "https://hst-api.wialon.eu"), env["WIALON_TOKEN"])
    try:
        reader = AlertReader(session)
        if args.hours:
            one_shot(reader, args.hours)
        else:
            os.makedirs(os.path.dirname(args.out), exist_ok=True)
            watch(reader, args.watch, args.out)
    except KeyboardInterrupt:
        pass
    finally:
        session.logout()
