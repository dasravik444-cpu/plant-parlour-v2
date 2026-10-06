"""OpenStreetMap (Overpass API): planning data (business density, locality names) and a free
discovery fallback. Public Overpass servers are often slow or overloaded (verified: one of three
answered in the live probe), so every call rotates across mirrors and failures are tolerated."""
from __future__ import annotations

import json
import time

from ..geo import square_bbox
from ..net import FetchError, Http, NetworkDown
from ..util import get_logger
from .base import Place, ProviderUnavailable

log = get_logger("osm")

ENDPOINTS = (
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
UA = "plant-parlour-leadgen/2.0 (+https://github.com/dasravik444-cpu/plant-parlour-v2)"

DENSITY_FILTERS = [
    '["amenity"~"^(cafe|restaurant|fast_food|bar|pub|ice_cream|food_court|events_venue|conference_centre|community_centre|coworking_space)$"]',
    '["tourism"~"^(hotel|resort|guest_house|motel)$"]',
    '["shop"]',
    '["office"]',
]
PLACE_RANK = {"city": 6, "town": 5, "suburb": 4, "borough": 4, "quarter": 3, "neighbourhood": 2, "village": 1, "hamlet": 0.5}


class Overpass:
    def __init__(self, http: Http, timeout: int = 180):
        self.http = http
        self.timeout = timeout

    def query(self, ql: str, *, expect: str = "json", attempts_per_endpoint: int = 1):
        last = None
        for url in ENDPOINTS:
            if self.http.breaker("overpass:" + url).is_open():
                continue
            for _ in range(attempts_per_endpoint):
                try:
                    r = self.http.post(url, data={"data": ql}, service="overpass:" + url, browser=False, interval=2.0,
                                       timeout=self.timeout + 20, retries=0, max_bytes=60_000_000,
                                       headers={"User-Agent": UA, "Accept": "*/*"}, block_statuses=(429,))
                except NetworkDown:
                    raise
                except FetchError as exc:
                    last = exc
                    continue
                if r.status == 200 and not r.truncated:
                    if expect == "json":
                        try:
                            return r.json()
                        except ValueError as exc:
                            last = exc
                            continue
                    return r.text
                last = FetchError(f"HTTP {r.status} from {url}")
                if r.status in (429, 504):
                    time.sleep(5)
        raise ProviderUnavailable(f"all Overpass servers failed: {last}")

    # -- planning data ----------------------------------------------------------
    def business_points(self, lat: float, lng: float, radius_km: float, attempts_per_endpoint: int = 1) -> list[tuple[float, float]]:
        around = f"(around:{int(radius_km * 1000)},{lat:.5f},{lng:.5f})"
        parts = "".join(f"nwr{f}{around};" for f in DENSITY_FILTERS)
        ql = f"[out:csv(::lat,::lon;false)][timeout:{self.timeout}];({parts});out center qt;"
        text = self.query(ql, expect="text", attempts_per_endpoint=attempts_per_endpoint)
        pts = []
        for line in text.splitlines():
            bits = line.strip().split("\t")
            if len(bits) >= 2:
                try:
                    pts.append((float(bits[0]), float(bits[1])))
                except ValueError:
                    continue
        return pts

    def localities(self, lat: float, lng: float, radius_km: float) -> list[dict]:
        around = f"(around:{int(radius_km * 1000)},{lat:.5f},{lng:.5f})"
        ql = (f'[out:csv(::lat,::lon,name,"name:en",place;false)][timeout:{self.timeout}];'
              f'node["place"~"^(city|town|suburb|borough|quarter|neighbourhood|village)$"]{around};out qt;')
        text = self.query(ql, expect="text")
        out = []
        for line in text.splitlines():
            bits = line.split("\t")
            if len(bits) < 5:
                continue
            try:
                la, lo = float(bits[0]), float(bits[1])
            except ValueError:
                continue
            name = (bits[3] or bits[2]).strip()
            if not name or not any(c.isascii() and c.isalpha() for c in name):
                name = bits[2].strip()
            if name:
                out.append({"lat": la, "lng": lo, "name": name, "place": bits[4].strip(), "rank": PLACE_RANK.get(bits[4].strip(), 0)})
        return out

    # -- discovery fallback -------------------------------------------------------
    def search_cell(self, lat: float, lng: float, size_km: float, osm_filters: list[str]) -> list[Place]:
        if not osm_filters:
            return []
        s, w, n, e = square_bbox(lat, lng, size_km)
        bbox = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
        body = "".join(f"nwr{f}{bbox};" for f in osm_filters)
        data = self.query(f"[out:json][timeout:90];({body});out center tags qt;")
        places = []
        for el in data.get("elements", []):
            tags = el.get("tags") or {}
            name = tags.get("name:en") or tags.get("name")
            if not name:
                continue
            la = el.get("lat") if el.get("type") == "node" else (el.get("center") or {}).get("lat")
            lo = el.get("lon") if el.get("type") == "node" else (el.get("center") or {}).get("lon")
            if la is None or lo is None:
                continue
            addr = ", ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:suburb"),
                                         tags.get("addr:city"), tags.get("addr:postcode")) if x)
            cats = [v.replace("_", " ") for k, v in tags.items() if k in ("amenity", "tourism", "shop", "office", "craft", "cuisine")]
            extra = {k: v for k, v in tags.items() if k in ("email", "contact:email", "contact:instagram", "contact:facebook",
                                                             "contact:whatsapp", "contact:mobile", "mobile", "contact:phone")}
            places.append(Place(key=f"osm:{el['type']}/{el['id']}", provider="osm", name=name.strip(), lat=float(la), lng=float(lo),
                                address=addr, categories=cats, phone=tags.get("phone") or tags.get("contact:phone") or "",
                                website=tags.get("website") or tags.get("contact:website") or "",
                                maps_url=f"https://www.openstreetmap.org/{el['type']}/{el['id']}", extra=extra))
        return places


def json_dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
