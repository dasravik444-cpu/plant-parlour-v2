import time

import pytest

from conftest import make_config
from leadgen.db import DB
from leadgen.geo import build_cells, haversine_km, hilbert_sort, split_balanced
from leadgen.planner import PlanMismatch, Planner


def test_queue_semantics(tmp_path):
    db = DB(str(tmp_path / "s.sqlite"))
    with db.tx():
        assert db.enqueue("site", "site:a", {"url": "x"})
        assert not db.enqueue("site", "site:a", {"url": "y"})  # idempotent key
    t = db.ready_tasks("site", 5)[0]
    with db.tx():
        assert db.fail(t["id"], "boom", max_attempts=2, backoff=(60,)) == "pending"
    assert db.ready_tasks("site", 5) == []                    # backing off
    with db.tx():
        assert db.fail(t["id"], "boom again", max_attempts=2) == "failed"
    with db.tx():
        db.enqueue("site", "site:b", {})
    tb = db.ready_tasks("site", 5)[0]
    with db.tx():
        db.set_running(tb["id"])
    assert db.reset_stale_running() == 1                      # crash recovery
    with db.tx():
        db.defer(tb["id"], 3600, "provider down")
    assert db.one("SELECT attempts FROM tasks WHERE id=?", (tb["id"],))["attempts"] == 0


def test_contacts_corroborate_not_duplicate(tmp_path):
    db = DB(str(tmp_path / "s.sqlite"))
    with db.tx():
        db.insert_place({"key": "g:1", "name": "A", "provider": "gmaps", "found_date": "2026-10-06"})
        assert db.add_contact("g:1", "phone", "+919830012345", source="google_maps", confidence="high")
        assert not db.add_contact("g:1", "phone", "+919830012345", source="website", source_url="https://a.in", confidence="medium")
    rows = db.contacts_for("g:1")
    assert len(rows) == 1 and rows[0]["confidence"] == "high" and "website|https://a.in" in rows[0]["sources"]


def test_budget(tmp_path):
    db = DB(str(tmp_path / "s.sqlite"))
    assert db.budget_take("x", "2026-10", 2) and db.budget_take("x", "2026-10", 2)
    assert not db.budget_take("x", "2026-10", 2)


def test_quadtree_and_split():
    pts = [(22.58 + i * 1e-4, 88.42) for i in range(500)]
    cells = build_cells(22.58, 88.42, 10, pts, 1.0, 4.0, 60)
    assert min(c.size_km for c in cells) == 1.0 and max(c.size_km for c in cells) <= 4.0
    assert sum(c.count for c in cells) == 500
    assert all(haversine_km(22.58, 88.42, c.lat, c.lng) <= 10 + c.size_km for c in cells)
    groups = split_balanced(hilbert_sort(cells), [1.0] * len(cells), 7)
    assert len(groups) == 7 and sum(len(g) for g in groups) == len(cells)


def test_planner_offline_schedule_and_mismatch(tmp_path):
    cfg = make_config(campaign={"start_date": "2026-10-06"})
    db = DB(str(tmp_path / "s.sqlite"))
    pl = Planner(cfg, db, http=None)
    s = pl.ensure_plan()
    assert s["parts"] == 3 and s["search_tasks"] > 0
    assert pl.scheduled_part("2026-10-06")["id"] == 1
    assert pl.scheduled_part("2026-10-08")["id"] == 3
    assert pl.scheduled_part("2026-10-09") is None
    assert pl.ensure_plan()["search_tasks"] == s["search_tasks"]          # idempotent
    cfg2 = make_config(campaign={"start_date": "2026-10-06"}, area={"radius_km": 4})
    with pytest.raises(PlanMismatch):
        Planner(cfg2, db, None).ensure_plan()
    s3 = Planner(cfg2, db, None).ensure_plan(force=True)
    assert s3["plan_version"] == 2
    # secondary queries reference their primary
    t = db.one("SELECT payload FROM tasks WHERE kind='search' AND payload LIKE '%coffee shop%'")
    assert '"primary":false' in t["payload"]


def test_overpass_race_uses_fastest_mirror_and_bounds_time():
    from helpers import FakeHttp
    from leadgen.providers.base import ProviderUnavailable
    from leadgen.providers.osm import Overpass

    def router(method, url, params, data):
        if "private.coffee" in url:
            time.sleep(1.5)
            return (200, "22.5\t88.3\n", "text/csv")
        if "overpass-api.de" in url:
            return (504, "", "text/html")
        return (200, "22.6\t88.4\n22.7\t88.5\n", "text/csv")
    t = time.time()
    pts = Overpass(FakeHttp(router), timeout=10).business_points(22.58, 88.42, 10, race_s=5)
    assert pts == [(22.6, 88.4), (22.7, 88.5)] and time.time() - t < 1.2

    def all_bad(method, url, params, data):
        time.sleep(0.2)
        return (504, "", "text/html")
    with pytest.raises(ProviderUnavailable):
        Overpass(FakeHttp(all_bad), timeout=10).localities(22.58, 88.42, 10, race_s=3)

    def slow(method, url, params, data):
        time.sleep(3)
        return (200, "", "text/csv")
    t = time.time()
    with pytest.raises(ProviderUnavailable):
        Overpass(FakeHttp(slow), timeout=10).business_points(22.58, 88.42, 10, race_s=1)
    assert time.time() - t < 2
