"""Social finder agent: locate a business's Instagram / Facebook / LinkedIn page (and, when Google
Maps had none, its own website) through web search.

A search result is accepted only when the profile name or handle clearly matches
the business name and the result does not point to a different city. The query,
engine, result title and URL are kept as evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..quality import is_aggregator, match_strength, mentions_other_city, name_score
from .emails import find_emails_in_text
from .extract import canonical_social, registrable
from .phones import find_phones_in_text
from .search import Result, WebSearch

TITLE_PATTERNS = {
    "instagram": [re.compile(r"^(?P<name>.*?)\s*\(\s*@(?P<handle>[A-Za-z0-9_.]{1,30})\s*\)"),
                  re.compile(r"^(?P<name>.*?)\s*[•|\-–]\s*Instagram", re.I)],
    "facebook": [re.compile(r"^(?P<name>.*?)\s*(?:[|\-–]\s*[^|\-–]*)?[|\-–]\s*Facebook\b", re.I),
                 re.compile(r"^(?P<name>.*?)\s*[|\-–]", re.I)],
    "linkedin": [re.compile(r"^(?P<name>.*?)\s*[|\-–]\s*LinkedIn", re.I), re.compile(r"^(?P<name>.*?)\s*[|\-–]", re.I)],
}
NON_BUSINESS_SITES = ("wikipedia.org", "gov.in", "nic.in", "wikimapia.org", "mapquest.com", "yellowpages", "asklaila.com",
                      "grotal.com", "cybo.com", "zaubacorp.com", "tofler.in", "indiacom", "wikidata.org", "britannica.com",
                      "tripoto.com", "holidify.com", "medium.com", "blogspot.com", "news", "times", "telegraphindia.com")


@dataclass
class SocialMatch:
    kind: str
    url: str
    score: float
    title: str
    engine: str
    query: str = ""
    extra_contacts: list = field(default_factory=list)   # [(kind, value, label)] from the result snippet
    strength: str = "strong"     # weak = name matches but too generic to be sure (shown as unverified)


@dataclass
class LookupResult:
    query: str
    results_seen: int
    matches: dict            # kind -> SocialMatch
    website: SocialMatch | None = None


def _candidate_name(kind: str, title: str) -> tuple[str, str]:
    for pat in TITLE_PATTERNS.get(kind, []):
        m = pat.search(title)
        if m:
            name = (m.group("name") or "").strip(" -|•–")
            handle = m.groupdict().get("handle") or ""
            if name or handle:
                return name, handle
    return title, ""


def _snippet_contacts(snippet: str) -> list:
    extra = [("email", e, "from search snippet") for e in find_emails_in_text(snippet)]
    extra += [("phone", e164, label + ", from search snippet") for e164, label, _ in find_phones_in_text(snippet, require_context=False, max_results=3)]
    return extra


def best_match(kind: str, business: str, results: list[Result], home_terms: list[str], threshold: float = 0.75) -> SocialMatch | None:
    best = None
    for r in results:
        canon = canonical_social(r.url, profile_only=True)
        if not canon or canon[0] != kind:
            continue
        if kind == "linkedin" and "/in/" in canon[1]:
            continue  # personal profiles are not company pages
        name, handle = _candidate_name(kind, r.title)
        if not handle:
            tail = canon[1].rstrip("/").rsplit("/", 1)[-1]
            handle = "" if "profile.php" in tail or tail.isdigit() else tail
        score = name_score(business, name, handle=handle)
        if mentions_other_city(r.title + " " + r.snippet, home_terms):
            score -= 0.3
        if best is None or score > best.score:
            best = SocialMatch(kind, canon[1], round(score, 3), r.title[:150], r.engine, extra_contacts=_snippet_contacts(r.snippet),
                               strength=match_strength(business, name, handle, r.title + " " + r.snippet, home_terms))
    if best and best.score >= threshold:
        return best
    return None


def find_website(business: str, results: list[Result], home_terms: list[str]) -> SocialMatch | None:
    best = None
    for r in results:
        host = (urlsplit(r.url).hostname or "").lower()
        if not host or canonical_social(r.url) or is_aggregator(r.url) or any(s in host for s in NON_BUSINESS_SITES):
            continue
        core = registrable(host[4:] if host.startswith("www.") else host).split(".")[0]
        s_dom = name_score(business, "", handle=core)
        s_title = name_score(business, r.title)
        score = s_dom if s_dom >= 0.85 else (min(s_dom, s_title) + 0.1 if s_dom >= 0.7 and s_title >= 0.8 else 0.0)
        if mentions_other_city(r.title + " " + r.snippet, home_terms):
            score -= 0.3
        if score >= 0.85 and (best is None or score > best.score):
            best = SocialMatch("website", f"{urlsplit(r.url).scheme}://{host}/", round(score, 3), r.title[:150], r.engine,
                               extra_contacts=_snippet_contacts(r.snippet))
    return best


def social_lookup(search: WebSearch, business: str, locality: str, city: str, kinds=("instagram", "facebook"),
                  want_website: bool = False, platform_word: str = "instagram", threshold: float = 0.75,
                  home_extra: tuple | list = ()) -> LookupResult:
    """home_extra: more names of the business's location (area parts, city aliases) that count as
    evidence when a profile mentions them."""
    place_hint = city if not locality or locality.lower() in city.lower() else f"{locality} {city}"
    query = f'"{business}" {place_hint} {platform_word}'.strip()
    results = search.search(query)
    home = [city, locality, *[h for h in home_extra if h]]
    matches = {}
    for kind in kinds:
        m = best_match(kind, business, results, home, threshold)
        if m:
            m.query = query
            matches[kind] = m
    website = find_website(business, results, home) if want_website else None
    if website:
        website.query = query
    return LookupResult(query=query, results_seen=len(results), matches=matches, website=website)
