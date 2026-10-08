"""E-mail hunt: a second, deeper look for every lead that still has no usable e-mail.

1. Its own website again, deeper: privacy/terms pages and the sitemap's contact pages, over http/https and
   with/without www when it did not answer, robots.txt read with a browser-like client when the plain one
   failed. Addresses written as text count when they carry the site's domain or the business's name.
2. No website on its listing (or a dead/taken-over one): the obvious addresses for its name are checked,
   and a site is accepted only when it shows the business's own phone number (or exact name + PIN code).
   Then that site is read like step 1.

Every e-mail kept comes from a page of the business's own site, with that page as its source. Nothing is
guessed. Network work runs in threads; the database is only touched by the main thread."""
from __future__ import annotations

import csv
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .db import DB
from .enrich.domains import Discovery, Resolver, discover_website
from .enrich.emails import MXChecker
from .enrich.extract import canonical_social, host_of, registrable
from .enrich.website import SiteResult, crawl_site, normalize_url
from .net import DeadlineReached, Http, NetworkDown
from .quality import is_aggregator, is_link_hub
from .report import lead_id
from .util import get_logger, jdump

log = get_logger("hunt")
HUNT_VERSION = "v1"
LEADS = "qualified=1 AND excluded IS NULL AND merged_into IS NULL"


@dataclass
class Job:
    key: str
    lead_id: str
    name: str
    category: str
    website: str
    phones: list[str]
    address: str


@dataclass
class Outcome:
    site: SiteResult | None = None          # the listed website, read deeply
    discovery: Discovery | None = None      # looking for a website the listing lacks
    found_site: SiteResult | None = None    # the website found that way, read deeply
    error: str = ""


def _usable_site(url: str) -> bool:
    return bool(url) and not canonical_social(url) and (not is_aggregator(url) or is_link_hub(url))


class EmailHunt:
    def __init__(self, cfg, db: DB, *, limit: int = 200, budget_minutes: float = 60.0, workers: int | None = None,
                 use_sheets: bool = True, http: Http | None = None, resolver: Resolver | None = None,
                 mx: MXChecker | None = None, now_fn=time.time, detail_path: str = "", sheets_factory=None):
        self.cfg, self.db = cfg, db
        self.limit = limit
        self.budget_s = budget_minutes * 60
        self.workers = int(workers or cfg["enrich"]["workers"])
        self.use_sheets = use_sheets and cfg["sheets"]["enabled"]
        self._http, self.resolver = http, resolver or Resolver()
        self.mx = mx or MXChecker(enabled=bool(cfg["enrich"]["check_email_mx"]))
        self.now = now_fn
        self.detail_path = detail_path
        self.sheets_factory = sheets_factory
        self.region = cfg["campaign"]["country"]
        self.city = (cfg["area"].get("name") or "kolkata").split(",")[0].strip().lower()
        self.stats: Counter = Counter()
        self.details: list[list] = []
        self.notes: list[str] = []

    # ------------------------------------------------------------------ selection
    def _select(self) -> list[Job]:
        done = {r["place_key"] for r in self.db.q("SELECT place_key FROM tasks WHERE kind='hunt' AND key LIKE ?",
                                                  (f"hunt:{HUNT_VERSION}:%",))}
        rows = self.db.q(f"SELECT key, lead_no, name, category, website, address FROM places WHERE {LEADS} "
                         "AND key NOT IN (SELECT place_key FROM contacts WHERE kind='email' AND confidence!='low') "
                         "ORDER BY (website IS NULL OR website=''), lead_no")
        jobs = []
        for r in rows:
            if r["key"] in done:
                continue
            phones = [c["value"] for c in self.db.q(
                "SELECT value FROM contacts WHERE place_key=? AND kind IN ('phone','whatsapp') AND value LIKE '+%'", (r["key"],))]
            jobs.append(Job(r["key"], lead_id(r["lead_no"]), r["name"], r["category"] or "", r["website"] or "",
                            list(dict.fromkeys(phones)), r["address"] or ""))
            if len(jobs) >= self.limit:
                break
        return jobs

    # ------------------------------------------------------------------ network work (worker threads)
    def _work(self, job: Job) -> Outcome:
        out = Outcome()
        e = self.cfg["enrich"]
        pages = max(10, int(e["max_pages_per_site"]))
        interval = float(e["site_interval_s"])
        listed = normalize_url(job.website) if job.website else ""
        if listed and _usable_site(listed):
            out.site = crawl_site(self.http, listed, job.name, max_pages=pages, region=self.region, interval=interval,
                                  known_phones=tuple(job.phones), deep=True, variants=True)
            if out.site.status in ("ok", "blocked_robots") and (out.site.owned or out.site.status == "blocked_robots"):
                return out          # their site was read (or refuses robots): nothing more to find
        if not job.phones:
            return out              # no phone number to prove a found site is theirs
        out.discovery = discover_website(self.http, job.name, job.phones, job.address, region=self.region, city=self.city,
                                         resolver=self.resolver)
        if out.discovery.url and (not listed or registrable(host_of(out.discovery.url)) != registrable(host_of(listed))):
            out.found_site = crawl_site(self.http, out.discovery.url, job.name, max_pages=pages, region=self.region,
                                        interval=interval, known_phones=tuple(job.phones), deep=True, variants=False)
        return out

    # ------------------------------------------------------------------ results (main thread)
    def _store_site(self, key: str, res: SiteResult) -> tuple[int, int]:
        """Contacts of a site read for this lead. Returns (new contacts, usable e-mails seen)."""
        added = emails = 0
        for c in res.contacts:
            if c.kind == "email":
                if self.mx.has_mx(c.value.split("@", 1)[1]) is False:
                    continue
                emails += c.confidence != "low"
            if self.db.add_contact(key, c.kind, c.value, label=c.label, source=c.source, source_url=c.source_url,
                                   confidence=c.confidence, evidence=c.evidence):
                added += 1
        return added, emails

    def _apply(self, job: Job, out: Outcome) -> str:
        place = self.db.get_place(job.key)
        if place is None:
            return "lead gone"
        changed = False
        site = out.site
        if site is not None:
            a, _ = self._store_site(job.key, site)
            changed |= a > 0
            if site.status in ("hijacked", "moved") and place["website"] and \
                    normalize_url(place["website"]) == normalize_url(job.website):
                self.db.update_place(job.key, website="")
                changed = True
            if site.description and not place["description"] and site.owned:
                self.db.update_place(job.key, description=site.description[:300])
                changed = True
        found = out.found_site
        if found is not None and found.status == "ok" and found.owned:
            a, _ = self._store_site(job.key, found)
            changed |= a > 0
            current = self.db.scalar("SELECT website FROM places WHERE key=?", (job.key,), "")
            if not current:
                self.db.update_place(job.key, website=normalize_url(out.discovery.url))
                changed = True
            if found.description and not place["description"]:
                self.db.update_place(job.key, description=found.description[:300])
                changed = True
        if changed:
            self.db.mark_dirty(job.key)
        has_email = self.db.one("SELECT 1 FROM contacts WHERE place_key=? AND kind='email' AND confidence!='low'",
                                (job.key,)) is not None
        if has_email:
            outcome = "e-mail found on a website found for it" if (found is not None and found.status == "ok") else \
                "e-mail found on its own website (deeper read)"
        elif site is not None and site.status == "blocked_robots":
            outcome = "website refuses robots"
        elif site is not None and site.status == "error" and not (found and found.status == "ok"):
            outcome = "website unreachable"
        elif site is not None and site.status == "ok" and site.owned:
            outcome = "website read - no e-mail on it"
        elif found is not None and found.status == "ok":
            outcome = "website found - no e-mail on it"
        elif not job.phones:
            outcome = "no website, no phone to check one"
        else:
            outcome = "no website found"
        if out.discovery is not None:
            self.stats["websites looked for"] += 1
            self.stats["websites found"] += bool(out.discovery.url)
        return outcome

    def _record(self, job: Job, out: Outcome, outcome: str) -> None:
        site, disc, found = out.site, out.discovery, out.found_site
        result = {"outcome": outcome, "site": site.status if site else "", "error": (site.error if site else "")[:200],
                  "found": disc.url if disc else "", "how": disc.how if disc else ""}
        with self.db.tx():
            self.db.enqueue("hunt", f"hunt:{HUNT_VERSION}:{job.key}", {"v": HUNT_VERSION}, place_key=job.key)
            self.db.conn.execute("UPDATE tasks SET status='done', result=?, updated_at=? WHERE key=?",
                                 (jdump(result), self.now(), f"hunt:{HUNT_VERSION}:{job.key}"))
        emails = [(c["value"], c["confidence"], c["label"] or "", c["source_url"] or "")
                  for c in self.db.q("SELECT * FROM contacts WHERE place_key=? AND kind='email'", (job.key,))]
        self.details.append([
            job.lead_id, job.name, job.category, job.website, outcome,
            site.status if site else "", (site.error if site else "")[:150], round(site.name_match, 2) if site else "",
            site.owned if site else "", len(site.pages) if site else "",
            disc.url if disc else "", disc.how if disc else "", f"{disc.existing}/{disc.tried}" if disc else "",
            "; ".join(disc.rejected[:4]) if disc else "",
            " | ".join(f"{v} [{conf}] {lab} <{src[:80]}>" for v, conf, lab, src in emails)])

    # ------------------------------------------------------------------ run
    def run(self) -> tuple[int, dict]:
        from .config import BOT_NAME, BOT_UA

        start = self.now()
        deadline = start + max(60.0, self.budget_s - 90)          # leave time for the sheet update
        bot = {"user_agent": BOT_UA, "robot_name": BOT_NAME} if self.cfg.open_data else {}
        self.http = self._http or Http(use_curl_cffi=self.cfg["runtime"]["use_curl_cffi"], deadline=deadline,
                                       default_interval=float(self.cfg["enrich"]["site_interval_s"]), **bot)
        if self._http is not None:
            self.http.deadline = deadline
        jobs = self._select()
        before = self._coverage()
        log.info("e-mail hunt: %d leads without a usable e-mail to look at (coverage now %s%%)", len(jobs), before[2])
        code = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._work, j): j for j in jobs}
            for fut in as_completed(futures):
                job = futures[fut]
                try:
                    out = fut.result()
                except DeadlineReached:
                    self.stats["not reached (time budget)"] += 1
                    continue
                except NetworkDown as exc:
                    self.stats["not reached (network down)"] += 1
                    self.notes.append(f"network down: {exc}"[:200])
                    code = 2
                    continue
                except Exception as exc:  # noqa: BLE001 - one odd site must not stop the hunt
                    out = Outcome(error=f"{type(exc).__name__}: {exc}"[:200])
                    log.warning("hunt error for %s: %s", job.lead_id, out.error)
                try:
                    with self.db.tx():
                        outcome = "error: " + out.error if out.error else self._apply(job, out)
                    self._record(job, out, outcome)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not store hunt result for %s: %s", job.lead_id, exc)
                    outcome = "error storing result"
                self.stats[outcome] += 1
        after = self._coverage()
        summary = {"leads_looked_at": sum(v for k, v in self.stats.items() if not k.startswith("websites ")),
                   "email_coverage_before": before[2], "email_coverage_after": after[2],
                   "leads_with_email_before": before[0], "leads_with_email_after": after[0], "leads": after[1],
                   "outcomes": dict(self.stats.most_common()), "minutes": round((self.now() - start) / 60, 1), "notes": self.notes}
        if self.use_sheets:
            summary["sheet"] = self._sync_sheet()
        if self.detail_path:
            self._write_details()
        return code, summary

    def _coverage(self) -> tuple[int, int, float]:
        total = self.db.scalar(f"SELECT COUNT(*) FROM places WHERE {LEADS}", (), 0)
        n = self.db.scalar(f"SELECT COUNT(DISTINCT p.key) FROM places p JOIN contacts c ON c.place_key=p.key "
                           f"WHERE p.{LEADS.replace(' AND ', ' AND p.')} AND c.kind='email' AND c.confidence!='low'", (), 0)
        return n, total, round(100.0 * n / total, 1) if total else 0.0

    def _sync_sheet(self) -> dict:
        if not (self.cfg.sheet_id or self.sheets_factory):
            return {"status": "not configured"}
        from .report import lead_row
        from .sheets import SheetsClient, SheetsError, SheetsSync

        sh = self.cfg["sheets"]
        try:
            sync = self.sheets_factory() if self.sheets_factory else SheetsSync(
                SheetsClient(self.cfg.sheet_id), sh["leads_tab"], sh["plan_tab"], sh["report_tab"])
            sync.ensure_tabs()
            pending = self.db.q(f"SELECT * FROM places WHERE sync_state='pending' AND {LEADS} ORDER BY lead_no")
            rows = [lead_row(self.db, p, self.cfg) for p in pending]
            added, updated, _ = sync.upsert_leads(rows) if rows else (0, 0, {})
            with self.db.tx():
                for p in pending:
                    self.db.update_place(p["key"], sync_state="synced", synced_at=self.now())
            return {"status": "ok", "added": added, "updated": updated}
        except SheetsError as exc:
            self.notes.append(f"Google Sheet update failed: {exc}"[:300])
            return {"status": f"error: {exc}"[:200]}

    def _write_details(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.detail_path)), exist_ok=True)
        with open(self.detail_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Lead ID", "Business", "Category", "Listed website", "Outcome", "Site status", "Site error",
                        "Name match", "Site is theirs", "Pages read", "Website found", "Why accepted", "Domains existing/tried",
                        "Rejected", "E-mails now (value [confidence] note <source>)"])
            w.writerows(self.details)
