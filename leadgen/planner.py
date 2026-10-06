"""Planner agent: divide the target circle into N daily parts and create the search tasks.

1. Business density from OpenStreetMap (shops, offices, cafes, hotels...) shapes a
   quadtree of search squares: small squares where businesses are dense, large
   ones in the countryside.  If OpenStreetMap is unreachable, a centre-weighted
   synthetic density is used instead (so a plan is ALWAYS produced).
2. Squares are ordered along a Hilbert curve (neighbours stay together) and cut
   into N consecutive groups of roughly equal expected yield -> N parts.
3. Parts are ordered (city centre outwards by default) and named after the main
   localities inside them.
4. Every square x every search query becomes one durable `search` task.

Which part is worked on which day is derived from the calendar (day 1 = start
date), never from how many times the program ran.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date

from .config import Config
from .db import DB
from .geo import Cell, build_cells, direction_name, haversine_km, hilbert_sort, split_balanced
from .net import Http, NetworkDown
from .providers.base import ProviderUnavailable
from .providers.gmaps import zoom_for_span
from .providers.osm import Overpass
from .util import get_logger, jdump, jload, local_date

log = get_logger("planner")


class PlanMismatch(RuntimeError):
    pass


def synthetic_points(lat0: float, lng0: float, radius_km: float) -> list[tuple[float, float]]:
    """Centre-weighted pseudo density used only when OpenStreetMap cannot be reached."""
    from .geo import offset

    pts = []
    step = 1.0
    n = int(radius_km / step)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            d = math.hypot(i * step, j * step)
            if d > radius_km:
                continue
            k = int(round(40 * math.exp(-d / 8.0) + (1 if (i + j) % 7 == 0 else 0)))
            if k:
                lat, lng = offset(lat0, lng0, j * step, i * step)
                pts.extend([(lat, lng)] * k)
    return pts


class Planner:
    def __init__(self, cfg: Config, db: DB, http: Http | None = None):
        self.cfg, self.db, self.http = cfg, db, http

    # -- public -----------------------------------------------------------------
    def has_plan(self) -> bool:
        return bool(self.db.scalar("SELECT COUNT(*) FROM parts", default=0))

    def ensure_plan(self, force: bool = False) -> dict:
        fp = jdump(self.cfg.plan_fingerprint())
        stored = self.db.get_meta("plan_fingerprint")
        if self.has_plan() and not force:
            if stored != fp:
                old = jload(stored, {}) or {}
                new = self.cfg.plan_fingerprint()
                same_geo = {k: v for k, v in old.items() if k != "queries"} == {k: v for k, v in jload(fp, {}).items() if k != "queries"}
                if not (old and same_geo):
                    raise PlanMismatch("The area or plan settings in the config changed since the plan was made. "
                                       "Run `python -m leadgen plan --force` to re-plan (found leads are kept).")
                self._update_categories(old.get("queries") or [], new["queries"])
                self.db.set_meta("plan_fingerprint", fp)
            synthetic = (self.db.get_meta("plan_density_source") or "").startswith("synthetic")
            started = self.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search' AND status!='pending'", default=0)
            if synthetic and not started and self.http is not None:
                log.info("planner: plan was built without OpenStreetMap data and no search has run yet - retrying OpenStreetMap")
                before = self.summary()
                s = self.build(fp, require_osm=True)
                return s if s else before
            return self.summary()
        return self.build(fp)

    def _update_categories(self, old_q: list, new_q: list) -> None:
        """Categories or their search words changed: add the new searches to every square and drop the
        pending ones that are no longer wanted. The parts, dates and leads stay as they are."""
        old_pairs = {(c, q) for c, qs in old_q for q in qs}
        new_pairs = [(c, q) for c, qs in new_q for q in qs]
        first = {c: qs[0] for c, qs in new_q if qs}
        cats = {c["key"]: c for c in self.cfg.categories}
        version = int(self.db.get_meta("plan_version", "1") or 1)
        osm_density = (self.db.get_meta("plan_density_source") or "") == "openstreetmap"
        added = removed = 0
        with self.db.tx():
            for c, q in old_pairs - set(new_pairs):
                cur = self.db.conn.execute(
                    "DELETE FROM tasks WHERE kind='search' AND status='pending' AND json_extract(payload,'$.category')=? "
                    "AND json_extract(payload,'$.query')=?", (c, q))
                removed += cur.rowcount
            todo = [(c, q) for c, q in new_pairs if (c, q) not in old_pairs]
            if todo:
                for cell in self.db.q("SELECT * FROM cells ORDER BY part_id, seq"):
                    for i, (c, q) in enumerate(todo):
                        if cell["osm_count"] < int(cats.get(c, {}).get("min_density", 0)) and osm_density:
                            continue
                        if self.db.enqueue("search", f"search:v{version}:{cell['id']}:{c}:{q}",
                                           {"cell_id": cell["id"], "lat": cell["lat"], "lng": cell["lng"], "size_km": cell["size_km"],
                                            "zoom": cell["zoom"], "category": c, "query": q, "primary": q == first.get(c),
                                            "primary_key": f"search:v{version}:{cell['id']}:{c}:{first.get(c, q)}"},
                                           seq=cell["seq"] * 100 + 60 + i, part_id=cell["part_id"]):
                            added += 1
        log.info("planner: categories changed - %d searches added, %d pending searches removed (no re-plan needed)", added, removed)

    def start_date(self) -> str:
        sd = str(self.cfg["campaign"].get("start_date") or "").strip()
        if sd:
            return sd
        stored = self.db.get_meta("start_date")
        if stored:
            return stored
        today = local_date(self.cfg.tz)
        self.db.set_meta("start_date", today)
        return today

    def day_number(self, today: str) -> int:
        return (date.fromisoformat(today) - date.fromisoformat(self.start_date())).days + 1

    def scheduled_part(self, today: str):
        """The part the calendar assigns to `today` (None before start / after the plan period)."""
        n = self.day_number(today)
        if n < 1:
            return None
        return self.db.one("SELECT * FROM parts WHERE scheduled_day=?", (n,))

    def summary(self) -> dict:
        parts = self.db.q("SELECT id, name, status, weight, cells, scheduled_day FROM parts ORDER BY id")
        return {"parts": len(parts), "cells": self.db.scalar("SELECT COUNT(*) FROM cells", default=0),
                "search_tasks": self.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search'", default=0),
                "density_source": self.db.get_meta("plan_density_source", "?"),
                "plan_version": int(self.db.get_meta("plan_version", "0") or 0),
                "first_parts": [dict(p) for p in parts[:5]]}

    # -- build --------------------------------------------------------------------
    def build(self, fingerprint: str, require_osm: bool = False) -> dict | None:
        cfg = self.cfg
        lat0, lng0 = float(cfg["area"]["center"][0]), float(cfg["area"]["center"][1])
        radius = float(cfg["area"]["radius_km"])
        p = cfg["plan"]
        points, source = [], "openstreetmap"
        localities: list[dict] = []
        if self.http is not None:
            from concurrent.futures import ThreadPoolExecutor

            osm = Overpass(self.http, timeout=150)
            log.info("planner: fetching business density and locality names from OpenStreetMap (radius %.0f km, max ~3 min)...", radius)
            # Both queries in parallel, each racing all mirrors: planning time is bounded (~3 min).
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_pts = ex.submit(osm.business_points, lat0, lng0, radius, 185)
                f_loc = ex.submit(osm.localities, lat0, lng0, radius, 185)
                try:
                    points = f_pts.result()
                    log.info("planner: %d mapped businesses", len(points))
                except NetworkDown:
                    raise
                except Exception as exc:  # noqa: BLE001 - planning must not fail on OSM
                    log.warning("planner: OpenStreetMap density unavailable (%s); using centre-weighted estimate", exc)
                    points = []
                try:
                    localities = f_loc.result()
                    log.info("planner: %d locality names", len(localities))
                except NetworkDown:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("planner: locality names unavailable (%s); parts will be named by direction", exc)
        if len(points) < 50:
            if require_osm:
                log.info("planner: OpenStreetMap still unavailable - keeping the current plan")
                return None
            points, source = synthetic_points(lat0, lng0, radius), "synthetic (OpenStreetMap unavailable)"
        cells = build_cells(lat0, lng0, radius, points, float(p["min_cell_km"]), float(p["max_cell_km"]), int(p["split_threshold"]))
        if not cells:
            raise RuntimeError("planner produced no search squares")
        weights = [c.count + 1.0 + (c.size_km ** 2) * 0.02 for c in cells]
        ordered = hilbert_sort(cells)
        w_by_id = {id(c): w for c, w in zip(cells, weights)}
        ow = [w_by_id[id(c)] for c in ordered]
        if p["days"] == "auto":
            est_total = sum(c.count * 0.8 + 2 for c in cells)
            n_parts = max(1, min(365, math.ceil(est_total / max(1, p["daily_target"]))))
        else:
            n_parts = int(p["days"])
        groups = split_balanced(ordered, ow, n_parts)

        def centroid(g):
            tw = sum(w_by_id[id(c)] for c in g)
            return (sum(c.lat * w_by_id[id(c)] for c in g) / tw, sum(c.lng * w_by_id[id(c)] for c in g) / tw)

        info = []
        for g in groups:
            clat, clng = centroid(g)
            tw = sum(w_by_id[id(c)] for c in g)
            area = sum(c.size_km ** 2 for c in g)
            info.append({"cells": g, "lat": clat, "lng": clng, "weight": tw, "density": tw / max(area, 0.01),
                         "dist": haversine_km(lat0, lng0, clat, clng)})
        if p["order"] == "center_out":
            info.sort(key=lambda x: x["dist"])
        elif p["order"] == "dense_first":
            info.sort(key=lambda x: -x["density"])

        # Locality names per part
        for loc in localities:
            for part in info:
                hit = next((c for c in part["cells"] if c.contains(loc["lat"], loc["lng"])), None)
                if hit is not None:
                    hit.names.append(loc)
                    break
        version = int(self.db.get_meta("plan_version", "0") or 0) + 1
        with self.db.tx():
            # Re-plan: drop the old geography and any search not yet done; found leads stay.
            self.db.conn.execute("DELETE FROM tasks WHERE kind='search' AND status IN ('pending','running')")
            self.db.conn.execute("UPDATE places SET part_id=NULL, cell_id=NULL WHERE part_id IS NOT NULL")
            self.db.conn.execute("DELETE FROM cells")
            self.db.conn.execute("DELETE FROM parts")
            seq = 0
            cats = self.cfg.categories
            for idx, part in enumerate(info, start=1):
                names = self._part_names(part["cells"])
                label = " / ".join(names[:3]) if names else direction_name(lat0, lng0, part["lat"], part["lng"])
                self.db.conn.execute(
                    "INSERT INTO parts(id,name,localities,center_lat,center_lng,weight,cells,status,scheduled_day) VALUES(?,?,?,?,?,?,?,?,?)",
                    (idx, label[:120], jdump(names[:10]), part["lat"], part["lng"], round(part["weight"], 1), len(part["cells"]), "pending", idx))
                for c in part["cells"]:
                    seq += 1
                    zoom = zoom_for_span(c.size_km, c.lat)
                    cname = c.names[0]["name"] if c.names else ""
                    cur = self.db.conn.execute(
                        "INSERT INTO cells(part_id,seq,lat,lng,size_km,zoom,osm_count,name) VALUES(?,?,?,?,?,?,?,?)",
                        (idx, seq, c.lat, c.lng, c.size_km, zoom, c.count, cname))
                    cell_id = cur.lastrowid
                    qi = 0
                    for cat in cats:
                        if c.count < int(cat.get("min_density", 0)) and source == "openstreetmap":
                            continue
                        primary_key = f"search:v{version}:{cell_id}:{cat['key']}:{cat['queries'][0]}"
                        for k, query in enumerate(cat["queries"]):
                            # Secondary queries ("rooftop restaurant") only run where the primary query
                            # ("restaurant") filled a whole page - see runner.run_search().
                            self.db.enqueue("search", f"search:v{version}:{cell_id}:{cat['key']}:{query}",
                                            {"cell_id": cell_id, "lat": c.lat, "lng": c.lng, "size_km": c.size_km, "zoom": zoom,
                                             "category": cat["key"], "query": query, "primary": k == 0,
                                             "primary_key": primary_key},
                                            seq=seq * 100 + qi, part_id=idx)
                            qi += 1
            self.db.set_meta("plan_fingerprint", fingerprint)
            self.db.set_meta("plan_version", str(version))
            self.db.set_meta("plan_density_source", source)
            self.db.set_meta("plan_built_at", local_date(self.cfg.tz))
            self.start_date()
        s = self.summary()
        log.info("planner: %d parts, %d squares, %d searches (density: %s)", s["parts"], s["cells"], s["search_tasks"], source)
        return s

    @staticmethod
    def _part_names(cells: list[Cell]) -> list[str]:
        scored: Counter = Counter()
        for c in cells:
            for loc in c.names:
                scored[loc["name"]] += loc["rank"] * 2 + math.log1p(c.count)
        return [n for n, _ in scored.most_common(10)]

    # -- progress -----------------------------------------------------------------
    def refresh_part_status(self, today: str) -> None:
        rows = self.db.q("SELECT part_id, SUM(CASE WHEN status IN ('pending','running') THEN 1 ELSE 0 END) open_n, "
                         "SUM(CASE WHEN status IN ('done','failed','skipped') THEN 1 ELSE 0 END) closed_n "
                         "FROM tasks WHERE kind='search' GROUP BY part_id")
        with self.db.tx():
            # Parts named only by direction ("North-East 15 km") get real neighbourhood names once
            # Google Maps results tell us where they are.
            for part in self.db.q("SELECT id, name FROM parts"):
                if part["name"].split(" ")[0] in ("North", "South", "East", "West", "Centre", "North-East", "North-West",
                                                  "South-East", "South-West") and "(" not in part["name"]:
                    seen = localities_seen(self.db, part["id"], 3)
                    if len(seen) >= 2:
                        self.db.conn.execute("UPDATE parts SET name=? WHERE id=?",
                                             (f"{' / '.join(seen)} ({part['name']})"[:120], part["id"]))
            for r in rows:
                part = self.db.one("SELECT status, started_on FROM parts WHERE id=?", (r["part_id"],))
                if part is None:
                    continue
                if r["open_n"] == 0 and r["closed_n"] > 0:
                    if part["status"] != "done":
                        self.db.conn.execute("UPDATE parts SET status='done', finished_on=?, started_on=COALESCE(started_on, ?) WHERE id=?",
                                             (today, today, r["part_id"]))
                elif r["closed_n"] > 0 and part["status"] == "pending":
                    self.db.conn.execute("UPDATE parts SET status='active', started_on=? WHERE id=?", (today, r["part_id"]))

    def progress(self) -> dict:
        total = self.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search'", default=0)
        closed = self.db.scalar("SELECT COUNT(*) FROM tasks WHERE kind='search' AND status IN ('done','failed','skipped')", default=0)
        parts_done = self.db.scalar("SELECT COUNT(*) FROM parts WHERE status='done'", default=0)
        parts_total = self.db.scalar("SELECT COUNT(*) FROM parts", default=0)
        return {"searches_total": total, "searches_done": closed, "percent": round(100.0 * closed / total, 1) if total else 0.0,
                "parts_done": parts_done, "parts_total": parts_total}


def localities_seen(db: DB, part_id: int, limit: int = 5) -> list[str]:
    """Most common neighbourhoods (Google Maps area labels) among leads found in a part."""
    areas: Counter = Counter()
    for r in db.q("SELECT area FROM places WHERE part_id=? AND excluded IS NULL AND area IS NOT NULL AND area != ''", (part_id,)):
        last = r["area"].split(",")[-1].strip()
        if last:
            areas[last] += 1
    return [a for a, _ in areas.most_common(limit)]


__all__ = ["Planner", "PlanMismatch", "localities_seen", "jload"]
