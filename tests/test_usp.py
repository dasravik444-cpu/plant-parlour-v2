"""USP line: the business's own one-line claim, from its own website only."""
from __future__ import annotations

from conftest import make_config
from helpers import FakeHttp, FakeSheetsSession

from leadgen.db import DB
from leadgen.enrich.usp import pick_usp
from leadgen.enrich.website import crawl_site
from leadgen.report import lead_row
from leadgen.sheets import LEAD_COLUMNS, OLD_LEAD_LAYOUTS, SheetsClient, SheetsSync


def test_usp_is_a_concrete_claim_in_the_business_s_own_words():
    assert pick_usp(["Peter Cat - Iconic Park Street restaurant famous for its Chelo Kebab since 1975"], "Peter Cat") == \
        "Iconic Park Street restaurant famous for its Chelo Kebab since 1975"
    assert pick_usp(["Welcome to Leaf Cafe, Kolkata's first plant-themed cafe"], "Leaf Cafe") == "Kolkata's first plant-themed cafe"
    assert pick_usp(["ABC Interiors - 15+ years of experience and 500+ projects across Kolkata"], "ABC Interiors") == \
        "15+ years of experience and 500+ projects across Kolkata"
    # SEO taglines, contact lines and nothing-special texts give no USP (the column stays empty)
    assert pick_usp(["Best Banquet Hall in Kolkata | Royal Banquet"], "Royal Banquet") == ""
    assert pick_usp(["Welcome to Hotel Samrat. Book now! Call us on 9830012345"], "Hotel Samrat") == ""
    assert pick_usp(["Spa & Salon in Kolkata", "Home | About | Contact"], "Desire Spa") == ""


def test_website_crawl_adds_the_usp_and_the_sheet_shows_it(tmp_path):
    def route(method, url, params, data):
        if url.endswith("/robots.txt"):
            return (200, "User-agent: *\nAllow: /\n", "text/plain")
        if url == "https://petercat.in/":
            return (200, "<html><head><title>Peter Cat</title><meta name='description' content='Iconic Park Street restaurant "
                         "famous for its Chelo Kebab since 1975'></head><body>Call 033 2229 8841</body></html>", "text/html")
        return (404, "not found", "text/html")
    res = crawl_site(FakeHttp(route), "https://petercat.in/", "Peter Cat", max_pages=2, known_phones=("+913322298841",))
    usp = [c for c in res.contacts if c.kind == "usp"]
    assert [c.value for c in usp] == ["Iconic Park Street restaurant famous for its Chelo Kebab since 1975"]
    assert usp[0].source_url == "https://petercat.in/"
    db = DB(str(tmp_path / "u.sqlite"))
    with db.tx():
        db.insert_place({"key": "k1", "name": "Peter Cat", "category": "restaurant", "provider": "overture",
                         "found_date": "2026-10-08", "qualified": 1, "lead_no": 1})
        db.add_contact("k1", "usp", usp[0].value, source="website", source_url=usp[0].source_url, confidence="medium")
    assert lead_row(db, db.get_place("k1"), make_config())["USP"] == usp[0].value


def test_sheet_with_the_signals_layout_gets_the_usp_column_with_data_kept():
    live = OLD_LEAD_LAYOUTS[-1]
    row = {c: "" for c in live}
    row.update({"Lead ID": "PP-00009", "Business Name": "Cafe Y", "Signals": "Runs Google Ads", "Description": "Cosy",
                "Key": "ov:y", "Status": "Emailed"})
    sess = FakeSheetsSession()
    sess.tabs["Leads"] = {"id": 7, "rows": [list(live), [row[c] for c in live]]}
    SheetsSync(SheetsClient("sheet", session=sess)).ensure_tabs()
    got = dict(zip(LEAD_COLUMNS, sess.tabs["Leads"]["rows"][1]))
    assert sess.tabs["Leads"]["rows"][0][:len(LEAD_COLUMNS)] == LEAD_COLUMNS
    assert (got["USP"], got["Signals"], got["Description"], got["Key"], got["Status"]) == ("", "Runs Google Ads", "Cosy", "ov:y", "Emailed")


def test_usp_backfill_from_stored_descriptions_runs_once(tmp_path):
    from leadgen.runner import Runner

    db = DB(str(tmp_path / "b.sqlite"))
    with db.tx():
        db.insert_place({"key": "k1", "name": "Leaf Cafe", "category": "cafe", "provider": "overture", "found_date": "2026-10-08",
                         "qualified": 1, "website": "https://leafcafe.in/", "description": "Leaf Cafe - Kolkata's first plant-themed cafe"})
        db.insert_place({"key": "k2", "name": "Plain Cafe", "category": "cafe", "provider": "overture", "found_date": "2026-10-08",
                         "qualified": 1, "description": "Cafe in Salt Lake"})
    r = Runner(make_config(), db)
    r._backfill_usp()
    vals = {x["place_key"]: x["value"] for x in db.q("SELECT place_key, value FROM contacts WHERE kind='usp'")}
    assert vals == {"k1": "Kolkata's first plant-themed cafe"}
    assert db.get_meta("usp_backfill") == "v1"


def test_usp_refresh_reads_homepages_of_leads_with_email_first_once(tmp_path):
    from helpers import FakeHttp

    from leadgen.hunt import UspRefresh

    db = DB(str(tmp_path / "r.sqlite"))
    with db.tx():
        db.insert_place({"key": "k1", "name": "Peter Cat", "category": "restaurant", "provider": "overture", "found_date": "2026-10-08",
                         "qualified": 1, "lead_no": 2, "website": "https://petercat.in/"})
        db.add_contact("k1", "email", "info@petercat.in", source="overture", confidence="medium")
        db.insert_place({"key": "k2", "name": "No Mail Cafe", "category": "cafe", "provider": "overture", "found_date": "2026-10-08",
                         "qualified": 1, "lead_no": 1, "website": "https://nomail.in/"})

    def route(method, url, params, data):
        if url.endswith("/robots.txt"):
            return (200, "User-agent: *\nAllow: /\n", "text/plain")
        if url == "https://petercat.in/":
            return (200, "<html><head><title>Peter Cat</title></head><body><h1>Iconic Park Street restaurant since 1975</h1>"
                         "</body></html>", "text/html")
        return (404, "x", "text/html")
    code, s = UspRefresh(make_config(), db, limit=1, use_sheets=False, http=FakeHttp(route), workers=1).run()
    assert s["outcomes"] == {"USP line found": 1}                               # the lead with an e-mail went first
    assert db.scalar("SELECT value FROM contacts WHERE place_key='k1' AND kind='usp'") == "Iconic Park Street restaurant since 1975"
    code, s = UspRefresh(make_config(), db, limit=5, use_sheets=False, http=FakeHttp(route), workers=1).run()
    assert s["outcomes"] == {"website read - nothing distinctive": 1} or s["outcomes"] == {"website unreachable": 1}
