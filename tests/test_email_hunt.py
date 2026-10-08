"""E-mail coverage audit and the extra e-mail hunt."""
from __future__ import annotations

import re

from leadgen.audit import email_audit, format_audit
from leadgen.db import DB

from conftest import make_config


def place(db, key, name, *, website="", category="cafe", qualified=1):
    db.insert_place({"key": key, "name": name, "category": category, "website": website, "provider": "overture",
                     "found_date": "2026-10-08", "qualified": qualified, "qualified_date": "2026-10-08"})


def test_audit_counts_coverage_and_why_emails_are_missing(tmp_path):
    db = DB(str(tmp_path / "a.sqlite"))
    cfg = make_config()
    with db.tx():
        place(db, "k1", "Leaf Cafe", website="https://leafcafe.in/")
        db.add_contact("k1", "email", "hello@leafcafe.in", source="website", confidence="high")
        place(db, "k2", "Tea Stall")                                   # no website, Facebook page only
        db.add_contact("k2", "facebook", "https://www.facebook.com/teastall", source="overture", confidence="high")
        db.add_contact("k2", "phone", "+919830055555", source="overture", confidence="medium")
        place(db, "k3", "Brew House", website="https://brewhouse.in/", category="restaurant")
        db.add_contact("k3", "email", "brewhouse@gmail.com", label="free-mail", source="website", confidence="low")
        db.add_contact("k3", "email", "info@brewhouse.in", source="guess", confidence="low")
        db.enqueue("site", "site:k3", {"url": "https://brewhouse.in/"})
        db.conn.execute("UPDATE tasks SET status='done', result='{\"status\": \"ok\"}', place_key='k3'")
        place(db, "k4", "Not A Lead", qualified=0)
    a = email_audit(db, cfg)
    assert (a["leads"], a["with_email"], a["email_pct"]) == (3, 1, 33.3)
    assert a["email_sources"] == {"website": 1}
    assert a["without_email_with_website"] == 1 and a["website_crawl_result"] == {"ok": 1}
    assert (a["without_email_no_website"], a["no_website_but_facebook"], a["no_website_but_phone"]) == (1, 1, 1)
    assert a["unverified_email_reasons"] == {"Gmail/Outlook-type address found as text": 1,
                                             "guessed info@/contact@ (not published)": 1}
    assert a["has a published but unverified e-mail"] == 1
    text = format_audit(a)
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", text)              # aggregate numbers only - safe for public logs
