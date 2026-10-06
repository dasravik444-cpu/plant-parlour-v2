"""Keyless web search over engines that answer automated requests (verified from GitHub Actions:
DuckDuckGo HTML/Lite and Yahoo). Engines that block are skipped by circuit breakers.
Optional: Brave Search API when BRAVE_API_KEY is set (paid beyond Brave's monthly credit)."""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup

from ..net import BreakerOpen, FetchError, Http, NetworkDown
from ..util import get_logger

log = get_logger("search")

CAPTCHA_MARKERS = ("captcha", "unusual traffic", "are you a robot", "anomaly-modal", "please verify you are a human",
                   "challenge-form", "/sorry/index")


@dataclass
class Result:
    title: str
    url: str
    snippet: str
    engine: str


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _ddg_target(href: str) -> str:
    if "uddg=" in href:
        q = parse_qs(urlsplit(href if href.startswith("http") else "https:" + href).query).get("uddg")
        if q:
            return unquote(q[0])
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_ddg_html(html: str) -> list[Result]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a.result__a"):
        url = _ddg_target(a.get("href", ""))
        if not url.startswith("http") or "duckduckgo.com/y.js" in url:
            continue
        body = a.find_parent(class_=re.compile(r"result__body|results_links|result"))
        snip = body.select_one(".result__snippet") if body else None
        out.append(Result(_clean(a.get_text(" ")), url, _clean(snip.get_text(" ")) if snip else "", "ddg_html"))
    return out


def parse_ddg_lite(html: str) -> list[Result]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    links = soup.select("a.result-link") or [a for a in soup.find_all("a", href=True) if "uddg=" in a["href"]]
    for a in links:
        url = _ddg_target(a.get("href", ""))
        if not url.startswith("http") or "duckduckgo.com" in urlsplit(url).netloc:
            continue
        snippet = ""
        tr = a.find_parent("tr")
        if tr is not None:
            nxt = tr.find_next_sibling("tr")
            if nxt is not None:
                td = nxt.select_one("td.result-snippet")
                if td is not None:
                    snippet = _clean(td.get_text(" "))
        out.append(Result(_clean(a.get_text(" ")), url, snippet, "ddg_lite"))
    return out


def _yahoo_target(href: str) -> str:
    m = re.search(r"/RU=([^/]+)/R[KS]=", href)
    if m:
        return unquote(m.group(1))
    return href


def parse_yahoo(html: str) -> list[Result]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    blocks = soup.select("div.algo") or soup.select("li div.dd")
    for b in blocks:
        a = b.select_one("h3 a") or b.select_one("a")
        if a is None or not a.get("href"):
            continue
        url = _yahoo_target(a["href"])
        if not url.startswith("http") or "yahoo.com" in (urlsplit(url).hostname or ""):
            continue
        # Title: drop the breadcrumb span ("www.instagram.com › handle") Yahoo puts inside the link
        for span in a.find_all("span"):
            if "›" in span.get_text() or re.match(r"^\s*(https?://|www\.)", span.get_text()):
                span.decompose()
        title = _clean(a.get_text(" "))
        snip_el = b.select_one(".compText") or b.select_one("p")
        out.append(Result(title, url, _clean(snip_el.get_text(" ")) if snip_el else "", "yahoo"))
    return out


class WebSearch:
    # Verified from GitHub Actions (2026-10): Yahoo answers steady automated queries; DuckDuckGo
    # answers about one query and then rate-limits (HTTP 202), so it is only a slow backup.
    ENGINES = ("yahoo", "ddg_html", "ddg_lite")
    INTERVALS = {"yahoo": 4.5, "ddg_html": 60.0, "ddg_lite": 60.0}

    def __init__(self, http: Http, interval: float | None = None, jitter: float = 4.0, engines: tuple | None = None):
        self.http = http
        self.base_interval, self.jitter = interval, jitter
        self.engines = list(engines or self.ENGINES)
        self.brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
        self._lock = threading.Lock()
        self.stats = {e: {"ok": 0, "blocked": 0, "empty": 0, "error": 0} for e in self.engines + ["brave_api"]}
        self.unconfirmed_empty = {e: 0 for e in self.engines}

    def available(self) -> bool:
        return bool(self.brave_key) or any(not self.http.breaker("search:" + e).is_open() for e in self.engines)

    def _fetch(self, engine: str, query: str):
        interval = max(self.base_interval or 0.0, self.INTERVALS.get(engine, 6.0))
        common = dict(service="search:" + engine, interval=interval, jitter=self.jitter, timeout=20, retries=0,
                      block_statuses=(429, 403, 202))
        if engine == "ddg_html":
            r = self.http.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "in-en"}, **common)
            return r, parse_ddg_html
        if engine == "ddg_lite":
            r = self.http.get("https://lite.duckduckgo.com/lite/", params={"q": query, "kl": "in-en"}, **common)
            return r, parse_ddg_lite
        if engine == "yahoo":
            r = self.http.get("https://search.yahoo.com/search", params={"p": query, "n": "15"}, **common)
            return r, parse_yahoo
        raise ValueError(engine)

    def _brave(self, query: str) -> list[Result]:
        r = self.http.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": "10", "country": "IN"},
                          headers={"X-Subscription-Token": self.brave_key, "Accept": "application/json"},
                          service="search:brave_api", interval=1.2, timeout=20, retries=1, browser=False)
        data = r.json() if r.status == 200 else {}
        return [Result(_clean(x.get("title", "")), x.get("url", ""), _clean(x.get("description", "")), "brave_api")
                for x in (data.get("web") or {}).get("results", []) if x.get("url")]

    def search(self, query: str) -> list[Result]:
        """Engines are tried in order of reliability; the first healthy one that answers wins.
        Returns [] if every engine is unavailable. Raises BreakerOpen when the only answers were
        empty pages from engines that have not returned any result yet in this run (a changed page
        layout looks exactly like "nothing found"), so the caller keeps the task for later."""
        unconfirmed = False
        for engine in self.engines:
            if self.http.breaker("search:" + engine).is_open():
                continue
            try:
                r, parser = self._fetch(engine, query)
            except NetworkDown:
                raise
            except (BreakerOpen, FetchError) as exc:
                self.stats[engine]["blocked" if "HTTP" in str(exc) else "error"] += 1
                continue
            low = r.text[:20000].lower()
            results = parser(r.text) if r.status == 200 else []
            if not results and any(m in low for m in CAPTCHA_MARKERS):
                self.stats[engine]["blocked"] += 1
                self.http.breaker("search:" + engine).failure("captcha/challenge page")
                continue
            if not results:
                self.stats[engine]["empty"] += 1
                if r.status != 200:
                    continue
                with self._lock:
                    trusted = self.stats[engine]["ok"] > 0
                    if not trusted:
                        self.unconfirmed_empty[engine] += 1
                        broken = self.unconfirmed_empty[engine] >= 5
                if trusted:
                    return []
                unconfirmed = True
                if broken:
                    self.http.breaker("search:" + engine).trip("only empty result pages - page layout changed?")
                continue
            with self._lock:
                self.stats[engine]["ok"] += 1
            return results
        if self.brave_key and not self.http.breaker("search:brave_api").is_open():
            try:
                res = self._brave(query)
                self.stats["brave_api"]["ok"] += 1
                return res
            except FetchError:
                self.stats["brave_api"]["error"] += 1
        if unconfirmed:
            raise BreakerOpen("web search gave only empty pages so far this run - kept for later")
        return []
