"""End-to-end daily cycle against a fake internet and a fake Google Sheet (synthetic data only)."""
import json
import math
from urllib.parse import urlsplit

import pytest

from conftest import make_config
from helpers import FakeHttp, FakeSheetsSession, biz, gmaps_payload, yahoo_html
from leadgen.db import DB
from leadgen.net import Transient
from leadgen.runner import Runner
from leadgen.sheets import KEY_COL, LEAD_COLUMNS, SheetsClient, SheetsSync

C = (22.58, 88.42)


def at(dn, de):
    return C[0] + dn / 111.0, C[1] + de / 103.0


GREEN_HOME = """<html><head><title>Green Leaf Cafe | Salt Lake</title></head><body>
<a href="mailto:hello@greenleafcafe.in">mail</a> <a href="https://www.instagram.com/greenleafcafe.kol/">ig</a>
<a href="https://wa.me/919830011111">whatsapp</a> <a href="/contact">Contact us</a></body></html>"""
GREEN_CONTACT = "<html><body><p>Events: events@greenleafcafe.in</p><p>Call: 033 4000 1111</p></body></html>"
BREW_HOME = '<html><head><title>Brew Lab Kolkata</title></head><body><a href="mailto:hello@brewlab.in">Email us</a></body></html>'

CAFES = [
    biz("Green Leaf Cafe", *at(0.2, 0.1), "ChIJgreenleaf0001", phone_local="098300 11111", phone_intl="+91 98300 11111",
        website="https://www.greenleafcafe.in/", area="Sector V, Salt Lake"),
    biz("Brew Lab", *at(-0.3, 0.2), "ChIJbrewlab00002", phone_local="098300 22222", phone_intl="+91 98300 22222", area="Salt Lake"),
    biz("Starbucks - Salt Lake", *at(0.1, -0.2), "ChIJstarbucks003", phone_intl="+91 98300 33333", phone_local="098300 33333"),
    biz("Closed Diner", *at(0.4, 0.4), "ChIJcloseddin004", closed=True, phone_intl="+91 98300 44444", phone_local="098300 44444"),
    biz("Sunrise ATM", *at(-0.1, -0.1), "ChIJatmbooth0005", cats=("ATM",)),
    biz("Chai Point Corner", *at(0.6, -0.5), "ChIJchaipoint006"),
    biz("Mocha Mansion", *at(-0.6, 0.5), "ChIJmochamans007", phone_intl="+91 98300 77777", phone_local="098300 77777",
        website="https://www.instagram.com/mochamansion.kol/"),
    biz("Far Away Cafe", 22.90, 88.90, "ChIJfaraway00008", phone_intl="+91 98300 88888", phone_local="098300 88888"),
] + [biz(f"Filler Cafe {i}", *at(-1.0 + i * 0.25, 1.0 - i * 0.2), f"ChIJfiller{i:06d}", phone_intl=f"+91 98301 0000{i}",
         phone_local=f"098301 0000{i}") for i in range(6)]
VENUES = [
    biz("Royal Banquet", *at(0.3, -0.3), "ChIJroyalbanq101", cats=("Banquet hall",), phone_intl="+91 98302 11111",
        phone_local="098302 11111", website="https://royalbanquet.in/"),
    biz("Lotus Events Hall", *at(-0.4, -0.4), "ChIJlotusevnt102", cats=("Event venue",), phone_intl="+91 98302 22222",
        phone_local="098302 22222", website="https://lotuseventshall.in/"),
]


def dist(rec, lat, lng):
    return math.hypot(rec[9][2] - lat, rec[9][3] - lng)


class World:
    def __init__(self):
        self.stop_after = None
        self.calls = 0
        self.runner = None

    def __call__(self, method, url, params, data):
        self.calls += 1
        if self.stop_after and self.calls >= self.stop_after and self.runner:
            self.runner.stop_requested = True
        host = urlsplit(url).hostname or ""
        path = urlsplit(url).path
        if "google.com" in host and path == "/search":
            pb = params["pb"]
            lng = float(pb.split("!2d")[1].split("!")[0])
            lat = float(pb.split("!3d")[1].split("!")[0])
            off = int(pb.split("!8i")[1].split("!")[0])
            pool = VENUES if "banquet" in params["q"] else CAFES
            recs = sorted(pool, key=lambda r: dist(r, lat, lng))[:8] if off == 0 else []
            return (200, gmaps_payload(recs), "application/json")
        if "overpass" in host:
            return Transient("overpass unavailable (test)")
        if path == "/robots.txt":
            if host == "royalbanquet.in":
                return (200, "User-agent: *\nDisallow: /\n", "text/plain")
            return (404, "", "text/html")
        if host == "www.greenleafcafe.in":
            return (200, GREEN_CONTACT if path.startswith("/contact") else GREEN_HOME, "text/html")
        if host == "brewlab.in" or host == "www.brewlab.in":
            return (200, BREW_HOME, "text/html")
        if host == "lotuseventshall.in":
            return Transient("timed out (test)")
        if host == "search.yahoo.com":
            q = params.get("p", "")
            if "Brew Lab" in q and "linkedin" not in q:
                return (200, yahoo_html([("Brew Lab (@brewlab.kol) • Instagram photos and videos", "https://www.instagram.com/brewlab.kol/", "Specialty coffee, Salt Lake"),
                                         ("Brew Lab Kolkata | Specialty Coffee", "https://www.brewlab.in/", "Salt Lake, Kolkata"),
                                         ("Brew Lab, Salt Lake | Zomato", "https://www.zomato.com/kolkata/brew-lab", "")]), "text/html")
            return (200, yahoo_html([("Something unrelated", "https://www.unrelated-site.com/", "")]), "text/html")
        if "instagram.com" in host:
            return (401, '{"message":"login required"}', "application/json")
        return (404, "not found", "text/html")


ALL_FIXTURE_TEXT = (GREEN_HOME + GREEN_CONTACT + BREW_HOME + json.dumps(CAFES) + json.dumps(VENUES)
                    + "brewlab.kol https://www.brewlab.in/ greenleafcafe.kol mochamansion.kol")


def digits(s):
    return "".join(ch for ch in s if ch.isdigit())


def make_runner(cfg, db, world, **kw):
    http = FakeHttp(world)
    r = Runner(cfg, db, http=http, **kw)
    world.runner = r
    return r


def test_full_cycle_evidence_dedup_and_rerun(tmp_path):
    cfg = make_config()
    db = DB(str(tmp_path / "state.sqlite"))
    world = World()
    code, summary = make_runner(cfg, db, world, use_sheets=True).run()
    assert code == 0, summary
    assert summary["sheets"]["status"] == "not configured"
    places = {r["name"]: r for r in db.q("SELECT * FROM places")}
    # exclusions recorded, never sent
    assert places["Starbucks - Salt Lake"]["excluded"].startswith("chain")
    assert places["Closed Diner"]["excluded"].startswith("closed")
    assert places["Sunrise ATM"]["excluded"].startswith("category")
    assert "Far Away Cafe" not in places                       # outside the 3 km circle
    assert places["Chai Point Corner"]["qualified"] == 0        # no contact found -> not a lead

    def contacts(name):
        return {(c["kind"], c["value"]): c for c in db.contacts_for(places[name]["key"])}

    g = contacts("Green Leaf Cafe")
    assert ("phone", "+919830011111") in g and g[("phone", "+919830011111")]["source"] == "google_maps"
    assert ("email", "hello@greenleafcafe.in") in g and ("email", "events@greenleafcafe.in") in g
    assert ("whatsapp", "+919830011111") in g and ("instagram", "https://www.instagram.com/greenleafcafe.kol/") in g
    assert g[("email", "events@greenleafcafe.in")]["source_url"] == "https://www.greenleafcafe.in/contact"
    b = contacts("Brew Lab")
    assert ("instagram", "https://www.instagram.com/brewlab.kol/") in b and b[("instagram", "https://www.instagram.com/brewlab.kol/")]["source"] == "search"
    assert ("email", "hello@brewlab.in") in b                   # website found through search, then crawled
    assert places["Brew Lab"]["website"] == "https://www.brewlab.in/"
    assert ("instagram", "https://www.instagram.com/mochamansion.kol/") in contacts("Mocha Mansion")
    # every stored contact value literally exists in the fake internet (nothing invented)
    for c in db.q("SELECT kind, value FROM contacts"):
        if c["kind"] in ("phone", "whatsapp"):
            assert digits(c["value"])[-10:] in digits(ALL_FIXTURE_TEXT) or digits(c["value"])[-8:] in digits(ALL_FIXTURE_TEXT), c["value"]
        else:
            token = c["value"].split("instagram.com/")[-1].strip("/") if "instagram.com/" in c["value"] else c["value"]
            assert token in ALL_FIXTURE_TEXT, c["value"]
    # failures were isolated to their own tasks
    site_tasks = {json.loads(t["payload"])["url"]: t for t in db.q("SELECT * FROM tasks WHERE kind='site'")}
    assert json.loads(site_tasks["https://royalbanquet.in/"]["result"])["status"] == "blocked_robots"
    assert site_tasks["https://lotuseventshall.in/"]["status"] == "pending" and site_tasks["https://lotuseventshall.in/"]["attempts"] == 1
    assert places["Royal Banquet"]["qualified"] == 1 and places["Lotus Events Hall"]["qualified"] == 1
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='insta' AND status='skipped'") >= 1   # Instagram login wall
    assert summary["new_leads_today"] >= 5
    assert db.one("SELECT status FROM parts WHERE id=1")["status"] == "done"          # scheduled part finished
    lead_nos = [r["lead_no"] for r in db.q("SELECT lead_no FROM places WHERE qualified=1")]
    assert len(lead_nos) == len(set(lead_nos)) and None not in lead_nos

    # --- second run with a Google Sheet: rows appear once, re-runs update instead of duplicating
    sess = FakeSheetsSession()
    factory = lambda: SheetsSync(SheetsClient("sheet", session=sess))  # noqa: E731
    code2, s2 = make_runner(cfg, db, world, sheets_factory=factory).run()
    assert code2 == 0 and s2["sheets"]["status"] == "ok"
    leads = [r for r in sess.tabs["Leads"]["rows"][1:] if any(str(x) for x in r)]
    n_qualified = db.scalar("SELECT COUNT(*) FROM places WHERE qualified=1 AND excluded IS NULL")
    assert len(leads) == n_qualified
    keys = [r[KEY_COL] for r in leads]
    assert len(keys) == len(set(keys))
    by_name = {r[2]: dict(zip(LEAD_COLUMNS, r)) for r in leads}
    assert "hello@greenleafcafe.in" in by_name["Green Leaf Cafe"]["Emails"]
    assert "Chai Point Corner" not in by_name and "Starbucks - Salt Lake" not in by_name
    code3, s3 = make_runner(cfg, db, world, sheets_factory=factory).run()
    leads3 = [r for r in sess.tabs["Leads"]["rows"][1:] if any(str(x) for x in r)]
    assert len(leads3) == len(leads) and s3["sheets"]["added"] == 0
    assert [r[0] for r in sess.tabs["Daily Report"]["rows"][1:]].count(s3["date"]) == 2


def test_stop_signal_saves_and_next_run_resumes(tmp_path):
    cfg = make_config()
    db = DB(str(tmp_path / "state.sqlite"))
    world = World()
    world.stop_after = 4
    code, summary = make_runner(cfg, db, world, use_sheets=False).run()
    assert "stopped early by a stop signal" in summary["warnings"]
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE status='running'") == 0       # nothing left half-done
    world.stop_after = None
    code, summary = make_runner(cfg, db, world, use_sheets=False).run()
    assert code == 0 and db.one("SELECT status FROM parts WHERE id=1")["status"] == "done"


def test_google_blocked_falls_back_and_reports(tmp_path):
    cfg = make_config()
    db = DB(str(tmp_path / "state.sqlite"))

    def blocked(method, url, params, data):
        if "google.com" in (urlsplit(url).hostname or ""):
            return (429, "", "text/html")
        return World()(method, url, params, data)
    r = Runner(cfg, db, http=FakeHttp(blocked), use_sheets=False)
    code, summary = r.run()
    assert code == 2                                            # loud failure -> GitHub emails the owner
    assert any("unavailable" in w for w in summary["warnings"])
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search' AND status='failed'") == 0   # deferred, not burned
    assert db.scalar("SELECT COUNT(*) FROM places") == 0


def test_sheet_error_does_not_lose_leads(tmp_path):
    cfg = make_config()
    db = DB(str(tmp_path / "state.sqlite"))

    class Broken:
        def ensure_tabs(self):
            from leadgen.sheets import SheetsError
            raise SheetsError("HTTP 403 (test)")
    code, summary = make_runner(cfg, db, World(), sheets_factory=lambda: Broken()).run()
    assert code == 2 and summary["sheets"]["status"].startswith("error")
    assert db.scalar("SELECT COUNT(*) FROM places WHERE qualified=1 AND sync_state='pending'") >= 5   # retried next run


def test_all_searches_failing_is_reported_and_keeps_tasks(tmp_path):
    cfg = make_config()
    db = DB(str(tmp_path / "state.sqlite"))

    def down(method, url, params, data):
        if "google.com" in (urlsplit(url).hostname or ""):
            return Transient("connection reset (test)")
        return World()(method, url, params, data)
    code, summary = Runner(cfg, db, http=FakeHttp(down), use_sheets=False, max_searches=2).run()
    assert code == 2 and summary["status"].startswith("degraded")
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search' AND status='failed'") == 0
    assert db.scalar("SELECT MAX(attempts) FROM tasks WHERE kind='search'") == 1


def test_checkpoint_syncs_during_the_run_without_duplicates(tmp_path):
    cfg = make_config()
    cfg["sheets"]["checkpoint_minutes"] = 0.0001          # checkpoint on (almost) every loop pass
    db = DB(str(tmp_path / "state.sqlite"))
    sess = FakeSheetsSession()
    r = make_runner(cfg, db, World(), sheets_factory=lambda: SheetsSync(SheetsClient("sheet", session=sess)))
    calls = []
    real = r._sync_leads
    r._sync_leads = lambda: calls.append(real()) or calls[-1]
    code, s = r.run()
    assert code == 0 and s["sheets"]["status"] == "ok"
    assert len(calls) >= 2 and sum(a for a, _ in calls[:-1]) > 0   # rows reached the sheet before the end
    leads = [row for row in sess.tabs["Leads"]["rows"][1:] if any(str(x) for x in row)]
    keys = [row[KEY_COL] for row in leads]
    assert len(keys) == len(set(keys)) == db.scalar("SELECT COUNT(*) FROM places WHERE qualified=1 AND excluded IS NULL")
    assert s["sheets"]["added"] == len(leads)              # report counts the whole run, checkpoints included
    assert db.scalar("SELECT COUNT(*) FROM places WHERE qualified=1 AND sync_state='pending'") == 0


def test_failed_checkpoint_stops_checkpoints_and_final_sync_retries(tmp_path):
    from leadgen.sheets import SheetsError

    cfg = make_config()
    cfg["sheets"]["checkpoint_minutes"] = 0.0001
    db = DB(str(tmp_path / "state.sqlite"))
    sess = FakeSheetsSession()
    made = []

    def factory():
        made.append(1)
        if len(made) == 1:
            raise SheetsError("HTTP 503 (test)")
        return SheetsSync(SheetsClient("sheet", session=sess))
    code, s = make_runner(cfg, db, World(), sheets_factory=factory).run()
    assert code == 0 and s["sheets"]["status"] == "ok"
    assert len(made) == 2                                   # one failed checkpoint, then only the final sync
    leads = [row for row in sess.tabs["Leads"]["rows"][1:] if any(str(x) for x in row)]
    assert len(leads) == db.scalar("SELECT COUNT(*) FROM places WHERE qualified=1 AND excluded IS NULL")


def test_empty_maps_answers_are_kept_for_later_and_reported(tmp_path):
    """A silent format change (valid JSON, no businesses) must not mark squares as empty."""
    cfg = make_config()
    db = DB(str(tmp_path / "state.sqlite"))
    state = {"broken": True}

    def maps(method, url, params, data):
        if state["broken"] and "google.com" in (urlsplit(url).hostname or ""):
            return (200, gmaps_payload([]), "application/json")
        return World()(method, url, params, data)
    code, s = Runner(cfg, db, http=FakeHttp(maps), use_sheets=False).run()
    assert code == 2 and s["status"] == "degraded (Google Maps returned nothing)"
    assert any("format may have changed" in w for w in s["warnings"])
    assert s["stats"]["searches_attempted"] == 10                      # stopped early, not the whole part
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search' AND status IN ('done','failed')") == 0
    # Maps recovers: the kept searches run normally on a later run.
    state["broken"] = False
    db.conn.execute("UPDATE tasks SET next_at=0 WHERE kind='search'")
    code2, s2 = Runner(cfg, db, http=FakeHttp(maps), use_sheets=False).run()
    assert code2 == 0 and s2["new_leads_today"] >= 5


def test_records_without_ids_count_as_format_change(tmp_path):
    cfg = make_config()
    db = DB(str(tmp_path / "state.sqlite"))
    broken = biz("Some Cafe", *at(0.1, 0.1), "ChIJnoids0000001", phone_intl="+91 98300 99999", phone_local="098300 99999")
    broken[78] = None
    broken[10] = None

    def maps(method, url, params, data):
        if "google.com" in (urlsplit(url).hostname or ""):
            return (200, gmaps_payload([broken] * 20), "application/json")
        return World()(method, url, params, data)
    code, s = Runner(cfg, db, http=FakeHttp(maps), use_sheets=False, max_searches=3).run()
    assert code == 2 and s["status"].startswith("degraded")
    assert "format changed" in s["warnings"][-1]
    assert db.scalar("SELECT COUNT(*) FROM places") == 0
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search' AND status='done'") == 0
