"""E-mail coverage audit: where do the leads without a usable e-mail stand? Aggregate numbers only
(safe for the public logs) - no names, addresses or contacts are printed."""
from __future__ import annotations

from collections import Counter, defaultdict

from .db import DB
from .util import jload

LEADS = "qualified=1 AND excluded IS NULL AND merged_into IS NULL"


def _reason(c) -> str:
    """Why an e-mail is kept as unverified, in one short bucket."""
    label = (c["label"] or "").lower()
    if c["source"] == "guess":
        return "guessed info@/contact@ (not published)"
    if "site may belong to another business" in label:
        return "on a site not proven to be theirs"
    if "suspicious" in label:
        return "typo-like address"
    if "free-mail" in label:
        return "Gmail/Outlook-type address found as text"
    if "other-domain" in label:
        return "other-domain address found as text"
    return f"other ({c['source']})"


def email_audit(db: DB, cfg) -> dict:
    leads = db.q(f"SELECT key, category, website FROM places WHERE {LEADS}")
    total = len(leads)
    by_key = {r["key"]: r for r in leads}
    contacts = defaultdict(list)
    for c in db.q(f"SELECT c.* FROM contacts c JOIN places p ON p.key=c.place_key WHERE p.{LEADS.replace(' AND ', ' AND p.')} "
                  "AND c.kind IN ('email','facebook','instagram','phone')"):
        contacts[c["place_key"]].append(c)
    site = {}
    for t in db.q("SELECT place_key, status, result, last_error FROM tasks WHERE kind='site'"):
        res = jload(t["result"], {}) or {}
        site[t["place_key"]] = res.get("status") if t["status"] == "done" else t["status"]

    def good(key, kind):
        return [c for c in contacts[key] if c["kind"] == kind and c["confidence"] != "low"]

    with_email = [k for k in by_key if good(k, "email")]
    src = Counter()
    for k in with_email:
        names = {s.split("|", 1)[0] for c in good(k, "email") for s in (jload(c["sources"], []) or [c["source"]])}
        for s in names - {"guess"}:      # a guessed info@ later found on the site counts as found there
            src[s] += 1
    cats = defaultdict(lambda: [0, 0, 0])
    for k, r in by_key.items():
        cats[r["category"] or "?"][0] += 1
        cats[r["category"] or "?"][1] += bool(good(k, "email"))
        cats[r["category"] or "?"][2] += bool(r["website"])
    missing = [k for k in by_key if not good(k, "email")]
    site_states = Counter()
    weak = Counter()
    weak_leads = Counter()
    no_web = 0
    no_web_fb = no_web_ig = no_web_phone = 0
    for k in missing:
        r = by_key[k]
        if r["website"]:
            site_states[site.get(k) or "no site task"] += 1
        else:
            no_web += 1
            no_web_fb += bool(good(k, "facebook"))
            no_web_ig += bool(good(k, "instagram"))
            no_web_phone += bool(good(k, "phone"))
        reasons = {_reason(c) for c in contacts[k] if c["kind"] == "email" and c["confidence"] == "low"}
        for reason in reasons:
            weak[reason] += 1
        if reasons - {"guessed info@/contact@ (not published)"}:
            weak_leads["has a published but unverified e-mail"] += 1
    open_total = db.scalar("SELECT COUNT(*) FROM open_places", (), 0)
    open_email = db.scalar("SELECT COUNT(*) FROM open_places WHERE emails IS NOT NULL AND emails NOT IN ('', '[]')", (), 0)
    pct = (lambda a, b: round(100.0 * a / b, 1) if b else 0.0)
    return {
        "leads": total,
        "with_email": len(with_email),
        "email_pct": pct(len(with_email), total),
        "email_sources": dict(src.most_common()),
        "by_category": {c: {"leads": n, "email_pct": pct(e, n), "website_pct": pct(w, n)}
                        for c, (n, e, w) in sorted(cats.items(), key=lambda x: -x[1][0])},
        "without_email": len(missing),
        "without_email_with_website": sum(site_states.values()),
        "website_crawl_result": dict(site_states.most_common()),
        "without_email_no_website": no_web,
        "no_website_but_facebook": no_web_fb,
        "no_website_but_instagram": no_web_ig,
        "no_website_but_phone": no_web_phone,
        "unverified_email_reasons": dict(weak.most_common()),
        **dict(weak_leads),
        "overture_places_cached": open_total,
        "overture_places_with_email_pct": pct(open_email, open_total),
    }


def format_audit(a: dict) -> str:
    lines = [f"Leads: {a['leads']}  |  with a usable e-mail: {a['with_email']} ({a['email_pct']}%)",
             "E-mail found via: " + ", ".join(f"{k} {v}" for k, v in a["email_sources"].items()),
             "", "By category (leads, e-mail %, website %):"]
    for c, v in a["by_category"].items():
        lines.append(f"  {c:28s} {v['leads']:5d}  {v['email_pct']:5.1f}%  {v['website_pct']:5.1f}%")
    lines += ["", f"Without e-mail: {a['without_email']}",
              f"  with a website: {a['without_email_with_website']}  -> crawl result: "
              + ", ".join(f"{k} {v}" for k, v in a["website_crawl_result"].items()),
              f"  no website: {a['without_email_no_website']} (Facebook page {a['no_website_but_facebook']}, "
              f"Instagram {a['no_website_but_instagram']}, phone {a['no_website_but_phone']})",
              "  unverified e-mails held back: " + ", ".join(f"{k}: {v}" for k, v in a["unverified_email_reasons"].items()),
              f"  leads with a published but unverified e-mail: {a.get('has a published but unverified e-mail', 0)}",
              f"Overture places cached: {a['overture_places_cached']} ({a['overture_places_with_email_pct']}% with an e-mail)"]
    return "\n".join(lines)
