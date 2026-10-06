"""Instagram profile agent (best effort).

Instagram refuses logged-out profile reads from data-centre IPs (verified from
GitHub Actions: HTTP 401 / redirect to login). On a home connection (the
tablet) it sometimes answers. The agent therefore tries once, and a circuit
breaker switches it off for the rest of the run at the first refusal, so it can
never slow the pipeline down. Handles found on websites / search results are
kept either way; this agent only adds bio contacts when Instagram allows it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..net import Blocked, BreakerOpen, FetchError, Http, NetworkDown
from ..quality import name_score
from .emails import find_emails_in_text, normalize_email
from .phones import find_phones_in_text, parse_phone

APP_ID = "936619743392459"


class InstagramUnavailable(Exception):
    pass


@dataclass
class Profile:
    handle: str
    exists: bool
    full_name: str = ""
    biography: str = ""
    external_url: str = ""
    category: str = ""
    is_business: bool = False
    followers: int | None = None
    contacts: list = field(default_factory=list)   # [(kind, value, label, how)]


def handle_from_url(url: str) -> str:
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]{1,30})", url or "")
    return m.group(1).lower() if m else ""


def fetch_profile(http: Http, handle: str) -> Profile:
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={handle}"
    try:
        r = http.get(url, service="instagram", interval=8.0, jitter=6.0, timeout=20, retries=0, block_statuses=(429, 401, 403),
                     headers={"x-ig-app-id": APP_ID, "Accept": "*/*", "X-Requested-With": "XMLHttpRequest",
                              "Referer": f"https://www.instagram.com/{handle}/"})
    except NetworkDown:
        raise
    except (Blocked, BreakerOpen) as exc:
        # One refusal is conclusive for this network: switch the agent off for the run.
        http.breaker("instagram").trip(str(exc))
        raise InstagramUnavailable(str(exc)) from exc
    except FetchError as exc:
        raise InstagramUnavailable(str(exc)) from exc
    if r.status == 404:
        return Profile(handle=handle, exists=False)
    if "/accounts/login" in r.url or r.status != 200:
        http.breaker("instagram").trip(f"HTTP {r.status} login wall")
        raise InstagramUnavailable(f"login wall (HTTP {r.status})")
    try:
        user = (r.json().get("data") or {}).get("user")
    except ValueError:
        http.breaker("instagram").trip("non-JSON response")
        raise InstagramUnavailable("non-JSON response")
    if not user:
        return Profile(handle=handle, exists=False)
    p = Profile(handle=handle, exists=True, full_name=user.get("full_name") or "", biography=user.get("biography") or "",
                external_url=user.get("external_url") or "", category=user.get("category_name") or "",
                is_business=bool(user.get("is_business_account")),
                followers=((user.get("edge_followed_by") or {}).get("count")))
    be = normalize_email(user.get("business_email") or "")
    if be:
        p.contacts.append(("email", be, "instagram business email", "profile-field"))
    bphone = (user.get("business_phone_number") or "").strip()
    if bphone:
        cc = str(user.get("business_contact_method") or "")
        country = str(user.get("public_phone_country_code") or "")
        raw = ("+" + country + bphone) if country and not bphone.startswith("+") else bphone
        parsed = parse_phone(raw)
        if parsed:
            p.contacts.append(("phone", parsed[0], parsed[1] + (", " + cc.lower() if cc else ""), "profile-field"))
    for e in find_emails_in_text(p.biography):
        p.contacts.append(("email", e, "in bio", "bio"))
    for e164, label, _ in find_phones_in_text(p.biography, require_context=False, max_results=4):
        p.contacts.append(("phone", e164, label + ", in bio", "bio"))
        if re.search(r"whats\s*app|wa\b", p.biography, re.I):
            p.contacts.append(("whatsapp", e164, "mentioned in bio", "bio"))
    return p


def profile_matches(business: str, profile: Profile) -> float:
    return name_score(business, profile.full_name, handle=profile.handle)
