"""Open-data discovery: Overture Maps places.

The Overture Maps Foundation publishes more than 60 million places (sources: Meta
business pages, Microsoft, Foursquare, AllThePlaces and others) as GeoParquet files
on a public S3 bucket, under CDLA-Permissive-2.0 / Apache-2.0 licences. Unlike Google
Maps content, this data may be saved in our own lists and used commercially. When
the data is shared with others (e.g. a client), keep the notice
"Contains data from the Overture Maps Foundation (CDLA-Permissive-2.0)".

Once a month the campaign area is extracted into the local state database (only the
categories the campaign targets); every search square is then answered locally,
without any request to Google.

Verified 2026-10-06 for a 100 km circle around Kolkata: 209k places, 73% with a phone,
40% with an email, 40% with a website, 88% with a social link.
"""
from __future__ import annotations

import fnmatch
import re
import time
from urllib.parse import quote

from ..geo import square_bbox
from ..util import get_logger, jdump, jload
from .base import Place, ProviderUnavailable

log = get_logger("overture")

BUCKET = "overturemaps-us-west-2"
SOURCE_URL = "https://overturemaps.org"
# Banquet halls and event planners share Overture's "party_and_event_planning" code; the name tells them apart.
VENUE_WORDS = re.compile(r"\b(banquet|banquets|hall|bhavan|bhawan|bhaban|lawn|lawns|venue|palace|marriage|convention|"
                         r"resort|farm ?house|club ?house|community)\b", re.I)
EVENT_CODES = {"party_and_event_planning", "wedding_planning"}
# Facebook pages of all kinds of businesses use "party_and_event_planning" (a petrochemical trader, a travel
# agent...): for that code the name, e-mail or website must show the business is really about events.
NOISY_CODES = {"party_and_event_planning"}
EVENT_WORDS = re.compile(r"\b(event|wedding|shaadi|shadi|biye|marriage|banquet|hall|bhavan|bhawan|bhaban|lawn|party|parties|"
                         r"decor|planner|management|cater|tent|shamiana|pandal|light|sound|dj\b|flower|floral|"
                         r"celebrat|function|ceremon|venue|mandap|entertain|production|occasion|utsav|convention|resort|"
                         r"palace|farm ?house|club|community|anushthan|birthday)", re.I)
# In e-mail addresses and web addresses words run together ("royalbanquethall@..."): longer words only.
EVENT_WORDS_JOINED = re.compile(r"(event|wedding|banquet|decorat|cater|planner|shamiana|celebrat|production|entertain|"
                                r"marriage|ceremon|convention|birthday)", re.I)


def latest_release(timeout: float = 30.0) -> str:
    import requests

    r = requests.get(f"https://{BUCKET}.s3.amazonaws.com/", params={"list-type": "2", "prefix": "release/", "delimiter": "/"},
                     timeout=timeout)
    r.raise_for_status()
    rels = re.findall(r"<Prefix>release/(\d{4}-\d{2}-\d{2})\.(\d+)/</Prefix>", r.text)
    if not rels:
        raise ProviderUnavailable("no Overture Maps release found")
    d, n = max(rels, key=lambda x: (x[0], int(x[1])))
    return f"{d}.{n}"


def _sql_list(values) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def fetch_area(release: str, bbox: tuple, codes: list[str], min_confidence: float, timeout_s: float = 600,
               source: str | None = None) -> list[dict]:
    """Read the places of one bounding box from the public Overture bucket (needs the `duckdb` package).
    `source` overrides the Parquet location (tests)."""
    try:
        import duckdb
    except ImportError as exc:
        raise ProviderUnavailable("the duckdb package is not installed (pip install duckdb)") from exc
    xmin, ymin, xmax, ymax = bbox
    exact = [c for c in codes if "*" not in c]
    likes = [c.replace("_", "\\_").replace("*", "%") for c in codes if "*" in c]
    code_filter = " OR ".join([f"taxonomy.primary IN ({_sql_list(exact)})", f"basic_category IN ({_sql_list(exact)})"] +
                              [f"taxonomy.primary LIKE '{p}' ESCAPE '\\'" for p in likes]) if (exact or likes) else "true"
    con = duckdb.connect()
    try:
        if source is None or source.startswith("s3://"):
            con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
            con.execute(f"SET http_timeout={int(timeout_s * 1000)}")
        sql = f"""
            SELECT id, names.primary AS name, bbox.ymin AS lat, bbox.xmin AS lng,
                   taxonomy.primary AS code, basic_category AS basic, taxonomy.alternates AS alternates,
                   phones, emails, websites, socials,
                   addresses[1].freeform AS street, addresses[1].locality AS locality, addresses[1].postcode AS postcode,
                   confidence, brand.names.primary AS brand, brand.wikidata AS brand_wikidata, operating_status AS status,
                   list_distinct([s.dataset FOR s IN sources]) AS datasets
            FROM read_parquet('{source or f"s3://{BUCKET}/release/{release}/theme=places/type=place/*"}', hive_partitioning=1)
            WHERE bbox.xmin BETWEEN {xmin} AND {xmax} AND bbox.ymin BETWEEN {ymin} AND {ymax}
              AND confidence >= {float(min_confidence)} AND names.primary IS NOT NULL AND ({code_filter})"""
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


class OvertureStore:
    """Local copy of the campaign area's relevant Overture places, refreshed monthly."""

    name = "overture"

    def __init__(self, db, cfg, *, fetcher=None, release_fn=None, now_fn=time.time):
        self.db, self.cfg = db, cfg
        self.fetcher = fetcher or fetch_area
        self.release_fn = release_fn or latest_release
        self.now = now_fn
        od = cfg["open_data"]
        self.min_conf = float(od.get("min_confidence", 0.4))
        self.refresh_s = float(od.get("refresh_days", 30)) * 86400
        self.exclude = set(od.get("exclude_codes") or [])
        self._ready = False
        self._error = ""

    # -- categories ---------------------------------------------------------------
    def codes(self) -> list[str]:
        out: list[str] = []
        for cat in self.cfg.categories:
            for c in cat.get("overture", []):
                if c not in out:
                    out.append(c)
        return out

    def category_for(self, code: str, basic: str, name: str, hints: str = "") -> str | None:
        """Our category for an Overture code (exact code first, then patterns, then the broad basic category).
        `hints`: the place's e-mails and websites, used as evidence for noisy codes."""
        if code in self.exclude:
            return None
        if code in NOISY_CODES and not (EVENT_WORDS.search(name or "") or EVENT_WORDS_JOINED.search(hints or "")):
            return None
        cats = self.cfg.categories
        if code in EVENT_CODES:
            keys = {c["key"] for c in cats}
            if "banquet_venue" in keys and VENUE_WORDS.search(name or ""):
                return "banquet_venue"
            if "event_planner" in keys:
                return "event_planner"
        for cat in cats:
            if code and code in cat.get("overture", []):
                return cat["key"]
        for cat in cats:
            if code and any("*" in p and fnmatch.fnmatchcase(code, p) for p in cat.get("overture", [])):
                return cat["key"]
        for cat in cats:
            if basic and basic in cat.get("overture", []):
                return cat["key"]
        return None

    # -- extract ------------------------------------------------------------------
    def _bbox(self) -> tuple:
        import math

        lat, lng = (float(v) for v in self.cfg["area"]["center"])
        r = float(self.cfg["area"]["radius_km"])
        dlat, dlng = r / 111.0, r / (111.32 * math.cos(math.radians(lat)))
        return (lng - dlng, lat - dlat, lng + dlng, lat + dlat)

    def ensure(self) -> None:
        """Make sure a usable local extract exists (download/refresh at most monthly)."""
        if self._ready:
            return
        have = int(self.db.scalar("SELECT COUNT(*) FROM open_places", default=0))
        sig = jdump({"codes": self.codes(), "bbox": [round(v, 4) for v in self._bbox()], "min_conf": self.min_conf})
        fresh = self.now() - float(self.db.get_meta("overture_checked_at", "0") or 0) < self.refresh_s
        if have and fresh and self.db.get_meta("overture_signature") == sig:
            self._ready = True
            return
        try:
            release = self.release_fn()
            if have and release == self.db.get_meta("overture_release") and self.db.get_meta("overture_signature") == sig:
                self.db.set_meta("overture_checked_at", str(self.now()))
                self._ready = True
                return
            t0 = self.now()
            log.info("open data: downloading Overture Maps places for the campaign area (release %s)...", release)
            rows = self.fetcher(release, self._bbox(), self.codes(), self.min_conf)
            if not rows:
                raise ProviderUnavailable(f"Overture release {release} returned no places for the campaign area")
            self._store(release, rows)
            self.db.set_meta("overture_release", release)
            self.db.set_meta("overture_signature", sig)
            self.db.set_meta("overture_checked_at", str(self.now()))
            log.info("open data: %d relevant places stored (%.0f s)", len(rows), self.now() - t0)
            self._ready = True
        except ProviderUnavailable:
            if have:
                log.warning("open data: refresh failed - using the stored extract (%d places)", have)
                self._ready = True
                return
            raise
        except Exception as exc:  # noqa: BLE001 - network/schema problems must not crash the run
            if have:
                log.warning("open data: refresh failed (%s) - using the stored extract (%d places)", exc, have)
                self._ready = True
                return
            raise ProviderUnavailable(f"Overture Maps download failed: {type(exc).__name__}: {exc}"[:300]) from exc

    def _store(self, release: str, rows: list[dict]) -> None:
        with self.db.tx():
            self.db.conn.execute("DELETE FROM open_places")
            self.db.conn.executemany(
                "INSERT OR REPLACE INTO open_places(id,release,name,lat,lng,code,basic,alternates,phones,emails,websites,socials,"
                "street,locality,postcode,confidence,brand,status,datasets) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["id"], release, (r.get("name") or "").strip(), float(r["lat"]), float(r["lng"]), r.get("code"), r.get("basic"),
                  jdump(list(r.get("alternates") or [])), jdump(list(r.get("phones") or [])), jdump(list(r.get("emails") or [])),
                  jdump(list(r.get("websites") or [])), jdump(list(r.get("socials") or [])), r.get("street"), r.get("locality"),
                  r.get("postcode"), None if r.get("confidence") is None else float(r["confidence"]),
                  "|".join(x for x in (r.get("brand") or "", r.get("brand_wikidata") or "") if x) or None, r.get("status"),
                  jdump(list(r.get("datasets") or [])))
                 for r in rows if r.get("id") and r.get("lat") is not None and r.get("lng") is not None and (r.get("name") or "").strip()])

    # -- search -------------------------------------------------------------------
    def search_cell(self, lat: float, lng: float, size_km: float, category: str) -> list[Place]:
        self.ensure()
        # Squares are computed one by one and can leave hairline gaps between neighbours: overlap them
        # slightly (a place found twice is merged by its id).
        s, w, n, e = square_bbox(lat, lng, size_km + max(0.04, size_km * 0.02))
        out = []
        for r in self.db.q("SELECT * FROM open_places WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ? ORDER BY confidence DESC",
                           (s, n, w, e)):
            hints = " ".join((jload(r["emails"], []) or []) + (jload(r["websites"], []) or []))
            cat = self.category_for(r["code"] or "", r["basic"] or "", r["name"], hints)
            if cat != category:
                continue
            out.append(self.to_place(r, cat))
        return out

    def to_place(self, r, category: str) -> Place:
        websites = [u for u in (jload(r["websites"], []) or []) if u]
        socials = [u for u in (jload(r["socials"], []) or []) if u]
        phones = [p for p in (jload(r["phones"], []) or []) if p]
        address = ", ".join(x for x in (r["street"], r["locality"], r["postcode"]) if x)
        labels = [x.replace("_", " ") for x in [r["code"], *(jload(r["alternates"], []) or [])[:3]] if x]
        query = ", ".join(x for x in (r["name"], address or r["locality"]) if x)
        return Place(
            key=f"ov:{r['id']}", provider="overture", name=r["name"], lat=r["lat"], lng=r["lng"], address=address,
            area=r["locality"] or "", categories=labels, phone=phones[0] if phones else "",
            website=websites[0] if websites else "", closed=(r["status"] or "") == "permanently_closed",
            status_text=r["status"] or "",
            # A Google Maps *search link* (Maps URLs) - no Google data is fetched or stored.
            maps_url=f"https://www.google.com/maps/search/?api=1&query={quote(query)}",
            extra={"category": category, "phones": phones, "emails": jload(r["emails"], []) or [], "websites": websites,
                   "socials": socials, "datasets": jload(r["datasets"], []) or [], "confidence": r["confidence"],
                   "brand": (r["brand"] or "").split("|")[0], "brand_known": "|Q" in (r["brand"] or "") or (r["brand"] or "").startswith("Q"),
                   "record": r["id"]})
