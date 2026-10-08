"""HTTP layer shared by every agent.

Design rules (learned from the old systems' failures):
  * exactly ONE retry owner: a request is retried at most `retries` times here,
    with short bounded waits. Longer back-off is the job queue's business, so a
    flaky host can never stall a whole run for half an hour;
  * every request has a timeout and a response-size cap;
  * per-host pacing keeps us polite and is thread-safe;
  * per-service circuit breakers stop hammering a service that is blocking us;
  * failures are classified so callers can decide: NetworkDown (our own
    connection is gone - do not burn task attempts), Blocked (service refuses
    us), Transient (try later), Permanent (do not retry).
"""
from __future__ import annotations

import random
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .util import get_logger

log = get_logger("net")

try:  # Browser-grade TLS fingerprint; optional (not available on every Android setup).
    from curl_cffi import requests as _creq  # type: ignore

    HAVE_CURL_CFFI = True
except Exception:  # pragma: no cover - optional dependency
    _creq = None
    HAVE_CURL_CFFI = False

import requests as _requests

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


class FetchError(Exception):
    kind = "error"

    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class NetworkDown(FetchError):
    kind = "network_down"


class Blocked(FetchError):
    kind = "blocked"


class Transient(FetchError):
    kind = "transient"


class Permanent(FetchError):
    kind = "permanent"


class DeadlineReached(FetchError):
    kind = "deadline"


class BreakerOpen(FetchError):
    kind = "breaker_open"


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    content: bytes
    elapsed: float
    truncated: bool = False
    _text: str | None = field(default=None, repr=False)

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = decode_body(self.content, self.headers.get("content-type", ""))
        return self._text

    def json(self):
        import json

        return json.loads(self.text)


_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([a-zA-Z0-9_\-]+)""", re.I)


def decode_body(content: bytes, content_type: str) -> str:
    charset = None
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    else:
        m2 = _CHARSET_RE.search(content[:4096])
        if m2:
            charset = m2.group(1).decode("ascii", "ignore")
    for cs in (charset, "utf-8"):
        if not cs:
            continue
        try:
            return content.decode(cs, "replace")
        except LookupError:
            continue
    return content.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------
class Breaker:
    """Opens after `threshold` consecutive failures; stays open for `cooldown` seconds."""

    def __init__(self, name: str, threshold: int = 3, cooldown: float = 1800.0):
        self.name, self.threshold, self.cooldown = name, threshold, cooldown
        self.failures = 0
        self.open_until = 0.0
        self.total_failures = 0
        self.total_success = 0
        self.last_error = ""
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            return time.time() < self.open_until

    def success(self):
        with self._lock:
            self.failures = 0
            self.total_success += 1

    def failure(self, error: str = ""):
        with self._lock:
            self.failures += 1
            self.total_failures += 1
            self.last_error = error[:200]
            if self.failures >= self.threshold:
                self.open_until = time.time() + self.cooldown
                log.warning("circuit breaker OPEN for %s (%s consecutive failures; last: %s)", self.name, self.failures, self.last_error)

    def trip(self, error: str = ""):
        """Open immediately (used when a single refusal is conclusive, e.g. a login wall)."""
        with self._lock:
            self.failures = max(self.failures, self.threshold)
            self.last_error = error[:200] or self.last_error
            if time.time() >= self.open_until:
                self.total_failures += 1
                log.warning("circuit breaker OPEN for %s (%s)", self.name, self.last_error)
            self.open_until = time.time() + self.cooldown

    def snapshot(self) -> dict:
        return {"open": self.is_open(), "ok": self.total_success, "failed": self.total_failures, "last_error": self.last_error}


class _Pacer:
    def __init__(self):
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def reserve(self, key: str, interval: float) -> float:
        """Reserve the next slot for `key`; returns the time.time() at which the caller may send."""
        with self._lock:
            now = time.time()
            slot = max(now, self._next.get(key, 0.0))
            self._next[key] = slot + interval
            return slot


# --------------------------------------------------------------------------
# Main client
# --------------------------------------------------------------------------
class Http:
    def __init__(self, *, use_curl_cffi: bool = True, impersonate: str = "chrome", deadline: float | None = None,
                 default_interval: float = 2.0, max_bytes: int = 3_000_000, user_agent: str | None = None,
                 robot_name: str = "*"):
        """user_agent/robot_name: crawl openly as a named bot (open-data mode) instead of as a browser;
        robots.txt rules are then read for that bot name."""
        self.user_agent = user_agent
        self.robot_name = robot_name
        self.use_curl = bool(use_curl_cffi and HAVE_CURL_CFFI)
        self.impersonate = impersonate
        self.deadline = deadline
        self.default_interval = default_interval
        self.max_bytes = max_bytes
        self._tls = threading.local()
        self._pacer = _Pacer()
        self.breakers: dict[str, Breaker] = {}
        self._breaker_lock = threading.Lock()
        self._net_check = (0.0, True)
        self._net_lock = threading.Lock()
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_reason: dict[str, str] = {}
        self._robots_lock = threading.Lock()
        self.stats = {"requests": 0, "bytes": 0, "errors": 0}
        self._stats_lock = threading.Lock()

    # -- sessions -------------------------------------------------------------
    def _session(self, browser: bool):
        attr = "curl" if (browser and self.use_curl) else "plain"
        s = getattr(self._tls, attr, None)
        if s is None:
            if attr == "curl":
                s = _creq.Session(impersonate=self.impersonate)
            else:
                s = _requests.Session()
                adapter = _requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
                s.mount("http://", adapter)
                s.mount("https://", adapter)
            setattr(self._tls, attr, s)
        return s

    def breaker(self, service: str, threshold: int = 3, cooldown: float = 1800.0) -> Breaker:
        with self._breaker_lock:
            b = self.breakers.get(service)
            if b is None:
                b = self.breakers[service] = Breaker(service, threshold, cooldown)
            return b

    def time_left(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - time.time()

    # -- connectivity ---------------------------------------------------------
    def network_ok(self) -> bool:
        """Cheap check that *our* connection works (cached 30 s)."""
        with self._net_lock:
            checked_at, ok = self._net_check
            if time.time() - checked_at < 30:
                return ok
        ok = False
        for host in ("www.google.com", "www.cloudflare.com", "github.com"):
            try:
                socket.getaddrinfo(host, 443)
                ok = True
                break
            except OSError:
                continue
        with self._net_lock:
            self._net_check = (time.time(), ok)
        return ok

    # -- request --------------------------------------------------------------
    def request(self, method: str, url: str, *, service: str | None = None, headers: dict | None = None,
                params: dict | None = None, data=None, json_body=None, timeout: float = 25.0,
                max_bytes: int | None = None, browser: bool = True, interval: float | None = None,
                jitter: float = 0.0, retries: int = 1, allow_redirects: bool = True,
                block_statuses: tuple = (429,), cookies: dict | None = None) -> Response:
        if service:
            br = self.breaker(service)
            if br.is_open():
                raise BreakerOpen(f"{service} temporarily disabled after repeated failures", url=url)
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            raise Permanent("invalid URL", url=url)
        max_bytes = max_bytes or self.max_bytes
        hdrs = dict(BROWSER_HEADERS)
        if self.user_agent:
            hdrs["User-Agent"] = self.user_agent
        if headers:
            hdrs.update(headers)
        attempt = 0
        while True:
            attempt += 1
            pace = self.default_interval if interval is None else interval
            slot = self._pacer.reserve(service or host, pace + (random.uniform(0, jitter) if jitter else 0.0))
            wait = slot - time.time()
            if wait > 0:
                if self.deadline is not None and time.time() + wait > self.deadline:
                    raise DeadlineReached("run deadline reached while waiting for politeness slot", url=url)
                time.sleep(wait)
            try:
                resp = self._do(method, url, hdrs, params, data, json_body, timeout, max_bytes, browser, allow_redirects, cookies)
            except FetchError as exc:
                retry = isinstance(exc, Transient) and attempt <= retries
                if isinstance(exc, (NetworkDown,)):
                    raise
                if not retry:
                    if service and isinstance(exc, (Transient, Blocked)):
                        self.breaker(service).failure(str(exc))
                    with self._stats_lock:
                        self.stats["errors"] += 1
                    raise
                time.sleep(min(6.0, 1.5 * attempt) + random.uniform(0, 1))
                continue
            status = resp.status
            if status in block_statuses or (status == 403 and service):
                exc = Blocked(f"HTTP {status}", status=status, url=url)
                if service:
                    self.breaker(service).failure(str(exc))
                raise exc
            if status >= 500:
                if attempt <= retries:
                    time.sleep(min(6.0, 2.0 * attempt) + random.uniform(0, 1))
                    continue
                if service:
                    self.breaker(service).failure(f"HTTP {status}")
                raise Transient(f"HTTP {status}", status=status, url=url)
            if service:
                self.breaker(service).success()
            return resp

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Response:
        return self.request("POST", url, **kw)

    def _do(self, method, url, headers, params, data, json_body, timeout, max_bytes, browser, allow_redirects, cookies) -> Response:
        session = self._session(browser)
        started = time.time()
        try:
            kwargs = dict(headers=headers, params=params, data=data, json=json_body, timeout=timeout,
                          allow_redirects=allow_redirects, stream=True, cookies=cookies)
            r = session.request(method, url, **kwargs)
            chunks, total, truncated = [], 0, False
            try:
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        truncated = True
                        break
                    if time.time() - started > timeout * 2:
                        raise Transient("body read too slow", url=url)
            finally:
                try:
                    r.close()
                except Exception:
                    pass
            content = b"".join(chunks)[:max_bytes]
            hdrs = {str(k).lower(): str(v) for k, v in r.headers.items()}
            with self._stats_lock:
                self.stats["requests"] += 1
                self.stats["bytes"] += len(content)
            return Response(url=str(r.url), status=int(r.status_code), headers=hdrs, content=content,
                            elapsed=time.time() - started, truncated=truncated)
        except FetchError:
            raise
        except Exception as exc:  # map library-specific exceptions to our classes
            raise self._classify(exc, url) from exc

    def _classify(self, exc: Exception, url: str) -> FetchError:
        text = f"{type(exc).__name__}: {exc}"
        low = text.lower()
        dns_markers = ("name resolution", "name or service not known", "could not resolve", "couldn't resolve",
                       "nodename nor servname", "getaddrinfo failed", "no address associated", "resolve host")
        if any(m in low for m in dns_markers):
            if not self.network_ok():
                return NetworkDown("network unavailable (DNS failing for well-known hosts)", url=url)
            return Permanent("domain does not resolve", url=url)
        if "invalid url" in low or "no connection adapters" in low or "unsupported protocol" in low or "missingschema" in low:
            return Permanent("invalid URL", url=url)
        if any(m in low for m in ("network is unreachable", "no route to host")) and not self.network_ok():
            return NetworkDown("network unreachable", url=url)
        if "too many redirects" in low or "toomanyredirects" in low or "maximum (" in low and "redirects" in low:
            return Permanent("redirect loop", url=url)
        if any(m in low for m in ("certificate", "ssl", "tls")):
            return Permanent("TLS/SSL error", url=url)
        return Transient(text[:200], url=url)

    # -- robots.txt -----------------------------------------------------------
    def robots_allowed(self, url: str, user_agent: str | None = None) -> tuple[bool, float]:
        """Return (allowed, crawl_delay). Follows Google's rules for robots.txt fetch outcomes."""
        user_agent = user_agent or self.robot_name
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            cached = self._robots.get(base, "missing")
        if cached == "missing":
            parser, reason = self._fetch_robots(base)
            with self._robots_lock:
                self._robots[base] = parser
                self._robots_reason[base] = reason
            cached = parser
        if cached is None:
            return False, 0.0
        allowed = cached.can_fetch(user_agent, url)
        if not allowed:
            with self._robots_lock:
                self._robots_reason[base] = "disallowed"
        delay = cached.crawl_delay(user_agent) or 0.0
        try:
            delay = float(delay)
        except (TypeError, ValueError):
            delay = 0.0
        return allowed, delay

    def _fetch_robots(self, base: str) -> tuple[RobotFileParser | None, str]:
        """(parser, reason). parser None = treat the site as closed for now (Google's rule for robots.txt
        that answers 429/5xx or cannot be reached); reason says which, so a site that merely failed to
        answer is retried later instead of being reported as refusing robots."""
        last = ""
        # Some small-business servers reject the plain client's TLS handshake but answer a browser-like one.
        for browser, timeout in ((False, 12), (True, 20)):
            try:
                r = self.get(base + "/robots.txt", timeout=timeout, max_bytes=500_000, browser=browser, retries=0,
                             interval=self.default_interval, block_statuses=())
            except NetworkDown:
                raise
            except FetchError as exc:
                last = str(exc)[:120] or type(exc).__name__
                continue
            if 200 <= r.status < 300 and "html" not in r.content_type:
                parser = RobotFileParser()
                parser.parse(r.text.splitlines())
                return parser, "ok"
            if r.status == 429 or r.status >= 500:
                return None, f"robots.txt answered HTTP {r.status}"
            parser = RobotFileParser()
            parser.parse([])  # 4xx / HTML soft-404: no restrictions
            return parser, "ok"
        return None, f"robots.txt unreachable ({last})"

    def robots_reason(self, url: str) -> str:
        """Why robots_allowed() answered as it did for this site: ok | disallowed | robots.txt answered HTTP n |
        robots.txt unreachable (...)."""
        parts = urlsplit(url)
        with self._robots_lock:
            return self._robots_reason.get(f"{parts.scheme}://{parts.netloc}", "")

    def breaker_report(self) -> dict:
        return {name: b.snapshot() for name, b in sorted(self.breakers.items())}
