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


# ---------------------------------------------------------------------------------------------- e-mail hunt
from helpers import FakeHttp  # noqa: E402

from leadgen.enrich.domains import Resolver, discover_website, name_slugs  # noqa: E402
from leadgen.enrich.website import crawl_site  # noqa: E402
from leadgen.hunt import EmailHunt  # noqa: E402
from leadgen.net import Permanent  # noqa: E402

ROBOTS_OK = (200, "User-agent: *\nAllow: /\n", "text/plain")


def page(title, body):
    return (200, f"<html><head><title>{title}</title></head><body>{body}</body></html>", "text/html")


class FakeResolver(Resolver):
    def __init__(self, existing):
        super().__init__()
        self.existing = set(existing)

    def exists(self, domain):
        return domain in self.existing


def router_for(sites):
    """sites: {url: response tuple or Exception}; anything else is a 404."""
    def route(method, url, params, data):
        if url in sites:
            return sites[url]
        if url.endswith("/robots.txt"):
            return ROBOTS_OK
        return (404, "not found", "text/html")
    return route


def test_deep_read_finds_the_email_on_the_privacy_page_and_retries_over_http():
    sites = {
        "https://leafcafe.in/": Permanent("TLS/SSL error"),
        "https://leafcafe.in/robots.txt": Permanent("TLS/SSL error"),
        "http://leafcafe.in/": page("Leaf Cafe Kolkata", 'Call 98300 11111 <a href="/privacy-policy">Privacy</a>'),
        "http://leafcafe.in/privacy-policy": page("Privacy", "Questions? Write to leafcafe.kolkata@gmail.com."),
    }
    http = FakeHttp(router_for(sites))
    plain = crawl_site(http, "https://leafcafe.in/", "Leaf Cafe", max_pages=6)
    assert plain.status == "error" and "robots.txt unreachable" in plain.error      # not reported as refusing robots
    res = crawl_site(FakeHttp(router_for(sites)), "https://leafcafe.in/", "Leaf Cafe", max_pages=10, deep=True, variants=True,
                     known_phones=("+919830011111",))
    emails = {c.value: c for c in res.contacts if c.kind == "email"}
    assert res.status == "ok" and "leafcafe.kolkata@gmail.com" in emails
    assert emails["leafcafe.kolkata@gmail.com"].confidence == "medium"            # Gmail address carrying the site's name
    assert emails["leafcafe.kolkata@gmail.com"].source_url == "http://leafcafe.in/privacy-policy"


def test_text_email_on_own_site_counts_only_when_tied_to_the_business():
    sites = {"https://brewhouse.in/": page("Welcome", "Phone 98300 22222. Mail brewhouse.orders@gmail.com or "
                                                      "ourwebdesigner@gmail.com, info@brewhouse.in")}
    res = crawl_site(FakeHttp(router_for(sites)), "https://brewhouse.in/", "BH Kitchen & Bar", max_pages=2,
                     known_phones=("+919830022222",))             # owned (phone), name does not match the title
    conf = {c.value: c.confidence for c in res.contacts if c.kind == "email"}
    assert conf == {"brewhouse.orders@gmail.com": "medium", "info@brewhouse.in": "medium", "ourwebdesigner@gmail.com": "low"}
    other = crawl_site(FakeHttp(router_for(sites)), "https://brewhouse.in/", "Totally Different Salon", max_pages=2,
                       known_phones=("+919830099999",))          # not theirs: nothing is promoted
    assert {c.confidence for c in other.contacts if c.kind == "email"} == {"low"}


def test_website_discovery_needs_the_business_phone_number():
    assert name_slugs("Kanchan Bakery")[0] == "kanchanbakery"
    sites = {
        "https://kanchanbakery.com/": page("Kanchan Bakery - Cakes", "Order: 94332 43392"),
        "https://kanchanbakery.in/": page("Kanchan Bakery Delhi", "Call 98111 00000"),        # another city's bakery
        "https://kanchanbakerykolkata.com/": page("Domain for sale", "This domain is for sale. Buy this domain."),
    }
    resolver = FakeResolver({"kanchanbakery.in", "kanchanbakerykolkata.com", "kanchanbakery.com"})
    d = discover_website(FakeHttp(router_for(sites)), "Kanchan Bakery", ["+919433243392"], resolver=resolver)
    assert d.url == "https://kanchanbakery.com/" and "phone" in d.how
    d2 = discover_website(FakeHttp(router_for(sites)), "Kanchan Bakery", ["+919000000001"], resolver=resolver)
    assert d2.url == "" and any("not proven" in r for r in d2.rejected) and any("parked" in r for r in d2.rejected)


def test_hunt_adds_real_emails_records_each_lead_once(tmp_path):
    db = DB(str(tmp_path / "h.sqlite"))
    cfg = make_config()
    with db.tx():
        place(db, "k1", "Leaf Cafe", website="https://leafcafe.in/")
        db.add_contact("k1", "phone", "+919830011111", source="overture", confidence="medium")
        place(db, "k2", "Kanchan Bakery")
        db.add_contact("k2", "phone", "+919433243392", source="overture", confidence="medium")
        place(db, "k3", "Tea Stall")
        db.add_contact("k3", "phone", "+919830055555", source="overture", confidence="medium")
        place(db, "k4", "Has Email", website="https://hasemail.in/")
        db.add_contact("k4", "email", "a@hasemail.in", source="overture", confidence="medium")
    sites = {
        "https://leafcafe.in/": page("Leaf Cafe", '98300 11111 <a href="/terms">Terms</a>'),
        "https://leafcafe.in/terms": page("Terms", "Contact hello@leafcafe.in"),
        "https://kanchanbakery.com/": page("Kanchan Bakery", 'Order on 94332 43392 <a href="/contact">Contact</a>'),
        "https://kanchanbakery.com/contact": page("Contact", '<a href="mailto:kanchanbakery@gmail.com">mail us</a>'),
    }
    hunt = EmailHunt(cfg, db, limit=10, use_sheets=False, http=FakeHttp(router_for(sites)),
                     resolver=FakeResolver({"kanchanbakery.com"}), workers=2, detail_path=str(tmp_path / "d.csv"))
    code, s = hunt.run()
    assert code == 0, s
    assert (s["leads_with_email_before"], s["leads_with_email_after"]) == (1, 3)
    assert s["outcomes"]["e-mail found on its own website (deeper read)"] == 1
    assert s["outcomes"]["e-mail found on a website found for it"] == 1
    assert s["outcomes"]["no website found"] == 1
    assert db.scalar("SELECT website FROM places WHERE key='k2'") == "https://kanchanbakery.com/"
    src = db.one("SELECT source_url FROM contacts WHERE place_key='k2' AND kind='email'")["source_url"]
    assert src == "https://kanchanbakery.com/contact"
    assert db.scalar("SELECT sync_state FROM places WHERE key='k2'") == "pending"     # goes to the sheet
    detail = (tmp_path / "d.csv").read_text()
    assert "kanchanbakery@gmail.com" in detail and "Tea Stall" in detail
    # a second run does not look at the same leads again
    code, s2 = EmailHunt(cfg, db, limit=10, use_sheets=False, http=FakeHttp(router_for(sites)),
                         resolver=FakeResolver(set()), workers=2).run()
    assert s2["leads_looked_at"] == 0
