"""Extract contact routes from an HTML page: literal values only, each with how it was found."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .emails import decode_cfemail, find_emails_in_text, normalize_email
from .phones import find_phones_in_text, parse_phone, whatsapp_number_from_link

SOCIAL_RESERVED = {
    "instagram": {"p", "reel", "reels", "tv", "explore", "stories", "accounts", "about", "developer", "legal", "direct",
                  "web", "share", "s", "sharer", "_u", "privacy", "terms", "help", "embed", "static", "graphql", "api",
                  "challenge", "emails", "session", "login", "signup", "oauth", "ar", "lite", "topics", "popular",
                  "locations", "tags", "hashtag", "music", "audio", "threads", "invites", "nametag"},
    "facebook": {"sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login", "login.php", "groups",
                 "events", "watch", "help", "policies", "privacy", "hashtag", "legal", "ads", "business", "gaming",
                 "marketplace", "photo", "photo.php", "photos", "story.php", "permalink.php", "home.php", "l.php",
                 "people", "search", "pg", "media", "video.php", "reel", "fundraisers", "notes", "settings", "messages", "2008"},
    "twitter": {"share", "intent", "home", "i", "search", "hashtag", "explore", "settings", "login", "signup", "tos",
                "privacy", "widgets", "about"},
    "linkedin": set(),
    "youtube": set(),
}

SOCIAL_HOST_KIND = {
    "instagram.com": "instagram", "instagr.am": "instagram",
    "facebook.com": "facebook", "fb.com": "facebook", "fb.me": "facebook", "m.facebook.com": "facebook",
    "twitter.com": "twitter", "x.com": "twitter",
    "linkedin.com": "linkedin",
    "youtube.com": "youtube", "youtu.be": "youtube",
}

CONTACT_LINK_WORDS = re.compile(r"contact|about|reach|find[\s\-_]?us|get[\s\-_]?in[\s\-_]?touch|connect|enquir|inquir|location|visit[\s\-_]?us|book|reserv|support", re.I)
POLICY_LINK_WORDS = re.compile(r"privacy|terms|polic|legal|disclaimer|refund|cancellation|imprint", re.I)
# "email": "x@y.com" / 'contactEmail':'x@y.com' / mailto:x@y.com inside a <script> (JSON settings of site builders)
SCRIPT_EMAIL_RE = re.compile(r"(?:[\"'](?:e-?mail|contact_?e-?mail|email_?address|mail)[\"']\s*:\s*[\"']|mailto:)"
                             r"([A-Za-z0-9._%+\-]{1,64}(?:@|\\u0040)[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,24})", re.I)
SKIP_LINK_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|rar|docx?|xlsx?|pptx?|mp4|mp3|avi|mov)(\?|$)", re.I)
# A named contact person, only when the business's own site says so explicitly ("Founder: Rahul Sharma",
# "Rahul Sharma, Owner", "Founded by Rahul Sharma"). Never inferred from reviews or e-mail addresses.
_ROLE = r"(?i:co[- ]?founder|founder|owner|proprietor|proprietress|managing director|director|ceo|chef[- ]owner|partner)"
_PNAME = r"(?:(?:Mr|Mrs|Ms|Dr|Smt|Shri|Sri)\.?\s+)?([A-Z][a-z]{1,15}(?:\s+[A-Z][a-z]{1,15}){1,2})"
PERSON_PATTERNS = [
    re.compile(rf"\b({_ROLE})\s*[:\-\u2013\u2014]\s*{_PNAME}"),
    re.compile(rf"{_PNAME}\s*(?:,|\(|\s[\-\u2013\u2014])\s*(?i:our\s+|the\s+)?({_ROLE})\b"),
    re.compile(rf"\b(?i:founded|started|established)\s+(?:(?i:in)\s+\d{{4}}\s+)?(?i:by)\s+{_PNAME}"),
]
NOT_NAME_WORDS = {
    "our", "the", "team", "contact", "home", "about", "read", "more", "welcome", "call", "email", "phone", "address",
    "menu", "book", "view", "click", "here", "privacy", "policy", "terms", "copyright", "rights", "reserved", "india",
    "kolkata", "calcutta", "west", "bengal", "salt", "lake", "new", "town", "private", "limited", "pvt", "ltd", "group",
    "company", "services", "service", "solutions", "cafe", "restaurant", "hotel", "banquet", "events", "event", "interior",
    "interiors", "design", "designs", "studio", "kitchen", "foods", "food", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "director", "founder", "owner", "manager", "chef", "partner", "and",
}


def valid_person_name(name: str) -> bool:
    words = name.split()
    return 2 <= len(words) <= 3 and all(w.isalpha() and w.lower() not in NOT_NAME_WORDS for w in words)


def find_people(text: str) -> list[tuple[str, str, str]]:
    """[(name, role, snippet)] for explicitly stated owners/founders in page text."""
    out, seen = [], set()
    for i, pat in enumerate(PERSON_PATTERNS):
        for m in pat.finditer(text):
            if i == 0:
                role, name = m.group(1), m.group(2)
            elif i == 1:
                name, role = m.group(1), m.group(2)
            else:
                name, role = m.group(1), "founder"
            words = name.split()
            while words and (words[0].lower() in ("meet", "hello", "hi", "dear", "with", "from", "by", "says", "ask", "us")
                             or words[0].lower() in NOT_NAME_WORDS):
                words.pop(0)                    # "Meet Priya Das, Founder" / "Contact Us Raju Ahamed, Proprietor"
            name = " ".join(words)
            if valid_person_name(name) and name.lower() not in seen:
                seen.add(name.lower())
                out.append((name, role.lower().replace("co founder", "co-founder"), text[max(0, m.start() - 20):m.end() + 20]))
    return out[:3]


AGENCY_CONTEXT = re.compile(r"(designed|developed|powered|crafted|built|maintained|created|hosted|managed)\s+(and\s+\w+\s+)?by", re.I)


def host_of(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def registrable(host: str) -> str:
    """Crude registrable domain: last two labels, or three for common 2nd-level public suffixes (co.in, org.in...)."""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "org", "net", "gov", "ac", "edu", "com", "res", "gen", "firm", "ind") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# Second path segments that mean "a post/photo/video by this account", not the account itself.
_CONTENT_SEGS = {
    "instagram": {"p", "reel", "reels", "tv", "stories", "tagged", "guide", "guides", "highlights"},
    "facebook": {"posts", "photos", "photo", "photo.php", "videos", "video", "reel", "reels", "events", "permalink.php",
                 "story.php", "notes", "albums", "media", "live", "community", "reviews", "mentions"},
    "twitter": {"status", "statuses", "photo", "media", "with_replies"},
    "youtube": {"watch", "shorts", "live"},
}


def canonical_social(url: str, profile_only: bool = False) -> tuple[str, str] | None:
    """Return (kind, canonical_url) for a profile/page URL, or None if it is not a profile.

    profile_only=True (used for search results): a link to a post, photo or video is
    rejected instead of being reduced to its account, because in search results that
    account is often someone else's (a food blogger reviewing the business)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host[2:] in SOCIAL_HOST_KIND:
        host = host[2:]
    if host.startswith("in.") and host.endswith("linkedin.com"):
        host = "linkedin.com"
    kind = SOCIAL_HOST_KIND.get(host)
    if not kind:
        return None
    segs = [unquote(s) for s in parts.path.split("/") if s]
    if profile_only and len(segs) >= 2 and segs[1].lower() in _CONTENT_SEGS.get(kind, set()):
        return None
    if profile_only and kind == "facebook" and segs and segs[0].lower() in ("story.php", "permalink.php", "photo.php", "watch"):
        return None
    if kind == "instagram":
        if not segs:
            return None
        handle = segs[0].lstrip("@").lower()
        if handle in SOCIAL_RESERVED["instagram"] or not re.fullmatch(r"[a-z0-9_.]{1,30}", handle) or handle.endswith(".") or not re.search(r"[a-z]", handle):
            return None
        return kind, f"https://www.instagram.com/{handle}/"
    if kind == "facebook":
        if host == "fb.me" and segs:
            return kind, f"https://www.facebook.com/{segs[0]}"
        if segs and segs[0] in ("profile.php",):
            pid = parse_qs(parts.query).get("id", [""])[0]
            return (kind, f"https://www.facebook.com/profile.php?id={pid}") if pid.isdigit() else None
        if segs and segs[0] == "pages" and len(segs) >= 2:
            return kind, "https://www.facebook.com/" + "/".join(segs[:3])
        if not segs or segs[0].lower() in SOCIAL_RESERVED["facebook"] or not re.fullmatch(r"[A-Za-z0-9.\-]{2,80}", segs[0]):
            return None
        return kind, f"https://www.facebook.com/{segs[0]}"
    if kind == "twitter":
        if not segs or segs[0].lower() in SOCIAL_RESERVED["twitter"] or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", segs[0]):
            return None
        return kind, f"https://x.com/{segs[0]}"
    if kind == "linkedin":
        if len(segs) >= 2 and segs[0] in ("company", "in", "school", "showcase") and re.fullmatch(r"[A-Za-z0-9\-_%.]{2,100}", segs[1]):
            return kind, f"https://www.linkedin.com/{segs[0]}/{segs[1]}/"
        return None
    if kind == "youtube":
        if host == "youtu.be":
            return None
        if segs and (segs[0].startswith("@") or segs[0] in ("channel", "c", "user")):
            path = segs[0] if segs[0].startswith("@") else "/".join(segs[:2])
            return kind, f"https://www.youtube.com/{path}"
        return None
    return None


@dataclass
class Found:
    kind: str            # email|phone|whatsapp|instagram|facebook|linkedin|twitter|youtube
    value: str           # normalised value (E.164 / lowercase email / canonical URL)
    how: str             # mailto|tel|wa-link|jsonld|text|link|cfemail|meta
    label: str = ""
    snippet: str = ""


@dataclass
class PageExtract:
    url: str
    title: str = ""
    description: str = ""
    site_name: str = ""
    found: list[Found] = field(default_factory=list)
    internal_links: list[tuple[str, str]] = field(default_factory=list)   # (url, anchor text) candidates for contact pages
    policy_links: list[tuple[str, str]] = field(default_factory=list)     # privacy/terms pages (deep crawl only)
    jsonld_names: list[str] = field(default_factory=list)
    redirect_to_social: str = ""

    def add(self, f: Found):
        for x in self.found:
            if x.kind == f.kind and x.value == f.value:
                return
        self.found.append(f)


def _walk_jsonld(node, out: list):
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk_jsonld(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, out)


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# Ad tracking code on a business's own website: it pays for online advertising (budget, growth). A buying
# signal read only from pages we already crawl - the ad platforms themselves are never scraped.
AD_TAGS = [
    ("Runs Meta (Facebook/Instagram) ads", re.compile(r"connect\.facebook\.net/[\w-]+/fbevents\.js|fbq\(\s*['\"]init['\"]", re.I)),
    ("Runs Google Ads", re.compile(r"googleadservices\.com/pagead/conversion|gtag\(\s*['\"]config['\"]\s*,\s*['\"]AW-\d+"
                                   r"|googleads\.g\.doubleclick\.net", re.I)),
]


def ad_signals(html: str) -> list[str]:
    return [label for label, rx in AD_TAGS if rx.search(html or "")]


def extract_page(html: str, url: str, region: str = "IN", contact_page: bool = False) -> PageExtract:
    soup = BeautifulSoup(html, "html.parser")
    pe = PageExtract(url=url)
    base_host = host_of(url)

    # --- metadata -----------------------------------------------------------
    if soup.title and soup.title.string:
        pe.title = re.sub(r"\s+", " ", soup.title.string).strip()[:200]
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content") and not pe.description:
            pe.description = re.sub(r"\s+", " ", tag["content"]).strip()[:400]
    tag = soup.find("meta", attrs={"property": "og:site_name"})
    if tag and tag.get("content"):
        pe.site_name = tag["content"].strip()[:120]

    # --- JSON-LD (structured data the site publishes about itself) ----------
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        nodes: list = []
        _walk_jsonld(data, nodes)
        for n in nodes:
            if isinstance(n.get("name"), str):
                pe.jsonld_names.append(n["name"].strip()[:120])
            for tel in _as_list(n.get("telephone")):
                if isinstance(tel, str):
                    p = parse_phone(tel, region)
                    if p:
                        pe.add(Found("phone", p[0], "jsonld", p[1]))
            for em in _as_list(n.get("email")):
                if isinstance(em, str):
                    e = normalize_email(em)
                    if e:
                        pe.add(Found("email", e, "jsonld"))
            for same in _as_list(n.get("sameAs")):
                if isinstance(same, str):
                    s = canonical_social(same)
                    if s:
                        pe.add(Found(s[0], s[1], "jsonld"))
            for f in _as_list(n.get("founder")):
                fname = (f.get("name") if isinstance(f, dict) else f) or ""
                if isinstance(fname, str) and valid_person_name(fname.strip()):
                    pe.add(Found("person", fname.strip(), "jsonld", "founder"))

    # --- microdata / itemprop (schema.org without JSON-LD) ------------------
    for el in soup.select('[itemprop="email"]'):
        raw = el.get("content") or el.get("href") or el.get_text(" ", strip=True)
        e = normalize_email(raw)
        if e:
            pe.add(Found("email", e, "microdata"))
    for el in soup.select('[itemprop="telephone"]'):
        raw = el.get("content") or el.get("href") or el.get_text(" ", strip=True)
        p2 = parse_phone(raw, region)
        if p2:
            pe.add(Found("phone", p2[0], "microdata", p2[1]))

    # --- links ----------------------------------------------------------------
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:80]
        # Skip links inside "designed by ..." credits (web agency contacts)
        parent_text = a.parent.get_text(" ", strip=True)[:200] if a.parent is not None else ""
        agency = bool(AGENCY_CONTEXT.search(parent_text))
        if low.startswith("mailto:"):
            e = normalize_email(href)
            if e and not agency:
                pe.add(Found("email", e, "mailto"))
            continue
        if low.startswith("tel:") or low.startswith("callto:"):
            p = parse_phone(href.split(":", 1)[1], region)
            if p and not agency:
                pe.add(Found("phone", p[0], "tel", p[1]))
            continue
        if "/cdn-cgi/l/email-protection#" in low:
            e = normalize_email(decode_cfemail(href.split("#", 1)[1]) or "")
            if e and not agency:
                pe.add(Found("email", e, "cfemail"))
            continue
        if "wa.me/" in low or "whatsapp.com/send" in low or low.startswith("whatsapp://"):
            num = whatsapp_number_from_link(href)
            if num:
                pe.add(Found("whatsapp", num, "wa-link"))
            elif "wa.me/message/" in low or "wa.me/c/" in low:
                pe.add(Found("whatsapp", href.split("?")[0][:200], "wa-link", "business link"))
            continue
        absu = urljoin(url, href)
        social = canonical_social(absu)
        if social:
            if not agency:
                pe.add(Found(social[0], social[1], "link"))
            continue
        if absu.startswith(("http://", "https://")) and host_of(absu) == base_host and not SKIP_LINK_EXT.search(absu):
            if CONTACT_LINK_WORDS.search(href) or CONTACT_LINK_WORDS.search(text):
                clean = urlunsplit(urlsplit(absu)._replace(fragment=""))
                if clean.rstrip("/") != url.rstrip("/") and all(clean != u for u, _ in pe.internal_links):
                    pe.internal_links.append((clean, text))
            elif POLICY_LINK_WORDS.search(href) or POLICY_LINK_WORDS.search(text):
                clean = urlunsplit(urlsplit(absu)._replace(fragment=""))
                if all(clean != u for u, _ in pe.policy_links):
                    pe.policy_links.append((clean, text))

    for el in soup.find_all(attrs={"data-cfemail": True}):
        e = normalize_email(decode_cfemail(el["data-cfemail"]) or "")
        if e:
            pe.add(Found("email", e, "cfemail"))

    # --- e-mails written into scripts (site builders keep contact settings as JSON) ----------------
    script_emails = []
    for sc in soup.find_all("script"):
        raw = sc.string or sc.get_text() or ""
        if "@" not in raw or len(raw) > 3_000_000:
            continue
        for m in SCRIPT_EMAIL_RE.finditer(raw):
            e = normalize_email(m.group(1).replace("\\u0040", "@").replace("\\/", "/"))
            if e:
                script_emails.append(e)

    # --- visible text ---------------------------------------------------------
    for t in soup(["script", "style", "noscript", "svg", "template"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    # Drop agency credit sentences before scanning text
    text_clean = re.sub(r"(designed|developed|powered|crafted|built|maintained|created|hosted)\s+(and\s+\w+\s+)?by[^.|]{0,120}", " ", text, flags=re.I)
    for e in find_emails_in_text(text_clean):
        pe.add(Found("email", e, "text"))
    for e in script_emails:
        pe.add(Found("email", e, "script"))
    for e164, label, snip in find_phones_in_text(text_clean, region, require_context=not contact_page):
        pe.add(Found("phone", e164, "text", label, snip))
    for name, role, snip in find_people(text_clean):
        pe.add(Found("person", name, "text", role, snip))
    # "WhatsApp: +91 ..." written as text
    for m in re.finditer(r"whats\s*app[^0-9+]{0,25}((?:\+?91[\s\-]?)?[6-9]\d{4}[\s\-]?\d{5})", text_clean, re.I):
        p = parse_phone(m.group(1), region)
        if p:
            pe.add(Found("whatsapp", p[0], "text", "", m.group(0)[:80]))

    # Pages that are only a redirect to a social profile (e.g. meta refresh)
    refresh = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if refresh and refresh.get("content"):
        m = re.search(r"url=(.+)", refresh["content"], re.I)
        if m:
            s = canonical_social(urljoin(url, m.group(1).strip("'\" ")))
            if s:
                pe.redirect_to_social = s[1]
    return pe


def rank_contact_links(links: list[tuple[str, str]], limit: int) -> list[str]:
    def score(item):
        u, t = item
        s = (u + " " + t).lower()
        if "contact" in s:
            return 0
        if "about" in s:
            return 1
        if any(w in s for w in ("reach", "find", "touch", "location", "visit", "connect")):
            return 2
        return 3
    return [u for u, _ in sorted(links, key=score)[:limit]]
