"""Optional official enrichment: the Foursquare Places API.

Foursquare offers a free tier and an official API, so this is fully within terms (unlike scraping).
It is OFF unless FOURSQUARE_API_KEY is set. Given a lead's name and location it looks up the matching
place and returns the phone, website and social links Foursquare holds, to fill gaps the open data left.

Honest note: Overture Maps (our default source) already *includes* Foursquare's open places data, so in
practice this adds little in India - it is here for completeness and for places Foursquare has that
Overture does not. It never returns e-mail addresses.
"""
from __future__ import annotations

import os

from ..net import Blocked, FetchError, Http
from ..quality import name_score
from ..util import get_logger

log = get_logger("fsq")

SEARCH_URL = "https://places-api.foursquare.com/places/search"
API_VERSION = "2025-06-17"
FIELDS = "fsq_place_id,name,location,tel,website,social_media,distance"


class FoursquareAPI:
    name = "foursquare"

    def __init__(self, http: Http, key: str | None = None, version: str = API_VERSION):
        self.http = http
        self.key = (key if key is not None else os.environ.get("FOURSQUARE_API_KEY", "")).strip()
        self.version = version

    def available(self) -> bool:
        return bool(self.key)

    def enrich(self, name: str, lat: float, lng: float, radius_m: int = 250, threshold: float = 0.8) -> list[tuple]:
        """Return [(kind, value, confidence, source_url, evidence)] for the best name match near (lat,lng)."""
        if not self.key:
            return []
        params = {"query": name[:120], "ll": f"{lat:.6f},{lng:.6f}", "radius": str(radius_m), "limit": "5", "fields": FIELDS}
        headers = {"Authorization": f"Bearer {self.key}", "X-Places-Api-Version": self.version, "Accept": "application/json"}
        try:
            r = self.http.get(SEARCH_URL, params=params, headers=headers, service="fsq", browser=False,
                              timeout=20, retries=1, interval=1.0, block_statuses=(429,))
        except Blocked as exc:
            raise FetchError(f"Foursquare API blocked: {exc}") from exc
        if r.status == 401 or r.status == 403:
            self.http.breaker("fsq").trip(f"HTTP {r.status} (check FOURSQUARE_API_KEY)")
            raise FetchError(f"Foursquare API rejected the key (HTTP {r.status})")
        if r.status != 200:
            raise FetchError(f"Foursquare API HTTP {r.status}")
        best, best_score = None, 0.0
        for place in (r.json().get("results") or []):
            score = name_score(name, place.get("name", ""))
            if score > best_score:
                best, best_score = place, score
        if best is None or best_score < threshold:
            return []
        out: list[tuple] = []
        src = f"https://foursquare.com/v/{best.get('fsq_place_id', '')}"
        ev = f"Foursquare Places API (name match {best_score:.2f})"
        tel = (best.get("tel") or "").strip()
        if tel:
            out.append(("phone", tel, "high", src, ev))
        web = (best.get("website") or "").strip()
        if web:
            out.append(("website", web, "high", src, ev))
        sm = best.get("social_media") or {}
        if sm.get("facebook_id"):
            out.append(("facebook", f"https://www.facebook.com/{sm['facebook_id']}", "high", src, ev))
        if sm.get("instagram"):
            out.append(("instagram", f"https://www.instagram.com/{str(sm['instagram']).lstrip('@')}/", "high", src, ev))
        if sm.get("twitter"):
            out.append(("twitter", f"https://x.com/{str(sm['twitter']).lstrip('@')}", "medium", src, ev))
        return out
