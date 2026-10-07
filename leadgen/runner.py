"""Daily orchestrator.

One run = plan check -> discovery (Google Maps, with fallbacks) -> enrichment
(website, web search, Instagram) in parallel -> qualification -> Google Sheets
sync -> report.  Every step is a durable task; a failure is recorded on that
task and never stops the others. The run always ends with a sync + report,
even when it was cut short by the time budget or a stop signal.

Stopping rule: keep discovering while today's new leads < daily target, or while
searches of today's scheduled part (or earlier, unfinished parts) remain.
"""
from __future__ import annotations

import os
import signal
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .config import Config
from .db import DB
from .enrich.emails import MXChecker
from .enrich.extract import canonical_social
from .enrich.instagram import InstagramUnavailable, fetch_profile, handle_from_url, profile_matches
from .enrich.phones import parse_phone
from .enrich.search import WebSearch
from .enrich.social import LookupResult, social_lookup
from .enrich.website import SiteResult, crawl_site, normalize_url
from .geo import haversine_km
from .net import BreakerOpen, DeadlineReached, FetchError, Http, NetworkDown
from .planner import PlanMismatch, Planner
from .providers.base import Place, ProviderUnavailable
from .providers.gmaps import GoogleMapsSearch
from .providers.osm import Overpass
from .providers.overture import SOURCE_URL as OVERTURE_URL
from .providers.overture import OvertureStore
from .providers.fsq import FoursquareAPI
from .providers.places_api import PlacesAPI
from .quality import is_aggregator, is_chain, is_link_hub, is_qualified, match_category, name_score
from .report import contact_coverage, lead_row, markdown_summary, masked_samples, plan_rows
from .util import get_logger, jdump, jload, local_date, norm_text

log = get_logger("runner")

PRIORITY = {"site": 1, "social": 2, "insta": 3, "li": 4, "api": 5}
PROVIDER_SOURCE = {"gmaps": "google_maps", "places_api": "places_api", "osm": "osm", "overture": "overture"}
LINKEDIN_CATEGORIES = {"interior_designer", "event_planner", "coworking", "banquet_venue", "hotel"}


class RunLock:
    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            return True
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fh.write(str(os.getpid()))
            self.fh.flush()
            return True
        except OSError:
            return False

    def release(self):
        if self.fh:
            try:
                import fcntl

                fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
            except Exception:
                pass


class Runner:
    def __init__(self, cfg: Config, db: DB, *, budget_minutes: float | None = None, target: int | None = None,
                 use_sheets: bool = True, discovery: bool = True, max_searches: int | None = None,
                 http: Http | None = None, gmaps=None, search: WebSearch | None = None, sheets_factory=None,
                 workers: int | None = None, now_fn=time.time, enrich: bool = True, overture=None, fsq=None):
        self.cfg, self.db = cfg, db
        rt = cfg["runtime"]
        self.budget_s = float(budget_minutes if budget_minutes is not None else rt["time_budget_minutes"]) * 60
        self.margin_s = min(float(rt["safety_margin_minutes"]) * 60, self.budget_s * 0.3)
        dt = target if target is not None else cfg["plan"]["daily_target"]
        self._auto_target = str(dt) == "auto"
        self.target = 150 if self._auto_target else int(dt)
        self.use_sheets = use_sheets and cfg["sheets"]["enabled"]
        self.discovery_requested = discovery
        self.max_searches = max_searches
        self.enrich_enabled = enrich
        self.workers = int(workers or cfg["enrich"]["workers"])
        self._http_override, self._gmaps_override, self._search_override = http, gmaps, search
        self._overture_override = overture
        self._fsq_override = fsq
        self.sheets_factory = sheets_factory
        self.now = now_fn
        self.stop_requested = False
        self.warnings: list[str] = []
        self.stats: Counter = Counter()
        self.parts_worked: set = set()
        self.network_failures = 0
        self.osm_cache: dict = {}
        self.center = (float(cfg["area"]["center"][0]), float(cfg["area"]["center"][1]))
        self.radius = float(cfg["area"]["radius_km"])
        self.region = cfg["campaign"]["country"]
        self.city = cfg["area"].get("name") or ""
        self._sheet = None
        self._sheet_totals = [0, 0]  # rows added, updated this run (checkpoints + final sync)
        self.checkpoint_s = float(cfg["sheets"].get("checkpoint_minutes") or 0) * 60

    # ------------------------------------------------------------------ setup
    def _setup_clients(self):
        cfg = self.cfg
        from .config import BOT_NAME, BOT_UA

        bot = {"user_agent": BOT_UA, "robot_name": BOT_NAME} if cfg.open_data else {}
        self.http = self._http_override or Http(use_curl_cffi=cfg["runtime"]["use_curl_cffi"], deadline=self.deadline,
                                                default_interval=float(cfg["enrich"]["site_interval_s"]), **bot)
        if self._http_override is not None:
            self.http.deadline = self.deadline
            if cfg.open_data:
                self.http.user_agent, self.http.robot_name = BOT_UA, BOT_NAME
        d = cfg["discovery"]
        self.gmaps = self._gmaps_override or GoogleMapsSearch(self.http, lang=cfg["campaign"]["language"],
                                                              region=cfg["campaign"]["region"], variant=d["gmaps_variant"],
                                                              interval=float(d["gmaps_interval_s"]), jitter=float(d["gmaps_jitter_s"]))
        self.places_api = PlacesAPI(self.http, self.db, daily_cap=int(d["places_api_daily_cap"]),
                                    monthly_cap=int(d["places_api_monthly_cap"]), lang=cfg["campaign"]["language"],
                                    region=self.region)
        self.overpass = Overpass(self.http, timeout=90)
        self.overture = self._overture_override or OvertureStore(self.db, cfg)
        self.search = self._search_override or WebSearch(self.http, interval=float(cfg["enrich"]["search_interval_s"]))
        self.fsq = self._fsq_override or FoursquareAPI(self.http)
        self.mx = MXChecker(enabled=bool(cfg["enrich"]["check_email_mx"]))

    def _install_signals(self):
        def handler(signum, _frame):
            log.warning("stop signal %s received - finishing current step, then saving", signum)
            self.stop_requested = True
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass

    # ------------------------------------------------------------------ main
    def run(self) -> tuple[int, dict]:
        lock = RunLock(self.db.path + ".lock")
        if not lock.acquire():
            log.warning("another run is already using %s - exiting", self.db.path)
            return 0, {"status": "skipped (another run in progress)"}
        try:
            return self._run()
        finally:
            lock.release()

    def _run(self) -> tuple[int, dict]:
        cfg, db = self.cfg, self.db
        self.started = self.now()
        self.deadline = self.started + self.budget_s - self.margin_s
        self.today = local_date(cfg.tz, self.started)
        abandoned = db.close_abandoned_runs()
        stale = db.reset_stale_running()
        if abandoned or stale:
            log.info("recovered from an interrupted run (%d runs, %d tasks put back in the queue)", abandoned, stale)
        self.run_id = db.start_run(self.today)
        self._install_signals()
        self._setup_clients()
        log.info("run %s for %s: budget %.0f min, target %d new leads, workers %d", self.run_id, self.today,
                 self.budget_s / 60, self.target, self.workers)
        status, code = "complete", 0
        planner = Planner(cfg, db, self.http)
        sheets_result: dict = {"status": "not run"}
        try:
            plan = planner.ensure_plan()
            # Planning (first run only, bounded to a few minutes) does not eat the discovery budget.
            planning_s = self.now() - self.started
            if planning_s > 30:
                self.deadline = self.now() + self.budget_s - self.margin_s
                self.http.deadline = self.deadline
                log.info("planning took %.1f min; the %.0f-minute work budget starts now", planning_s / 60, self.budget_s / 60)
            if not db.get_meta("plan_logged"):
                log.info("plan: %d parts, %d search squares, %d searches (density source: %s)", plan["parts"], plan["cells"],
                         plan["search_tasks"], plan["density_source"])
                db.set_meta("plan_logged", "1")
            self._resolve_auto_target(plan)
            self.plan_day = planner.day_number(self.today)
            sched = planner.scheduled_part(self.today)
            self.sched = dict(sched) if sched else None
            if self.sched:
                log.info("plan day %d: scheduled part %d - %s", self.plan_day, self.sched["id"], self.sched["name"])
            else:
                log.info("plan day %d: outside the planned period (finishing any remaining work)", self.plan_day)
            self._loop()
            try:
                self._add_role_candidates()
            except Exception as exc:  # noqa: BLE001 - a candidate-generation slip must not fail the run
                log.warning("role-email candidates skipped: %s", exc)
            planner.refresh_part_status(self.today)
        except PlanMismatch as exc:
            self.warnings.append(str(exc))
            log.error("%s", exc)
            status, code = "config error", 1
        except NetworkDown as exc:
            self.warnings.append(f"network unavailable: {exc}")
            status, code = "network down", 2
        except Exception as exc:  # noqa: BLE001 - always save and report
            log.error("unexpected error: %s\n%s", exc, traceback.format_exc())
            self.warnings.append(f"unexpected error: {type(exc).__name__}: {exc}")
            status, code = "crashed (state saved)", 2
        # Always finish with sync + report.
        try:
            if code != 1:
                sheets_result = self._sync_sheets(planner)
                if sheets_result["status"].startswith("error"):
                    code = max(code, 2)
                    status = status if status != "complete" else "complete (sheet sync failed)"
        except Exception as exc:  # noqa: BLE001
            sheets_result = {"status": f"error: {exc}"}
            code = max(code, 2)
        if self.stats["provider_unavailable"] and not self.stats["searches"] and self.stats["searches_attempted"]:
            self.warnings.append("all discovery providers were unavailable this run")
            code = max(code, 2)
        if (code == 0 and self.discovery_requested and not self.stats["searches_attempted"] and self.max_searches != 0
                and planner.has_plan() and self._open_searches() and self._qualified_today() < self.target):
            self.warnings.append("no searches were run this time (time budget used up before discovery) - work continues next run")
        if self.enrich_enabled and self.cfg["enrich"]["social_search"] and not self.search.available():
            self.warnings.append("web search unavailable this run - Instagram/Facebook lookups postponed")
        if self.stats["gmaps_empty_unconfirmed"] >= 10 and not self.stats["returned_gmaps"]:
            self.warnings.append(f"Google Maps answered {self.stats['gmaps_empty_unconfirmed']} searches with no businesses at all - "
                                 "its response format may have changed or it is soft-blocking (searches kept for later; "
                                 "run the Live source probe)")
            code = max(code, 2)
            if status == "complete":
                status = "degraded (Google Maps returned nothing)"
        if self.stats["searches_attempted"] and not self.stats["searches"] and self.stats["searches_failed"]:
            self.warnings.append(f"none of the {self.stats['searches_attempted']} searches succeeded "
                                 f"(last error: {getattr(self, 'last_search_error', '')})")
            code = max(code, 2)
            if status == "complete":
                status = "degraded (searches failing)"
        summary = self._summary(planner, status, sheets_result)
        db.finish_run(self.run_id, status, summary)
        self._write_outputs(summary)
        return code, summary

    def _add_role_candidates(self) -> None:
        """For a lead with its own website but no published e-mail, add info@/contact@ as UNVERIFIED
        candidates when the domain can receive mail. The standard 'role-based' method - never shown as
        confirmed (kept in the Other Contacts column), so nothing is invented in the Emails column."""
        if not self.cfg["enrich"].get("role_email_candidates"):
            return
        from .enrich.emails import FREE_PROVIDERS, normalize_email
        from .enrich.extract import host_of, registrable
        from .quality import is_aggregator

        mx = self.mx if getattr(self.mx, "enabled", False) else MXChecker(enabled=True)
        rows = self.db.q("SELECT key, website FROM places WHERE qualified=1 AND excluded IS NULL AND merged_into IS NULL "
                         "AND qualified_date=? AND website!='' ORDER BY lead_no DESC", (self.today,))
        # Own small time budget (the main loop has usually spent the deadline by now); leave room for the sheet sync.
        end = self.now() + max(5.0, min(90.0, self.margin_s * 0.4))
        checked: dict = {}
        added = 0
        for r in rows:
            if self.stop_requested or self.now() >= end:
                break
            if self.db.one("SELECT 1 FROM contacts WHERE place_key=? AND kind='email' AND confidence!='low'", (r["key"],)):
                continue
            host = host_of(r["website"])
            dom = registrable(host) if host else ""
            if not dom or dom in FREE_PROVIDERS or is_aggregator(r["website"]):
                continue
            if dom not in checked:
                try:
                    checked[dom] = mx.has_mx(dom)
                except Exception:  # noqa: BLE001
                    checked[dom] = None
            if checked[dom] is not True:
                continue
            for local in ("info", "contact"):
                e = normalize_email(f"{local}@{dom}")
                if e and self.db.add_contact(r["key"], "email", e, label="role address (guessed; domain accepts mail - verify before use)",
                                             source="guess", source_url=r["website"], confidence="low", evidence="role-based address, not published"):
                    added += 1
        if added:
            self.stats["role_email_candidates"] = added
            log.info("added %d role-email candidates (unverified) for leads with a website but no published e-mail", added)

    def _resolve_auto_target(self, plan: dict) -> None:
        """daily_target = "auto": aim for (all businesses in the area / number of days) each day,
        so the whole area is covered over the campaign. Each day's part already holds ~this many."""
        if not self._auto_target:
            return
        days = max(1, int(plan.get("parts") or self.cfg["plan"]["days"] or 1))
        total = 0
        try:
            total = self.overture.mapped_count()
        except Exception as exc:  # noqa: BLE001 - never fail the run over the count
            log.warning("auto target: could not count businesses (%s) - using 150/day", exc)
        if total > 0:
            import math
            self.target = max(50, min(3000, math.ceil(total / days)))
            log.info("auto daily target: %d businesses in the area / %d days = %d leads/day", total, days, self.target)

    # ------------------------------------------------------------------ loop
    def _loop(self):
        pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="enrich")
        inflight: dict = {}
        self.discovery_on = self.discovery_requested
        sched_id = self.sched["id"] if self.sched else None
        finish_part = bool(self.cfg["plan"]["finish_scheduled_part"])
        checkpoints = self.checkpoint_s > 0 and self.use_sheets and bool(self.cfg.sheet_id or self.sheets_factory)
        next_checkpoint = self.now() + self.checkpoint_s
        try:
            while True:
                if self.stop_requested:
                    self.warnings.append("stopped early by a stop signal")
                    break
                if self.now() >= self.deadline:
                    log.info("time budget reached")
                    break
                if self.network_failures >= 6 and not self.http.network_ok():
                    raise NetworkDown("internet connection lost during the run")
                self._harvest(inflight, block=False)
                if checkpoints and self.now() >= next_checkpoint:
                    checkpoints = self._checkpoint_sync()
                    next_checkpoint = self.now() + self.checkpoint_s
                qualified = self._qualified_today()
                need_target = qualified < self.target
                need_schedule = finish_part and sched_id is not None and self._open_searches(upto_part=sched_id) > 0
                did = False
                if (self.discovery_on and (need_target or need_schedule) and len(inflight) < self.workers * 4
                        and (self.max_searches is None or self.stats["searches_attempted"] < self.max_searches)):
                    task = self._next_search(None if need_target else sched_id)
                    if task is None:
                        # Nothing runnable now: plan finished, remaining searches deferred, or (target met)
                        # only later parts left. The need only shrinks during a run, so stop discovering.
                        if not self._open_searches():
                            if self._requeue_failed_searches():
                                continue
                            log.info("all planned searches are done")
                        self.discovery_on = False
                    else:
                        self._run_search(task)
                        did = True
                while len(inflight) < self.workers * 2:
                    t = self._next_enrichment()
                    if t is None:
                        break
                    self.db.set_running(t["id"])
                    inflight[pool.submit(self._execute, dict(t))] = dict(t)
                    did = True
                if not did:
                    if inflight:
                        self._harvest(inflight, block=True, timeout=2.0)
                    else:
                        break
        finally:
            # Drain: give in-flight work a bounded chance to finish, then put the rest back in the queue.
            end = min(self.now() + 90, self.deadline + self.margin_s * 0.5)
            while inflight and self.now() < end:
                self._harvest(inflight, block=True, timeout=2.0)
            with self.db.tx():
                for t in inflight.values():
                    self.db.defer(t["id"], 0, "run ended before this finished")
            pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ queue helpers
    def _qualified_today(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM places WHERE qualified=1 AND excluded IS NULL AND qualified_date=?", (self.today,), 0))

    def _open_searches(self, upto_part: int | None = None) -> int:
        if upto_part is None:
            return int(self.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search' AND status IN ('pending','running')", default=0))
        return int(self.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search' AND status IN ('pending','running') AND part_id<=?", (upto_part,), 0))

    def _requeue_failed_searches(self) -> int:
        """Once the plan is otherwise finished, give each failed search one more chance (a day later)."""
        cutoff = self.now() - 24 * 3600
        with self.db.tx():
            cur = self.db.conn.execute(
                "UPDATE tasks SET status='pending', attempts=0, next_at=0, result=?, updated_at=? "
                "WHERE kind='search' AND status='failed' AND updated_at<? AND (result IS NULL OR result NOT LIKE '%requeued%')",
                ('{"requeued": 1}', self.now(), cutoff))
        if cur.rowcount:
            log.info("plan finished except %d failed searches - retrying them once", cur.rowcount)
        return cur.rowcount

    def _ready_count(self, kind: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind=? AND status='pending' AND next_at<=?", (kind, self.now()), 0))

    def _next_search(self, upto_part: int | None):
        if upto_part is None:
            return self.db.one("SELECT * FROM tasks WHERE kind='search' AND status='pending' AND next_at<=? ORDER BY part_id, seq LIMIT 1", (self.now(),))
        return self.db.one("SELECT * FROM tasks WHERE kind='search' AND status='pending' AND next_at<=? AND part_id<=? ORDER BY part_id, seq LIMIT 1",
                           (self.now(), upto_part))

    def _next_enrichment(self):
        if not self.enrich_enabled:
            return None
        kinds = [k for k in ("site", "social", "insta", "li", "api") if self._kind_enabled(k)]
        if not kinds:
            return None
        marks = ",".join("?" for _ in kinds)
        return self.db.one(f"SELECT * FROM tasks WHERE kind IN ({marks}) AND status='pending' AND next_at<=? ORDER BY seq, id LIMIT 1",
                           (*kinds, self.now()))

    def _kind_enabled(self, kind: str) -> bool:
        e = self.cfg["enrich"]
        if kind == "site":
            return bool(e["website"])
        if kind in ("social", "li"):
            return bool(e["social_search"]) and self.search.available()
        if kind == "insta":
            return bool(e["instagram_profile"]) and not self.http.breaker("instagram").is_open()
        if kind == "api":
            return bool(e.get("api_enrich")) and self.fsq.available() and not self.http.breaker("fsq").is_open()
        return False

    def _site_rejected(self, key: str, url: str) -> bool:
        """True when this URL was already crawled for this place and turned out to be a hijacked domain."""
        row = self.db.one("SELECT result FROM tasks WHERE key=?", (f"site:{key}:{url}"[:300],))
        return bool(row and (jload(row["result"], {}) or {}).get("status") in ("hijacked", "moved"))

    def _listing_phones(self, key: str) -> list[str]:
        return [r["value"] for r in self.db.q("SELECT value FROM contacts WHERE place_key=? AND kind='phone' AND "
                                              "source IN ('google_maps','places_api','osm')", (key,))]

    def _enqueue(self, kind: str, place_key: str, payload: dict, part_id=None, delay: float = 0.0):
        if kind == "site" and "phones" not in payload:
            payload = {**payload, "phones": self._listing_phones(place_key)}
        # Band 0: the day's first ~target leads are enriched before surplus/backlog (band 1),
        # so each day's batch gets complete contact details quickly.
        band = 0 if self._qualified_today() < self.target * 1.2 else 1
        seq = (band * 10 + PRIORITY[kind]) * 10**13 + int(self.now() * 1000)
        return self.db.enqueue(kind, f"{kind}:{place_key}:{payload.get('url') or payload.get('handle') or ''}"[:300], payload,
                               seq=seq, part_id=part_id, place_key=place_key, next_at=self.now() + delay)

    # ------------------------------------------------------------------ discovery
    def _run_search(self, task):
        p = jload(task["payload"], {})
        if not p.get("primary", True):
            prim = self.db.one("SELECT status, result FROM tasks WHERE key=?", (p.get("primary_key"),))
            if prim is not None and prim["status"] in ("pending", "running"):
                with self.db.tx():
                    self.db.defer(task["id"], 900, "waiting for primary query")
                return
            first = (jload(prim["result"], {}) or {}).get("first_page", 0) if prim is not None else 0
            if prim is None or prim["status"] != "done" or first < 18:
                with self.db.tx():
                    self.db.complete(task["id"], {"skipped": "primary query found few places here"}, status="skipped")
                self.stats["searches_skipped_sparse"] += 1
                return
        self.stats["searches_attempted"] += 1
        with self.db.tx():
            self.db.set_running(task["id"])
        self.last_search_error = ""
        places, meta, provider, error = None, {}, None, None
        unavailable = []
        for prov in self.cfg["discovery"]["providers"]:
            try:
                if prov == "gmaps":
                    if self.http.breaker("gmaps").is_open():
                        unavailable.append("gmaps (paused after repeated blocks)")
                        continue
                    places, meta = self.gmaps.search(p["query"], p["lat"], p["lng"], p["zoom"],
                                                     max_pages=int(self.cfg["discovery"]["max_pages"]),
                                                     is_known=self._is_known)
                elif prov == "places_api":
                    if not self.places_api.enabled() or self.http.breaker("places_api").is_open():
                        continue
                    places, meta = self.places_api.search(p["query"], p["lat"], p["lng"], p["size_km"])
                elif prov == "overture":
                    # Open data: one local lookup per square and category answers all its queries.
                    places = [] if not p.get("primary", True) else self.overture.search_cell(p["lat"], p["lng"], p["size_km"], p["category"])
                    meta = {"pages": [{"valid": len(places)}]}
                elif prov == "osm":
                    places, meta = self._osm_search(p)
                provider = prov
                break
            except NetworkDown:
                with self.db.tx():
                    self.db.defer(task["id"], 600, "network down")
                raise
            except DeadlineReached:
                with self.db.tx():
                    self.db.defer(task["id"], 0, "run deadline")
                return
            except (ProviderUnavailable, BreakerOpen) as exc:
                unavailable.append(f"{prov}: {exc}")
                places = None
                continue
            except FetchError as exc:
                error = exc
                places = None
                break
        if places is None:
            with self.db.tx():
                if error is not None:
                    # Searches get more patience than other tasks: an outage must not leave holes in the map.
                    st = self.db.fail(task["id"], f"{provider or 'search'}: {error}", max_attempts=5,
                                      backoff=(900, 2 * 3600, 12 * 3600, 24 * 3600))
                    self.stats["searches_failed"] += 1
                    self.last_search_error = str(error)[:200]
                    log.warning("search failed (%s): %s -> %s", p["query"], error, st)
                else:
                    self.db.defer(task["id"], 3600, "; ".join(unavailable)[:300] or "no provider available")
                    self.stats["provider_unavailable"] += 1
                    msg = "discovery providers unavailable: " + ("; ".join(unavailable)[:300] or "none enabled")
                    if msg not in self.warnings:
                        self.warnings.append(msg)
                    self.discovery_on = False
            return
        if provider == "gmaps" and not places and not self.stats["returned_gmaps"]:
            # An empty answer before Maps has returned anything this run cannot be told apart from a
            # silent format change or soft block: keep the search for later instead of marking the
            # square as empty. Ten in a row stop discovery for this run and turn it red.
            self.stats["gmaps_empty_unconfirmed"] += 1
            with self.db.tx():
                self.db.defer(task["id"], 6 * 3600, "empty Google Maps answer before any result this run")
            if self.stats["gmaps_empty_unconfirmed"] >= 10:
                self.discovery_on = False
            return
        with self.db.tx():
            new = self._ingest(places, task, p, provider)
            first_page = (meta.get("pages") or [{}])[0].get("valid", len(places)) if provider == "gmaps" else len(places)
            self.db.complete(task["id"], {"provider": provider, "returned": len(places), "new": new, "first_page": first_page,
                                          "pages": len(meta.get("pages") or [1])})
        self.stats["searches"] += 1
        self.stats[f"searches_{provider}"] += 1
        self.stats[f"returned_{provider}"] += len(places)
        self.parts_worked.add(task["part_id"])
        if provider != self.cfg["discovery"]["providers"][0]:
            msg = f"main discovery source unavailable for some searches; used fallback provider '{provider}'"
            if msg not in self.warnings:
                self.warnings.append(msg)
        log.info("search %-22s @%.4f,%.4f z%s via %s: %d results, %d new", f"'{p['query']}'", p["lat"], p["lng"], p["zoom"], provider, len(places), new)

    def _is_known(self, key: str) -> bool:
        return self.db.one("SELECT 1 FROM places WHERE key=?", (key,)) is not None

    def _osm_search(self, p: dict):
        cell = p["cell_id"]
        if cell not in self.osm_cache:
            filters = sorted({f for c in self.cfg.categories for f in c.get("osm", [])})
            self.osm_cache[cell] = self.overpass.search_cell(p["lat"], p["lng"], p["size_km"], filters)
        cat = self.cfg.category(p["category"]) or {}
        wanted = [f.split('"')[1::2] for f in cat.get("osm", [])]
        out = []
        for place in self.osm_cache[cell]:
            labels = {c.replace(" ", "_") for c in place.categories}
            if any(len(w) >= 2 and w[1] in labels for w in wanted):
                out.append(place)
        return out, {"pages": [{"valid": len(out)}]}

    # ------------------------------------------------------------------ ingest
    def _ingest(self, places: list[Place], task, payload: dict, provider: str) -> int:
        new = 0
        filters = self.cfg["filters"]
        for pl in places:
            if pl.lat is None or pl.lng is None or not pl.name:
                continue
            if haversine_km(self.center[0], self.center[1], pl.lat, pl.lng) > self.radius:
                self.stats["outside_area"] += 1
                continue
            if provider == "overture" and (not pl.area or pl.area.lower() in (self.city.lower(), "")):
                cell = self.db.one("SELECT name FROM cells WHERE id=?", (payload.get("cell_id"),))
                if cell and cell["name"]:
                    pl.area = f"{cell['name']}, {pl.area}" if pl.area else cell["name"]
            existing = self.db.find_existing(pl.place_id, pl.data_id, pl.key) or self._fuzzy_duplicate(pl)
            if existing:
                self._merge_into(existing, pl, provider)
                self.stats["seen_again"] += 1
                continue
            category = pl.extra.get("category") or match_category(pl.categories, payload["category"], self.cfg.categories)
            excluded = None
            chain = is_chain(pl.name, filters["exclude_chains"]) or (is_chain(pl.extra["brand"], filters["exclude_chains"])
                                                                     if pl.extra.get("brand") else None)
            if not chain and pl.extra.get("brand_known"):
                chain = f"brand {pl.extra.get('brand') or 'listed on Wikidata'}"   # known multi-outlet brand
            if filters["exclude_closed"] and pl.closed:
                excluded = f"closed ({pl.status_text or 'per listing'})"
            elif chain:
                excluded = f"chain ({chain})"
            elif any(norm_text(w) and norm_text(w) in norm_text(pl.name) for w in filters["exclude_name_words"]):
                excluded = "excluded name word"
            elif category is None:
                if filters["allow_unmatched_categories"]:
                    category = payload["category"]
                else:
                    excluded = "category not relevant (" + ", ".join(pl.categories[:3]) + ")"
            self.db.insert_place({
                "key": pl.key, "name": pl.name, "category": category, "gcategories": pl.categories, "address": pl.address,
                "area": pl.area, "city": pl.city, "lat": pl.lat, "lng": pl.lng, "rating": pl.rating, "reviews": pl.reviews,
                "website": "", "maps_url": pl.maps_link(), "place_id": pl.place_id, "data_id": pl.data_id,
                "description": pl.description, "status_text": pl.status_text, "provider": provider, "query": payload["query"],
                "part_id": task["part_id"], "cell_id": payload["cell_id"], "found_date": self.today, "excluded": excluded})
            new += 1
            self.stats["places_new"] += 1
            if excluded:
                self.stats["excluded:" + excluded.split(" (")[0]] += 1
                continue
            self._provider_contacts(pl.key, pl, provider)
            self._plan_enrichment(pl.key, pl.name, pl.website, pl.area, category, task["part_id"])
            self._qualify(pl.key)
        return new

    def _fuzzy_duplicate(self, pl: Place) -> str | None:
        for r in self.db.nearby_places(pl.lat, pl.lng, 0.0015, 0.0016):
            if haversine_km(pl.lat, pl.lng, r["lat"], r["lng"]) <= 0.15 and name_score(pl.name, r["name"]) >= 0.9:
                return r["merged_into"] or r["key"]
        return None

    def _merge_into(self, key: str, pl: Place, provider: str):
        row = self.db.get_place(key)
        if row is None or row["excluded"]:
            return
        updates = {}
        for col, val in (("address", pl.address), ("area", pl.area), ("rating", pl.rating), ("reviews", pl.reviews),
                         ("description", pl.description)):
            if val and not row[col]:
                updates[col] = val
        if updates:
            self.db.update_place(key, **updates)
            self.db.mark_dirty(key)
        self._provider_contacts(key, pl, provider)
        if pl.website and not row["website"] and not self.db.one("SELECT 1 FROM tasks WHERE place_key=? AND kind='site'", (key,)):
            self._plan_enrichment(key, row["name"], pl.website, row["area"] or "", row["category"], row["part_id"], website_only=True)
        self._qualify(key)

    def _provider_contacts(self, key: str, pl: Place, provider: str):
        if provider == "overture":
            if pl.extra:
                self._open_data_contacts(key, pl)
            return
        src = PROVIDER_SOURCE.get(provider, provider)
        conf = "high" if provider in ("gmaps", "places_api") else "medium"
        for raw in (pl.phone_intl, pl.phone):
            parsed = parse_phone(raw, self.region) if raw else None
            if parsed:
                self.db.add_contact(key, "phone", parsed[0], label=parsed[1], source=src, source_url=pl.maps_link(),
                                    confidence=conf, evidence="business listing")
                break
        if provider == "osm" and pl.extra:
            from .enrich.emails import normalize_email

            for k, v in pl.extra.items():
                if "email" in k:
                    e = normalize_email(v)
                    if e:
                        self.db.add_contact(key, "email", e, source="osm", source_url=pl.maps_url, confidence="medium", evidence=k)
                elif k == "contact:instagram":
                    s = canonical_social(v if "/" in v else f"https://instagram.com/{v.lstrip('@')}")
                    if s:
                        self.db.add_contact(key, s[0], s[1], source="osm", source_url=pl.maps_url, confidence="medium", evidence=k)
                elif k in ("contact:whatsapp",):
                    parsed = parse_phone(v, self.region)
                    if parsed:
                        self.db.add_contact(key, "whatsapp", parsed[0], source="osm", source_url=pl.maps_url, confidence="medium", evidence=k)

    def _open_data_contacts(self, key: str, pl: Place):
        """Phones, emails and social pages the business published on its own Facebook page/listings, as
        released by Overture Maps (openly licensed). Kept with the source datasets as evidence."""
        from .enrich.emails import normalize_email, suspicious_email

        ex = pl.extra
        datasets = ",".join(ex.get("datasets") or []) or "overture"
        socials = [s for s in (canonical_social(u) for u in ex.get("socials") or []) if s]
        page = next((u for k, u in socials if k == "facebook"), "") or OVERTURE_URL
        ev = f"Overture Maps open data ({datasets}), record {ex.get('record', '')}"[:250]
        for raw in (ex.get("phones") or [])[:4]:
            parsed = parse_phone(raw, self.region)
            if parsed:
                self.db.add_contact(key, "phone", parsed[0], label=parsed[1], source="overture", source_url=page,
                                    confidence="medium", evidence=ev)
        for raw in (ex.get("emails") or [])[:3]:
            e = normalize_email(raw)
            if e:
                why = suspicious_email(e)
                self.db.add_contact(key, "email", e, label=why or "", source="overture", source_url=page,
                                    confidence="low" if why else "medium", evidence=ev)
        for kind, url in socials[:4]:
            # A Facebook page delivered by Meta's own dataset is the business's page by construction.
            conf = "high" if kind == "facebook" and "meta" in datasets else "medium"
            self.db.add_contact(key, kind, url, source="overture", source_url=url, confidence=conf, evidence=ev)

    def _plan_enrichment(self, key: str, name: str, website: str, area: str, category: str | None, part_id,
                         website_only: bool = False):
        e = self.cfg["enrich"]
        web = normalize_url(website) if website else ""
        social_kinds = [k for k in e["social_kinds"] if k in ("instagram", "facebook")]
        if web:
            s = canonical_social(web)
            if s:
                self.db.add_contact(key, s[0], s[1], source="google_maps", source_url=web, confidence="high",
                                    evidence="listed as the website on Google Maps")
                web = ""
            elif is_aggregator(web) and not is_link_hub(web):
                web = ""
        if web:
            self.db.update_place(key, website=web)
            if e["website"]:
                self._enqueue("site", key, {"url": web, "name": name, "area": area}, part_id)
        elif not website_only and e["social_search"]:
            self._enqueue("social", key, {"name": name, "area": area, "kinds": social_kinds, "want_website": True}, part_id)
        if not website_only and e["social_search"] and "linkedin" in e["social_kinds"] and category in LINKEDIN_CATEGORIES:
            self._enqueue("li", key, {"name": name, "area": area}, part_id)
        if not web and not website_only and e.get("api_enrich") and self.fsq.available():
            self._enqueue("api", key, {"name": name, "lat": self.db.scalar("SELECT lat FROM places WHERE key=?", (key,)),
                                       "lng": self.db.scalar("SELECT lng FROM places WHERE key=?", (key,))}, part_id)

    def _qualify(self, key: str):
        kinds = {r["kind"] for r in self.db.q("SELECT kind FROM contacts WHERE place_key=? AND confidence!='low'", (key,))}
        row = self.db.one("SELECT qualified, lead_no, excluded FROM places WHERE key=?", (key,))
        if row is None or row["excluded"]:
            return
        if is_qualified(kinds) and not row["qualified"]:
            no = row["lead_no"]
            if not no:
                no = int(self.db.get_meta("lead_counter", "0") or 0) + 1
                while self.db.one("SELECT 1 FROM places WHERE lead_no=?", (no,)):
                    no += 1
                self.db.set_meta("lead_counter", str(no))
            self.db.update_place(key, qualified=1, qualified_date=self.today, lead_no=no, sync_state="pending")
            self.stats["new_leads"] += 1

    # ------------------------------------------------------------------ enrichment execution (worker threads)
    def _execute(self, t: dict):
        p = jload(t["payload"], {})
        kind = t["kind"]
        if kind == "site":
            return crawl_site(self.http, p["url"], p["name"], max_pages=int(self.cfg["enrich"]["max_pages_per_site"]),
                              region=self.region, interval=float(self.cfg["enrich"]["site_interval_s"]),
                              known_phones=tuple(p.get("phones") or ()))
        if kind == "social":
            return social_lookup(self.search, p["name"], p.get("area_short") or self._short_area(p.get("area", "")), self.city,
                                 kinds=tuple(p.get("kinds") or ("instagram", "facebook")), want_website=bool(p.get("want_website")),
                                 home_extra=self._home_terms(p.get("area", "")))
        if kind == "li":
            return social_lookup(self.search, p["name"], self._short_area(p.get("area", "")), self.city, kinds=("linkedin",),
                                 platform_word="linkedin", home_extra=self._home_terms(p.get("area", "")))
        if kind == "insta":
            return fetch_profile(self.http, p["handle"])
        if kind == "api":
            return self.fsq.enrich(p["name"], p["lat"], p["lng"])
        raise ValueError(f"unknown task kind {kind}")

    @staticmethod
    def _short_area(area: str) -> str:
        return area.split(",")[-1].strip() if area else ""

    def _home_terms(self, area: str) -> list[str]:
        """Names of the business's location that count as evidence when a profile mentions them."""
        parts = [a.strip() for a in (area or "").split(",") if len(a.strip()) >= 4]
        return parts + [a for a in self.cfg["area"].get("aliases", []) if a]

    def _harvest(self, inflight: dict, block: bool, timeout: float = 0.0):
        if not inflight:
            return
        if block:
            done, _ = wait(list(inflight), timeout=timeout, return_when=FIRST_COMPLETED)
        else:
            done = [f for f in inflight if f.done()]
        for fut in done:
            t = inflight.pop(fut)
            try:
                res = fut.result()
            except NetworkDown:
                self.network_failures += 1
                with self.db.tx():
                    self.db.defer(t["id"], 600, "network down")
                continue
            except DeadlineReached:
                with self.db.tx():
                    self.db.defer(t["id"], 0, "run deadline")
                continue
            except InstagramUnavailable as exc:
                with self.db.tx():
                    self.db.complete(t["id"], {"skipped": f"instagram unavailable: {exc}"}, status="skipped")
                self.stats["insta_unavailable"] += 1
                continue
            except BreakerOpen as exc:
                with self.db.tx():
                    self.db.defer(t["id"], 6 * 3600, str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - isolate every task
                with self.db.tx():
                    st = self.db.fail(t["id"], f"{type(exc).__name__}: {exc}")
                self.stats[f"{t['kind']}_failed"] += 1
                log.debug("task %s failed: %s (%s)", t["key"], exc, st)
                continue
            self.network_failures = 0
            try:
                with self.db.tx():
                    self._apply(t, res)
            except Exception as exc:  # noqa: BLE001
                log.error("could not record result of %s: %s\n%s", t["key"], exc, traceback.format_exc())
                with self.db.tx():
                    self.db.fail(t["id"], f"record error: {exc}")

    # ------------------------------------------------------------------ enrichment results (main thread)
    def _apply(self, t: dict, res):
        key = t["place_key"]
        place = self.db.get_place(key)
        if place is None:
            self.db.complete(t["id"], {"skipped": "place missing"}, status="skipped")
            return
        kind = t["kind"]
        p = jload(t["payload"], {})
        if kind == "site":
            self._apply_site(t, place, res)
        elif kind in ("social", "li"):
            self._apply_social(t, place, res, p)
        elif kind == "insta":
            self._apply_insta(t, place, res, p)
        elif kind == "api":
            self._apply_api(t, place, res)
        self._qualify(key)
        self.stats[f"{kind}_done"] += 1

    def _have(self, key: str, kind: str) -> bool:
        return self.db.one("SELECT 1 FROM contacts WHERE place_key=? AND kind=? AND confidence!='low'", (key, kind)) is not None

    def _apply_site(self, t: dict, place, res: SiteResult):
        key = place["key"]
        added = 0
        for c in res.contacts:
            if c.kind == "email":
                domain = c.value.split("@", 1)[1]
                if self.mx.has_mx(domain) is False:
                    continue  # domain cannot receive email -> not a usable contact
            if self.db.add_contact(key, c.kind, c.value, label=c.label, source=c.source, source_url=c.source_url,
                                   confidence=c.confidence, evidence=c.evidence):
                added += 1
        if res.status in ("hijacked", "moved"):
            # The listed domain now belongs to a spam/unrelated site: don't show it as the business's website.
            site_url = jload(t["payload"], {}).get("url", "")
            if place["website"] and normalize_url(place["website"]) == normalize_url(site_url):
                self.db.update_place(key, website="")
                self.db.mark_dirty(key)
            log.info("website of %s ignored (%s): %s", place["name"], site_url, res.error)
        if res.description and not place["description"] and res.owned:
            self.db.update_place(key, description=res.description[:300])
            self.db.mark_dirty(key)
        if res.status == "error" and t["attempts"] < 1:
            self.db.fail(t["id"], res.error or "site error", max_attempts=2, backoff=(12 * 3600,))
        else:
            self.db.complete(t["id"], {"status": res.status, "pages": len(res.pages), "contacts_added": added,
                                       "name_match": round(res.name_match, 2), "error": res.error})
        self.stats[f"site_{res.status}"] += 1
        self._after_contacts(place, want_website=res.status in ("aggregator", "social", "error", "blocked_robots", "skipped", "hijacked",
                                                                "moved") or not res.owned)

    def _after_contacts(self, place, want_website: bool = False):
        """Queue the next useful step for this place."""
        key = place["key"]
        e = self.cfg["enrich"]
        insta = self.db.one("SELECT value, source FROM contacts WHERE place_key=? AND kind='instagram' AND confidence!='low' LIMIT 1", (key,))
        if insta is None:
            kinds = [k for k in e["social_kinds"] if k in ("instagram", "facebook") and not self._have(key, k)]
            if e["social_search"] and kinds and not self.db.one("SELECT 1 FROM tasks WHERE place_key=? AND kind='social'", (key,)):
                self._enqueue("social", key, {"name": place["name"], "area": place["area"] or "", "kinds": kinds,
                                              "want_website": want_website or not place["website"]}, place["part_id"])
        elif e["instagram_profile"] and not self._have(key, "email"):
            handle = handle_from_url(insta["value"])
            if handle:
                self._enqueue("insta", key, {"handle": handle, "name": place["name"], "from": insta["source"]}, place["part_id"])
        self._maybe_api(place)

    def _maybe_api(self, place) -> None:
        """Queue an official Foursquare lookup once, for a lead still missing a website (fills gaps legally)."""
        e = self.cfg["enrich"]
        key = place["key"]
        if not (e.get("api_enrich") and self.fsq.available()):
            return
        if place["website"] or self.db.one("SELECT 1 FROM tasks WHERE place_key=? AND kind='api'", (key,)):
            return
        self._enqueue("api", key, {"name": place["name"], "lat": place["lat"], "lng": place["lng"]}, place["part_id"])

    def _apply_api(self, t: dict, place, res: list):
        key = place["key"]
        added = 0
        for kind, value, conf, src_url, ev in res or []:
            if kind == "phone":
                parsed = parse_phone(value, self.region)
                if not parsed:
                    continue
                kind, value = "phone", parsed[0]
            elif kind == "website":
                value = normalize_url(value)
                s = canonical_social(value)
                if s:
                    kind, value = s
                elif not value or (is_aggregator(value) and not is_link_hub(value)):
                    continue
                else:
                    if not place["website"]:
                        self.db.update_place(key, website=value)
                        self.db.mark_dirty(key)
                        if self.cfg["enrich"]["website"] and not self._site_rejected(key, value):
                            self._enqueue("site", key, {"url": value, "name": place["name"], "area": place["area"] or "",
                                                        "found_by": "foursquare"}, place["part_id"])
                    continue
            if self.db.add_contact(key, kind, value, label="", source="foursquare", source_url=src_url, confidence=conf, evidence=ev):
                added += 1
        self.db.complete(t["id"], {"added": added, "found": len(res or [])})

    def _apply_social(self, t: dict, place, res: LookupResult, p: dict):
        key = place["key"]
        if res.results_seen == 0 and not self.search.available():
            self.db.defer(t["id"], 12 * 3600, "web search unavailable")
            return
        accepted = set()
        for kind, m in res.matches.items():
            # The account the business itself lists (Maps, its website) wins; search only fills gaps.
            own = {r["value"] for r in self.db.q(
                "SELECT value FROM contacts WHERE place_key=? AND kind=? AND confidence!='low' AND source IN "
                "('google_maps','website','jsonld','places_api','osm','instagram')", (key, kind))}
            conf, label = "medium", f"name match {m.score:.2f}"
            if m.strength != "strong":
                conf, label = "low", label + ", name too common to confirm"
            if own and m.url not in own:
                conf, label = "low", label + ", differs from the account the business lists"
            self.db.add_contact(key, kind, m.url, label=label, source="search", source_url=m.url, confidence=conf,
                                evidence=f"{m.engine} result: {m.title}"[:250])
            if conf != "low":
                accepted.add(kind)
            for ck, cv, cl in m.extra_contacts:
                self.db.add_contact(key, ck, cv, label=cl, source="search", source_url=m.url, confidence="low", evidence=m.title[:200])
        if res.website and not place["website"] and not self._site_rejected(key, res.website.url):
            self.db.update_place(key, website=res.website.url)
            self.db.mark_dirty(key)
            if self.cfg["enrich"]["website"]:
                self._enqueue("site", key, {"url": res.website.url, "name": place["name"], "area": place["area"] or "",
                                            "found_by": "search"}, place["part_id"])
        self.db.complete(t["id"], {"results": res.results_seen, "found": sorted(res.matches), "website": bool(res.website),
                                   "query": res.query})
        if "instagram" in accepted and self.cfg["enrich"]["instagram_profile"] and not self._have(key, "email"):
            handle = handle_from_url(res.matches["instagram"].url)
            if handle:
                self._enqueue("insta", key, {"handle": handle, "name": place["name"], "from": "search"}, place["part_id"])

    def _apply_insta(self, t: dict, place, prof, p: dict):
        key = place["key"]
        url = f"https://www.instagram.com/{prof.handle}/"
        if not prof.exists:
            if p.get("from") == "search":
                self.db.remove_contact(key, "instagram", url)
            self.db.complete(t["id"], {"exists": False})
            return
        score = profile_matches(place["name"], prof)
        if p.get("from") == "search" and score < 0.6:
            self.db.remove_contact(key, "instagram", url)
            self.db.complete(t["id"], {"exists": True, "rejected": f"profile name '{prof.full_name}' does not match"})
            return
        for kind, value, label, how in prof.contacts:
            self.db.add_contact(key, kind, value, label=label, source="instagram", source_url=url,
                                confidence="high" if how == "profile-field" else "medium", evidence=how)
        if prof.external_url and not place["website"]:
            web = normalize_url(prof.external_url)
            if web and not canonical_social(web) and (not is_aggregator(web) or is_link_hub(web)) and not self._site_rejected(key, web):
                self.db.update_place(key, website=web)
                self.db.mark_dirty(key)
                self._enqueue("site", key, {"url": web, "name": place["name"], "area": place["area"] or "", "found_by": "instagram"},
                              place["part_id"])
        self.db.complete(t["id"], {"exists": True, "contacts": len(prof.contacts), "is_business": prof.is_business})

    # ------------------------------------------------------------------ outputs
    def _sync_sheets(self, planner: Planner) -> dict:
        if not self.use_sheets:
            return {"status": "disabled"}
        sid = self.cfg.sheet_id
        if not sid and self.sheets_factory is None:
            self.warnings.append("Google Sheet not configured (set PP_SHEET_ID) - leads are kept in the state database")
            return {"status": "not configured"}
        from .sheets import SheetsError

        try:
            self._sync_leads()
            added, updated = self._sheet_totals
            sync = self._sheet_client()
            sync.write_plan(plan_rows(self.db, self.cfg, planner.start_date()))
            sync.append_report(self._report_row(planner, added, updated))
            log.info("Google Sheet updated: %d rows added, %d updated", added, updated)
            return {"status": "ok", "added": added, "updated": updated}
        except SheetsError as exc:
            msg = f"Google Sheet sync failed: {exc}"
            self.warnings.append(msg)
            log.error("%s", msg)
            return {"status": f"error: {exc}"}

    def _sheet_client(self):
        if self._sheet is None:
            from .sheets import SheetsClient, SheetsSync

            sh = self.cfg["sheets"]
            sync = self.sheets_factory() if self.sheets_factory else SheetsSync(
                SheetsClient(self.cfg.sheet_id), sh["leads_tab"], sh["plan_tab"], sh["report_tab"])
            sync.ensure_tabs()
            self._sheet = sync
        return self._sheet

    def _sync_leads(self) -> tuple[int, int]:
        """Write new and changed lead rows to the sheet (rows are matched by Key, so this is safe to repeat)."""
        sync = self._sheet_client()
        cond = "qualified=1" if self.cfg["filters"]["require_contact"] else "1=1"
        pending = self.db.q(f"SELECT * FROM places WHERE sync_state='pending' AND excluded IS NULL AND merged_into IS NULL AND {cond} "
                            "ORDER BY lead_no, first_seen")
        rows = [lead_row(self.db, p, self.cfg) for p in pending]
        added, updated, adopted = sync.upsert_leads(rows) if rows else (0, 0, {})
        with self.db.tx():
            for p in pending:
                self.db.update_place(p["key"], sync_state="synced", synced_at=self.now())
            for k, sheet_id in adopted.items():
                try:
                    no = int(sheet_id.split("-")[-1])
                except ValueError:
                    continue
                if not self.db.one("SELECT 1 FROM places WHERE lead_no=? AND key!=?", (no, k)):
                    self.db.update_place(k, lead_no=no, sync_state="synced")
        self._sheet_totals[0] += added
        self._sheet_totals[1] += updated
        return added, updated

    def _checkpoint_sync(self) -> bool:
        """Mid-run copy of new leads to the sheet, so a lost runner loses little. Returns False to stop checkpoints."""
        try:
            added, updated = self._sync_leads()
            log.info("checkpoint: Google Sheet +%d rows, %d updated", added, updated)
            return True
        except Exception as exc:  # noqa: BLE001 - the final sync retries and reports
            log.warning("checkpoint sync skipped (%s) - the end-of-run sync will retry", exc)
            return False

    def _report_row(self, planner: Planner, added: int, updated: int) -> list:
        cov = contact_coverage(self.db, self.today)
        prog = planner.progress()
        health = ", ".join(f"{k}:{'paused' if v['open'] else 'ok'}" for k, v in self.http.breaker_report().items()
                           if not k.startswith("overpass:") or v["failed"])
        return [self.today, time.strftime("%H:%M", time.localtime(self.started)), round((self.now() - self.started) / 60, 1),
                getattr(self, "plan_day", ""), f"{self.sched['id']}. {self.sched['name']}" if getattr(self, "sched", None) else "",
                ", ".join(str(x) for x in sorted(x for x in self.parts_worked if x)), self.stats["searches"],
                self.stats["places_new"], cov["leads"], cov["phone"], cov["whatsapp"], cov["email"], cov["instagram"],
                int(self.db.scalar("SELECT COUNT(*) FROM places WHERE qualified=1 AND excluded IS NULL", default=0)),
                added, updated, f"{prog['percent']}% ({prog['parts_done']}/{prog['parts_total']} parts)", health[:300],
                " | ".join(self.warnings)[:500]]

    def _summary(self, planner: Planner, status: str, sheets_result: dict) -> dict:
        prog = planner.progress() if planner.has_plan() else {}
        parts_worked = []
        for pid in sorted(x for x in self.parts_worked if x):
            r = self.db.one("SELECT id, name FROM parts WHERE id=?", (pid,))
            if r:
                parts_worked.append(f"{r['id']}. {r['name']}")
        return {
            "date": self.today, "status": status, "minutes": round((self.now() - self.started) / 60, 1),
            "plan_day": getattr(self, "plan_day", None),
            "scheduled_part": f"{self.sched['id']}. {self.sched['name']}" if getattr(self, "sched", None) else None,
            "parts_worked": parts_worked, "target": self.target,
            "new_leads_today": self._qualified_today(),
            "searches_run": self.stats["searches"], "places_new": self.stats["places_new"],
            "coverage_today": contact_coverage(self.db, self.today),
            "leads_total": int(self.db.scalar("SELECT COUNT(*) FROM places WHERE qualified=1 AND excluded IS NULL", default=0)),
            "plan_progress": prog, "sheets": sheets_result, "warnings": self.warnings,
            "stats": dict(self.stats), "tasks": self.db.task_counts(), "health": self.http.breaker_report(),
            "http": dict(self.http.stats),
        }

    def _write_outputs(self, summary: dict):
        out_dir = os.path.join(os.path.dirname(os.path.abspath(self.db.path)), "reports")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"report-{self.today}.json"), "w", encoding="utf-8") as fh:
            fh.write(jdump(summary))
        md = markdown_summary(summary)
        gh = os.environ.get("GITHUB_STEP_SUMMARY")
        if gh:
            try:
                with open(gh, "a", encoding="utf-8") as fh:
                    fh.write(md)
            except OSError:
                pass
        log.info("\n%s", md)
        log.info("stats: %s", jdump({"run": dict(self.stats), "queue": self.db.task_counts(),
                                     "health": {k: ("paused" if v["open"] else "ok") for k, v in self.http.breaker_report().items()}}))
        for line in masked_samples(self.db, self.today):
            log.info("sample lead: %s", line)
