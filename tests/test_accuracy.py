"""Regression tests for wrong attributions seen in a live sample (synthetic data shaped like the real cases)."""
from urllib.parse import urlsplit

from helpers import FakeHttp, biz, gmaps_payload
from leadgen.enrich.extract import canonical_social
from leadgen.enrich.phones import refine_phone_label
from leadgen.enrich.search import Result
from leadgen.enrich.social import best_match
from leadgen.enrich.website import crawl_site, normalize_url
from leadgen.providers.gmaps import parse_search_response
from leadgen.quality import is_chain, match_strength, name_score

HOME = ["Kolkata", "Taltala", "Calcutta"]


def R(title, url, snippet=""):
    return Result(title, url, snippet, "yahoo")


def test_posts_by_other_accounts_are_not_the_business_page():
    blogger_post = "https://www.facebook.com/FoodieCoupleOfficialPage/posts/123456"
    assert canonical_social(blogger_post, profile_only=True) is None
    assert canonical_social("https://www.facebook.com/someone.54772/photos/a.1/2/", profile_only=True) is None
    assert canonical_social("https://www.instagram.com/foodblogger/p/ABC123/", profile_only=True) is None
    assert canonical_social("https://www.facebook.com/glenburncafe/about", profile_only=True) == ("facebook", "https://www.facebook.com/glenburncafe")
    res = [R("Abdul Khalique & Sons Restaurant - Foodie Couple | Facebook", blogger_post)]
    assert best_match("facebook", "Abdul Khalique & Sons Restaurant", res, HOME) is None


def test_near_miss_and_embedded_names_are_rejected():
    assert name_score("Cafe Arabiya", "Cafe Arabica", handle="cafearabicaofficial") < 0.75
    assert name_score("Alisha", "Talishaa", handle="talishaa_t") < 0.75
    assert name_score("Green Leaf Cafe", "Blue Leaf Restaurant") < 0.75


def test_spelling_variants_and_seo_names_still_match():
    assert name_score("Aami Bangali Restaurant Park Street", "Aami Bengali", handle="aamibangali_kolkata") >= 0.9
    assert name_score("Dawat Restaurant", "", handle="daawatrestaurant") >= 0.9
    assert name_score("The Prime Banquet - Best Banquet Hall in Dharmatala, Esplanade Kolkata", "The Prime Banquet",
                      handle="theprimebanquet") >= 0.9
    assert name_score("Zero Degree Cafe and Lounge Esplanade", "Zero Degree", handle="zerodegreekolkata") >= 0.75
    assert name_score("Raj's Spanish Cafe", "", handle="raj_spanish_cafe") >= 0.9


def test_common_word_names_need_local_evidence():
    assert match_strength("Natural", "Natural", "natural", "Natural (@natural) • Instagram photos", HOME) == "weak"
    assert match_strength("Natural", "Natural", "natural", "Natural, Janbazar, Kolkata - healthy food", HOME) == "strong"
    assert match_strength("EMPIRE RESTAURANT & BAR", "Empire", "EMPIRE1950", "", HOME) == "weak"
    assert match_strength("Nutririch cafe", "Nutririch", "the.nutririch", "", HOME) == "strong"      # unusual word
    assert match_strength("Ruby Kitchen", "The Ruby Kitchen", "the_ruby_kitchen", "", HOME) == "weak"        # common word only
    assert match_strength("Ruby Kitchen", "The Ruby Kitchen", "the_ruby_kitchen", "Ruby Kitchen, Kolkata", HOME) == "strong"
    assert match_strength("Kzar Banquet", "Kzar Banquets", "kzar.banquets", "", HOME) == "strong"            # unusual word
    assert match_strength("Jimmy's Restaurant and Bar", "Jimmy's Restaurant", "jimmys_restaurant", "", HOME) == "weak"   # first name
    assert match_strength("Jimmy's Restaurant and Bar", "Jimmy's Restaurant", "jimmys_restaurant", "New Market, Kolkata", HOME) == "strong"
    # a person who shares the restaurant's name is someone else, even if they live in Kolkata
    assert match_strength("Alisha", "Alisha Mondal", "alisha.mondal2", "Alisha Mondal is on Facebook. Lives in Kolkata", HOME) == "weak"
    assert match_strength("The Street Cafe", "The Street Cafe", "officialthestreetcafe", "", HOME) == "weak"
    weak = best_match("instagram", "Natural", [R("Natural (@natural) • Instagram photos and videos", "https://www.instagram.com/natural/")], HOME)
    assert weak is not None and weak.strength == "weak"


def _site_router(pages):
    def router(method, url, params, data):
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return (404, "", "text/html")
        body = pages.get(parts.hostname + parts.path)
        return (200, body, "text/html") if body is not None else (404, "not found", "text/html")
    return router


def test_hijacked_domain_gives_nothing():
    spam = ('<html><head><title>BOSMUDA77 : Situs Slot Gacor Hari Ini</title></head><body>'
            '<a href="https://wa.me/6281262589513">wa</a><a href="https://www.instagram.com/bosmuda77/">ig</a></body></html>')
    res = crawl_site(FakeHttp(_site_router({"daawatrestaurant.org/": spam})), "https://daawatrestaurant.org/", "Dawat Restaurant")
    assert res.status == "hijacked" and res.contacts == []


def test_listed_domain_forwarding_to_an_unrelated_site_is_dropped():
    spam = ('<html><head><title>BOSMUDA77 Login</title></head><body><a href="https://wa.me/6281262589513">wa</a>'
            '<a href="https://www.instagram.com/bosmuda77/">ig</a></body></html>')

    def router(method, url, params, data):
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return (404, "", "text/html")
        if parts.hostname == "daawatrestaurant.org":       # the transport followed a redirect to another domain
            return (200, spam, "text/html", "https://bosmuda77.xyz/")
        return (404, "", "text/html")
    res = crawl_site(FakeHttp(router), "https://daawatrestaurant.org/", "Dawat Restaurant", known_phones=("+919831724018",))
    assert res.status == "moved" and res.contacts == [] and "bosmuda77.xyz" in res.error


def test_unrelated_site_contacts_are_unverified_unless_the_listing_phone_is_there():
    page = ('<html><head><title>Jalan Builders | Real estate</title><meta name="description" content="Jalan Builders develop homes">'
            '</head><body><a href="mailto:inquiry@jalanbuilders.com">mail</a><a href="tel:+913322904155">call</a>'
            '<a href="https://www.instagram.com/jalan_builders/">ig</a></body></html>')
    http = FakeHttp(_site_router({"www.jalanbuilders.com/": page}))
    res = crawl_site(http, "https://www.jalanbuilders.com/", "Myrah Banquets", known_phones=("+919073971151",))
    assert not res.owned and res.description == ""
    assert res.contacts and all(c.confidence == "low" for c in res.contacts)
    res2 = crawl_site(http, "https://www.jalanbuilders.com/", "Myrah Banquets", known_phones=("+913322904155",))
    assert res2.owned and any(c.confidence != "low" for c in res2.contacts)


def test_foreign_numbers_on_a_site_are_unverified():
    page = ('<html><head><title>Kzar Banquet Kolkata</title></head><body><a href="tel:+919836888788">call</a>'
            '<a href="tel:+6281262589513">intl</a></body></html>')
    res = crawl_site(FakeHttp(_site_router({"kzarbanquet.com/": page})), "https://kzarbanquet.com/", "Kzar Banquet")
    conf = {c.value: c.confidence for c in res.contacts}
    assert conf["+919836888788"] != "low" and conf["+6281262589513"] == "low"


def test_phone_labels_for_a_local_campaign():
    assert refine_phone_label("+918820486316", "mobile/landline", ["+913"]) == "mobile"
    assert refine_phone_label("+911140119724", "landline", ["+913"]).startswith("landline outside the area")
    assert refine_phone_label("+913322523456", "landline", ["+913"]) == "landline"
    assert refine_phone_label("+918820486316", "mobile/landline", []) == "mobile/landline"


def test_url_cleanup_price_tags_and_chains():
    assert normalize_url("https://roshnienterprise.com/?utm_source=google&utm_medium=wix") == "https://roshnienterprise.com/"
    assert normalize_url("https://x.in/page?id=4&gclid=abc") == "https://x.in/page?id=4"
    rec = biz("Hotel Q Inn", 22.57, 88.36, "ChIJqinn00000001", cats=("Hotel",), tag="$13")
    places, _ = parse_search_response(gmaps_payload([rec]))
    assert places[0].description == ""
    assert is_chain("SPOT ON 83258 Hotel Shabnam", []) == "oyo network"
    assert is_chain("Spot On Cafe", []) is None


def test_search_account_differing_from_listed_one_is_unverified(tmp_path):
    from conftest import make_config
    from leadgen.db import DB
    from leadgen.runner import Runner
    from test_runner_e2e import World, yahoo_html

    base = World()

    def world(method, url, params, data):
        if "search.yahoo.com" in url and "Mocha Mansion" in params.get("p", ""):
            return (200, yahoo_html([("Mocha Mansion Bistro (@mochamansionbistro) • Instagram", "https://www.instagram.com/mochamansionbistro/",
                                      "Cafe in Kolkata")]), "text/html")
        return base(method, url, params, data)
    db = DB(str(tmp_path / "s.sqlite"))
    code, _ = Runner(make_config(), db, http=FakeHttp(world), use_sheets=False).run()
    key = db.one("SELECT key FROM places WHERE name='Mocha Mansion'")["key"]
    conf = {c["value"]: c["confidence"] for c in db.contacts_for(key) if c["kind"] == "instagram"}
    assert conf["https://www.instagram.com/mochamansion.kol/"] != "low"          # listed on Google Maps
    assert conf["https://www.instagram.com/mochamansionbistro/"] == "low"      # found by search, differs -> unverified
    from leadgen.report import lead_row
    row = lead_row(db, db.get_place(key), make_config())
    assert "mochamansion.kol" in row["Instagram"] and "mochamansionbistro" not in row["Instagram"]
    assert "mochamansionbistro/ (differs from the account the business lists)" in row["Other Contacts (unverified)"]


def test_contact_person_only_when_the_site_says_so():
    from leadgen.enrich.extract import extract_page, find_people

    assert find_people("Meet Priya Das, Founder of Bloom Cafe") == [("Priya Das", "founder", "Meet Priya Das, Founder of Bloom Cafe")]
    assert [p[0] for p in find_people("Founded in 2015 by Ankit Jain and his wife")] == ["Ankit Jain"]
    assert find_people("Our Team Contact Us Home About") == [] and find_people("The Owner: Kzar Banquet") == []
    assert [p[0] for p in find_people("Contact Us Raju Ahamed, Proprietor")] == ["Raju Ahamed"]
    html = ('<html><head><title>Bloom Cafe</title><script type="application/ld+json">{"@type": "CafeOrCoffeeShop", '
            '"name": "Bloom Cafe", "founder": {"@type": "Person", "name": "Riya Sen"}}</script></head>'
            '<body><p>Proprietor: Mr. Amit Ghosh</p><p>Designed by Akash Web</p></body></html>')
    people = {(f.value, f.how, f.label) for f in extract_page(html, "https://bloomcafe.in/").found if f.kind == "person"}
    assert people == {("Riya Sen", "jsonld", "founder"), ("Amit Ghosh", "text", "proprietor")}


def test_sheet_made_with_the_old_layout_is_upgraded_not_rejected():
    from helpers import FakeSheetsSession
    from leadgen.sheets import LEAD_COLUMNS, OLD_LEAD_LAYOUTS, SheetsClient, SheetsSync

    sess = FakeSheetsSession()
    sess.tabs["Leads"] = {"id": 7, "rows": [list(OLD_LEAD_LAYOUTS[0]), ["PP-00001", "2026-10-06", "Old Cafe"] + [""] * 9 +
                                           ["https://old.in/"] + [""] * 8 + ["g:old", "Called"]]}
    sync = SheetsSync(SheetsClient("sheet", session=sess))
    sync.ensure_tabs()
    rows = sess.tabs["Leads"]["rows"]
    assert rows[0][:len(LEAD_COLUMNS)] == LEAD_COLUMNS
    old = dict(zip(LEAD_COLUMNS, rows[1]))
    assert old["Website"] == "https://old.in/" and old["Key"] == "g:old" and old["Status"] == "Called" and old["Contact Person"] == ""


def test_known_brand_is_a_chain_and_wikipedia_is_not_a_website(tmp_path):
    from leadgen.quality import is_aggregator

    assert is_aggregator("https://en.m.wikipedia.org/wiki/Reem")
    from test_open_data import od_config, row, store
    from leadgen.db import DB

    cfg = od_config()
    db = DB(str(tmp_path / "s.sqlite"))
    rows = [dict(row(1, "Monginis Cake Shop", "cafe"), brand="Monginis", brand_wikidata="Q6900993")]
    p = store(db, cfg, rows=rows).search_cell(22.58, 88.42, 2.0, "cafe")[0]
    assert p.extra["brand_known"] and p.extra["brand"] == "Monginis"


def test_common_contact_paths_find_email_not_linked_on_homepage():
    from leadgen.enrich.website import crawl_site
    from helpers import FakeHttp
    from urllib.parse import urlsplit

    HOME = '<html><head><title>Bloom Cafe</title></head><body><h1>Welcome to Bloom Cafe</h1></body></html>'
    CONTACT = '<html><body><p>Email us: hello@bloomcafe.in</p><p>Call 033 4000 2222</p></body></html>'

    def router(method, url, params, data):
        p = urlsplit(url).path
        if p == "/robots.txt":
            return (404, "", "text/html")
        if p in ("/", ""):
            return (200, HOME, "text/html")
        if p.startswith("/contact"):
            return (200, CONTACT, "text/html")
        return (404, "", "text/html")
    res = crawl_site(FakeHttp(router), "https://bloomcafe.in/", "Bloom Cafe")
    emails = {c.value for c in res.contacts if c.kind == "email"}
    assert "hello@bloomcafe.in" in emails          # found by probing /contact directly


def test_microdata_email_and_phone_are_extracted():
    from leadgen.enrich.extract import extract_page
    html = ('<html><body><span itemprop="email">owner@shop.in</span>'
            '<span itemprop="telephone">+91 98300 12345</span></body></html>')
    found = {(f.kind, f.value) for f in extract_page(html, "https://shop.in/").found}
    assert ("email", "owner@shop.in") in found and ("phone", "+919830012345") in found


def test_role_email_candidates_are_unverified_and_gated(monkeypatch, tmp_path):
    from urllib.parse import urlsplit
    from helpers import FakeHttp
    from leadgen.db import DB
    from leadgen.enrich.emails import MXChecker
    from leadgen.report import lead_row
    from leadgen.runner import Runner
    from test_open_data import od_config, row, store

    monkeypatch.setattr(MXChecker, "has_mx", lambda self, d: d == "brewcorner.in")   # only this domain accepts mail
    cfg = od_config(enrich={"role_email_candidates": True, "check_email_mx": True, "website": True,
                            "social_search": False, "instagram_profile": False, "workers": 2})
    rows = [row(1, "Brew Corner", "cafe", websites=["https://brewcorner.in"]),            # own site, MX ok -> candidates
            row(2, "No MX Cafe", "cafe", dlat=0.002, websites=["https://nomxcafe.in"]),    # own site, no MX -> none
            row(3, "Gmail Cafe", "cafe", dlng=0.002, emails=["real@gmail.com"])]           # already has an email -> none
    db = DB(str(tmp_path / "s.sqlite"))

    def world(method, url, params, data):
        if urlsplit(url).path == "/robots.txt":
            return (404, "", "text/html")
        return (404, "", "text/html")                  # no site content, so no published email is found
    code, s = Runner(cfg, db, http=FakeHttp(world), use_sheets=False, overture=store(db, cfg, rows=rows)).run()
    assert code == 0, s

    def emails(name, conf=None):
        k = db.one("SELECT key FROM places WHERE name=?", (name,))["key"]
        return {c["value"]: c for c in db.contacts_for(k) if c["kind"] == "email" and (conf is None or c["confidence"] == conf)}
    brew = emails("Brew Corner")
    assert set(brew) == {"info@brewcorner.in", "contact@brewcorner.in"}
    assert all(c["confidence"] == "low" and c["source"] == "guess" for c in brew.values())
    assert emails("No MX Cafe") == {}                   # domain can't receive mail -> not added
    assert set(emails("Gmail Cafe")) == {"real@gmail.com"}   # already had a real email -> no guesses
    # the guessed addresses never appear in the verified Emails column
    row_brew = lead_row(db, db.get_place(db.one("SELECT key FROM places WHERE name='Brew Corner'")["key"]), cfg)
    assert row_brew["Emails"] == "" and "info@brewcorner.in (role address" in row_brew["Other Contacts (unverified)"]


def test_role_email_candidates_added_even_after_the_main_deadline(monkeypatch, tmp_path):
    """Regression (commit 8e512cd): a big run spends the whole time budget in the main loop,
    so by the time finalize runs now() is already past the deadline. Role-email finalize has
    its OWN small budget and must still add candidates - it must not be gated on the main
    deadline, or large runs (the ones that most need role addresses) would add none."""
    from urllib.parse import urlsplit  # noqa: F401 - kept parallel to the gated test
    from helpers import FakeHttp
    from leadgen.db import DB
    from leadgen.enrich.emails import MXChecker
    from leadgen.runner import Runner
    from test_open_data import od_config, row, store

    monkeypatch.setattr(MXChecker, "has_mx", lambda self, d: d == "brewcorner.in")
    # Role candidates OFF during the run, so the normal finalize adds none - we add them by hand after.
    cfg = od_config(enrich={"role_email_candidates": False, "check_email_mx": True, "website": True,
                            "social_search": False, "instagram_profile": False, "workers": 2})
    rows = [row(1, "Brew Corner", "cafe", websites=["https://brewcorner.in"])]
    db = DB(str(tmp_path / "s.sqlite"))

    def world(method, url, params, data):
        return (404, "", "text/html")                  # no site content, so no published email is found

    runner = Runner(cfg, db, http=FakeHttp(world), use_sheets=False, overture=store(db, cfg, rows=rows))
    code, s = runner.run()
    assert code == 0, s
    key = db.one("SELECT key FROM places WHERE name='Brew Corner'")["key"]
    assert not [c for c in db.contacts_for(key) if c["source"] == "guess"]   # none yet (feature was off)

    # Reproduce the big-run situation: feature on, and the main loop's deadline already in the past.
    runner.cfg["enrich"]["role_email_candidates"] = True
    runner.deadline = runner.now() - 1.0                 # "out of time" for the main loop
    assert runner.now() >= runner.deadline               # the exact condition the old code tripped on
    runner._add_role_candidates()

    guesses = {c["value"] for c in db.contacts_for(key) if c["source"] == "guess"}
    assert guesses == {"info@brewcorner.in", "contact@brewcorner.in"}
    assert runner.stats["role_email_candidates"] == 2
