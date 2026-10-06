"""Open-data mode: Overture Maps discovery, category mapping, plan updates and a full compliant run (synthetic data)."""
from urllib.parse import urlsplit

from conftest import make_config
from helpers import FakeHttp
from leadgen.db import DB
from leadgen.planner import Planner
from leadgen.providers.overture import OvertureStore
from leadgen.runner import Runner

C = (22.58, 88.42)
OD_CATEGORIES = [
    {"key": "cafe", "label": "Cafe", "queries": ["cafe", "coffee shop"], "match": ["cafe"], "overture": ["cafe", "coffee_shop"]},
    {"key": "restaurant", "label": "Restaurant", "queries": ["restaurant"], "match": ["restaurant"],
     "overture": ["restaurant", "*_restaurant", "bar"]},
    {"key": "banquet_venue", "label": "Banquet", "queries": ["banquet hall"], "match": ["banquet"],
     "overture": ["event_venue", "party_and_event_planning"]},
    {"key": "event_planner", "label": "Events", "queries": ["event management company"], "match": ["event"],
     "overture": ["party_and_event_planning", "caterer"]},
]


def od_config(**kw):
    return make_config(compliance={"mode": "open-data"}, categories=OD_CATEGORIES, **kw)


def row(i, name, code, basic=None, dlat=0.0, dlng=0.0, phones=("+91 98300 1%04d",), emails=(), websites=(), socials=(),
        conf=0.8, brand=None, status=None, datasets=("meta",)):
    return {"id": f"08f{i:013d}", "name": name, "lat": C[0] + dlat, "lng": C[1] + dlng, "code": code, "basic": basic or code,
            "alternates": [], "phones": [p % i if "%" in p else p for p in phones], "emails": list(emails), "websites": list(websites),
            "socials": list(socials), "street": f"{i} Test Road", "locality": "Salt Lake", "postcode": "700091",
            "confidence": conf, "brand": brand, "status": status, "datasets": list(datasets)}


ROWS = [
    row(1, "Green Leaf Cafe", "cafe", emails=["hello@greenleafcafe.in"], websites=["https://www.greenleafcafe.in/"],
        socials=["https://www.facebook.com/greenleafcafe.kol"]),
    row(2, "Brew Corner", "coffee_shop", dlat=0.002),
    row(3, "Tandoor House", "indian_restaurant", basic="restaurant", dlng=0.003),
    row(4, "Royal Banquet Hall", "party_and_event_planning", basic="event_or_party_service", dlat=-0.003),
    row(5, "Dream Events Kolkata", "party_and_event_planning", basic="event_or_party_service", dlat=-0.004),
    row(6, "Net Zone", "internet_cafe", basic="cafe", dlng=-0.002),                      # excluded code
    row(7, "Coffee Spot Salt Lake", "cafe", brand="Starbucks", dlng=0.004),             # chain via brand
    row(8, "Old Cafe", "cafe", status="permanently_closed", dlat=0.004),                 # closed
    row(9, "Silent Cafe", "cafe", phones=(), dlat=0.005),                                # no contact -> not a lead
    row(10, "Far Away Cafe", "cafe", dlat=0.3),                                          # outside the circle
]

GREEN_HOME = ('<html><head><title>Green Leaf Cafe | Salt Lake</title></head><body>'
              '<a href="mailto:events@greenleafcafe.in">mail</a><a href="https://wa.me/919830011111">wa</a></body></html>')


def store(db, cfg, rows=ROWS, release="2026-09-23.1", calls=None):
    def fetcher(rel, bbox, codes, min_conf):
        if calls is not None:
            calls.append((rel, tuple(codes)))
        return [dict(r) for r in rows]
    return OvertureStore(db, cfg, fetcher=fetcher, release_fn=lambda: release)


def test_category_mapping_and_exclusions(tmp_path):
    cfg = od_config()
    st = store(DB(str(tmp_path / "s.sqlite")), cfg)
    assert st.category_for("cafe", "cafe", "X") == "cafe"
    assert st.category_for("internet_cafe", "cafe", "Net Zone") is None           # excluded even though basic is cafe
    assert st.category_for("thai_restaurant", "restaurant", "Thai") == "restaurant"
    assert st.category_for("party_and_event_planning", "event_or_party_service", "Royal Banquet Hall") == "banquet_venue"
    assert st.category_for("party_and_event_planning", "event_or_party_service", "Dream Events") == "event_planner"
    assert st.category_for("caterer", "event_or_party_service", "Tasty Caterers") == "event_planner"
    assert st.category_for("bank", "financial_service", "Some Bank") is None
    # the noisy Facebook category needs evidence that the business is about events
    assert st.category_for("party_and_event_planning", "event_or_party_service", "Assam Petro Chemicals") is None
    assert st.category_for("party_and_event_planning", "event_or_party_service", "B You", "events.byou@gmail.com") == "event_planner"
    assert st.category_for("party_and_event_planning", "event_or_party_service", "RedMagma Productions") == "event_planner"


def test_extract_is_cached_and_refreshed_only_when_needed(tmp_path):
    cfg = od_config()
    db = DB(str(tmp_path / "s.sqlite"))
    calls = []
    st = store(db, cfg, calls=calls)
    places = st.search_cell(C[0], C[1], 2.0, "cafe")
    names = {p.name for p in places}
    assert "Green Leaf Cafe" in names and "Brew Corner" in names and "Net Zone" not in names and "Far Away Cafe" not in names
    assert len(calls) == 1 and "*_restaurant" in calls[0][1]
    store(db, cfg, calls=calls).search_cell(C[0], C[1], 2.0, "cafe")            # fresh extract: no download
    assert len(calls) == 1

    def broken(*a):
        raise RuntimeError("S3 unreachable")
    db.set_meta("overture_checked_at", "0")                                     # stale -> refresh attempt fails ...
    st2 = OvertureStore(db, cfg, fetcher=broken, release_fn=lambda: "2026-10-21.0")
    assert st2.search_cell(C[0], C[1], 2.0, "cafe")                             # ... but the stored extract is still used


def test_open_data_place_details(tmp_path):
    cfg = od_config()
    st = store(DB(str(tmp_path / "s.sqlite")), cfg)
    p = next(x for x in st.search_cell(C[0], C[1], 2.0, "cafe") if x.name == "Green Leaf Cafe")
    assert p.key.startswith("ov:") and p.provider == "overture" and p.extra["category"] == "cafe"
    assert p.website == "https://www.greenleafcafe.in/" and p.extra["emails"] == ["hello@greenleafcafe.in"]
    assert p.maps_url.startswith("https://www.google.com/maps/search/?api=1&query=Green%20Leaf%20Cafe")


def test_category_change_does_not_need_a_replan(tmp_path):
    cfg = make_config()
    db = DB(str(tmp_path / "s.sqlite"))
    pl = Planner(cfg, db, None)
    pl.ensure_plan()
    before = db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search'")
    cells = db.scalar("SELECT COUNT(*) FROM cells")
    cats = cfg["categories"] + [{"key": "hotel", "label": "Hotel", "queries": ["hotel"], "match": ["hotel"]}]
    cfg2 = make_config(categories=cats)
    Planner(cfg2, db, None).ensure_plan()                                       # no PlanMismatch
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search'") == before + cells
    assert db.scalar("SELECT COUNT(*) FROM parts") == 3
    Planner(make_config(categories=cfg["categories"][:1]), db, None).ensure_plan()   # drop the banquet category
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search' AND json_extract(payload,'$.category')='banquet_venue'") == 0


def test_full_open_data_run_touches_no_google_and_crawls_as_a_named_bot(tmp_path):
    cfg = od_config()
    db = DB(str(tmp_path / "s.sqlite"))
    seen = []

    def world(method, url, params, data):
        host = urlsplit(url).hostname or ""
        seen.append(host)
        if urlsplit(url).path == "/robots.txt":
            return (404, "", "text/html")
        if host == "www.greenleafcafe.in":
            return (200, GREEN_HOME, "text/html")
        if "overpass" in host:
            return (503, "", "text/html")
        return (404, "not found", "text/html")
    http = FakeHttp(world)
    ua = []
    real_do = http._do

    def spy(method, url, headers, *a, **k):
        ua.append((urlsplit(url).hostname, headers.get("User-Agent", "")))
        return real_do(method, url, headers, *a, **k)
    http._do = spy
    code, s = Runner(cfg, db, http=http, use_sheets=False, overture=store(db, cfg)).run()
    assert code == 0, s
    assert not any(h.endswith(("google.com", "yahoo.com", "duckduckgo.com", "instagram.com", "facebook.com")) for h in seen)
    assert ua and not any("Chrome/" in u for _, u in ua)                                  # never disguised as a browser
    assert all("PlantParlourLeadBot" in u for h, u in ua if h == "www.greenleafcafe.in")
    places = {r["name"]: r for r in db.q("SELECT * FROM places")}
    assert places["Coffee Spot Salt Lake"]["excluded"].startswith("chain")
    assert places["Old Cafe"]["excluded"].startswith("closed")
    assert "Net Zone" not in places and "Far Away Cafe" not in places
    assert places["Silent Cafe"]["qualified"] == 0
    assert places["Royal Banquet Hall"]["category"] == "banquet_venue" and places["Dream Events Kolkata"]["category"] == "event_planner"
    g = {(c["kind"], c["value"]): c for c in db.contacts_for(places["Green Leaf Cafe"]["key"])}
    assert g[("phone", "+919830010001")]["source"] == "overture"
    assert g[("email", "hello@greenleafcafe.in")]["source"] == "overture"
    assert g[("facebook", "https://www.facebook.com/greenleafcafe.kol")]["confidence"] == "high"
    assert ("email", "events@greenleafcafe.in") in g and ("whatsapp", "+919830011111") in g     # from its own website
    assert s["new_leads_today"] >= 5
    from leadgen.report import lead_row
    r = lead_row(db, places["Green Leaf Cafe"], cfg)
    assert "Overture Maps (open data)" in r["Contact Sources"]


def test_real_extract_query_on_a_file_with_overtures_layout(tmp_path):
    """Runs the production SQL against a Parquet file with the same nested columns as Overture's places."""
    import pytest

    duckdb = pytest.importorskip("duckdb")
    from leadgen.providers.overture import fetch_area

    path = str(tmp_path / "places.parquet")
    name = "{'primary': %s, 'common': NULL, 'rules': NULL}"
    brand = "{'wikidata': NULL, 'names': {'primary': %s, 'common': NULL, 'rules': NULL}}"

    def rec(i, nm, conf, code, basic, lng, lat, phones="[]", brand_name="NULL"):
        return (f"('id{i}', {name % repr(nm)}, {conf}::DOUBLE, ['https://x{i}.in'], ['a@x{i}.in'], ['https://www.facebook.com/p{i}'], "
                f"{phones}::VARCHAR[], {brand % brand_name}, [{{'freeform': '{i} Road', 'locality': 'Kolkata', 'postcode': '700001', "
                f"'region': 'WB', 'country': 'IN'}}], [{{'property': '', 'dataset': 'meta', 'license': 'CDLA-Permissive-2.0', "
                f"'record_id': 'r{i}', 'update_time': NULL, 'confidence': 0.9}}], NULL, '{basic}', "
                f"{{'primary': '{code}', 'hierarchy': ['x'], 'alternates': ['y']}}, "
                f"{{'xmin': {lng}::DOUBLE, 'xmax': {lng}::DOUBLE, 'ymin': {lat}::DOUBLE, 'ymax': {lat}::DOUBLE}})")
    rows = [rec(1, "Green Leaf Cafe", 0.9, "cafe", "cafe", 88.35, 22.55, "['+919830012345']"),
            rec(2, "Thai Spot", 0.7, "thai_restaurant", "restaurant", 88.36, 22.56),
            rec(3, "Some Bank", 0.95, "bank", "financial_service", 88.36, 22.56, brand_name="'Bank X'"),
            rec(4, "Low Conf Cafe", 0.1, "cafe", "cafe", 88.36, 22.56),
            rec(5, "Far Cafe", 0.9, "cafe", "cafe", 80.0, 22.56)]
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES {', '.join(rows)}) t(id, names, confidence, websites, emails, socials, phones, brand, "
        f"addresses, sources, operating_status, basic_category, taxonomy, bbox)) TO '{path}' (FORMAT parquet)")
    got = fetch_area("test", (87.39, 21.67, 89.34, 23.47), ["cafe", "*_restaurant"], 0.4, source=path)
    by = {r["name"]: r for r in got}
    assert set(by) == {"Green Leaf Cafe", "Thai Spot"}
    g = by["Green Leaf Cafe"]
    assert g["phones"] == ["+919830012345"] and g["datasets"] == ["meta"] and g["locality"] == "Kolkata" and g["code"] == "cafe"
