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
