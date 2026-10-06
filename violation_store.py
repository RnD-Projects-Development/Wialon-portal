"""
Violations archive in Supabase
==============================
Each finished day (00:00-24:00 PKT) is calculated once and stored here, so reports read rows instead
of downloading telemetry. Tables are created by supabase_violations_schema.sql.

  portal_violations     one row per violation, the same record the portal shows
  portal_archive_days   one row per calculated day: status, counts, and the limits used

A day is written as: mark it "building", delete its old rows, insert the new ones, mark it "done".
Readers only trust days marked "done", so a job that dies half way leaves the day to be rebuilt,
never half a day that looks complete.

prune() drops days that have fallen out of the archive window, so the tables settle at the size of
that window instead of growing for ever. Reviews are deliberately left out of it: see the method.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import fleet_violations as fv
import notifier

VIOLATIONS = "portal_violations"
DAYS = "portal_archive_days"
REVIEWS = "portal_violation_reviews"
ALERTS = "portal_fatigue_alerts"
VERDICTS = ("GENUINE", "FALSE")
PAGE = 1000          # PostgREST returns at most this many rows per request
LOAD_WORKERS = 6     # pages fetched at once when reading a long period
INSERT_CHUNK = 500
# enqueue() will not offer a violation older than MAX_ALERT_AGE_DAYS again, so an alert row past that
# age (with room to spare) can no longer stop a second email and is only taking up space.
ALERT_KEEP_DAYS = notifier.MAX_ALERT_AGE_DAYS + 4

# violation record key -> column; everything the portal shows for a violation
COLUMNS = ["id", "type", "type_label", "source", "time_unix", "vehicle_id", "vehicle", "driver",
           "speed_kmh", "continuous_drive_minutes", "rest_minutes", "period", "lat", "lon",
           "address", "location", "details", "telemetry", "text_names_other_unit"]


def pkt_day(ts):
    return datetime.fromtimestamp(int(ts), fv.PKT).strftime("%Y-%m-%d")


def review_key(v):
    """What a review is attached to: vehicle, type and second. Survives the nightly recalculation of a
    day, and is the key the report uses to merge Wialon's double-fired alerts."""
    return int(v["vehicle_id"]), str(v["type"]), int(v["time_unix"])


def to_row(v):
    row = {k: v.get(k) for k in COLUMNS}
    row["day"] = pkt_day(v["time_unix"])
    row["time_pkt"] = fv.fmt_time(v["time_unix"])
    row["text_names_other_unit"] = bool(v.get("text_names_other_unit"))
    return row


def from_row(row):
    v = {k: row.get(k) for k in COLUMNS}
    v["time_unix"] = int(row["time_unix"])
    v["vehicle_id"] = int(row["vehicle_id"])
    v["time"] = fv.fmt_time(v["time_unix"])
    telemetry = row.get("telemetry")
    v["telemetry"] = json.loads(telemetry) if isinstance(telemetry, str) else telemetry
    return v


class ViolationStore:
    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------------ status
    def table_problems(self):
        """{table: what is wrong} for each archive table the portal cannot use; empty when ready."""
        if self.client is None:
            return {t: "Supabase is not connected (check SUPABASE_URL and SUPABASE_KEY in .env)"
                    for t in (VIOLATIONS, DAYS)}
        problems = {}
        for table in (VIOLATIONS, DAYS):
            try:
                self.client.table(table).select("*").limit(1).execute()
            except Exception as e:
                text = str(e)
                if "PGRST205" in text:
                    problems[table] = "table does not exist: run supabase_violations_schema.sql"
                elif "42501" in text:
                    problems[table] = ("permission denied: run the two GRANT lines at the end of "
                                       "supabase_violations_schema.sql")
                else:
                    problems[table] = text[:200]
        return problems

    def ready(self):
        return not self.table_problems()

    # ------------------------------------------------------------------ reviews
    def reviews_problem(self):
        """What stops reviews being saved, or None when they can be."""
        if self.client is None:
            return "Supabase is not connected"
        try:
            self.client.table(REVIEWS).select("*").limit(1).execute()
            return None
        except Exception as e:
            text = str(e)
            if "PGRST205" in text:
                return "the reviews table does not exist: run supabase_violations_schema.sql again"
            if "42501" in text:
                return "permission denied on the reviews table: run the GRANT lines in supabase_violations_schema.sql"
            return text[:200]

    def load_reviews(self, since_unix):
        """{review_key: 'GENUINE' | 'FALSE'} for violations at or after since_unix."""
        out, start = {}, 0
        while True:
            res = (self.client.table(REVIEWS).select("vehicle_id,type,time_unix,verdict")
                   .gte("time_unix", int(since_unix)).order("time_unix").order("vehicle_id").order("type")
                   .range(start, start + PAGE - 1).execute())
            batch = res.data or []
            for r in batch:
                out[(int(r["vehicle_id"]), r["type"], int(r["time_unix"]))] = r["verdict"]
            if len(batch) < PAGE:
                return out
            start += PAGE

    def set_review(self, key, verdict):
        """Save a verdict for one violation; None clears it back to pending."""
        vehicle_id, vtype, time_unix = key
        if verdict is None:
            (self.client.table(REVIEWS).delete().eq("vehicle_id", vehicle_id)
             .eq("type", vtype).eq("time_unix", time_unix).execute())
            return
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS} or None")
        self.client.table(REVIEWS).upsert(
            {"vehicle_id": vehicle_id, "type": vtype, "time_unix": time_unix, "verdict": verdict,
             "reviewed_at": datetime.now(fv.PKT).isoformat()},
            on_conflict="vehicle_id,type,time_unix").execute()

    # ------------------------------------------------------------------ days
    def days(self, first_day, last_day):
        """{ 'YYYY-MM-DD': day row } for days between first_day and last_day inclusive."""
        res = (self.client.table(DAYS).select("*")
               .gte("day", first_day).lte("day", last_day).execute())
        return {r["day"]: r for r in res.data or []}

    def done_days(self, first_day, last_day):
        return {d: r for d, r in self.days(first_day, last_day).items() if r.get("status") == "done"}

    def mark_day(self, day, status, **fields):
        row = {"day": day, "status": status, "built_at": datetime.now(fv.PKT).isoformat(), **fields}
        self.client.table(DAYS).upsert(row, on_conflict="day").execute()

    def save_day(self, day, violations, limits, meta):
        """Replace everything stored for one PKT day with this calculation."""
        self.mark_day(day, "building", limits=limits)
        self.client.table(VIOLATIONS).delete().eq("day", day).execute()
        rows = [to_row(v) for v in violations]
        for i in range(0, len(rows), INSERT_CHUNK):
            self.client.table(VIOLATIONS).upsert(rows[i:i + INSERT_CHUNK], on_conflict="id").execute()
        self.mark_day(day, "done", limits=limits, violations=len(rows),
                      fatigue=sum(1 for v in violations if v["type"] == "FATIGUE_DRIVING"),
                      error=None, **meta)

    # ------------------------------------------------------------------ violations
    def load(self, first_day, last_day, types=None):
        """Every stored violation between first_day and last_day inclusive, newest first. The first page
        also returns the total, and the remaining pages are then fetched side by side: a round trip to
        Supabase costs over half a second, so 30 days (~8 pages) one after another took several."""
        def page(start, count=None):
            query = (self.client.table(VIOLATIONS).select("*", count=count)
                     .gte("day", first_day).lte("day", last_day))
            if types:
                query = query.in_("type", list(types))
            # id breaks ties between violations in the same second, so pages never overlap or skip
            return query.order("time_unix", desc=True).order("id").range(start, start + PAGE - 1).execute()

        first = page(0, count="exact")
        rows = list(first.data or [])
        total = first.count if first.count is not None else len(rows)
        starts = list(range(PAGE, total, PAGE))
        if starts:
            with ThreadPoolExecutor(min(LOAD_WORKERS, len(starts))) as ex:
                for res in ex.map(page, starts):
                    rows += res.data or []
        return [from_row(r) for r in rows]

    # ------------------------------------------------------------------ retention
    def prune(self, keep_days):
        """Delete archived days older than keep_days. Returns {table: rows deleted}.

        keep_days has to stay clear of the window catch_up() rebuilds (reports.ARCHIVE_DAYS): pruning a
        day that is still in it makes catch_up find the day missing and download it from Wialon again,
        on every run. Violations go before the day row, so a prune that dies half way through leaves a
        day that reports read as never calculated -- which by then it is -- rather than one that looks
        calculated and empty.

        Reviews are never pruned. Everything else here can be rebuilt from Wialon; a person's verdict on
        a violation cannot, and the table costs a few hundred bytes a row.
        """
        today = datetime.now(fv.PKT).date()
        deleted = {}
        cutoff = (today - timedelta(days=keep_days)).isoformat()
        deleted[VIOLATIONS] = self._delete_before(VIOLATIONS, cutoff)
        deleted[DAYS] = self._delete_before(DAYS, cutoff)
        alert_cutoff = (today - timedelta(days=ALERT_KEEP_DAYS)).isoformat()
        deleted[ALERTS] = self._delete_before(ALERTS, alert_cutoff, sent_only=True)
        return deleted

    def _delete_before(self, table, cutoff, sent_only=False):
        """Rows with day < cutoff. "minimal" stops Supabase returning every row it deleted, which on a
        first prune of a table left to grow is megabytes of egress for a number we get from count."""
        query = self.client.table(table).delete(count="exact", returning="minimal").lt("day", cutoff)
        if sent_only:
            # keep a row the worker could still act on; one it has given up on (attempts exhausted) has
            # nothing left to do and would otherwise sit there for ever
            query = query.or_(f"status.neq.queued,attempts.gte.{notifier.MAX_ATTEMPTS}")
        return query.execute().count or 0
