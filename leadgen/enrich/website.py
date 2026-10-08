"""Website agent: crawl a business's own site (homepage + a few contact/about pages) for contact routes."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..net import BreakerOpen, DeadlineReached, FetchError, Http, NetworkDown
from ..quality import is_aggregator, is_link_hub, name_score
from ..util import get_logger
from .extract import Found, ad_signals, canonical_social, extract_page, host_of, rank_contact_links, registrable

log = get_logger("website")

# Expired business domains are often taken over by gambling/spam sites while Google Maps still
# lists them. Such a site's phones and social links belong to the spammer, not the business.
SPAM_MARKERS = re.compile(
    r"\b(slot\s?gacor|situs\s+slot|slot\s+online|judi\s+online|togel|sbobet|casino\s+online|online\s+casino|"
    r"rtp\s+slot|agen\s+slot|bandar\s+(?:togel|slot|judi)|poker\s+online|maxwin|slot88|slot777|scatter\s+hitam|"
    r"sports?\s+betting|bet365|1xbet|satta\s+matka|link\s+alternatif)\b", re.I)
TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|gclid|fbclid|gbraid|wbraid|msclkid|srsltid|_ga|mc_[a-z]+|ref|igshid)$", re.I)
# Paths most small-business sites use for contact details, tried directly if not linked with obvious text.
COMMON_CONTACT_PATHS = ["/contact", "/contact-us", "/contactus", "/contact-us/", "/about", "/about-us", "/reach-us",
                        "/get-in-touch", "/connect", "/enquiry", "/reach-us/"]
# Deep crawl (e-mail hunt): policy pages very often carry the business's e-mail ("write to us at ...").
POLICY_PATHS = ["/privacy-policy", "/privacy", "/terms-and-conditions", "/terms", "/refund-policy", "/disclaimer"]
POLICY_LINK_WORDS = re.compile(r"privacy|terms|policy|polic|legal|disclaimer|refund|cancellation|imprint", re.I)
SITEMAP_PAGE_WORDS = re.compile(r"contact|about|reach|enquir|inquir|privacy|terms|policy|location|support|connect|touch", re.I)


@dataclass
class Contact:
    kind: str
    value: str
    source: str            # website | jsonld | instagram | search | google_maps | places_api | osm
    source_url: str
    confidence: str        # high | medium | low
    label: str = ""
    evidence: str = ""


@dataclass
class SiteResult:
    status: str                     # ok | skipped | blocked_robots | error | social | aggregator | hijacked | moved
    contacts: list[Contact] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    description: str = ""
    title: str = ""
    name_match: float = 0.0
    error: str = ""
    final_url: str = ""
    owned: bool = True     # False: nothing ties the site to this business (contacts kept as unverified)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", query, ""))


def _confidence(f: Found, name_ok: bool, multi_location: bool) -> str:
    strong = f.how in ("mailto", "tel", "wa-link", "jsonld", "cfemail", "link", "microdata")
    if strong and name_ok and not multi_location:
        return "high"
    if strong or (f.how == "text" and name_ok and not multi_location):
        return "medium"
    return "low"


def url_variants(url: str) -> list[str]:
    """Other addresses of the same site to try when the listed one does not answer: http/https and with or
    without www (small-business sites often have a broken certificate or only one of the two names)."""
    parts = urlsplit(url)
    host = parts.netloc
    hosts = [host[4:]] if host.startswith("www.") else ["www." + host]
    out = []
    for scheme in ("https", "http"):
        for h in [host] + hosts:
            v = urlunsplit((scheme, h, parts.path or "/", parts.query, ""))
            if v != url and v not in out:
                out.append(v)
    return out


def crawl_site(http: Http, url: str, business_name: str, *, max_pages: int = 4, region: str = "IN",
               interval: float = 2.0, known_phones: tuple | list = (), deep: bool = False, variants: bool = False) -> SiteResult:
    """known_phones: the business's phone numbers from its listing (E.164). A site whose name does
    not match the business still counts as its own when it shows one of these numbers.
    deep: also read policy pages and the sitemap's contact pages (more pages - for the e-mail hunt).
    variants: when the site does not answer, try it over http/https and with/without www."""
    url = normalize_url(url)
    if not url:
        return SiteResult(status="skipped", error="no usable URL")
    res = _crawl(http, url, business_name, max_pages=max_pages, region=region, interval=interval,
                 known_phones=known_phones, deep=deep)
    if variants and res.status == "error" and not res.pages:
        for alt in url_variants(url):
            alt_res = _crawl(http, alt, business_name, max_pages=max_pages, region=region, interval=interval,
                             known_phones=known_phones, deep=deep)
            if alt_res.pages or alt_res.status not in ("error",):
                alt_res.error = (alt_res.error + f" (reached as {alt})").strip()
                return alt_res
    return res


def _sitemap_pages(http: Http, root: str, limit: int = 4) -> list[str]:
    """Contact/about/policy pages listed in the site's sitemap (robots-permitted, one or two small fetches)."""
    out: list[str] = []
    for path in ("/sitemap.xml", "/wp-sitemap.xml", "/sitemap_index.xml"):
        try:
            allowed, _ = http.robots_allowed(root + path)
            if not allowed:
                return out
            r = http.get(root + path, timeout=15, max_bytes=2_000_000, retries=0, browser=True)
        except (NetworkDown, DeadlineReached):
            raise
        except (FetchError, BreakerOpen):
            continue
        if r.status >= 400 or "<loc>" not in r.text:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)[:2000]
        pages = [u for u in locs if not u.endswith(".xml") and registrable(host_of(u)) == registrable(host_of(root))]
        subs = [u for u in locs if u.endswith(".xml")][:3]
        if not pages and subs:  # a sitemap index: read the page sitemap(s)
            for sub in subs:
                try:
                    r2 = http.get(sub, timeout=15, max_bytes=2_000_000, retries=0, browser=True)
                except (NetworkDown, DeadlineReached):
                    raise
                except (FetchError, BreakerOpen):
                    continue
                pages += [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r2.text)[:2000]
                          if not u.endswith(".xml") and registrable(host_of(u)) == registrable(host_of(root))]
        for u in pages:
            if SITEMAP_PAGE_WORDS.search(urlsplit(u).path) and u not in out:
                out.append(u)
            if len(out) >= limit:
                break
        break
    return out


def _crawl(http: Http, url: str, business_name: str, *, max_pages: int, region: str, interval: float,
           known_phones: tuple | list, deep: bool) -> SiteResult:
    social = canonical_social(url)
    if social:
        return SiteResult(status="social", contacts=[Contact(social[0], social[1], "google_maps", url, "high", "listed as website")])
    if is_aggregator(url) and not is_link_hub(url):
        return SiteResult(status="aggregator", error=f"listing site ({host_of(url)}), not the business's own website")

    res = SiteResult(status="ok")
    found: dict[tuple[str, str], tuple[Found, str]] = {}
    queue = [url]
    visited: set[str] = set()
    site_host = host_of(url)
    titles = []
    multi_phone_pages = 0
    signals: dict[str, str] = {}
    attempts = 0
    while queue and len(res.pages) < max_pages and attempts < max_pages * 3:
        page = queue.pop(0)
        attempts += 1
        if page in visited:
            continue
        visited.add(page)
        try:
            allowed, delay = http.robots_allowed(page)
        except NetworkDown:
            raise
        if not allowed:
            if not res.pages:
                why = http.robots_reason(page) if hasattr(http, "robots_reason") else ""
                if why and why not in ("disallowed", "ok"):
                    # robots.txt could not be read (server error/timeout): not a refusal - try again later.
                    res.status = "error"
                    res.error = why
                else:
                    res.status = "blocked_robots"
                    res.error = "robots.txt disallows crawling"
                return res
            continue
        if delay > 10:
            res.status = "skipped"
            res.error = f"robots.txt crawl-delay {delay:.0f}s too long"
            return res
        try:
            r = http.get(page, timeout=20, max_bytes=2_500_000, retries=1, interval=max(interval, delay))
        except (NetworkDown, DeadlineReached):
            raise
        except (FetchError, BreakerOpen) as exc:
            if not res.pages:
                res.status = "error"
                res.error = f"{type(exc).__name__}: {exc}"[:200]
                return res
            continue
        if r.status >= 400 or ("html" not in r.content_type and r.content_type not in ("", "text/plain")):
            if not res.pages:
                res.status = "error"
                res.error = f"HTTP {r.status} {r.content_type}"
                return res
            continue
        final_host = host_of(r.url)
        if not res.pages:
            res.final_url = r.url
            # Redirected to a social profile or a listing site?
            s = canonical_social(r.url)
            if s:
                return SiteResult(status="social", contacts=[Contact(s[0], s[1], "website", url, "high", "website redirects here")])
            if is_aggregator(r.url) and not is_link_hub(r.url):
                return SiteResult(status="aggregator", error=f"website redirects to listing site {final_host}")
            if registrable(final_host) != registrable(site_host):
                site_host = final_host
        elif registrable(final_host) != registrable(site_host):
            continue  # left the site
        is_contact_page = len(res.pages) > 0
        if not res.pages:
            head = r.text[:150_000]
            title_m = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
            title_spam = bool(title_m and SPAM_MARKERS.search(title_m.group(1)))
            if title_spam or len({m.group(0).lower() for m in SPAM_MARKERS.finditer(head)}) >= 2:
                return SiteResult(status="hijacked", final_url=r.url,
                                  error="the listed website now shows unrelated gambling/spam content")
        pe = extract_page(r.text, r.url, region=region, contact_page=is_contact_page)
        for label in ad_signals(r.text):
            signals.setdefault(label, r.url)
        res.pages.append(r.url)
        if pe.redirect_to_social:
            k, v = canonical_social(pe.redirect_to_social)
            found.setdefault((k, v), (Found(k, v, "link"), r.url))
        if len(res.pages) == 1:
            res.title = pe.title
            res.description = pe.description or ""
            titles = [pe.title, pe.site_name, *pe.jsonld_names]
            root = f"{urlsplit(r.url).scheme}://{urlsplit(r.url).netloc}"
            ranked = rank_contact_links(pe.internal_links, max_pages - 1)
            # Also try the usual contact/about paths directly - many sites don't link them with obvious text.
            common = [root + c for c in COMMON_CONTACT_PATHS]
            extra: list[str] = []
            if deep:
                policy_links = [u for u, t in pe.policy_links]
                extra = _sitemap_pages(http, root) + policy_links[:3] + [root + c for c in POLICY_PATHS]
            for link in ranked + [c for c in common if c not in ranked] + extra:
                if link not in visited and link not in queue:
                    queue.append(link)
        phones_here = {f.value for f in pe.found if f.kind == "phone"}
        if len(phones_here) >= 5:
            multi_phone_pages += 1
        for f in pe.found:
            found.setdefault((f.kind, f.value), (f, r.url))

    # Does this site look like it belongs to the business?
    domain_core = registrable(site_host).split(".")[0]
    res.name_match = max([name_score(business_name, t) for t in titles if t] + [name_score(business_name, "", handle=domain_core)])
    name_ok = res.name_match >= 0.6
    distinct_phones = {v for (k, v) in found if k == "phone"}
    on_site_numbers = {v for (k, v) in found if k in ("phone", "whatsapp")}
    res.owned = name_ok or bool(on_site_numbers & set(known_phones or ()))
    if not res.owned and res.final_url and registrable(host_of(res.final_url)) != registrable(host_of(url)):
        # The listed address now forwards to an unrelated site (expired or taken-over domain).
        return SiteResult(status="moved", final_url=res.final_url, pages=res.pages, name_match=res.name_match,
                          error=f"the listed website now forwards to an unrelated site ({host_of(res.final_url)})")
    if not res.owned:
        res.description = ""    # e.g. a parent company's site: its description is not this business's
    multi_location = multi_phone_pages > 0 or len(distinct_phones) >= 6
    home_cc = _country_prefix(region)
    for (kind, value), (f, page_url) in found.items():
        conf = _confidence(f, name_ok, multi_location)
        label = f.label
        if not res.owned:
            conf, label = "low", (label + ",site may belong to another business").strip(",")
        if kind in ("phone", "whatsapp") and home_cc and not value.startswith(home_cc):
            conf, label = "low", (label + ",foreign number").strip(",")
        if kind == "email":
            from .emails import email_label, related_email, suspicious_email

            label = email_label(value, registrable(site_host))
            if not res.owned:
                label = (label + ",site may belong to another business").strip(",")
            elif conf == "low" and f.how in ("text", "script") and not multi_location:
                # Plain text on the business's own site: theirs when the address carries the site's domain
                # or name (a supplier's or web designer's address does not).
                tie = related_email(value, business_name, site_host)
                if tie:
                    conf, label = "medium", (label + "," + tie).strip(",")
            why = suspicious_email(value)
            if why:
                # Published like this on the site, but probably undeliverable: keep it, as unverified.
                conf, label = "low", (label + "," + why).strip(",")
        if multi_location and kind in ("phone", "whatsapp"):
            label = (label + ",multi-location site").strip(",")
        res.contacts.append(Contact(kind, value, "jsonld" if f.how == "jsonld" else "website", page_url, conf, label,
                                    f"{f.how}: {f.snippet}"[:200] if f.snippet else f.how))
    if res.owned:
        for label, page_url in signals.items():
            res.contacts.append(Contact("signal", label, "website", page_url, "high", "", "ad tracking code on the website"))
    if not res.pages:
        res.status = "error"
        res.error = res.error or "no pages fetched"
    return res


def _country_prefix(region: str) -> str:
    try:
        import phonenumbers

        cc = phonenumbers.country_code_for_region((region or "").upper())
        return f"+{cc}" if cc else ""
    except Exception:  # noqa: BLE001
        return ""
