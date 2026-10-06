import json

import pytest

from helpers import FakeHttp, biz, gmaps_payload
from leadgen.providers.base import ProviderUnavailable
from leadgen.providers.gmaps import GoogleMapsSearch, build_pb, parse_search_response, real_url, zoom_for_span


def test_parse_search_response_fields():
    recs = [biz("Green Leaf Cafe", 22.58, 88.42, "ChIJgreenleaf001", phone_local="098300 12345", phone_intl="+91 98300 12345",
                website="/url?q=https://www.greenleafcafe.in/&opi=1", area="Sector V, Salt Lake", rating=4.4, reviews=310, tag="Brunch"),
            biz("Closed Diner", 22.581, 88.421, "ChIJcloseddiner02", closed=True)]
    places, meta = parse_search_response(gmaps_payload(recs))
    assert meta["method"] == "indexed" and meta["valid"] == 2
    p = places[0]
    assert p.key == "g:ChIJgreenleaf001" and p.name == "Green Leaf Cafe"
    assert p.phone_intl == "+91 98300 12345" and p.website == "https://www.greenleafcafe.in/"
    assert (p.rating, p.reviews, p.area) == (4.4, 310, "Sector V, Salt Lake")
    assert p.description == "Brunch" and not p.closed
    assert places[1].closed and "Permanently closed" in places[1].status_text


def test_parser_rejects_records_without_identity():
    bad = biz("No Coords", None, None, "ChIJnocoords0001")
    bad[9] = None
    places, meta = parse_search_response(gmaps_payload([bad]))
    assert places == [] and meta["records"] == 0


def test_parser_falls_back_to_structural_scan():
    rec = biz("Nested Cafe", 22.5, 88.3, "ChIJnested000001")
    text = ")]}'\n" + json.dumps([[None, None, [[["x", rec]]]]])
    places, meta = parse_search_response(text)
    assert meta["method"] == "scan" and places[0].name == "Nested Cafe"


def test_wrapped_payload():
    text = json.dumps({"c": 0, "d": gmaps_payload([biz("W Cafe", 22.5, 88.3, "ChIJwrapped00001")])}) + '/*""*/'
    assert parse_search_response(text)[0][0].name == "W Cafe"


def test_block_page_raises_provider_unavailable():
    http = FakeHttp(lambda m, u, p, d: (200, "<html>Our systems have detected unusual traffic</html>", "text/html"))
    with pytest.raises(ProviderUnavailable):
        GoogleMapsSearch(http, interval=0, jitter=0).search_page("cafe", 22.5, 88.3, 15)
    http2 = FakeHttp(lambda m, u, p, d: (429, "", "text/html"))
    with pytest.raises(ProviderUnavailable):
        GoogleMapsSearch(http2, interval=0, jitter=0).search_page("cafe", 22.5, 88.3, 15)


def test_pagination_stops_when_results_are_known():
    def router(method, url, params, data):
        off = int(params["pb"].split("!8i")[1].split("!")[0])
        recs = [biz(f"Cafe {off + i}", 22.5 + i * 1e-4, 88.3, f"ChIJpage{off + i:08d}") for i in range(20)]
        return (200, gmaps_payload(recs), "application/json")
    gm = GoogleMapsSearch(FakeHttp(router), interval=0, jitter=0)
    places, meta = gm.search("cafe", 22.5, 88.3, 15, max_pages=3)
    assert len(places) == 60 and len(meta["pages"]) == 3
    places, meta = gm.search("cafe", 22.5, 88.3, 15, max_pages=3, is_known=lambda k: True)
    assert len(meta["pages"]) == 1


def test_helpers():
    assert real_url("/url?q=https://a.in/x&sa=U") == "https://a.in/x"
    assert "!8i40" in build_pb(22.5, 88.3, 15, offset=40)
    assert zoom_for_span(1.0, 22.5) == 16 and zoom_for_span(16, 22.5) == 12
