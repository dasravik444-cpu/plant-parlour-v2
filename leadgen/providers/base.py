"""Common record type returned by every discovery provider."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Place:
    key: str                      # stable id, e.g. "g:ChIJ..." (Google place id) or "osm:node/123"
    provider: str                 # gmaps | places_api | osm
    name: str
    lat: float | None = None
    lng: float | None = None
    address: str = ""
    area: str = ""                # neighbourhood / locality label (e.g. "Action Area II, Rajarhat")
    city: str = ""
    categories: list[str] = field(default_factory=list)
    phone: str = ""               # raw phone exactly as the provider showed it
    phone_intl: str = ""          # provider's international format, when given
    website: str = ""
    rating: float | None = None
    reviews: int | None = None
    place_id: str = ""
    data_id: str = ""
    maps_url: str = ""
    description: str = ""
    closed: bool = False          # permanently/temporarily closed according to the provider
    status_text: str = ""
    extra: dict = field(default_factory=dict)   # provider-specific contact hints (e.g. OSM email/instagram tags)

    def maps_link(self) -> str:
        if self.maps_url:
            return self.maps_url
        if self.place_id:
            from urllib.parse import quote

            return f"https://www.google.com/maps/search/?api=1&query={quote(self.name)}&query_place_id={self.place_id}"
        if self.lat is not None and self.lng is not None:
            return f"https://www.google.com/maps/search/?api=1&query={self.lat:.6f}%2C{self.lng:.6f}"
        return ""


class ProviderUnavailable(Exception):
    """The provider cannot serve requests right now (blocked, disabled, budget used, misconfigured)."""
