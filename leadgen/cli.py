"""Command line: python -m leadgen <command> [options]

Commands
  run          daily cycle: plan -> discover -> enrich -> sheet -> report
  plan         build (or show) the area plan; --force re-plans after config changes
  status       plan progress, lead counts, last runs
  sync         push pending leads to the Google Sheet only
  export-csv   write all leads to a CSV file (local use; never commit it)
  doctor       check config, secrets, database and (with --live / --sheet-test) connectivity
  backup       consistent copy of the state database (for encryption/upload)
  probe        live diagnostics of every external source
  outreach     e-mail sequences + WhatsApp send queue from the Leads tab (dry-run until switched to live)
  email-audit  e-mail coverage of the leads and where the gaps are (aggregate numbers only)
  email-hunt   deeper e-mail search for leads that still have none (own website again, found websites)
  usp-refresh  read the homepage of leads crawled before USP lines existed, for their USP line
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import ConfigError, load_config
from .util import get_logger, jload, local_date

log = get_logger("cli")
DEFAULT_CONFIG = os.environ.get("PP_CONFIG", "config/plant-parlour.toml")
DEFAULT_DB = os.environ.get("PP_DB", "state/leadgen.sqlite")
DEFAULT_OUTREACH_DB = os.environ.get("PP_OUTREACH_DB", "state/outreach.sqlite")


def _common(ap):
    ap.add_argument("--config", default=DEFAULT_CONFIG, help=f"campaign config (default {DEFAULT_CONFIG})")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"state database (default {DEFAULT_DB})")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m leadgen", description="Plant Parlour lead generation " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the daily cycle")
    _common(r)
    r.add_argument("--budget-minutes", type=float, default=None)
    r.add_argument("--target", type=int, default=None, help="override the daily lead target")
    r.add_argument("--no-sheets", action="store_true")
    r.add_argument("--no-discovery", action="store_true", help="only enrich/sync already discovered places")
    r.add_argument("--max-searches", type=int, default=None)
    r.add_argument("--workers", type=int, default=None)
    p = sub.add_parser("plan", help="build or show the plan")
    _common(p)
    p.add_argument("--force", action="store_true", help="re-plan (keeps found leads)")
    p.add_argument("--offline", action="store_true", help="do not contact OpenStreetMap (synthetic density)")
    s = sub.add_parser("status", help="show progress")
    _common(s)
    y = sub.add_parser("sync", help="push pending leads to Google Sheets")
    _common(y)
    e = sub.add_parser("export-csv", help="export leads to CSV")
    _common(e)
    e.add_argument("--out", required=True)
    e.add_argument("--without-email", action="store_true", help="only leads that have no usable e-mail yet")
    a = sub.add_parser("email-audit", help="e-mail coverage and gaps (aggregate numbers only)")
    _common(a)
    a.add_argument("--json", default="", help="also write the numbers to this JSON file")
    h = sub.add_parser("email-hunt", help="deeper e-mail search for leads that still have none")
    _common(h)
    h.add_argument("--limit", type=int, default=200, help="leads to look at in this run")
    h.add_argument("--budget-minutes", type=float, default=60.0)
    h.add_argument("--workers", type=int, default=None)
    h.add_argument("--no-sheets", action="store_true", help="do not update the Google Sheet")
    h.add_argument("--detail-csv", default="", help="per-lead results (contains contacts - local/encrypted use only)")
    h.add_argument("--force", action="store_true", help="run even when [enrich] email_hunt = false (manual trials)")
    u = sub.add_parser("usp-refresh", help="USP lines for leads crawled before they existed (leads with e-mail first)")
    _common(u)
    u.add_argument("--limit", type=int, default=300)
    u.add_argument("--budget-minutes", type=float, default=10.0)
    u.add_argument("--no-sheets", action="store_true")
    d = sub.add_parser("doctor", help="health checks")
    _common(d)
    d.add_argument("--live", action="store_true", help="also test Google Maps and a website fetch")
    d.add_argument("--sheet-test", action="store_true", help="write+delete a temporary tab in the Google Sheet")
    d.add_argument("--secrets-only", action="store_true")
    d.add_argument("--require-sheet", action="store_true", help="fail if the Google Sheet is not configured")
    b = sub.add_parser("backup", help="consistent copy of the database")
    _common(b)
    b.add_argument("--out", required=True)
    o = sub.add_parser("outreach", help="send today's e-mails and build the WhatsApp queue from the Leads tab")
    o.add_argument("--config", default=DEFAULT_CONFIG, help=f"campaign config (default {DEFAULT_CONFIG})")
    o.add_argument("--db", default=DEFAULT_OUTREACH_DB, help=f"outreach state database (default {DEFAULT_OUTREACH_DB})")
    o.add_argument("--mode", choices=["dry-run", "live"], default=None, help="override [outreach].mode")
    o.add_argument("--max-emails", type=int, default=None, help="send at most this many e-mails in this run (testing)")
    sub.add_parser("probe", help="live diagnostics of external sources").add_argument("--only", default="")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "probe":
        from .probe import main as probe_main

        return probe_main(["--only", args.only] if args.only else [])
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 1
    from .db import DB

    if args.cmd == "doctor":
        return cmd_doctor(cfg, args)
    if args.cmd == "outreach":
        return cmd_outreach(cfg, args)
    db = DB(args.db)
    try:
        if args.cmd == "run":
            from .runner import Runner

            code, _ = Runner(cfg, db, budget_minutes=args.budget_minutes, target=args.target, use_sheets=not args.no_sheets,
                             discovery=not args.no_discovery, max_searches=args.max_searches, workers=args.workers).run()
            return code
        if args.cmd == "plan":
            return cmd_plan(cfg, db, args)
        if args.cmd == "status":
            return cmd_status(cfg, db)
        if args.cmd == "sync":
            from .runner import Runner

            code, summary = Runner(cfg, db, budget_minutes=10, discovery=False, use_sheets=True, max_searches=0,
                                   enrich=False).run()
            return code
        if args.cmd == "export-csv":
            from .report import export_csv

            n = export_csv(db, cfg, args.out, without_email=args.without_email)
            print(f"wrote {n} leads to {args.out}")
            return 0
        if args.cmd == "email-hunt":
            from .hunt import EmailHunt

            if not cfg["enrich"].get("email_hunt", True) and not args.force:
                print("e-mail hunt is switched off ([enrich] email_hunt = false) - nothing to do")
                return 0

            code, summary = EmailHunt(cfg, db, limit=args.limit, budget_minutes=args.budget_minutes, workers=args.workers,
                                      use_sheets=not args.no_sheets, detail_path=args.detail_csv).run()
            print(json.dumps(summary, indent=1, ensure_ascii=False))
            summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as fh:
                    fh.write("## E-mail hunt\n\n```\n" + json.dumps(summary, indent=1, ensure_ascii=False) + "\n```\n")
            return code
        if args.cmd == "usp-refresh":
            from .hunt import UspRefresh

            code, summary = UspRefresh(cfg, db, limit=args.limit, budget_minutes=args.budget_minutes,
                                       use_sheets=not args.no_sheets).run()
            summary.pop("email_coverage_before", None)
            print(json.dumps(summary, indent=1, ensure_ascii=False))
            return code
        if args.cmd == "email-audit":
            from .audit import email_audit, format_audit

            a = email_audit(db, cfg)
            text = format_audit(a)
            print(text)
            if args.json:
                with open(args.json, "w", encoding="utf-8") as fh:
                    json.dump(a, fh, indent=1)
            summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as fh:
                    fh.write("## E-mail coverage audit\n\n```\n" + text + "\n```\n")
            return 0
        if args.cmd == "backup":
            if not db.integrity_ok():
                print("database integrity check FAILED - not backing up", file=sys.stderr)
                return 1
            db.backup_to(args.out)
            print(f"backup written to {args.out}")
            return 0
    finally:
        db.close()
    return 1


def _mask_contacts(text: str) -> str:
    """Logs of a public repository must not show lead contacts."""
    import re

    text = re.sub(r"([A-Za-z0-9._%+'-])[A-Za-z0-9._%+'-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,24})", r"\1***@\2", text)
    return re.sub(r"\+\d[\d \-]{8,}\d", lambda m: m.group(0)[:4] + "*******" + m.group(0)[-3:], text)


def cmd_outreach(cfg, args) -> int:
    from .outreach.engine import Outreach
    from .outreach.store import OutreachStore

    store = OutreachStore(args.db)
    try:
        live = None if args.mode is None else args.mode == "live"
        engine = Outreach(cfg, store, live=live, max_emails=args.max_emails)
        code, summary = engine.run()
    finally:
        store.close()
    sent_csv = os.environ.get("OUTREACH_SENT_CSV", "")
    if sent_csv and engine.sent_log:
        # The e-mails this run sent, for the maintainer's encrypted check (never printed in the public log).
        import csv

        with open(sent_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Lead ID", "Business", "To", "Result", "Subject", "Body"])
            w.writerows(engine.sent_log)
    text = _mask_contacts(json.dumps(summary, indent=1, ensure_ascii=False))
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        lines = [f"## Outreach - {summary.get('mode')} ({summary.get('status')})", "",
                 f"* Daily e-mail limit: {summary.get('daily_limit')}",
                 f"* New e-mails: {summary.get('new')}  |  follow-ups: {summary.get('followups')}  |  planned (dry-run): {summary.get('planned')}",
                 f"* Replies: interested {summary.get('positive')}, not interested {summary.get('negative')}, other {summary.get('other')}",
                 f"* Bounces: {summary.get('bounces')}  |  WhatsApp queued: {summary.get('wa_queued')}"]
        lines += [f"* Note: {_mask_contacts(n)}" for n in summary.get("notes") or []]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return code


def cmd_plan(cfg, db, args) -> int:
    from .net import Http
    from .planner import PlanMismatch, Planner

    http = None if args.offline else Http(use_curl_cffi=cfg["runtime"]["use_curl_cffi"])
    pl = Planner(cfg, db, http)
    try:
        s = pl.ensure_plan(force=args.force)
    except PlanMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Plan: {s['parts']} parts, {s['cells']} search squares, {s['search_tasks']} searches (density: {s['density_source']})")
    sd = pl.start_date()
    from datetime import date, timedelta

    for p in db.q("SELECT * FROM parts ORDER BY id"):
        when = (date.fromisoformat(sd) + timedelta(days=p["scheduled_day"] - 1)).isoformat()
        print(f"  Day {p['scheduled_day']:>3} ({when})  part {p['id']:>3}: {p['name'][:60]:60s} squares={p['cells']:<4} status={p['status']}")
    return 0


def cmd_status(cfg, db) -> int:
    from .planner import Planner
    from .report import contact_coverage

    pl = Planner(cfg, db, None)
    if not pl.has_plan():
        print("No plan yet - run `python -m leadgen plan` or `run`.")
        return 0
    today = local_date(cfg.tz)
    prog = pl.progress()
    sched = pl.scheduled_part(today)
    cov = contact_coverage(db)
    print(f"Campaign {cfg['campaign']['id']} - today {today} is plan day {pl.day_number(today)}"
          + (f" (part {sched['id']}: {sched['name']})" if sched else ""))
    print(f"Plan progress: {prog['percent']}% of searches, {prog['parts_done']}/{prog['parts_total']} parts done")
    print(f"Leads: {cov['leads']} total | phone {cov['phone']} | WhatsApp {cov['whatsapp']} | email {cov['email']} | "
          f"Instagram {cov['instagram']} | Facebook {cov['facebook']} | LinkedIn {cov['linkedin']}")
    print("Tasks:", json.dumps(db.task_counts()))
    print("Last runs:")
    for r in db.q("SELECT * FROM runs ORDER BY id DESC LIMIT 7"):
        s = jload(r["summary"], {}) or {}
        print(f"  {r['run_date']} {r['status']:<28} new leads {s.get('new_leads_today', '?'):<5} searches {s.get('searches_run', '?')}")
    return 0


def cmd_doctor(cfg, args) -> int:
    ok = True

    def check(name, good, detail=""):
        nonlocal ok
        print(f"[{'OK' if good else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
        ok = ok and good

    print(f"Plant Parlour lead generation {__version__}  config={args.config}")
    check("config valid", True, f"{len(cfg.categories)} active categories, area {cfg['area']['name']} r={cfg['area']['radius_km']} km")
    sid = cfg.sheet_id
    has_key = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
                   or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    if cfg["sheets"]["enabled"]:
        if args.require_sheet:
            check("PP_SHEET_ID set", bool(sid))
            check("Google service-account key set", has_key)
        else:
            print(f"[{'OK' if sid else 'WARN'}] PP_SHEET_ID " + ("set" if sid else "not set (leads stay in the database only)"))
            print(f"[{'OK' if has_key else 'WARN'}] service-account key " + ("set" if has_key else "not set"))
    mode = cfg["compliance"]["mode"]
    if mode == "open-data":
        print("[OK] compliance mode: open-data (Overture Maps + OpenStreetMap + the businesses' own websites; "
              "no scraping of Google, search engines or Instagram)")
    elif mode == "hybrid":
        print("[OK] compliance mode: hybrid (open-data discovery + web search & Instagram for richer contacts; "
              "no Google Maps scraping)")
    else:
        print("[WARN] compliance mode: standard (scrapes Google Maps and search engines - against Google's terms; "
              "best run from the tablet)")
        print(f"[INFO] GOOGLE_PLACES_API_KEY {'set' if os.environ.get('GOOGLE_PLACES_API_KEY') else 'not set (optional)'}")
    if mode in ("open-data", "hybrid"):
        try:
            import duckdb  # noqa: F401
            print("[OK] duckdb installed (reads Overture Maps open data)")
        except ImportError:
            print("[WARN] duckdb not installed - discovery falls back to OpenStreetMap (pip install duckdb)")
    print(f"[INFO] FOURSQUARE_API_KEY {'set (official enrichment enabled)' if os.environ.get('FOURSQUARE_API_KEY') else 'not set (optional)'}")
    if args.secrets_only:
        return 0 if ok else 1
    if os.path.exists(args.db):
        from .db import DB

        db = DB(args.db)
        check("state database integrity", db.integrity_ok(), args.db)
        db.close()
    else:
        print(f"[INFO] no state database yet at {args.db} (created on first run)")
    try:
        from .net import HAVE_CURL_CFFI

        print(f"[INFO] curl_cffi {'available' if HAVE_CURL_CFFI else 'not installed (using requests)'}")
    except Exception:
        pass
    if sid and has_key and (args.sheet_test or args.live):
        try:
            from .sheets import SheetsClient

            c = SheetsClient(sid)
            meta = c.metadata()
            check("Google Sheet reachable", True, f"title '{meta.get('properties', {}).get('title')}'")
            if args.sheet_test:
                import time as _t

                tab = f"pp-selftest-{int(_t.time())}"
                res = c.batch_update([{"addSheet": {"properties": {"title": tab}}}])
                sheet_id = res["replies"][0]["addSheet"]["properties"]["sheetId"]
                c.update_values(f"'{tab}'!A1", [["self-test", "ok"]])
                back = c.get_values(f"'{tab}'!A1:B1")
                c.batch_update([{"deleteSheet": {"sheetId": sheet_id}}])
                check("Google Sheet write/read/delete", back == [["self-test", "ok"]])
        except Exception as exc:  # noqa: BLE001
            check("Google Sheet access", False, str(exc)[:300])
    if args.live:
        from .net import Http
        from .providers.gmaps import GoogleMapsSearch

        http = Http(use_curl_cffi=cfg["runtime"]["use_curl_cffi"])
        try:
            lat, lng = cfg["area"]["center"]
            places, meta = GoogleMapsSearch(http).search_page(cfg.categories[0]["queries"][0], lat, lng, 15)
            check("Google Maps search", len(places) > 0, f"{len(places)} results")
        except Exception as exc:  # noqa: BLE001
            check("Google Maps search", False, str(exc)[:200])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
