"""
Fatigue Driving alert email
===========================
One email per Fatigue Driving violation, sent once and never again.

Why a table and not just "send it when we find it": archive_day() replaces a whole day's violations
every time it runs, and catch_up() re-archives days whenever the limits change. The same violation is
therefore handed to us many times over its life. portal_fatigue_alerts has (violation_key, email) as
its primary key, so the second and later offers of a violation are dropped by Postgres rather than by
logic we would have to keep correct.

Sending is done by a worker thread, not by the archive job: a mail server that is slow or down must
never hold up (or fail) the nightly archive.

  enqueue(violations)  called after a day is archived; writes queued rows, ignoring ones already there
  drain()              sends what is queued, one mail per recipient per run, marks rows sent/failed

Set up: SMTP_* and ALERT_RECIPIENTS in .env, and run supabase_notifications_schema.sql once.
"""

import os
import smtplib
import ssl
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr

import fleet_violations as fv
from wialon_alerts import load_env

TABLE = "portal_fatigue_alerts"
ALERT_TYPE = "FATIGUE_DRIVING"

MAX_ATTEMPTS = 5      # a row that has failed this often is left alone, so one bad address cannot loop
BATCH = 200           # rows read per drain; a day of fatigue violations is far below this
POLL_SEC = 60
SEND_TIMEOUT = 30


def smtp_config():
    """Read at send time (and from .env, which wins over the environment), so a corrected password
    takes effect without restarting the portal."""
    env = load_env()
    return {
        "host": (env.get("SMTP_HOST") or "").strip(),
        "port": int(env.get("SMTP_PORT") or 587),
        "user": (env.get("SMTP_USER") or "").strip(),
        "password": env.get("SMTP_PASSWORD") or "",
        "sender": (env.get("SMTP_EMAIL") or "").strip(),
        "from_name": (env.get("SMTP_FROM_NAME") or "TPL Trakker Fleet Safety").strip(),
        "use_tls": (env.get("SMTP_USE_TLS") or "true").lower() in ("1", "true", "yes"),
        # The mail server presents a self-signed certificate. Point SMTP_CA_FILE at that certificate to
        # keep verification on and trust just it; SMTP_TLS_INSECURE=true switches verification off
        # altogether, which encrypts the connection but no longer proves who is on the other end.
        "ca_file": (env.get("SMTP_CA_FILE") or "").strip(),
        "insecure": (env.get("SMTP_TLS_INSECURE") or "false").lower() in ("1", "true", "yes"),
    }


def recipients():
    """Who gets fatigue alerts. One address per logged-in user once accounts exist; a fixed list until then."""
    raw = load_env().get("ALERT_RECIPIENTS") or ""
    return [a.strip() for a in raw.split(",") if a.strip()]


def alert_key(v):
    """Same identity the reviews use (vehicle, type, second): survives a day being recalculated."""
    return "{}:{}:{}".format(int(v["vehicle_id"]), v["type"], int(v["time_unix"]))


def key_day(key):
    """The PKT day a queued alert belongs to, read back out of its key."""
    return fv.fmt_time(int(key.rsplit(":", 1)[1]))[:10]


def drive_hours(mins):
    """2h 31m - the portal's own driveHours()."""
    if not mins:
        return "-"
    return "{}h {:02d}m".format(int(mins // 60), int(round(mins % 60)))


def ignition_text(v):
    tel = v.get("telemetry") or {}
    if tel.get("ignition") is None:
        return "-"
    return "ON" if tel.get("ignition") == 1 else "OFF"


def speed_text(v):
    tel = v.get("telemetry") or {}
    speed = v.get("speed_kmh") if v.get("speed_kmh") is not None else tel.get("speed_kmh")
    return "-" if speed is None else "{} km/h".format(speed)


# --------------------------------------------------------------------------- the email itself
CELL = "padding:10px 12px;border-bottom:1px solid #25344F;color:#FFFFFF;font-size:13px;"
HEAD = ("padding:12px;background:#0B1019;color:#C5DDF5;font-size:11px;letter-spacing:.06em;"
        "text-transform:uppercase;font-weight:700;border-bottom:1px solid #9EC5E8;text-align:center;")
HEADERS = ("Vehicle", "Driver", "Violation Time (PKT)", "Violation Type", "Speed", "Ignition",
           "Driving Hours", "Violation Location", "Details")


def _esc(text):
    return (str(text if text is not None else "-")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_html(violations):
    rows = []
    for v in violations:
        period = ""
        if v.get("period"):
            period = " <span style='color:#EBD2B2;font-size:11px;'>(" + _esc(v["period"]) + ")</span>"
        rows.append(
            "<tr>"
            + "<td style='" + CELL + "font-weight:600;'>" + _esc(v.get("vehicle")) + "</td>"
            + "<td style='" + CELL + "color:#C5DDF5;'>" + _esc(v.get("driver") or "Unassigned") + "</td>"
            + "<td style='" + CELL + "font-family:monospace;'>" + _esc(v.get("time")) + "</td>"
            + "<td style='" + CELL + "color:#E25763;font-weight:700;'>"
            + _esc(v.get("type_label") or "Fatigue Driving") + "</td>"
            + "<td style='" + CELL + "text-align:center;font-family:monospace;'>" + _esc(speed_text(v)) + "</td>"
            + "<td style='" + CELL + "text-align:center;font-family:monospace;'>" + _esc(ignition_text(v)) + "</td>"
            + "<td style='" + CELL + "text-align:center;font-family:monospace;font-weight:700;'>"
            + _esc(drive_hours(v.get("continuous_drive_minutes"))) + period + "</td>"
            + "<td style='" + CELL + "'>" + _esc(v.get("location") or v.get("address")) + "</td>"
            + "<td style='" + CELL + "color:#C5DDF5;font-size:12px;'>" + _esc(v.get("details")) + "</td>"
            + "</tr>")

    head = "".join("<th style='" + HEAD + "'>" + h + "</th>" for h in HEADERS)
    count = len(violations)
    plural = "s" if count != 1 else ""
    return (
        '<html><body style="margin:0;padding:24px;background:#070B12;'
        'font-family:Segoe UI,Arial,sans-serif;">'
        '<div style="max-width:1100px;margin:0 auto;background:#111A2B;border:1px solid #25344F;'
        'border-radius:12px;overflow:hidden;">'
        '<div style="padding:20px 24px;background:#0B1019;border-bottom:1px solid #9EC5E8;">'
        '<div style="color:#FFFFFF;font-size:18px;font-weight:700;">Fatigue Driving Alert</div>'
        '<div style="color:#9EC5E8;font-size:13px;margin-top:4px;">'
        + str(count) + " fatigue driving violation" + plural
        + " recorded in the last 24 hours.</div></div>"
        '<table style="width:100%;border-collapse:collapse;"><thead><tr>' + head + "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
        '<div style="padding:16px 24px;background:#0B1019;border-top:1px solid #25344F;'
        'color:#7C95B1;font-size:11px;">Automated message from the Vehicle Operations &amp; '
        "Compliance Portal. Do not reply to this address.</div></div></body></html>")


def render_text(violations):
    lines = ["Fatigue Driving Alert - {} violation(s) in the last 24 hours".format(len(violations)), ""]
    for v in violations:
        lines += [
            "{} ({})".format(v.get("vehicle"), v.get("driver") or "Unassigned"),
            "  Time      : {} PKT".format(v.get("time")),
            "  Speed     : {}    Ignition: {}".format(speed_text(v), ignition_text(v)),
            "  Driving   : {} ({})".format(drive_hours(v.get("continuous_drive_minutes")),
                                           v.get("period") or "-"),
            "  Location  : {}".format(v.get("location") or v.get("address")),
            "  Details   : {}".format(v.get("details")),
            "",
        ]
    lines.append("Automated message from the Vehicle Operations & Compliance Portal. Do not reply.")
    return "\n".join(lines)


def send_mail(to_email, subject, violations):
    cfg = smtp_config()
    missing = [k for k in ("host", "sender", "password") if not cfg[k]]
    if missing:
        raise RuntimeError("SMTP is not configured: missing "
                           + ", ".join("SMTP_" + m.upper() for m in missing))

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((cfg["from_name"], cfg["sender"]))
    message["To"] = to_email
    message["Auto-Submitted"] = "auto-generated"      # keeps out-of-office replies from bouncing back
    message.set_content(render_text(violations))
    message.add_alternative(render_html(violations), subtype="html")

    context = ssl.create_default_context(cafile=cfg["ca_file"] or None)
    if cfg["insecure"]:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=SEND_TIMEOUT) as server:
        server.ehlo()
        if cfg["use_tls"]:
            server.starttls(context=context)
            server.ehlo()
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        server.send_message(message)


# --------------------------------------------------------------------------- queue
class FatigueAlerter:
    def __init__(self, client_getter):
        # A getter, not the client: supabase_client builds its client lazily and may reconnect.
        self._client_getter = client_getter
        self._lock = threading.Lock()
        self._worker_started = False
        self.last_error = None
        self.sent_total = 0

    @property
    def client(self):
        try:
            return self._client_getter()
        except Exception:
            return None

    def problem(self):
        """Why alerts cannot be stored, or None when they can."""
        client = self.client
        if client is None:
            return "Supabase is not connected (check SUPABASE_URL and SUPABASE_KEY in .env)"
        try:
            client.table(TABLE).select("violation_key").limit(1).execute()
            return None
        except Exception as e:
            text = str(e)
            if "PGRST205" in text:
                return "table " + TABLE + " does not exist: run supabase_notifications_schema.sql"
            if "42501" in text:
                return ("permission denied on " + TABLE
                        + ": run the GRANT lines in supabase_notifications_schema.sql")
            return text[:200]

    def enqueue(self, violations):
        """Queue one row per (fatigue violation x recipient). Rows already queued or sent are ignored."""
        targets = recipients()
        fatigue = [v for v in violations if v.get("type") == ALERT_TYPE]
        if not targets or not fatigue:
            return 0
        problem = self.problem()
        if problem:
            print("[Alerts] not queued: " + problem)
            return 0

        rows = [{
            "violation_key": alert_key(v),
            "email": email,
            "day": fv.fmt_time(v["time_unix"])[:10],
            "vehicle": v.get("vehicle"),
            "driver": v.get("driver"),
            "violation_time": v.get("time"),
            "status": "queued",
        } for v in fatigue for email in targets]

        try:
            self.client.table(TABLE).upsert(
                rows, on_conflict="violation_key,email", ignore_duplicates=True).execute()
        except Exception as e:
            self.last_error = str(e)[:300]
            print("[Alerts] could not queue {} row(s): {}".format(len(rows), self.last_error))
            return 0
        print("[Alerts] queued up to {} row(s) for {} fatigue violation(s)".format(len(rows), len(fatigue)))
        return len(rows)

    def _pending(self):
        res = (self.client.table(TABLE).select("*")
               .eq("status", "queued").lt("attempts", MAX_ATTEMPTS)
               .order("day").limit(BATCH).execute())
        return res.data or []

    def drain(self, violation_lookup):
        """Send what is queued. violation_lookup(keys) -> {key: violation record} for the mail body."""
        if self.problem():
            return 0
        try:
            pending = self._pending()
        except Exception as e:
            self.last_error = str(e)[:300]
            return 0
        if not pending:
            return 0

        records = violation_lookup([r["violation_key"] for r in pending])
        by_email = {}
        for row in pending:
            by_email.setdefault(row["email"], []).append(row)

        sent = 0
        for email, rows in by_email.items():
            bodies = [records[r["violation_key"]] for r in rows if r["violation_key"] in records]
            keys = [r["violation_key"] for r in rows]
            if not bodies:
                # Queued but no longer in the archive (the day was rebuilt and it went away). There is
                # nothing to describe, so retire the row rather than retry it forever.
                self._mark(keys, email, "obsolete", "violation no longer in the archive")
                continue
            if len(bodies) == 1:
                subject = "Fatigue Driving Alert - {} - {} PKT".format(
                    bodies[0].get("vehicle"), bodies[0].get("time"))
            else:
                subject = "Fatigue Driving Alert - {} violations".format(len(bodies))
            try:
                send_mail(email, subject, bodies)
            except Exception as e:
                self.last_error = str(e)[:300]
                print("[Alerts] send to {} failed: {}".format(email, self.last_error))
                self._mark(keys, email, "queued", str(e)[:300], bump=True)
                continue
            self._mark(keys, email, "sent")
            sent += len(bodies)
            self.sent_total += len(bodies)
            print("[Alerts] sent {} fatigue violation(s) to {}".format(len(bodies), email))
        return sent

    def _mark(self, keys, email, status, error=None, bump=False):
        patch = {"status": status, "error": error}
        if status == "sent":
            patch["sent_at"] = datetime.now(timezone.utc).isoformat()
        try:
            for key in keys:
                if bump:
                    current = (self.client.table(TABLE).select("attempts")
                               .eq("violation_key", key).eq("email", email).execute().data or [{}])
                    patch["attempts"] = (current[0].get("attempts") or 0) + 1
                (self.client.table(TABLE).update(patch)
                 .eq("violation_key", key).eq("email", email).execute())
        except Exception as e:
            self.last_error = str(e)[:300]

    def start_worker(self, violation_lookup):
        with self._lock:
            if self._worker_started:
                return
            self._worker_started = True

        def run():
            while True:
                try:
                    self.drain(violation_lookup)
                except Exception as e:
                    self.last_error = str(e)[:300]
                    print("[Alerts] worker error: " + self.last_error)
                time.sleep(POLL_SEC)

        threading.Thread(target=run, name="fatigue-alerts", daemon=True).start()
        print("[Alerts] worker started")
