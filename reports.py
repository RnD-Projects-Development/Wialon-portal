"""
Historical violation reports
============================
Builds a frozen violation report for any period (1D / 7D / 15D / 30D or a custom range):

  Wialon alert types   read from each unit's violation event history (fast for any period)
  Fatigue Driving      calculated with fatigue_engine from raw telemetry

Downloading every vehicle's telemetry costs about two minutes per day, so two things keep it quick:

  1. Shortlist first. A Wialon "Trips" report over the whole fleet takes a few seconds and lists the
     vehicles with a long enough continuous trip. Wialon splits trips at stops of minStayTime (5 min on
     this fleet) and counts movement from 1 km/h, so its trips are never shorter than our driving runs:
     a vehicle that could break the limit cannot be missing from that list. If any unit's detector is
     configured with a shorter stop than the configured reset stop, the shortlist is skipped and every
     vehicle is downloaded instead.
  2. One day, one download. Each completed day (00:00-00:00 PKT) is calculated once and stored, so
     later reports covering that day cost nothing. Days are calculated with a lead-in from the
     previous day, so a drive that started before midnight is timed correctly.

The archive
-----------
Reports cover whole days ending yesterday, and every such day is kept in Supabase (violation_store):
all seven violation types, with telemetry, display location and driver already attached. A report is
then one database read. A background job keeps the archive complete:

  on start-up    calculates any of the last 30 days that is missing, failed, or used other limits
  after 00:00    recalculates yesterday and the day before (trackers out of coverage upload late),
                 then fills anything else missing
  limits change  recalculates the stored days with the new limits

Until the Supabase tables exist, completed days are cached on local disk instead.
"""

import gzip
import hashlib
import json
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import fatigue_engine
import fleet_violations as fv
import regions
import violation_store
from wialon_alerts import SessionPool, load_env

LEAD_IN_SEC = 3 * 3600        # history loaded before a period so runs starting earlier are timed correctly
CANDIDATE_MARGIN_SEC = 600    # shortlist threshold sits this far below the limit
EVENT_CHUNK = 100
DAY_SEC = 24 * 3600
TELEMETRY_LOOKBACK_SEC = 120   # window read around a violation to recover its telemetry reading
TELEMETRY_LOOKAHEAD_SEC = 60    # ...and past it: telling a false stop needs the driving reading after it
TELEMETRY_BATCH = 40
CACHE_VERSION = 7   # Driver Seat Belt Disconnected read as a violation type
ARCHIVE_DAYS = 30             # whole days the archive keeps complete: the longest shortcut
PRUNE_KEEP_DAYS = ARCHIVE_DAYS + 5   # days kept in Supabase. Past the window catch_up() rebuilds, or it
                                     # would download a pruned day again on every run; the five days of
                                     # room are the same REVIEW_HORIZON_SEC leaves
SETTLE_DAYS = 2               # the nightly run recalculates this many recent days (late uploads)
ARCHIVE_DELAY_SEC = 120       # the nightly run starts this long after 00:00 PKT
STORE_RECHECK_SEC = 60        # scheduler tick; also how often to look for the archive tables
RETRY_FAILED_SEC = 15 * 60    # a day that failed (Wialon timing out, say) is tried again after this long
LIMIT_KEYS = ("max_continuous_drive_day", "max_continuous_drive_night",
              "min_break_day", "min_break_night", "min_reset_stop")
WARM_PERIODS = (1, 7, 15, 30)  # the shortcuts, held in memory so even a first click is instant
REVIEW_HORIZON_SEC = 35 * DAY_SEC   # reviews held in memory: every shortcut's period, with room
REVIEWS_RETRY_SEC = 60              # how soon to try again when the reviews table cannot be read
REGIONS_TTL_SEC = 3600              # vehicle regions are re-read from Wialon at most hourly


def _fingerprint(items):
    return hashlib.sha1(json.dumps(items, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:16]


def violation_store_day(v):
    """PKT calendar day of a violation, 'YYYY-MM-DD' (the archive's day column)."""
    return day_name(v["time_unix"])


def day_start(ts):
    """Midnight (PKT) at or before ts."""
    d = datetime.fromtimestamp(ts, fv.PKT).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp())


def day_name(ts):
    return datetime.fromtimestamp(ts, fv.PKT).strftime("%Y-%m-%d")


def trips_template(min_duration_sec):
    """Self-contained 'Trips' report template, passed inline so nothing is saved to the Wialon account."""
    return {
        "id": 0,
        "n": "portal fatigue shortlist",
        "ct": "avl_unit_group",
        "p": "{}",
        "tbl": [{
            "n": "unit_group_trips",
            "l": "Trips",
            "c": "[\"time_begin\",\"time_end\",\"duration\"]",
            "cl": "[\"Beginning\",\"End\",\"Duration\"]",
            "cp": "[{\"calc_type\":5},{\"calc_type\":6},{\"calc_type\":1}]",
            "s": "", "sl": "", "sp": "",
            "filter_order": ["duration"],
            "p": json.dumps({"duration": {"min": int(min_duration_sec), "flags": 1}}),
            "sch": {"f1": 0, "f2": 0, "t1": 0, "t2": 0, "m": 0, "y": 0, "w": 0, "fl": 0},
            "f": 0,
        }],
    }


class ReportService:
    def __init__(self, cache_dir, driver_lookup=None, group_name="Unilever", store=None, locate=None,
                 notify=None):
        env = load_env()
        self.base_url = env.get("WIALON_BASE_URL", "https://hst-api.wialon.eu")
        self.token = env["WIALON_TOKEN"]
        self.cache_dir = cache_dir
        self.driver_lookup = driver_lookup or (lambda: {})
        self.group_name = group_name
        self.store = store                              # violation_store.ViolationStore, or None
        self.locate = locate or (lambda violations: violations)   # adds a display location to each
        self.notify = notify or (lambda violations: None)         # queues alert emails for a stored day
        # Driver names and display locations are attached while a day is built and cannot be added later.
        # A service built without them may still read the archive, but must not write to it.
        self.can_archive = driver_lookup is not None and locate is not None
        self.sessions = SessionPool(self.base_url)
        self.jobs = {}
        self.reports = {}
        self._lock = threading.Lock()
        self._store_ok = False
        self._store_checked = 0.0
        self._day_locks = {}
        self._catching_up = False
        self._rescan = False
        self._scheduler_started = False
        self._caught_up_at = 0.0      # when the last catch-up run ended
        self.generation = 0          # bumped whenever a stored day changes; keys the report cache
        self._built = {}             # (first day, last day) -> (generation, violations, meta)
        self.archive = {"running": False, "day": None, "done": 0, "total": 0, "reason": None,
                        "last_run": None, "last_error": None}
        self._reviews = {}            # {review key: verdict}; reviews change only through this portal
        self._reviews_loaded = False  # whether the read of the stored verdicts has succeeded
        self._reviews_at = 0.0
        self.reviews_error = None
        self._reviews_lock = threading.Lock()
        self._regions = {}            # {unit id: {"region", "raw", "source"}}
        self._regions_at = 0.0
        self.reviews_version = 0      # bumped on every review, so cached dashboards know to rebuild
        self.pinned = {}              # (first day, last day) -> the report a cached shortcut payload came from
        self.after_prewarm = None     # called once the shortcuts are held in memory
        os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------------------ Wialon helpers
    def _session(self):
        """One Wialon session, reused. Opening one per operation made hundreds of logins during an archive
        rebuild, and Wialon started refusing them."""
        return self.sessions.use()

    def _fleet(self, s):
        units = {u["id"]: u["nm"] for u in s.search("avl_unit", 1)}
        group = next((g for g in s.search("avl_unit_group", 1) if g["nm"] == self.group_name), None)
        resource = s.search("avl_resource", 1)[0]
        return units, group, resource

    def _shortlist_ok(self, s, unit_ids, reset_stop_sec):
        """True when every unit splits trips at a stop no shorter than our reset stop."""
        for i in range(0, len(unit_ids), EVENT_CHUNK):
            chunk = unit_ids[i:i + EVENT_CHUNK]
            res = s.call("core/batch", {"params": [{"svc": "unit/get_trip_detector", "params": {"itemId": u}} for u in chunk], "flags": 0})
            for r in res:
                if not isinstance(r, dict) or r.get("minStayTime") is None or r["minStayTime"] > reset_stop_sec:
                    return False
        return True

    def _candidates(self, s, resource, group, units, time_from, time_to, min_duration_sec):
        """Units with a Wialon trip at least min_duration_sec long, plus any unit outside the group."""
        s.call("report/cleanup_result", {})
        s.call("report/exec_report", {
            "reportResourceId": resource["id"], "reportTemplateId": 0, "reportObjectId": group["id"],
            "reportObjectSecId": 0, "interval": {"from": time_from, "to": time_to, "flags": 0},
            "reportTemplate": trips_template(min_duration_sec), "remoteExec": 1,
        })
        for _ in range(300):
            if str(s.call("report/get_report_status", {}).get("status")) == "4":
                break
            time.sleep(2)
        else:
            raise RuntimeError("trips report did not finish")
        result = s.call("report/apply_report_result", {}).get("reportResult", {})
        found = set()
        for ti, table in enumerate(result.get("tables", [])):
            if not table["rows"]:
                continue
            rows = s.call("report/select_result_rows", {
                "tableIndex": ti, "config": {"type": "range", "data": {"from": 0, "to": table["rows"] - 1, "level": 1}}})
            name_to_id = {nm: uid for uid, nm in units.items()}
            for row in rows:
                cells = row.get("c") or []
                for cell in cells[1:]:
                    if isinstance(cell, dict) and cell.get("u"):
                        found.add(int(cell["u"]))
                if len(cells) > 1 and not isinstance(cells[1], dict) and cells[1] != "-----":
                    found.add(name_to_id.get(cells[0]))
                for sub in row.get("r") or []:
                    for cell in (sub.get("c") or [])[1:]:
                        if isinstance(cell, dict) and cell.get("u"):
                            found.add(int(cell["u"]))
        s.call("report/cleanup_result", {})
        outside_group = [uid for uid in units if uid not in set(group.get("u") or [])]
        return sorted({uid for uid in found if uid} | set(outside_group))

    def _events(self, s, units, unit_ids, time_from, time_to, drivers):
        out = []
        for i in range(0, len(unit_ids), EVENT_CHUNK):
            chunk = unit_ids[i:i + EVENT_CHUNK]
            calls = [{"svc": "messages/load_interval",
                      "params": {"itemId": uid, "timeFrom": time_from, "timeTo": time_to,
                                 "flags": 0x0601, "flagsMask": 0xFF01, "loadCount": 0xFFFFFFFF}} for uid in chunk]
            for uid, r in zip(chunk, s.call("core/batch", {"params": calls, "flags": 0})):
                if not isinstance(r, dict) or "messages" not in r:
                    raise RuntimeError(f"event history failed for unit {uid}: {r}")
                out += [fv.event_violation(units, uid, m, k, drivers) for k, m in enumerate(r["messages"])
                        if (m.get("p") or {}).get("evt_name") in fv.EVT_NAME_TO_TYPE]
        s.call("messages/unload", {})
        return out

    def _telemetry(self, unit_ids, time_from, time_to):
        """Every data message with position for these units, fetched in parallel batches."""
        rows = {}
        chunks = [unit_ids[i:i + fv.BATCH_UNITS] for i in range(0, len(unit_ids), fv.BATCH_UNITS)]

        def work(my_chunks):
            with self._session() as s:
                for chunk in my_chunks:
                    calls = [c for uid in chunk for c in fv.telemetry_call(uid, time_from, time_to)]
                    res = s.call("core/batch", {"params": calls, "flags": 0})
                    for k, uid in enumerate(chunk):
                        loaded, msgs = res[3 * k], res[3 * k + 1]
                        if not isinstance(loaded, dict) or "count" not in loaded:
                            raise RuntimeError(f"load_interval failed for unit {uid}: {loaded}")
                        if loaded["count"] == 0:
                            rows[uid] = []
                            continue
                        if not isinstance(msgs, list) or len(msgs) != loaded["count"]:
                            raise RuntimeError(f"unit {uid}: expected {loaded['count']} messages")
                        rows[uid] = sorted((fv.message_row(m) for m in msgs), key=lambda r: r[0])

        workers = min(fv.WORKERS, max(1, len(chunks)))
        with ThreadPoolExecutor(workers) as ex:
            for f in [ex.submit(work, chunks[i::workers]) for i in range(workers)]:
                f.result()
        return rows

    # ------------------------------------------------------------------ building
    def _attach_telemetry(self, violations):
        """Read the telemetry message recorded at each violation that does not have one yet."""
        missing = [v for v in violations if not v.get("telemetry")]
        if not missing:
            return
        chunks = [missing[i:i + TELEMETRY_BATCH] for i in range(0, len(missing), TELEMETRY_BATCH)]

        def work(my_chunks):
            with self._session() as s:
                for chunk in my_chunks:
                    calls = [c for v in chunk
                             for c in fv.telemetry_call(v["vehicle_id"], v["time_unix"] - TELEMETRY_LOOKBACK_SEC,
                                                        v["time_unix"] + TELEMETRY_LOOKAHEAD_SEC)]
                    res = s.call("core/batch", {"params": calls, "flags": 0})
                    for k, v in enumerate(chunk):
                        loaded, msgs = res[3 * k], res[3 * k + 1]
                        if not isinstance(loaded, dict) or not loaded.get("count") or not isinstance(msgs, list):
                            continue
                        rows = sorted((fv.message_row(m) for m in msgs), key=lambda r: r[0])
                        v["telemetry"] = fv.telemetry_at(rows, v["time_unix"])

        workers = min(fv.WORKERS, max(1, len(chunks)))
        with ThreadPoolExecutor(workers) as ex:
            for f in [ex.submit(work, chunks[i::workers]) for i in range(workers)]:
                f.result()

    def _fatigue(self, unit_ids, units, drivers, window_from, window_to):
        """Fatigue Driving violations with a mark inside the window (history loaded from before it)."""
        if not unit_ids:
            return [], 0
        rows_by_unit = self._telemetry(unit_ids, window_from - LEAD_IN_SEC, window_to)
        violations, messages = [], 0
        for uid, rows in rows_by_unit.items():
            messages += len(rows)
            stream = [{"t": t, "pos": {"s": s, "y": y if y is not None else 0.0, "x": x if x is not None else 0.0},
                       "p": {"io_239": ign}} for (t, s, ign, y, x, _odo) in rows]
            result = fatigue_engine.analyze_telemetry_stream(
                stream, vehicle_name=units.get(uid, str(uid)), driver_name=drivers.get(uid, "Unassigned"))
            for v in result["violations"]:
                if v.get("violation_type") != "CONTINUOUS_DRIVE_EXCEEDED":
                    continue
                record = fv.fatigue_violation(uid, v)
                if window_from <= record["time_unix"] < window_to:
                    record["telemetry"] = fv.telemetry_at(rows, record["time_unix"])
                    violations.append(record)
        return violations, messages

    def build_window(self, time_from, time_to, progress=None):
        """Every violation with a time inside [time_from, time_to)."""
        cfg = fatigue_engine.get_compliance_config()
        limit_sec = min(fatigue_engine.parse_time_str_to_minutes(cfg["max_continuous_drive_day"]),
                        fatigue_engine.parse_time_str_to_minutes(cfg["max_continuous_drive_night"])) * 60
        reset_stop_sec = fatigue_engine.parse_time_str_to_minutes(cfg["min_reset_stop"]) * 60

        with self._session() as s:
            units, group, resource = self._fleet(s)
            unit_ids = list(units)
            self.fleet_size, self._fleet_at = len(units), time.time()
            drivers = {}
            try:
                drivers = self.driver_lookup() or {}
            except Exception:
                pass

            if progress:
                progress("Reading Wialon alerts")
            events = self._events(s, units, unit_ids, time_from, time_to, drivers)

            shortlist = unit_ids
            if group and self._shortlist_ok(s, unit_ids, reset_stop_sec):
                if progress:
                    progress("Finding vehicles with long trips")
                shortlist = self._candidates(s, resource, group, units, time_from - LEAD_IN_SEC, time_to,
                                             max(60, limit_sec - CANDIDATE_MARGIN_SEC))

        if progress:
            progress(f"Checking fatigue on {len(shortlist)} vehicle(s)")
        fatigue, messages = self._fatigue(shortlist, units, drivers, time_from, time_to)

        violations = [v for v in events if time_from <= v["time_unix"] < time_to] + fatigue
        if progress:
            progress(f"Reading telemetry for {len(violations)} violation(s)")
        self._attach_telemetry(violations)
        return {"violations": sorted(violations, key=lambda v: v["time_unix"], reverse=True),
                "units": units, "shortlisted": len(shortlist), "messages": messages}

    def _day_file(self, ts):
        return os.path.join(self.cache_dir, f"day_{day_name(ts)}.json.gz")

    def day_violations(self, ts, progress=None):
        """Violations for the whole day containing ts, cached once the day is over."""
        start = day_start(ts)
        end = start + DAY_SEC
        path = self._day_file(start)
        complete = end <= time.time()
        if complete and os.path.exists(path):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    cached = json.load(f)
                if cached.get("v") == CACHE_VERSION:
                    return cached["violations"]
            except Exception:
                pass
        built = self.build_window(start, min(end, int(time.time())), progress)
        if complete:
            try:
                with gzip.open(path, "wt", encoding="utf-8", compresslevel=1) as f:
                    json.dump({"v": CACHE_VERSION, "day": day_name(start), "built_at": int(time.time()),
                               "violations": built["violations"]}, f)
            except Exception as e:
                print(f"[Reports] could not cache {day_name(start)}: {e}")
        return built["violations"]

    def build_report(self, time_from, time_to, label, progress=None):
        """A frozen report for [time_from, time_to). Whole finished days come from the archive; anything
        else from the local day cache plus a live calculation of partial days."""
        violations = []
        day = day_start(time_from)
        days = []
        while day < time_to:
            days.append(day)
            day += DAY_SEC
        whole_days = (time_from == day_start(time_from) and time_to == day_start(time_to)
                      and time_to <= time.time())
        archive = {"source": "live"}
        if whole_days and self.store_ready():
            violations, archive = self._from_archive(days, progress)
        else:
            for i, day in enumerate(days):
                if progress:
                    progress(f"{day_name(day)} ({i + 1} of {len(days)})")
                whole_day_inside = day >= time_from and day + DAY_SEC <= time_to
                if whole_day_inside or day + DAY_SEC <= time.time():
                    got = self.day_violations(day, progress)
                else:
                    got = self.build_window(max(day, time_from), min(day + DAY_SEC, time_to), progress)["violations"]
                violations += [v for v in got if time_from <= v["time_unix"] < time_to]

        seen, unique = set(), []
        for v in sorted(violations, key=lambda v: v["time_unix"], reverse=True):
            key = (v["vehicle_id"], v["type"], v["time_unix"])
            if key not in seen:
                seen.add(key)
                unique.append(v)
        return {
            "id": uuid.uuid4().hex[:12],
            "label": label,
            "fleet_size": archive.get("fleet_size") or self.fleet_count(),
            "from_unix": time_from,
            "to_unix": time_to,
            "from": fv.fmt_time(time_from),
            # a whole-day report ends at 23:59:59 of its last day, not 00:00 of the next
            "to": fv.fmt_time(time_to - 1 if whole_days else time_to),
            "generated_at": fv.fmt_time(int(time.time())),
            "days": len(days),
            "archive": archive,
            "violations": unique,
        }

    def _from_archive(self, days, progress=None):
        """Every violation stored for these days. A day not archived yet is calculated first (oldest
        first, while the background job works newest first); a day stored with older limits is still
        served, and listed so the page can say so, because the background job is already redoing it."""
        first, last = day_name(days[0]), day_name(days[-1])
        with self._lock:
            hit = self._built.get((first, last))
            if hit and hit[0] == self.generation:
                return hit[1], hit[2]
            generation = self.generation

        rows = self.store.days(first, last)
        missing = [d for d in days if (rows.get(day_name(d)) or {}).get("status") != "done"]
        for day in missing:
            self.ensure_day(day, "missing", progress)
        if missing:
            rows = self.store.days(first, last)
            with self._lock:
                generation = self.generation

        if progress:
            progress("Reading the archive")
        violations = self.store.load(first, last)
        meta = self._archive_meta(rows, [day_name(d) for d in missing])
        with self._lock:
            self._built[(first, last)] = (generation, violations, meta)
        return violations, meta

    def _archive_meta(self, rows, calculated_now=()):
        limits = self.current_limits()
        return {
            "source": "archive",
            "fleet_size": max([r.get("fleet_size") or 0 for r in rows.values()] or [0]),
            "stale_days": sorted(d for d, r in rows.items() if r.get("limits") != limits),
            "archived_at": max([r.get("built_at") or "" for r in rows.values()] or [""])[:19].replace("T", " "),
            "calculated_now": list(calculated_now),
        }

    # ------------------------------------------------------------------ reviews and regions
    def reviews(self, retry=False):
        """{review key: 'GENUINE' | 'FALSE'} for the shortcut periods. Read from Supabase once and then
        kept current by set_review, since reviews are only ever made through this portal. If the first
        read fails, only the archive job (retry=True) tries again, so no page load waits on it."""
        if self.store is None:
            return {}
        due = self._reviews_at == 0 or (retry and time.time() - self._reviews_at >= REVIEWS_RETRY_SEC)
        if not self._reviews_loaded and due:
            with self._reviews_lock:
                if not self._reviews_loaded:
                    self._reviews_at = time.time()
                    try:
                        stored = self.store.load_reviews(time.time() - REVIEW_HORIZON_SEC)
                        # a verdict set through this portal while the read was owed is already in the
                        # table, but keep it anyway in case the read crossed the write
                        stored.update(self._reviews)
                        self._reviews = stored
                        self._reviews_loaded = True
                        self.reviews_error = None
                        self.reviews_version += 1
                    except Exception:
                        self.reviews_error = self.store.reviews_problem() or "reviews could not be read"
        return self._reviews

    def set_review(self, key, verdict):
        """Save a verdict (None clears it) and keep the in-memory copy in step."""
        self.store.set_review(key, verdict)
        with self._reviews_lock:
            if verdict:
                self._reviews[key] = verdict
            else:
                self._reviews.pop(key, None)
            # reviews_error is left alone on purpose: writing one verdict says nothing about whether
            # the stored ones could be read, and clearing it here would hide that they are missing
            self.reviews_version += 1

    def vehicle_regions(self):
        """{unit id: {"region", "raw", "source"}} from each vehicle's Wialon record, re-read hourly."""
        if self._regions and time.time() - self._regions_at < REGIONS_TTL_SEC:
            return self._regions
        try:
            with self._session() as s:
                units = s.search("avl_unit", 0x1 | 0x8 | 0x80)
                groups = s.search("avl_unit_group", 1)
            self._regions = regions.vehicle_regions(units, groups)
            self.fleet_size, self._fleet_at = len(units), time.time()
            self._regions_at = time.time()
        except Exception as e:
            print(f"[Regions] could not read vehicle regions: {e}")
            self._regions_at = time.time() - REGIONS_TTL_SEC + 60     # try again in a minute
        return self._regions

    def regions_version(self):
        """Fingerprint of every vehicle's region: changes only when a region does."""
        return _fingerprint(sorted((uid, r["region"]) for uid, r in self.vehicle_regions().items()))

    def reviews_fingerprint(self):
        """Fingerprint of the verdicts: changes only when a review does (a counter would restart with
        the server and could repeat a version a browser already holds)."""
        with self._reviews_lock:
            if getattr(self, "_fp_at", None) != self.reviews_version:
                self._fp = _fingerprint(sorted(self._reviews.items()))
                self._fp_at = self.reviews_version
            return self._fp

    def annotate(self, violations, verdicts=True):
        """Attach the vehicle's region and (unless verdicts=False) the review verdict to each violation,
        in place. Payloads the browser caches leave verdicts out: they change with every review."""
        known, where = (self.reviews() if verdicts else {}), self.vehicle_regions()
        for v in violations:
            if verdicts:
                v["verdict"] = known.get(violation_store.review_key(v))
            region = where.get(v["vehicle_id"]) or {}
            v["vehicle_region"] = region.get("region") or regions.UNASSIGNED
            v["vehicle_region_raw"] = region.get("raw") or ""
            v["vehicle_region_source"] = region.get("source") or "Not set in Wialon"
        return violations

    def prewarm(self):
        """Hold every shortcut period in memory, so the first click after midnight or a restart is as
        quick as the second. One read of the longest period; the shorter ones are cut from it."""
        self.vehicle_regions()
        self.reviews()
        if not self.store_ready():
            return
        today = day_start(int(time.time()))
        longest = max(WARM_PERIODS)
        days = [today - (longest - i) * DAY_SEC for i in range(longest)]
        try:
            violations, _meta = self._from_archive(days)
            rows = self.store.days(day_name(days[0]), day_name(days[-1]))
        except Exception as e:
            print(f"[Archive] could not pre-load the shortcuts: {e}")
            return
        with self._lock:
            generation = self.generation
            for n in WARM_PERIODS:
                if n == longest:
                    continue
                first, last = day_name(today - n * DAY_SEC), day_name(today - DAY_SEC)
                part = [v for v in violations if first <= violation_store_day(v) <= last]
                part_rows = {d: r for d, r in rows.items() if first <= d <= last}
                self._built[(first, last)] = (generation, part, self._archive_meta(part_rows))
        print(f"[Archive] shortcuts pre-loaded: {len(violations)} violations over {longest} days")
        if self.after_prewarm:
            try:
                self.after_prewarm()
            except Exception as e:
                print(f"[Archive] packaging the shortcuts failed: {e}")

    # ------------------------------------------------------------------ the archive
    def fleet_count(self):
        """Vehicles in the account: from the last calculation, else looked up (at most once an hour)."""
        known = getattr(self, "fleet_size", 0)
        if known and time.time() - getattr(self, "_fleet_at", 0.0) < 3600:
            return known
        try:
            with self._session() as s:
                self.fleet_size = len(s.search("avl_unit", 1))
            self._fleet_at = time.time()
        except Exception as e:
            print(f"[Reports] fleet size lookup failed: {e}")
        return getattr(self, "fleet_size", 0)

    @staticmethod
    def current_limits():
        """What a stored day was calculated with: the limits, and the calculation's version."""
        cfg = fatigue_engine.get_compliance_config()
        return {**{k: cfg[k] for k in LIMIT_KEYS}, "engine": fatigue_engine.ENGINE_VERSION}

    def store_ready(self):
        """True once the Supabase archive tables exist; looked for again every minute until they do."""
        if self._store_ok or self.store is None:
            return self._store_ok
        if time.time() - self._store_checked >= STORE_RECHECK_SEC:
            self._store_checked = time.time()
            try:
                self._store_ok = self.store.ready()
            except Exception:
                self._store_ok = False
            if self._store_ok:
                print("[Archive] Supabase archive tables found")
        return self._store_ok

    def _day_lock(self, name):
        with self._lock:
            return self._day_locks.setdefault(name, threading.Lock())

    def archive_day(self, day_ts, progress=None):
        """Calculate one whole PKT day and store it, replacing whatever was stored for it."""
        start = day_start(day_ts)
        name = day_name(start)
        limits = self.current_limits()
        started = time.time()
        if not self.can_archive:
            raise RuntimeError(f"refusing to archive {name}: this ReportService has no driver lookup or "
                               "location resolver, so the day would be stored with every driver Unassigned")
        try:
            built = self.build_window(start, start + DAY_SEC, progress)
            violations = self.locate(built["violations"])    # display locations, resolved once
            self.store.save_day(name, violations, limits, {
                "fleet_size": len(built["units"]), "vehicles_checked": built["shortlisted"],
                "messages_read": built["messages"], "seconds": round(time.time() - started)})
        except Exception as e:
            try:
                self.store.mark_day(name, "failed", limits=limits, error=str(e)[:500])
            except Exception:
                pass
            raise
        with self._lock:
            self.generation += 1
            self._built.clear()
        print(f"[Archive] {name}: {len(violations)} violations stored ({round(time.time() - started)} s)")
        # Alerts are queued only once the day is safely stored, and never allowed to fail the archive:
        # a mail server that is down must cost us an email, not a day of violations.
        try:
            self.notify(violations)
        except Exception as e:
            print(f"[Archive] {name}: could not queue alert emails: {e}")
        return violations

    def ensure_day(self, day_ts, mode, progress=None):
        """Archive one day if it needs it. mode: 'missing' (only if never stored), 'current' (also if
        stored with other limits), 'force' (always). Waits if another job is calculating the same day."""
        name = day_name(day_ts)
        with self._day_lock(name):
            if mode != "force":
                row = self.store.days(name, name).get(name) or {}
                if row.get("status") == "done" and (mode == "missing" or row.get("limits") == self.current_limits()):
                    return False
            step = (lambda text: progress(f"Calculating {name}: {text}")) if progress else None
            if step:
                step("not archived yet")
            self.archive_day(day_ts, step)
            return True

    def archive_days(self):
        """The whole days the archive keeps complete, newest first: the last ARCHIVE_DAYS up to yesterday."""
        today = day_start(int(time.time()))
        return [today - i * DAY_SEC for i in range(1, ARCHIVE_DAYS + 1)]

    def days_to_calculate(self):
        days = self.archive_days()
        rows = self.store.days(day_name(days[-1]), day_name(days[0]))
        limits = self.current_limits()
        return [d for d in days
                if (rows.get(day_name(d)) or {}).get("status") != "done"
                or (rows.get(day_name(d)) or {}).get("limits") != limits]

    def catch_up(self, reason):
        """Calculate every archive day that is missing, failed, or used other limits. One run at a
        time: asking while one runs makes it look again when it finishes."""
        if not self.store_ready():
            return
        with self._lock:
            if self._catching_up:
                self._rescan = True
                return
            self._catching_up = True
        try:
            while True:
                with self._lock:
                    self._rescan = False
                todo = self.days_to_calculate()
                self.archive.update(running=bool(todo), done=0, total=len(todo), reason=reason, last_error=None)
                if todo:
                    print(f"[Archive] {reason}: {len(todo)} day(s) to calculate")
                for i, day in enumerate(todo):
                    self.archive.update(day=day_name(day), done=i)
                    try:
                        self.ensure_day(day, "current")
                    except Exception as e:
                        self.archive["last_error"] = f"{day_name(day)}: {e}"
                        print(f"[Archive] {day_name(day)} failed: {e}")
                with self._lock:
                    if not self._rescan:
                        break
            self.prewarm()
        finally:
            with self._lock:
                self._catching_up = False
            self._caught_up_at = time.time()
            self.archive.update(running=False, day=None, done=self.archive["total"],
                                last_run=fv.fmt_time(int(time.time())))

    def prune_archive(self):
        """Drop archived days that have fallen out of PRUNE_KEEP_DAYS, so the Supabase tables settle at
        the size of the window instead of growing for ever. Never allowed to fail the nightly run: rows
        left behind cost a little disk, while a raised exception would cost the days still to calculate."""
        if not self.store_ready():
            return
        try:
            deleted = self.store.prune(PRUNE_KEEP_DAYS)
        except Exception as e:
            print(f"[Archive] prune failed: {e}")
            return
        if any(deleted.values()):
            print("[Archive] pruned days older than {}: {}".format(
                PRUNE_KEEP_DAYS, ", ".join(f"{n} from {t}" for t, n in deleted.items() if n)))

    def limits_changed(self):
        """New fatigue limits: recalculate the archive in the background."""
        with self._lock:
            self._built.clear()
        threading.Thread(target=self.catch_up, args=("limits changed",), name="archive-limits", daemon=True).start()

    def archive_status(self):
        status = dict(self.archive)
        if not self._store_ok:
            self._store_checked = 0.0      # the status page always looks again, never shows a stale "no"
        status["ready"] = self.store_ready()
        if self.store is not None and not status["ready"]:
            try:
                status["problems"] = self.store.table_problems()
            except Exception as e:
                status["problems"] = {"supabase": str(e)}
        if status["ready"]:
            try:
                days = self.archive_days()
                rows = self.store.days(day_name(days[-1]), day_name(days[0]))
                done = sorted(d for d, r in rows.items() if r.get("status") == "done")
                status.update(stored_days=len(done), window_days=len(days),
                              newest=done[-1] if done else None, oldest=done[0] if done else None,
                              failed=sorted(d for d, r in rows.items() if r.get("status") == "failed"))
            except Exception as e:
                status["read_error"] = str(e)
        return status

    # ------------------------------------------------------------------ background jobs
    def start(self, time_from, time_to, label):
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self.jobs[job_id] = {"state": "running", "step": "Starting", "started": time.time(),
                                 "label": label, "from": fv.fmt_time(time_from), "to": fv.fmt_time(time_to)}

        def run():
            def progress(step):
                with self._lock:
                    self.jobs[job_id]["step"] = step
            try:
                report = self.build_report(time_from, time_to, label, progress)
                with self._lock:
                    self.reports[report["id"]] = report
                    for old in list(self.reports)[:-5]:
                        self.reports.pop(old, None)
                    self.jobs[job_id].update(state="done", report_id=report["id"],
                                             seconds=round(time.time() - self.jobs[job_id]["started"]))
            except Exception as e:
                traceback.print_exc()
                with self._lock:
                    self.jobs[job_id].update(state="error", error=str(e))

        threading.Thread(target=run, name=f"report-{job_id}", daemon=True).start()
        return job_id

    def start_scheduler(self):
        """The archive job. Fills the archive as soon as the tables exist, then just after every 00:00
        PKT recalculates the most recent days and fills anything missing. Checks the clock once a minute
        rather than sleeping until midnight, so a machine that was asleep still runs it on waking."""
        with self._lock:
            if self._scheduler_started:
                return
            self._scheduler_started = True

        def next_run():
            return day_start(int(time.time())) + DAY_SEC + ARCHIVE_DELAY_SEC

        def run():
            due = next_run()
            self.archive["next_run"] = fv.fmt_time(due)
            filled = False
            while True:
                try:
                    if not self._reviews_loaded:
                        self.reviews(retry=True)          # the reviews table may have been created since
                    if self.store_ready():
                        if not filled:
                            filled = True
                            self.catch_up("start-up")
                        if time.time() >= due:
                            due = next_run()
                            self.archive["next_run"] = fv.fmt_time(due)
                            today = day_start(int(time.time()))
                            for i in range(1, SETTLE_DAYS + 1):       # yesterday first, so 1D is ready soonest
                                name = day_name(today - i * DAY_SEC)
                                try:
                                    self.archive.update(running=True, day=name, reason="nightly")
                                    self.ensure_day(today - i * DAY_SEC, "force")
                                except Exception as e:
                                    self.archive["last_error"] = f"{name}: {e}"
                                    print(f"[Archive] nightly {name} failed: {e}")
                            self.archive.update(running=False, day=None)
                            self.catch_up("nightly")
                            self.prune_archive()
                        elif self.archive.get("last_error") and time.time() - self._caught_up_at >= RETRY_FAILED_SEC:
                            self.catch_up("retrying failed days")
                    elif time.time() >= due:
                        # no archive tables yet: keep the local day cache warm instead
                        due = next_run()
                        self.archive["next_run"] = fv.fmt_time(due)
                        finished = day_start(int(time.time())) - DAY_SEC
                        print(f"[Reports] caching {day_name(finished)} locally (archive tables not found)")
                        self.day_violations(finished)
                except Exception as e:
                    print(f"[Archive] scheduler: {e}")
                time.sleep(STORE_RECHECK_SEC)

        threading.Thread(target=run, name="violation-archive", daemon=True).start()

    def clear_cache(self):
        """Drop cached days and built reports: the limits changed, so they must be calculated again."""
        with self._lock:
            self.reports.clear()
        for name in os.listdir(self.cache_dir):
            if name.startswith("day_") and name.endswith(".json.gz"):
                try:
                    os.remove(os.path.join(self.cache_dir, name))
                except OSError:
                    pass

    def job(self, job_id):
        with self._lock:
            return dict(self.jobs.get(job_id) or {})

    def report(self, report_id):
        with self._lock:
            return self.reports.get(report_id) or next(
                (r for r in self.pinned.values() if r["id"] == report_id), None)

    def pin(self, rep):
        """Keep the report behind a cached shortcut payload reachable by its id (Save, exports)."""
        with self._lock:
            self.pinned[(rep["from"][:10], rep["to"][:10])] = rep

    def window_ready(self, time_from, time_to):
        """True when [from, to) is whole days that are all archived, so a report needs no calculating."""
        if time_from != day_start(time_from) or time_to != day_start(time_to) or time_to > time.time():
            return False
        if not self.store_ready():
            return False
        first, last = day_name(time_from), day_name(time_to - DAY_SEC)
        with self._lock:
            hit = self._built.get((first, last))
            if hit and hit[0] == self.generation:
                return True
        rows = self.store.days(first, last)
        return sum(1 for r in rows.values() if r.get("status") == "done") >= (time_to - time_from) // DAY_SEC
