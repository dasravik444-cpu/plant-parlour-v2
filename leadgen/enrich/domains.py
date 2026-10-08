"""Find the own website of a business whose listing shows none.

The obvious addresses are checked (its name + .com / .in / .co.in, with and without "kolkata"). A site is
accepted only when it proves it is this business: it shows the business's own phone number, or its exact
name together with its PIN code. Anything less is ignored - no website or e-mail is ever guessed into the
sheet. Domains are checked with one DNS lookup first, so a name with no website costs almost nothing."""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass, field

from ..net import BreakerOpen, DeadlineReached, FetchError, Http, NetworkDown
from ..quality import is_aggregator, name_score, tokens
from .extract import canonical_social, extract_page, host_of, rank_contact_links, registrable

TLDS = (".com", ".in", ".co.in")
# Words that are part of how a business is listed but rarely of its domain name.
DROP_WORDS = {"pvt", "private", "ltd", "limited", "llp", "the", "and", "n", "amp", "co", "company", "kolkata", "calcutta",
              "india", "branch", "unit", "of", "a", "an", "at", "in", "by"}
PARKED = re.compile(r"domain (?:name )?(?:is|may be) for sale|buy this domain|this domain is parked|parked (?:free|domain)|"
                    r"sedoparking|parkingcrew|bodis\.com|hugedomains|afternic|dan\.com|domain has expired|"
                    r"account (?:has been )?suspended|welcome to nginx|apache2? (?:ubuntu )?default page|it works!|"
                    r"default web site page|index of /|future home of something quite cool|website coming soon|"
                    r"this site can.t be reached|domain not configured|plesk|cpanel", re.I)
PIN_RE = re.compile(r"\b7\d{2}\s?\d{3}\b")      # West Bengal PIN codes 7xxxxx


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")


def name_slugs(name: str, city: str = "kolkata") -> list[str]:
    """Domain labels a business with this name would plausibly use, most likely first."""
    base = re.split(r"\s[|\-–—]\s|\(|,", _ascii(name))[0]       # "Evokes SPA | New Market" -> "Evokes SPA"
    words = [w for w in tokens(base) if re.fullmatch(r"[a-z0-9]+", w)]
    core = [w for w in words if w not in DROP_WORDS]
    if not core:
        return []
    out: list[str] = []

    def add(label: str):
        label = re.sub(r"[^a-z0-9]", "", label)
        if 4 <= len(label) <= 40 and label not in out:
            out.append(label)

    add("".join(core))
    add("".join(w for w in words if w not in ("the", "and", "n", "amp", "of")))     # "The Bhoj Company" -> bhojcompany
    if words and words[0] == "the":
        add("the" + "".join(core))
    add("".join(core) + city)
    if len(core) > 2:
        add("".join(core[:2]))
    if len(core) >= 2:
        add("-".join(core))
    return out[:5]


def candidate_domains(name: str, city: str = "kolkata", extra_labels: tuple = ()) -> list[str]:
    labels = list(dict.fromkeys([*name_slugs(name, city), *[re.sub(r"[^a-z0-9\-]", "", x.lower()) for x in extra_labels if x]]))
    return [lab + tld for lab in labels if len(lab) >= 4 for tld in TLDS][:15]


class Resolver:
    """Cached 'does this domain exist?' check (DNS A/AAAA). Unknown answers count as 'no'."""

    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout
        self._cache: dict[str, bool] = {}
        self._lock = threading.Lock()

    def exists(self, domain: str) -> bool:
        with self._lock:
            if domain in self._cache:
                return self._cache[domain]
        ok = False
        try:
            import dns.resolver

            r = dns.resolver.Resolver()
            r.lifetime = self.timeout
            for rtype in ("A", "AAAA"):
                try:
                    if len(r.resolve(domain, rtype)) > 0:
                        ok = True
                        break
                except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
                    continue
        except Exception:  # noqa: BLE001 - NXDOMAIN, timeouts: treat as no website
            ok = False
        with self._lock:
            self._cache[domain] = ok
        return ok


@dataclass
class Discovery:
    url: str = ""
    how: str = ""                       # why the site is accepted as theirs
    tried: int = 0                      # candidate domains looked up
    existing: int = 0                   # of which exist
    fetched: int = 0                    # homepages read
    rejected: list[str] = field(default_factory=list)   # "domain: reason" (for the maintainer's sample)


def _digits(e164: str) -> str:
    return re.sub(r"\D", "", e164 or "")


def _phone_match(site_numbers: set[str], phones: list[str]) -> bool:
    """Same number, written with or without country/STD code."""
    want = {_digits(p)[-10:] for p in phones if len(_digits(p)) >= 10}
    return any(_digits(n)[-10:] in want for n in site_numbers)


def discover_website(http: Http, name: str, phones: list[str], address: str = "", *, region: str = "IN",
                     city: str = "kolkata", resolver: Resolver | None = None, max_fetch: int = 4,
                     extra_labels: tuple = ()) -> Discovery:
    resolver = resolver or Resolver()
    d = Discovery()
    pins = set(PIN_RE.findall(address or ""))
    for dom in candidate_domains(name, city, extra_labels):
        d.tried += 1
        if not resolver.exists(dom):
            continue
        d.existing += 1
        if d.fetched >= max_fetch:
            break
        page_found = None
        for url in (f"https://{dom}/", f"http://{dom}/"):
            try:
                allowed, _ = http.robots_allowed(url)
                if not allowed:
                    d.rejected.append(f"{dom}: robots.txt")
                    break
                r = http.get(url, timeout=15, max_bytes=1_500_000, retries=0)
            except (NetworkDown, DeadlineReached):
                raise
            except (FetchError, BreakerOpen):
                continue
            d.fetched += 1
            if r.status < 400 and "html" in (r.content_type or "text/html"):
                page_found = r
            break
        if page_found is None:
            continue
        r = page_found
        final_host = host_of(r.url)
        if is_aggregator(r.url) or canonical_social(r.url) or registrable(final_host) != registrable(dom):
            d.rejected.append(f"{dom}: redirects elsewhere")
            continue
        if PARKED.search(r.text[:200_000]) and len(r.text) < 60_000:
            d.rejected.append(f"{dom}: parked/empty")
            continue
        pe = extract_page(r.text, r.url, region=region, contact_page=True)
        numbers = {f.value for f in pe.found if f.kind in ("phone", "whatsapp")}
        names = [x for x in (pe.title, pe.site_name, *pe.jsonld_names) if x]
        nscore = max([name_score(name, x) for x in names] + [0.0])
        if numbers and _phone_match(numbers, phones):
            d.url, d.how = r.url, "the site shows the business's phone number"
            return d
        # The number is often only on the contact page: read it when the name fits.
        if nscore >= 0.6 and pe.internal_links:
            link = rank_contact_links(pe.internal_links, 1)[0]
            try:
                allowed, _ = http.robots_allowed(link)
                r2 = http.get(link, timeout=15, max_bytes=1_500_000, retries=0) if allowed else None
            except (NetworkDown, DeadlineReached):
                raise
            except (FetchError, BreakerOpen):
                r2 = None
            if r2 is not None and r2.status < 400:
                d.fetched += 1
                pe2 = extract_page(r2.text, r2.url, region=region, contact_page=True)
                numbers2 = {f.value for f in pe2.found if f.kind in ("phone", "whatsapp")}
                if numbers2 and _phone_match(numbers2, phones):
                    d.url, d.how = r.url, "the site's contact page shows the business's phone number"
                    return d
                if nscore >= 0.85 and pins and pins & set(PIN_RE.findall(r2.text)):
                    d.url, d.how = r.url, "same name and PIN code"
                    return d
        if nscore >= 0.85 and pins and pins & set(PIN_RE.findall(r.text)):
            d.url, d.how = r.url, "same name and PIN code"
            return d
        d.rejected.append(f"{dom}: not proven theirs (name match {nscore:.2f}, {len(numbers)} other numbers)")
    return d
