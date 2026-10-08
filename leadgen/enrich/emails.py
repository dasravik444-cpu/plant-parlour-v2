"""Email extraction (incl. Cloudflare-protected and [at]/[dot] obfuscated forms) and validation."""
from __future__ import annotations

import re
import threading
from urllib.parse import unquote

EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,24})(?![A-Za-z0-9\-])")
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]{1,64})\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+)\s*([A-Za-z0-9\-]{1,63})\s*"
    r"(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+)\s*([A-Za-z]{2,24}(?:\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+)\s*[A-Za-z]{2,24})?)",
    re.I)

FREE_PROVIDERS = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com", "rediffmail.com",
                  "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
                  "proton.me", "zoho.com", "zohomail.in", "mail.com", "gmx.com", "yandex.com", "rocketmail.com"}

# Domains that appear in page source but never belong to the business itself.
JUNK_DOMAINS = {"example.com", "example.org", "domain.com", "email.com", "yourdomain.com", "yoursite.com", "website.com",
                "sentry.io", "sentry-next.wixpress.com", "wixpress.com", "wix.com", "godaddy.com", "squarespace.com",
                "wordpress.com", "wordpress.org", "w3.org", "schema.org", "jquery.com", "cloudflare.com", "google.com",
                "googleapis.com", "gstatic.com", "facebook.com", "instagram.com", "twitter.com", "zomato.com", "swiggy.in",
                "swiggy.com", "magicpin.in", "justdial.com", "dineout.co.in", "eazydiner.com", "tripadvisor.com",
                "booking.com", "makemytrip.com", "goibibo.com", "sulekha.com", "indiamart.com", "weddingz.in",
                "wedmegood.com", "shaadisaga.com", "zoho.in", "mailchimp.com", "sendgrid.net", "amazonaws.com",
                "github.com", "gravatar.com", "shopify.com", "myshopify.com", "test.com", "company.com", "mysite.com",
                "sitename.com", "address.com", "mail.ru", "local", "localhost"}
JUNK_LOCAL = {"you", "your", "yourname", "name", "user", "username", "email", "example", "test", "someone", "john.doe",
              "johndoe", "firstname.lastname", "first.last", "noreply", "no-reply", "donotreply", "do-not-reply"}
ASSET_SUFFIX = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|bmp|css|js|map|mp4|webm|woff2?|ttf|eot|pdf)$", re.I)
ROLE_LOCALS = {"info", "contact", "hello", "enquiry", "enquiries", "inquiry", "sales", "support", "admin", "office",
               "booking", "bookings", "reservations", "reservation", "events", "marketing", "hr", "careers", "accounts",
               "mail", "team", "help", "care", "customercare", "feedback"}


def decode_cfemail(hexstr: str) -> str | None:
    """Decode Cloudflare email protection (data-cfemail / #hex)."""
    try:
        data = bytes.fromhex(hexstr.strip())
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    try:
        return bytes(b ^ key for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def normalize_email(raw: str) -> str | None:
    if not raw:
        return None
    e = unquote(raw).strip().strip(".,;:()[]<>\"'").lower()
    if e.startswith("mailto:"):
        e = e[7:]
    e = e.split("?")[0].strip()
    m = EMAIL_RE.fullmatch(e)
    if not m:
        return None
    local, _, domain = e.rpartition("@")
    if ASSET_SUFFIX.search(e) or "@2x" in e or "@3x" in e:
        return None
    if domain in JUNK_DOMAINS or any(domain.endswith("." + j) for j in JUNK_DOMAINS if "." in j):
        return None
    if local in JUNK_LOCAL or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    if re.fullmatch(r"[0-9a-f]{16,}", local):  # hashed tracking ids
        return None
    tld = domain.rsplit(".", 1)[-1]
    if not tld.isalpha():
        return None
    return e


def suspicious_email(email: str) -> str:
    """Reason an address as written is probably a typo (kept, but marked unverified)."""
    local, _, domain = email.partition("@")
    if domain.startswith("www."):
        return "suspicious: 'www.' inside the address"
    if re.search(r"\.(con|cmo|comm|om|cm|co\.on|inn)$", domain):
        return "suspicious: misspelt domain ending"
    if domain.count(".") >= 4:
        return "suspicious: unusual domain"
    return ""


def email_label(email: str, site_domain: str = "") -> str:
    local, _, domain = email.partition("@")
    labels = []
    if domain in FREE_PROVIDERS:
        labels.append("free-mail")
    elif site_domain and not (domain == site_domain or domain.endswith("." + site_domain) or site_domain.endswith("." + domain)):
        labels.append("other-domain")
    if local in ROLE_LOCALS:
        labels.append("role")
    return ",".join(labels)


# Free website builders: the business's name is the sub-domain (dsmscompany.wixsite.com), not the domain.
HOSTING_PLATFORMS = {"business.site", "wixsite.com", "wix.com", "blogspot.com", "blogspot.in", "wordpress.com", "weebly.com",
                     "godaddysites.com", "site123.me", "jimdosite.com", "jimdofree.com", "webnode.com", "webnode.in",
                     "square.site", "mystrikingly.com", "strikingly.com", "carrd.co", "github.io", "netlify.app",
                     "vercel.app", "web.app", "firebaseapp.com", "000webhostapp.com", "tumblr.com", "sites.google.com",
                     "yolasite.com", "ueniweb.com", "ueni.com", "zohosites.com", "zohosites.in", "dorik.io", "framer.website",
                     "framer.ai", "notion.site", "myshopify.com", "mydukaan.io", "dukaan.app", "instamojo.com", "bikayi.com",
                     "canva.site", "my.canva.site", "hostingersite.com", "wpcomstaging.com", "squarespace.com", "webflow.io",
                     "business.page", "negocio.site", "blogspot.co.uk", "edublogs.org"}


def platform_of(host: str) -> str:
    """The website builder a host is a sub-domain of ('' for an own domain)."""
    host = (host or "").lower()
    host = host[4:] if host.startswith("www.") else host
    for plat in HOSTING_PLATFORMS:
        if host == plat or host.endswith("." + plat):
            return plat
    return ""


def site_label(host: str) -> str:
    """The part of a website's host that names the business: 'leafcafe' for leafcafe.in, 'dsmscompany' for
    dsmscompany.wixsite.com ('' when the host is the platform itself)."""
    host = (host or "").lower()
    host = host[4:] if host.startswith("www.") else host
    plat = platform_of(host)
    if plat:
        return host[: -len(plat) - 1].split(".")[-1] if host != plat else ""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "org", "net", "gov", "ac", "edu", "com", "res", "gen", "firm", "ind") and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else host


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def related_email(email: str, business_name: str, site_host: str) -> str:
    """What ties an address found as plain text on a business's own website to that business, or '' if
    nothing does (it may then be a supplier's, a partner's or a web designer's address)."""
    from ..quality import _is_common, distinctive_tokens, tokens

    local, _, domain = email.partition("@")
    host = (site_host or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if not platform_of(host) and host:
        parts = host.split(".")
        reg = ".".join(parts[-3:]) if (len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in
                                       ("co", "org", "net", "gov", "ac", "edu", "com", "res", "gen", "firm", "ind")) else ".".join(parts[-2:])
        if domain == reg or domain.endswith("." + reg):
            return "same domain as the website"
    label = _alnum(site_label(host))
    # Only unusual words of the name tie an address to it: "metro" in metrobrands.com says nothing about
    # "Metro Restaurant". The whole name joined up ("universalnursery") always does.
    words = [_alnum(t) for t in distinctive_tokens(business_name) if len(_alnum(t)) >= 4 and not _is_common(t)]
    joined = _alnum("".join(tokens(business_name)))
    parts = [_alnum(local)]
    if domain not in FREE_PROVIDERS:
        parts.append(_alnum(domain.split(".")[0]))
    for part in parts:
        if len(part) < 4:
            continue
        if len(label) >= 4 and (label in part or (len(part) >= 6 and part in label)):
            return "matches the website's name"
        if any(w in part for w in words) or (len(joined) >= 6 and joined in part):
            return "contains the business name"
        if len(part) >= 6 and part in joined:
            return "contains the business name"
    return ""


def find_emails_in_text(text: str) -> list[str]:
    out, seen = [], set()
    for m in EMAIL_RE.finditer(text or ""):
        e = normalize_email(m.group(1))
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    for m in OBFUSCATED_RE.finditer(text or ""):
        tail = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+)\s*", ".", m.group(3), flags=re.I)
        e = normalize_email(f"{m.group(1)}@{m.group(2)}.{tail}")
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


class MXChecker:
    """Cached DNS MX lookup (no SMTP probing). Unknown/unavailable DNS never drops an email."""

    def __init__(self, enabled: bool = True, timeout: float = 4.0):
        self.enabled = enabled
        self.timeout = timeout
        self._cache: dict[str, bool | None] = {}
        self._lock = threading.Lock()
        try:
            import dns.resolver  # noqa: F401

            self._available = True
        except Exception:  # pragma: no cover
            self._available = False

    def has_mx(self, domain: str) -> bool | None:
        """True = mail servers exist; False = domain definitely cannot receive mail; None = unknown."""
        if not (self.enabled and self._available) or not domain:
            return None
        with self._lock:
            if domain in self._cache:
                return self._cache[domain]
        result: bool | None
        try:
            import dns.resolver

            resolver = dns.resolver.Resolver()
            resolver.lifetime = self.timeout
            try:
                answers = resolver.resolve(domain, "MX")
                result = len(answers) > 0
            except dns.resolver.NoAnswer:
                try:  # RFC 5321: fall back to an A record
                    resolver.resolve(domain, "A")
                    result = True
                except Exception:
                    result = False
            except dns.resolver.NXDOMAIN:
                result = False
        except Exception:
            result = None
        with self._lock:
            self._cache[domain] = result
        return result
