"""Official Google Places API (New) Text Search - optional fallback when GOOGLE_PLACES_API_KEY is set.

Field mask is limited to Text Search *Enterprise* fields (phone/website need
Enterprise). Google's free usage cap for that SKU is 1,000 requests/month
(each request returns up to 20 places). The provider enforces its own daily and
monthly caps in the state database so it stays inside the free cap.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ..geo import square_bbox
from ..net import Blocked, FetchError, Http
from .base import Place, ProviderUnavailable

URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.location", "places.types",
    "places.primaryTypeDisplayName", "places.nationalPhoneNumber", "places.internationalPhoneNumber",
    "places.websiteUri", "places.googleMapsUri", "places.businessStatus", "places.rating", "places.userRatingCount",
    "nextPageToken",
])


class PlacesAPI:
    name = "places_api"

    def __init__(self, http: Http, db, *, daily_cap: int = 30, monthly_cap: int = 900, lang: str = "en", region: str = "IN"):
        self.http, self.db = http, db
        self.key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
        self.daily_cap, self.monthly_cap = daily_cap, monthly_cap
        self.lang, self.region = lang, region

    def enabled(self) -> bool:
        return bool(self.key)

    def _take_budget(self) -> bool:
        now = datetime.now(timezone.utc)
        month, day = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
        if self.db.budget_used("places_api_month", month) >= self.monthly_cap:
            return False
        if not self.db.budget_take("places_api_day", day, self.daily_cap):
            return False
        return self.db.budget_take("places_api_month", month, self.monthly_cap)

    def search(self, query: str, lat: float, lng: float, size_km: float, max_pages: int = 1) -> tuple[list[Place], dict]:
        if not self.key:
            raise ProviderUnavailable("GOOGLE_PLACES_API_KEY not set")
        s, w, n, e = square_bbox(lat, lng, size_km)
        body = {"textQuery": query, "pageSize": 20, "languageCode": self.lang, "regionCode": self.region,
                "locationRestriction": {"rectangle": {"low": {"latitude": s, "longitude": w}, "high": {"latitude": n, "longitude": e}}}}
        places, pages, token = [], 0, None
        while pages < max_pages:
            if not self._take_budget():
                if pages == 0:
                    raise ProviderUnavailable("Places API daily/monthly free-tier budget used")
                break
            if token:
                body["pageToken"] = token
            try:
                r = self.http.post(URL, json_body=body, service="places_api", interval=0.5, timeout=25, retries=1, browser=False,
                                   headers={"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": FIELD_MASK, "Content-Type": "application/json"},
                                   block_statuses=(429,))
            except Blocked as exc:
                raise ProviderUnavailable(f"Places API refused: {exc}") from exc
            pages += 1
            if r.status in (400, 401, 403):
                self.http.breaker("places_api").trip(f"HTTP {r.status}")
                raise ProviderUnavailable(f"Places API error HTTP {r.status}: {r.text[:200]}")
            if r.status != 200:
                raise FetchError(f"Places API HTTP {r.status}")
            data = r.json()
            for p in data.get("places", []):
                loc = p.get("location") or {}
                status = p.get("businessStatus") or ""
                places.append(Place(
                    key=f"g:{p['id']}", provider="places_api", name=(p.get("displayName") or {}).get("text", ""),
                    lat=loc.get("latitude"), lng=loc.get("longitude"), address=p.get("formattedAddress", ""),
                    categories=[(p.get("primaryTypeDisplayName") or {}).get("text", "")] + [t.replace("_", " ") for t in p.get("types", [])],
                    phone=p.get("nationalPhoneNumber", ""), phone_intl=p.get("internationalPhoneNumber", ""),
                    website=p.get("websiteUri", ""), rating=p.get("rating"), reviews=p.get("userRatingCount"),
                    place_id=p["id"], maps_url=p.get("googleMapsUri", ""),
                    closed=status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"), status_text=status))
            token = data.get("nextPageToken")
            if not token:
                break
        return [p for p in places if p.name], {"pages": pages, "count": len(places)}
