from leadgen.enrich.search import Result, parse_ddg_html, parse_yahoo
from leadgen.enrich.social import best_match, find_website
from leadgen.quality import is_aggregator, is_chain, match_category, name_score
from helpers import yahoo_html

CATS = [{"key": "cafe", "match": ["cafe", "coffee", "bakery"]}, {"key": "restaurant", "match": ["restaurant", "bar"]},
        {"key": "hotel", "match": ["hotel", "resort"]}]


def R(t, u, s=""):
    return Result(t, u, s, "yahoo")


def test_name_score():
    assert name_score("Afraa Deli", "AfraaDeli", handle="afraa.deli") >= 0.9
    assert name_score("Chai Break - City Centre 2", "Chai Break", handle="chaibreak") >= 0.9
    assert name_score("Green Leaf Cafe", "Blue Leaf Restaurant") < 0.75
    assert name_score("Coffee House", "Indian Coffee House") < 0.75   # generic names need near-exact matches


def test_social_matching_accepts_and_rejects():
    res = [R("AfraaDeli (@afraa.deli) • Instagram photos and videos", "https://www.instagram.com/afraa.deli/"),
           R("AfraaDeli | One dish - Instagram", "https://www.instagram.com/reel/DcInc3DTiYu/"),
           R("Afraa Deli | Kolkata - Facebook", "https://www.facebook.com/afraadeli/")]
    ig = best_match("instagram", "Afraa Deli", res, ["Kolkata"])
    assert ig and ig.url == "https://www.instagram.com/afraa.deli/"
    assert best_match("facebook", "Afraa Deli", res, ["Kolkata"]).url == "https://www.facebook.com/afraadeli"
    other_city = [R("Urban Bites (@urbanbites_mumbai) • Instagram", "https://www.instagram.com/urbanbites_mumbai/", "Mumbai cafe")]
    assert best_match("instagram", "Urban Bites cafe", other_city, ["Kolkata"]) is None
    topic_only = [R("Flurys Park Street Kolkata - Instagram", "https://www.instagram.com/popular/flurys-park-street-kolkata/")]
    assert best_match("instagram", "Flurys", topic_only, ["Kolkata"]) is None
    person = [R("Jamuna Banquets - JAMUNA BANQUETS | LinkedIn", "https://in.linkedin.com/in/jamuna-banquets-8674009a")]
    assert best_match("linkedin", "Jamuna Banquets", person, ["Kolkata"]) is None


def test_find_website_from_results():
    res = [R("Flurys Contact Number", "https://www.flurys.com/pages/contact"), R("Flurys - Wikipedia", "https://en.wikipedia.org/wiki/Flurys"),
           R("Flurys, Park Street | Zomato", "https://www.zomato.com/kolkata/flurys")]
    w = find_website("Flurys", res, ["Kolkata"])
    assert w and w.url == "https://www.flurys.com/"
    assert find_website("Green Leaf Cafe", [R("Best cafes", "https://www.bestcafes.in/")], ["Kolkata"]) is None


def test_filters():
    assert is_chain("Starbucks - Rajarhat", ["starbucks"]) == "starbucks"
    assert is_chain("Starbuck Lane Cafe", ["starbucks"]) is None
    assert match_category(["Coffee shop", "Cafe"], "restaurant", CATS) == "cafe"
    assert match_category(["Hotel"], "cafe", CATS) == "hotel"
    assert match_category(["ATM"], "cafe", CATS) is None
    assert is_aggregator("https://www.zomato.com/kolkata/x") and not is_aggregator("https://greenleafcafe.in")


def test_search_parsers():
    html = yahoo_html([("AfraaDeli (@afraa.deli) • Instagram photos and videos", "https://www.instagram.com/afraa.deli/", "Cafe in Kolkata")])
    res = parse_yahoo(html)
    assert res[0].url == "https://www.instagram.com/afraa.deli/" and res[0].title.startswith("AfraaDeli (@afraa.deli)")
    ddg = ('<div class="result results_links"><div class="links_main result__body"><h2 class="result__title">'
           '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.facebook.com%2Fafraadeli%2F&rut=x">Afraa Deli | Facebook</a></h2>'
           '<a class="result__snippet">Cafe</a></div></div>')
    assert parse_ddg_html(ddg)[0].url == "https://www.facebook.com/afraadeli/"


def _search_with(router):
    from helpers import FakeHttp
    from leadgen.enrich.search import WebSearch

    return WebSearch(FakeHttp(router), interval=0.0, jitter=0.0)


def test_empty_pages_are_not_trusted_until_an_engine_has_returned_results():
    import pytest
    from leadgen.net import BreakerOpen

    state = {"yahoo_results": False}

    def router(method, url, params, data):
        if "yahoo.com" in url:
            if state["yahoo_results"]:
                return (200, yahoo_html([("Cafe X (@cafex) • Instagram", "https://www.instagram.com/cafex/", "")]), "text/html")
            return (200, "<html><body><ol></ol></body></html>", "text/html")   # parses to nothing
        return (202, "", "text/html")                                          # DuckDuckGo rate limit
    ws = _search_with(router)
    for _ in range(5):
        with pytest.raises(BreakerOpen):            # empty answers before any result: keep the task for later
            ws.search('"Cafe X" Kolkata instagram')
    assert ws.http.breaker("search:yahoo").is_open()   # five empty pages in a row: layout probably changed
    assert not ws.available()

    ws2 = _search_with(router)
    state["yahoo_results"] = True
    assert ws2.search('"Cafe X" Kolkata instagram')     # engine proves it parses...
    state["yahoo_results"] = False
    assert ws2.search('"Cafe Y" Kolkata instagram') == []   # ...so a later empty page is a real "nothing found"
