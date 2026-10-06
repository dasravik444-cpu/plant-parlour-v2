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


# Common English words often used as business names ("Natural", "Empire", "Ruby"). A profile that
# matches only such a word (or a very short one) is weak evidence on its own.
COMMON_NAME_WORDS = {
    "royal", "empire", "grand", "golden", "gold", "silver", "diamond", "crown", "star", "stars", "sun", "moon", "sky",
    "cloud", "clouds", "blue", "green", "red", "white", "black", "orange", "yellow", "pink", "purple", "natural",
    "nature", "organic", "fresh", "urban", "modern", "classic", "elite", "prime", "premium", "luxury", "supreme",
    "perfect", "smart", "happy", "lucky", "bliss", "joy", "delight", "delights", "dream", "dreams", "magic",
    "paradise", "heaven", "spice", "spices", "aroma", "flavour", "flavours", "flavor", "flavors", "taste", "tastes",
    "treat", "treats", "tasty", "yummy", "sweet", "honey", "sugar", "cream", "ruby", "pearl", "emerald", "crystal",
    "lotus", "rose", "lily", "orchid", "jasmine", "tulip", "sunflower", "sunrise", "sunset", "rainbow", "ocean",
    "river", "garden", "gardens", "forest", "hills", "valley", "village", "coastal", "harbour", "metro", "central",
    "capital", "tower", "towers", "plaza", "square", "avenue", "lane", "bloom", "blossom", "aura", "zen", "karma",
    "vibe", "vibes", "mood", "soul", "spirit", "story", "stories", "tales", "chapter", "tribe", "nest", "den", "hut",
    "cabin", "cottage", "mansion", "castle", "fort", "kingdom", "king", "kings", "queen", "prince", "princess",
    "angel", "angels", "boss", "chief", "master", "legend", "legends", "hero", "titan", "phoenix", "eagle", "tiger",
    "lion", "falcon", "global", "world", "universal", "galaxy", "planet", "earth", "fire", "ice", "water", "air",
    "spring", "summer", "winter", "autumn", "season", "seasons", "time", "times", "life", "living", "home", "homes",
    "comfort", "cozy", "cosy", "plus", "max", "pro", "one", "first", "best", "top", "new", "old", "little", "big",
    "mega", "super", "ultra", "true", "pure", "simple", "basic", "elegant", "elegance", "style", "styles", "trend",
    "trends", "fashion", "art", "arts", "craft", "crafts", "creative", "creations", "concept", "concepts", "idea",
    "ideas", "vision", "image", "images", "touch", "space", "square", "circle", "line", "lines", "point", "edge",
    "peak", "summit", "horizon", "sunshine", "breeze", "mist", "dew", "leaf", "leaves", "tree", "trees", "root",
    "roots", "seed", "seeds", "harvest", "farm", "farms", "field", "fields", "meadow", "brew", "bean", "beans",
    "cup", "mug", "plate", "bowl", "spoon", "fork", "oven", "flame", "smoke", "grill", "chill", "frost", "delicious",
    "crunch", "bite", "bites", "nibbles", "feast", "platter", "thali", "tiffin", "dabba", "masala", "tadka",
    "zaika", "swad", "rasoi", "khana", "annapurna", "lakshmi", "ganesh", "durga", "kali", "shiva", "krishna",
    "balaji", "sai", "om", "shree", "shri", "sri", "jai", "maa", "baba", "new", "natural", "lifestyle", "signature",
    "expressions", "impressions", "moments", "memories", "celebrations", "occasions", "glamour", "glory", "pride",
}
_VOWELS = set("aeiouy")
_HANDLE_FILLERS = {"official", "the", "its", "iam", "im", "we", "my", "our", "real", "hq", "club", "world", "by",
                   "and", "co", "inc", "ltd", "pvt", "online", "live", "daily", "original", "team", "shop", "store",
                   "page", "kol", "kolkata", "calcutta", "cal", "ccu", "india", "in", "wb", "bengal", "west"}


def tokens(s: str) -> list[str]:
    return [t for t in norm_text(s).split() if t]


def distinctive_tokens(name: str) -> list[str]:
    return [t for t in tokens(name) if t not in GENERIC_WORDS and len(t) > 1]


def _joined(ts: list[str]) -> str:
    return "".join(ts)


def _skeleton(t: str) -> str:
    """Consonant skeleton, tolerant to the vowel/doubling variations of transliterated names
    (Daawat/Dawat, Bangali/Bengali, Kolkatta/Kolkata) but not to different consonants (Arabiya/Arabica)."""
    if not t:
        return ""
    out = [t[0]]
    for ch in t[1:]:
        if ch in _VOWELS or ch == out[-1]:
            continue
        out.append(ch)
    return "".join(out)


def token_equiv(a: str, b: str) -> bool:
    if a == b:
        return True
    if a + "s" == b or b + "s" == a or a + "es" == b or b + "es" == a:
        return True
    if len(a) >= 4 and len(b) >= 4 and a[0] == b[0] and not (a.isdigit() or b.isdigit()):
        sa = _skeleton(a)
        return len(sa) >= 3 and sa == _skeleton(b)
    return False


def _segment(s: str, biz: list[str], vocab: set) -> tuple[bool, set]:
    """Split a joined string ("cafebloomkolkata") into business-name tokens (spelling-tolerant), generic or
    location words and digit runs. Returns (fully explained, business tokens used)."""
    n = len(s)
    best: list = [None] * (n + 1)
    best[0] = frozenset()

    def upd(j, used):
        if best[j] is None or len(used) > len(best[j]):
            best[j] = used
    for i in range(n):
        if best[i] is None:
            continue
        j = i
        while j < n and s[j].isdigit():
            j += 1
        if j > i:
            upd(j, best[i])
        for j in range(i + 2, min(n, i + 30) + 1):
            piece = s[i:j]
            hit = next((t for t in biz if token_equiv(piece, t)), None)
            if hit:
                upd(j, best[i] | {hit})
            elif piece in vocab:
                upd(j, best[i])
    return best[n] is not None, set(best[n] or ())


class _Match:
    __slots__ = ("score", "dist", "covered", "handle_full")

    def __init__(self, score=0.0, dist=(), covered=(), handle_full=False):
        self.score, self.dist, self.covered, self.handle_full = score, list(dist), set(covered), handle_full


def core_name(business: str) -> str:
    """The name without SEO tails: "The Prime Banquet - Best Banquet Hall in Kolkata" -> "The Prime Banquet"."""
    parts = re.split(r"\s[-|–:]\s|\s?\|\s?|:\s|\s\(|,\s", business or "", maxsplit=1)
    return parts[0].strip() if parts and parts[0].strip() else (business or "")


def _match_one(business: str, candidate: str, handle: str) -> _Match:
    b_all, b_dist = tokens(business), distinctive_tokens(business)
    if not b_all:
        return _Match()
    biz = [t for t in b_all if len(t) >= 2]
    vocab = {w for w in GENERIC_WORDS if len(w) >= 2} | _HANDLE_FILLERS | set(biz)
    covered: set = set()
    c_all = tokens(candidate)
    for t in c_all:
        _, used = _segment(t, biz, vocab)
        covered |= used
    handle_full, h_cov = False, set()
    if handle:
        segs = [x for x in re.split(r"[^a-z0-9]+", handle.lower()) if x]
        full = bool(segs)
        for seg in segs:
            if len(seg) == 1:
                continue
            ok, used = _segment(seg, biz, vocab)
            if not ok:
                full = False
                used = {t for t in biz if len(t) >= 3 and seg.startswith(t)}   # "cafebloom123xyz"
            h_cov |= used
        handle_full = full and bool(h_cov)
        covered |= h_cov
    scores = []
    if b_dist:
        hits = sum(1 for t in b_dist if t in covered)
        cov = hits / len(b_dist)
        scores.append(cov)
        if c_all:
            c_dist = distinctive_tokens(candidate)
            if c_dist:
                scores.append(SequenceMatcher(None, _joined(b_dist), _joined(c_dist)).ratio())
            scores.append(SequenceMatcher(None, " ".join(b_all), " ".join(c_all)).ratio())
        if handle and all(t in h_cov for t in b_dist):
            scores.append(0.95 if handle_full else 0.88)
        if cov < 1.0 and hits >= 2 and cov >= 0.6:
            # most of a longer name matches ("Zero Degree Cafe and Lounge Esplanade" -> @zerodegreekolkata)
            scores.append(0.75 + 0.2 * cov)
        score = max(scores)
        if cov < 1.0 and not (hits >= 2 and cov >= 0.6):
            score = min(score, 0.7)    # a distinctive word of the name is missing
        return _Match(score, b_dist, covered, handle_full)
    # Entirely generic names ("Coffee House", "The Street Cafe") only match near-identical candidates.
    if c_all:
        scores.append(SequenceMatcher(None, " ".join(b_all), " ".join(c_all)).ratio())
    if handle and handle_full and all(t in h_cov for t in biz if t not in ("the", "and", "of")):
        scores.append(0.95)
    best = max(scores or [0.0])
    return _Match(best if best >= 0.95 else min(best, 0.6), [], covered, handle_full)


def name_match(business: str, candidate: str, handle: str = "") -> _Match:
    m = _match_one(business, candidate, handle)
    core = core_name(business)
    if core and core != business and distinctive_tokens(core):
        m2 = _match_one(core, candidate, handle)
        if m2.score > m.score:
            m = m2
    return m


def name_score(business: str, candidate: str, handle: str = "") -> float:
    """0..1 similarity between a business name and a candidate profile/page name or handle."""
    return name_match(business, candidate, handle).score


def match_strength(business: str, candidate: str, handle: str, text: str, home_terms: list[str]) -> str:
    """How sure a matching search result is this business: 'strong' or 'weak'.

    Strong: two distinctive words of the name match; or one unusual word (not a common English
    word, 5+ letters); or one word plus another word of the name ("the_ruby_kitchen"); or the
    profile mentions the business's city/locality. Everything else is weak (shown as unverified)."""
    m = name_match(business, candidate, handle)
    hay = " " + norm_text(" ".join([text or "", candidate or "", re.sub(r"[^a-z0-9]+", " ", (handle or "").lower())])) + " "
    home = any(" " + norm_text(h) + " " in hay for h in home_terms if h and len(norm_text(h)) >= 3) or " kol " in hay
    if not m.dist:
        return "strong" if home and m.score >= 0.95 else "weak"
    hits = [t for t in m.dist if t in m.covered]
    if home:
        return "strong"
    if len(hits) >= 2 and (len(hits) == len(m.dist) or any(t not in COMMON_NAME_WORDS for t in hits)):
        return "strong"
    if len(hits) == 1:
        t = hits[0]
        if len(t) >= 5 and t not in COMMON_NAME_WORDS and not t.isdigit():
            return "strong"
        others = {x for x in m.covered if x != t and len(x) >= 3 and x not in ("the", "and")}
        if others:
            return "strong"
    return "weak"


def mentions_other_city(text: str, home_terms: list[str]) -> bool:
    low = " " + norm_text(text) + " "
    if any(" " + norm_text(h) + " " in low for h in home_terms if h):
        return False
    return any(" " + c + " " in low for c in OTHER_CITIES)


# OYO lists franchise hotels under sub-brands followed by a property number: "SPOT ON 83258 Hotel X".
_OYO_BRANDS = re.compile(r"^(spot on|capital o|collection o|townhouse(?: oak)?|flagship|silverkey|super oyo)\s+\d{3,}")


def is_chain(name: str, chains: list[str]) -> str | None:
    low = norm_text(name)
    if _OYO_BRANDS.search(low):
        return "oyo network"
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
