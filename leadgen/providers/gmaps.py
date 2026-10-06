"""Google Maps search over plain HTTPS (no browser).

Uses the same lightweight `search?tbm=map` endpoint as the open-source
gosom/google-maps-scraper "fast mode" (MIT licence). Field positions inside a
result follow gosom's maintained parser (gmaps/multiple.go, gmaps/entry.go),
verified against its real-response fixtures:

    [11] name        [13] categories     [2]/[18] address    [7][0] website
    [178][0][0] phone (local format)     [178][0][1] list of formats incl. +intl
    [9][2],[9][3] lat/lng   [78] place id  [10] data id  [4][7] rating  [4][8] reviews
    [32][1][1] description  [34][4][4] hours/status text  [88][0] state enum

Google changes this format from time to time, so the parser validates every
record (name + coordinates must be present) and falls back to a structural scan
when the outer wrapper moves. A record that does not validate is dropped,
never "repaired" with guessed values.
"""
from __future__ import annotations

import json
import math
import re
from urllib.parse import parse_qs, quote, urlsplit

from ..net import Blocked, FetchError, Http
from ..util import get_logger
from .base import Place, ProviderUnavailable

log = get_logger("gmaps")

SEARCH_URLS = ("https://www.google.com/search", "https://maps.google.com/search")

_PB_REST = ("!10b1!12m22!1m3!18b1!30b1!34e1!2m3!5m1!6e2!20e3!4b0!10b1!12b1!13b1!16b1!17m1!3e1!20m3!5e2!6b1!14b1"
            "!46m1!1b0!96b1!19m4!2m3!1i360!2i120!4i8")


def build_pb(lat: float, lng: float, zoom: float, offset: int = 0, count: int = 20, variant: str = "gosom",
             width: int = 600, height: int = 800) -> str:
    if variant == "client":
        # Altitude-style viewport span (metres) the web client sends; FOV constant 13.1.
        dist = height * 156543.03392 / (2 ** zoom)
        head = f"!4m12!1m3!1d{dist:.6f}!2d{lng:.6f}!3d{lat:.6f}!2m3!1f0!2f0!3f0!3m2!1i{width}!2i{height}!4f13.1"
    else:  # exactly what gosom's fast mode sends
        head = f"!4m12!1m3!1d3826.902183192154!2d{lng:.4f}!3d{lat:.4f}!2m3!1f0!2f0!3f0!3m2!1i{width}!2i{height}!4f{zoom:.1f}"
    return f"{head}!7i{count}!8i{offset}{_PB_REST}"


def _g(arr, *idx):
    """Safe nested index access: returns None when any level is missing."""
    cur = arr
    for i in idx:
        if not isinstance(cur, list) or i >= len(cur) or i < -len(cur):
            return None
        cur = cur[i]
        if cur is None:
            return None
    return cur


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def strip_xssi(text: str) -> str:
    t = text.lstrip()
    if t.startswith(")]}'"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl >= 0 else ""
    return t


def decode_payload(text: str):
    """Return the JSON array payload of a tbm=map response, handling known wrappers."""
    t = strip_xssi(text).strip()
    if t.endswith('/*""*/'):
        t = t[: -len('/*""*/')].rstrip()
    if t.startswith("{"):
        obj = json.loads(t)
        inner = obj.get("d") if isinstance(obj, dict) else None
        if isinstance(inner, str):
            return json.loads(strip_xssi(inner))
        raise ValueError("unexpected JSON object payload")
    return json.loads(t)


def _looks_like_business(arr) -> bool:
    if not isinstance(arr, list) or len(arr) < 40:
        return False
    name = _g(arr, 11)
    lat, lng = _num(_g(arr, 9, 2)), _num(_g(arr, 9, 3))
    return isinstance(name, str) and bool(name.strip()) and lat is not None and lng is not None


def _scan_businesses(node, out: list, depth: int = 0):
    if depth > 8 or not isinstance(node, list):
        return
    if _looks_like_business(node):
        out.append(node)
        return
    for child in node:
        if isinstance(child, list):
            _scan_businesses(child, out, depth + 1)


def extract_business_arrays(payload) -> tuple[list, str]:
    """Locate result records. Returns (records, method)."""
    items = _g(payload, 0, 1)
    found = []
    if isinstance(items, list):
        for item in items[1:] if len(items) > 1 else []:
            biz = _g(item, 14)
            if _looks_like_business(biz):
                found.append(biz)
    if found:
        return found, "indexed"
    scanned: list = []
    _scan_businesses(payload, scanned)
    return scanned, "scan" if scanned else "none"


def real_url(url: str) -> str:
    if url.startswith("/url?"):
        q = parse_qs(urlsplit(url).query).get("q")
        if q:
            return q[0]
    return url


def parse_business(biz: list) -> Place | None:
    name = _s(_g(biz, 11))
    lat, lng = _num(_g(biz, 9, 2)), _num(_g(biz, 9, 3))
    if not name or lat is None or lng is None:
        return None
    place_id = _s(_g(biz, 78))
    data_id = _s(_g(biz, 10))
    if not (place_id or data_id):
        return None
    cats = [c for c in (_g(biz, 13) or []) if isinstance(c, str)]
    addr39 = _s(_g(biz, 39))            # full address without the name (2026 layout)
    addr18 = _s(_g(biz, 18))            # "Name, full address"
    if addr18.startswith(name + ","):
        addr18 = addr18[len(name) + 1:].strip()
    parts = [p for p in (_g(biz, 2) or []) if isinstance(p, str)]
    address = addr39 or addr18 or ", ".join(parts)
    area = _s(_g(biz, 14)) if isinstance(_g(biz, 14), str) else ""
    city = _s(_g(biz, 166)) if isinstance(_g(biz, 166), str) else ""
    phone_local = _s(_g(biz, 178, 0, 0))
    phone_intl = ""
    for fmt in _g(biz, 178, 0, 1) or []:
        val = _s(_g(fmt, 0))
        if val.startswith("+"):
            phone_intl = val
            break
    website = real_url(_s(_g(biz, 7, 0)))
    # Closure: Google shows "Permanently closed"/"Temporarily closed" in the hours/status blocks.
    status_blob = " ".join(json.dumps(_g(biz, i) or "", ensure_ascii=False) for i in (34, 88, 203))
    m_closed = re.search(r"(permanently closed|temporarily closed|closed permanently|closed temporarily)", status_blob, re.I)
    tag = _s(_g(biz, 88, 0))            # short tagline, e.g. "Brunch" / "Iconic coffeehouse chain"
    closed = bool(m_closed) or tag.upper() in ("CLOSED", "PERMANENTLY_CLOSED", "CLOSED_PERMANENTLY")
    status_text = m_closed.group(1) if m_closed else ""
    rating = _num(_g(biz, 4, 7))
    reviews = _num(_g(biz, 4, 8))
    tag_ok = tag and tag != name and not tag.isupper() and len(tag) > 2 and not re.match(r"^[$₹€£]\s?\d|^\d", tag)
    description = _s(_g(biz, 32, 1, 1)) or (tag if tag_ok else "")   # (the tag is sometimes a price: "$13")
    link = _s(_g(biz, 27))
    key = f"g:{place_id}" if place_id else f"gd:{data_id}"
    maps_url = link if link.startswith("https://") else ""
    if not maps_url and place_id:
        maps_url = f"https://www.google.com/maps/search/?api=1&query={quote(name)}&query_place_id={place_id}"
    return Place(key=key, provider="gmaps", name=name, lat=float(lat), lng=float(lng), address=address,
                 area=area, city=city, categories=cats, phone=phone_local, phone_intl=phone_intl, website=website,
                 rating=float(rating) if rating is not None else None,
                 reviews=int(reviews) if reviews is not None else None,
                 place_id=place_id, data_id=data_id, maps_url=maps_url, description=description,
                 closed=closed, status_text=status_text)


def parse_search_response(text: str) -> tuple[list[Place], dict]:
    meta = {"records": 0, "valid": 0, "method": "none"}
    payload = decode_payload(text)
    arrays, method = extract_business_arrays(payload)
    meta["method"], meta["records"] = method, len(arrays)
    places, seen = [], set()
    for biz in arrays:
        p = parse_business(biz)
        if p and p.key not in seen:
            seen.add(p.key)
            places.append(p)
    meta["valid"] = len(places)
    return places, meta


_BLOCK_MARKERS = ("/sorry/index", "unusual traffic", "detected unusual", "captcha", "g-recaptcha")


def zoom_for_span(span_km: float, lat: float, width_px: int = 600) -> int:
    span_km = max(span_km, 0.2)
    z = math.log2(156543.03392 * math.cos(math.radians(lat)) * width_px / 1000.0 / span_km)
    return int(max(11, min(17, math.floor(z))))


class GoogleMapsSearch:
    name = "gmaps"

    def __init__(self, http: Http, *, lang: str = "en", region: str = "in", variant: str = "gosom",
                 interval: float = 4.0, jitter: float = 3.0, url_index: int = 0):
        self.http, self.lang, self.region, self.variant = http, lang, region, variant
        self.interval, self.jitter = interval, jitter
        self.url = SEARCH_URLS[url_index % len(SEARCH_URLS)]

    def search_page(self, query: str, lat: float, lng: float, zoom: float, offset: int = 0) -> tuple[list[Place], dict]:
        params = {"tbm": "map", "authuser": "0", "hl": self.lang, "gl": self.region, "q": query,
                  "pb": build_pb(lat, lng, zoom, offset=offset, variant=self.variant)}
        try:
            r = self.http.get(self.url, params=params, service="gmaps", interval=self.interval, jitter=self.jitter,
                              timeout=25, max_bytes=8_000_000, retries=1,
                              headers={"Accept": "*/*", "Referer": "https://www.google.com/maps"},
                              cookies={"CONSENT": "YES+cb", "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"})
        except Blocked as exc:
            raise ProviderUnavailable(f"Google Maps blocked us: {exc}") from exc
        low = r.text[:3000].lower()
        if r.status != 200 or any(m in low for m in _BLOCK_MARKERS) or "/sorry/" in r.url:
            self.http.breaker("gmaps").failure(f"HTTP {r.status} / block page")
            raise ProviderUnavailable(f"Google Maps returned a block/consent page (HTTP {r.status})")
        try:
            places, meta = parse_search_response(r.text)
        except (ValueError, json.JSONDecodeError) as exc:
            self.http.breaker("gmaps").failure(f"unparseable response: {exc}")
            raise FetchError(f"unparseable Google Maps response: {exc}") from exc
        if meta["records"] and not meta["valid"]:
            # Result records are there but none has the expected fields: the format changed.
            # Fail loudly instead of recording "no businesses here".
            self.http.breaker("gmaps").failure("records without the expected fields")
            raise FetchError(f"Google Maps response format changed ({meta['records']} records, none parseable)")
        meta.update({"status": r.status, "bytes": len(r.content), "offset": offset})
        return places, meta

    def search(self, query: str, lat: float, lng: float, zoom: float, max_pages: int = 3, is_known=None) -> tuple[list[Place], dict]:
        """Fetch up to `max_pages` pages of 20. Stops when a page is short, adds nothing new, or
        (with `is_known`) consists mostly of businesses already in the database."""
        all_places: dict[str, Place] = {}
        pages = []
        for page in range(max_pages):
            places, meta = self.search_page(query, lat, lng, zoom, offset=page * 20)
            new = [p for p in places if p.key not in all_places]
            for p in new:
                all_places[p.key] = p
            unknown = [p for p in new if not (is_known and is_known(p.key))]
            meta["new"], meta["unknown"] = len(new), len(unknown)
            pages.append(meta)
            if len(places) < 18 or not new:
                break
            if is_known is not None and len(unknown) < 0.3 * len(places):
                break
        return list(all_places.values()), {"pages": pages, "count": len(all_places)}
