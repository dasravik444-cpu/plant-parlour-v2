"""Observer/reporting: build lead rows, plan rows and the daily report (sheet, JSON, CSV, markdown)."""
from __future__ import annotations

import csv
import os
from datetime import date, timedelta

from .db import DB
from .enrich.phones import display_phone, refine_phone_label
from .planner import localities_seen
from .quality import is_aggregator, lead_priority
from .sheets import LEAD_COLUMNS, PLAN_COLUMNS
from .util import fmt_local, jload, mask_value

SOURCE_NAMES = {"google_maps": "Google Maps", "website": "website", "jsonld": "website (structured data)",
                "search": "web search", "instagram": "Instagram profile", "places_api": "Google Places API",
                "osm": "OpenStreetMap", "overture": "Overture Maps (open data)"}
KIND_ORDER = ["phone", "whatsapp", "email", "instagram", "facebook", "linkedin", "person", "twitter", "youtube"]


def lead_id(no: int | None) -> str:
    return f"PP-{no:05d}" if no else ""


def lead_row(db: DB, p, cfg) -> dict:
    contacts = db.contacts_for(p["key"])
    local_prefixes = cfg["area"].get("local_landline_prefixes") or []
    good: dict[str, list[str]] = {k: [] for k in KIND_ORDER}
    weak: list[str] = []
    sources: dict[str, set] = {}
    for c in contacts:
        kind, value = c["kind"], c["value"]
        if kind in ("phone", "whatsapp") and value.startswith("+"):
            label = refine_phone_label(value, c["label"], local_prefixes) if kind == "phone" else ""
            shown = display_phone(value) + (f" ({label})" if label else "")
        elif kind == "person":
            role = (c["label"] or "").split(",")[0].strip()
            shown = value + (f" ({role})" if role else "")
        else:
            shown = value
        if c["confidence"] == "low":
            # Say why it is unverified ("name too common to confirm", "foreign number", ...).
            reason = ", ".join(x.strip() for x in (c["label"] or "").split(",")
                               if x.strip() and not x.strip().startswith("name match") and kind != "phone")
            weak.append(f"{kind}: {shown}" + (f" ({reason})" if reason else ""))
            continue
        if kind in good and shown not in good[kind]:
            good[kind].append(shown)
        srcs = {s.split("|", 1)[0] for s in (jload(c["sources"], []) or [])} or {c["source"]}
        sources.setdefault(kind, set()).update(SOURCE_NAMES.get(s, s) for s in srcs)
    kinds = {k for k, v in good.items() if v}
    signals = sorted({c["value"] for c in contacts if c["kind"] == "signal"})
    usp = next((c["value"] for c in contacts if c["kind"] == "usp"), "")
    website = p["website"] or ""
    if website and is_aggregator(website):
        website = ""
    part = db.one("SELECT id, name FROM parts WHERE id=?", (p["part_id"],)) if p["part_id"] else None
    cat = cfg.category(p["category"]) if p["category"] else None
    rating = f"{p['rating']:.1f} ({p['reviews']} reviews)" if p["rating"] and p["reviews"] else (f"{p['rating']:.1f}" if p["rating"] else "")
    src_text = "; ".join(f"{k}: {', '.join(sorted(sources[k]))}" for k in KIND_ORDER if k in sources)
    return {
        "Lead ID": lead_id(p["lead_no"]),
        "Date Added": p["found_date"],
        "Business Name": p["name"],
        "Category": cat["label"] if cat else (p["category"] or ""),
        "Area": p["area"] or "",
        "Address": p["address"] or "",
        "Phones": "\n".join(good["phone"]),
        "WhatsApp": "\n".join(good["whatsapp"]),
        "Emails": "\n".join(good["email"]),
        "Instagram": "\n".join(good["instagram"]),
        "Facebook": "\n".join(good["facebook"]),
        "LinkedIn": "\n".join(good["linkedin"]),
        "Contact Person": "\n".join(good["person"]),
        "Website": website,
        "Google Maps": p["maps_url"] or "",
        "Rating": rating,
        "Priority": lead_priority(kinds, p["rating"], p["reviews"], advertises=bool(signals)),
        "Signals": "\n".join(signals),
        "USP": usp,
        "Description": p["description"] or "",
        "Contact Sources": src_text,
        "Other Contacts (unverified)": "\n".join(weak[:6]),
        "Plan Part": f"{part['id']}. {part['name']}" if part else "",
        "Last Updated": fmt_local(cfg.tz, p["updated_at"]),
        "Key": p["key"],
        "Status": "New",
    }


def plan_rows(db: DB, cfg, start_date: str) -> list[list]:
    rows = []
    stats = {r["part_id"]: r for r in db.q(
        "SELECT part_id, SUM(CASE WHEN status IN ('done','failed','skipped') THEN 1 ELSE 0 END) done_n, COUNT(*) total_n "
        "FROM tasks WHERE kind='search' GROUP BY part_id")}
    leads = {r["part_id"]: r["n"] for r in db.q("SELECT part_id, COUNT(*) n FROM places WHERE qualified=1 AND excluded IS NULL GROUP BY part_id")}
    sd = date.fromisoformat(start_date)
    for p in db.q("SELECT * FROM parts ORDER BY id"):
        st = stats.get(p["id"])
        locs = jload(p["localities"], []) or []
        rows.append([p["id"], p["name"], ", ".join(locs[:6]), ", ".join(localities_seen(db, p["id"])), p["cells"],
                     (sd + timedelta(days=(p["scheduled_day"] or p["id"]) - 1)).isoformat(), p["status"],
                     p["started_on"] or "", p["finished_on"] or "", leads.get(p["id"], 0),
                     st["done_n"] if st else 0, st["total_n"] if st else 0])
    return rows


def contact_coverage(db: DB, day: str | None = None) -> dict:
    where = "p.qualified=1 AND p.excluded IS NULL" + (" AND p.qualified_date=?" if day else "")
    params = (day,) if day else ()
    total = db.scalar(f"SELECT COUNT(*) FROM places p WHERE {where}", params, 0)
    out = {"leads": total}
    for kind in ("phone", "whatsapp", "email", "instagram", "facebook", "linkedin"):
        out[kind] = db.scalar(
            f"SELECT COUNT(DISTINCT p.key) FROM places p JOIN contacts c ON c.place_key=p.key "
            f"WHERE {where} AND c.kind=? AND c.confidence!='low'", params + (kind,), 0)
    return out


def export_csv(db: DB, cfg, path: str, without_email: bool = False) -> int:
    rows = db.q("SELECT * FROM places WHERE excluded IS NULL AND merged_into IS NULL AND qualified=1 ORDER BY lead_no, first_seen")
    if without_email:
        rows = [p for p in rows if not db.one("SELECT 1 FROM contacts WHERE place_key=? AND kind='email' AND confidence!='low'",
                                              (p["key"],))]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(LEAD_COLUMNS)
        for p in rows:
            r = lead_row(db, p, cfg)
            w.writerow([_csv_safe(r.get(c, "")) for c in LEAD_COLUMNS])
    return len(rows)


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    # Prevent spreadsheet formula injection when the CSV is opened in Excel/Sheets.
    return "'" + s if s[:1] in ("=", "@", "-") or (s[:1] == "+" and not s[1:2].isdigit()) else s


def markdown_summary(summary: dict) -> str:
    s = summary
    cov = s.get("coverage_today", {})
    lines = [
        f"## Plant Parlour lead generation - {s.get('date')}",
        "",
        f"* Status: **{s.get('status')}**  |  minutes: {s.get('minutes')}",
        f"* Plan day {s.get('plan_day')}: scheduled part **{s.get('scheduled_part') or '-'}**",
        f"* Parts worked: {', '.join(s.get('parts_worked') or []) or '-'}",
        f"* Searches run: {s.get('searches_run', 0)}  |  new places: {s.get('places_new', 0)}",
        f"* **New leads today: {s.get('new_leads_today', 0)}** (target {s.get('target')})",
        f"* With phone {cov.get('phone', 0)}, WhatsApp {cov.get('whatsapp', 0)}, email {cov.get('email', 0)}, Instagram {cov.get('instagram', 0)}",
        f"* Leads in total: {s.get('leads_total', 0)}  |  plan progress: {s.get('plan_progress', {}).get('percent', 0)}% "
        f"({s.get('plan_progress', {}).get('parts_done', 0)}/{s.get('plan_progress', {}).get('parts_total', 0)} parts done)",
        f"* Google Sheet: {s.get('sheets', {}).get('status', 'n/a')} (added {s.get('sheets', {}).get('added', 0)}, updated {s.get('sheets', {}).get('updated', 0)})",
    ]
    if s.get("warnings"):
        lines += ["", "### Warnings"] + [f"* {w}" for w in s["warnings"]]
    return "\n".join(lines) + "\n"


def masked_samples(db: DB, day: str, n: int = 5) -> list[str]:
    """A few sample lines for logs with contact values masked (CI logs of a public repo are public)."""
    out = []
    for p in db.q("SELECT key, name, category FROM places WHERE qualified=1 AND qualified_date=? AND excluded IS NULL LIMIT ?", (day, n)):
        kinds = []
        for c in db.contacts_for(p["key"]):
            if c["confidence"] != "low":
                kinds.append(f"{c['kind']}={mask_value(c['kind'], c['value'])}")
        out.append(f"{p['name'][:40]} [{p['category']}] " + ", ".join(kinds[:6]))
    return out
