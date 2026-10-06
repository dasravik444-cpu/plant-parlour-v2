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
    assert match_strength("Ruby Kitchen", "The Ruby Kitchen", "the_ruby_kitchen", "", HOME) == "strong"
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
