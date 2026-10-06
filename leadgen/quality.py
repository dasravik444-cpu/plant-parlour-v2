"""Quality agent: name matching, chain/category filters, duplicate detection, lead qualification."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .util import norm_text

# Words that say what a business *is* or where it is, not *which* business it is.
GENERIC_WORDS = {
    "the", "a", "an", "and", "of", "by", "at", "in", "on", "for", "n", "amp",
    "cafe", "caf", "coffee", "tea", "chai", "restaurant", "restro", "resto", "bar", "pub", "lounge", "kitchen",
    "bistro", "diner", "eatery", "dhaba", "foods", "food", "house", "corner", "point", "hub", "zone", "spot",
    "bakery", "bakers", "cakes", "sweets", "biryani", "family", "multicuisine", "cuisine", "grill", "brewery",
    "hotel", "hotels", "resort", "resorts", "inn", "lodge", "guest", "stay", "suites", "residency", "palace",
    "banquet", "banquets", "hall", "halls", "venue", "venues", "lawn", "lawns", "convention", "centre", "center",
    "party", "events", "event", "management", "planner", "planners", "wedding", "weddings", "decor", "decorators",
    "decorator", "decoration", "caterers", "catering", "services", "service", "solutions", "studio", "studios",
    "interior", "interiors", "design", "designs", "designer", "designers", "architect", "architects", "associates",
    "consultants", "group", "enterprise", "enterprises", "company", "co", "pvt", "private", "ltd", "limited", "llp",
    "inc", "india", "indian", "official", "coworking", "cowork", "space", "spaces", "office", "offices", "workspace",
    "kolkata", "calcutta", "howrah", "salt", "lake", "sector", "new", "town", "newtown", "rajarhat", "park", "street",
    "road", "branch", "outlet", "city", "centre1", "centre2", "mall", "near", "opp", "opposite", "west", "bengal",
}

OTHER_CITIES = {"delhi", "new delhi", "mumbai", "bombay", "bangalore", "bengaluru", "chennai", "hyderabad", "pune",
                "ahmedabad", "jaipur", "lucknow", "noida", "gurgaon", "gurugram", "chandigarh", "bhubaneswar",
                "guwahati", "patna", "ranchi", "indore", "surat", "kochi", "goa", "dubai", "london", "singapore",
                "dhaka", "kathmandu", "siliguri", "durgapur", "asansol"}

# Listing/aggregator sites: not the business's own website (no contact crawl; not "the" website)
AGGREGATOR_DOMAINS = {
    "zomato.com", "swiggy.com", "swiggy.in", "magicpin.in", "justdial.com", "dineout.co.in", "eazydiner.com",
    "tripadvisor.com", "tripadvisor.in", "booking.com", "makemytrip.com", "goibibo.com", "agoda.com", "expedia.com",
    "hotels.com", "airbnb.com", "airbnb.co.in", "oyorooms.com", "sulekha.com", "indiamart.com", "tradeindia.com",
    "weddingz.in", "wedmegood.com", "shaadisaga.com", "venuelook.com", "bookeventz.com", "weddingwire.in",
    "urbanclap.com", "urbancompany.com", "houzz.com", "houzz.in", "yelp.com", "foursquare.com", "facebook.com",
    "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "google.com", "goo.gl", "g.page",
    "business.google.com", "maps.app.goo.gl", "wa.me", "whatsapp.com", "linktr.ee", "bit.ly", "zaubacorp.com",
    "thefork.com", "restaurantguru.com", "lbb.in", "so.city", "whatshot.in", "nearbuy.com", "dunzo.com",
    "zeptonow.com", "blinkit.com", "bigbasket.com", "amazon.in", "flipkart.com", "practo.com", "quora.com",
}
# Link hubs owned by the business: worth crawling for social links, but not a "website".
LINK_HUB_DOMAINS = {"linktr.ee", "beacons.ai", "bio.link", "linkin.bio", "taplink.cc", "campsite.bio", "lnk.bio"}


def tokens(s: str) -> list[str]:
    return [t for t in norm_text(s).split() if t]


def distinctive_tokens(name: str) -> list[str]:
    return [t for t in tokens(name) if t not in GENERIC_WORDS and len(t) > 1]


def _joined(ts: list[str]) -> str:
    return "".join(ts)


def name_score(business: str, candidate: str, handle: str = "") -> float:
    """0..1 similarity between a business name and a candidate profile/page name or handle."""
    b_all, c_all = tokens(business), tokens(candidate)
    b_dist = distinctive_tokens(business)
    if not b_all:
        return 0.0
    scores = []
    if c_all:
        scores.append(SequenceMatcher(None, " ".join(b_all), " ".join(c_all)).ratio())
        if b_dist:
            c_set = set(c_all) | {t for t in distinctive_tokens(candidate)}
            covered = sum(1 for t in b_dist if t in c_set or any(len(t) >= 4 and (t in c or c in t) for c in c_set if len(c) >= 4))
            scores.append(covered / len(b_dist))
            c_dist = distinctive_tokens(candidate)
            if c_dist:
                scores.append(SequenceMatcher(None, _joined(b_dist), _joined(c_dist)).ratio())
    if handle:
        h = re.sub(r"[^a-z0-9]", "", handle.lower())
        core = _joined(b_dist) if b_dist else _joined(b_all)
        if len(core) >= 4 and h:
            if core in h or (len(h) >= 5 and h in core):
                scores.append(0.92)
            else:
                scores.append(SequenceMatcher(None, core, h).ratio() * 0.95)
    if not b_dist:
        # Entirely generic names ("Coffee House") only match near-identical candidates.
        return max(scores or [0.0]) if max(scores or [0.0]) >= 0.95 else min(max(scores or [0.0]), 0.6)
    return max(scores or [0.0])


def mentions_other_city(text: str, home_terms: list[str]) -> bool:
    low = " " + norm_text(text) + " "
    if any(" " + norm_text(h) + " " in low for h in home_terms if h):
        return False
    return any(" " + c + " " in low for c in OTHER_CITIES)


def is_chain(name: str, chains: list[str]) -> str | None:
    low = norm_text(name)
    for c in chains:
        cn = norm_text(c)
        if cn and re.search(r"(^| )" + re.escape(cn) + r"( |$)", low):
            return c
    return None


def _word_hits(word: str, label: str) -> bool:
    """Whole-word match (plural 's'/'es' allowed); a trailing '*' in the config word means prefix match."""
    prefix = word.endswith("*")
    w = norm_text(word.rstrip("*"))
    if not w:
        return False
    tail = r"" if prefix else r"(s|es)?( |$)"
    return re.search(r"(^| )" + re.escape(w) + tail, label) is not None


def match_category(gcategories: list[str], query_category: str, categories: list[dict]) -> str | None:
    """Pick our category for a place from Google's labels; None when nothing matches.

    The most specific match wins ("Event venue" -> banquet/event venue rather than
    event planner via "event"); ties go to the category whose search found the place."""
    labels = [norm_text(c) for c in gcategories or [] if c]
    if not labels:
        return None
    # Google lists the primary category first: decide on the first label that matches anything.
    for lab in labels:
        best, best_len = None, 0
        for cat in categories:
            for word in cat.get("match", []):
                if word and _word_hits(word, lab):
                    n = len(norm_text(word.rstrip("*")))
                    if n > best_len or (n == best_len and cat["key"] == query_category):
                        best, best_len = cat["key"], n
        if best:
            return best
    return None


def domain_of(url: str) -> str:
    from urllib.parse import urlsplit

    h = (urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_aggregator(url: str) -> bool:
    d = domain_of(url)
    return any(d == a or d.endswith("." + a) for a in AGGREGATOR_DOMAINS)


def is_link_hub(url: str) -> bool:
    d = domain_of(url)
    return any(d == a or d.endswith("." + a) for a in LINK_HUB_DOMAINS)


QUALIFYING_KINDS = ("phone", "whatsapp", "email", "instagram")


def is_qualified(contact_kinds: set[str]) -> bool:
    return any(k in contact_kinds for k in QUALIFYING_KINDS)


def lead_priority(kinds: set[str], rating, reviews) -> str:
    score = 0
    score += 2 if "email" in kinds else 0
    score += 2 if "whatsapp" in kinds else 0
    score += 1 if "phone" in kinds else 0
    score += 1 if "instagram" in kinds else 0
    score += 1 if (rating or 0) >= 4.2 else 0
    score += 1 if (reviews or 0) >= 200 else 0
    return "High" if score >= 5 else "Medium" if score >= 3 else "Low"
