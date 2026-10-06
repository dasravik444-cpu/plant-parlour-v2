"""Live diagnostics: checks every external source the system depends on.

Run:  python -m leadgen.probe [--quick]
Prints status codes, parse results and counts. Contact values are masked
because CI logs of a public repository are public.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from urllib.parse import quote_plus, urlsplit

from .net import HAVE_CURL_CFFI, FetchError, Http
from .providers.base import ProviderUnavailable
from .providers.gmaps import GoogleMapsSearch, SEARCH_URLS
from .util import mask_value

TEST_POINTS = [("Rajarhat/New Town", 22.6200, 88.4500), ("Park Street", 22.5530, 88.3520)]


def line(msg=""):
    print(msg, flush=True)


def probe_gmaps(http_curl: Http, http_plain: Http, quick: bool) -> dict:
    line("=" * 70)
    line("GOOGLE MAPS (tbm=map over HTTPS)")
    results = {}
    combos = []
    for client_name, http in (("curl_cffi", http_curl), ("requests", http_plain)):
        if http is None:
            continue
        for ui in (0, 1):
            for variant in ("gosom", "client"):
                combos.append((client_name, http, ui, variant))
    if quick:
        combos = combos[:2]
    best = None
    for client_name, http, ui, variant in combos:
        label = f"{client_name} {SEARCH_URLS[ui]} pb={variant}"
        gm = GoogleMapsSearch(http, variant=variant, url_index=ui, interval=3.0, jitter=1.0)
        try:
            places, meta = gm.search_page("cafe", 22.6200, 88.4500, 15, offset=0)
            n_phone = sum(1 for p in places if p.phone or p.phone_intl)
            n_web = sum(1 for p in places if p.website)
            line(f"  [{label}] OK status={meta['status']} bytes={meta['bytes']} method={meta['method']} "
                 f"records={meta['records']} valid={meta['valid']} with_phone={n_phone} with_website={n_web}")
            results[label] = {"ok": True, **meta, "with_phone": n_phone, "with_website": n_web}
            if best is None and places:
                best = (gm, places)
        except (ProviderUnavailable, FetchError) as exc:
            line(f"  [{label}] FAILED: {type(exc).__name__}: {exc}")
            results[label] = {"ok": False, "error": str(exc)[:200]}
        except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
            line(f"  [{label}] ERROR: {type(exc).__name__}: {exc}")
            results[label] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    if best:
        gm, places = best
        line("  sample records (public listing data; phone masked):")
        for p in places[:8]:
            dom = urlsplit(p.website).hostname if p.website else ""
            ph = p.phone_intl or p.phone
            line(f"    - {p.name[:40]:40s} | cats={','.join(p.categories[:2])[:30]:30s} | phone={mask_value('phone', ph) if ph.startswith('+') else ('yes' if ph else '-')}"
                 f" | web={dom or '-'} | rating={p.rating} ({p.reviews}) | status='{p.status_text[:40]}' | key={p.key[:14]}")
        st = Counter(p.status_text.split(' | ')[-1] if p.status_text else '' for p in places)
        line(f"  status field values: {dict(st)}")
        dists = []
        import math
        for p in places:
            dists.append(round(math.dist((p.lat, p.lng), (22.62, 88.45)) * 111, 1))
        line(f"  approx distance (km) of results from search point: {sorted(dists)}")
        # pagination
        line("  pagination test (offset 0/20/40):")
        seen = set(p.key for p in places)
        for off in (20, 40):
            try:
                more, meta = gm.search_page("cafe", 22.6200, 88.4500, 15, offset=off)
                new = [p for p in more if p.key not in seen]
                seen.update(p.key for p in more)
                line(f"    offset {off}: valid={meta['valid']} new={len(new)}")
                results[f"page_offset_{off}"] = {"valid": meta["valid"], "new": len(new)}
            except Exception as exc:  # noqa: BLE001
                line(f"    offset {off}: FAILED {type(exc).__name__}: {exc}")
                results[f"page_offset_{off}"] = {"error": str(exc)[:200]}
        if not quick:
            for q in ("banquet hall", "interior designer", "restaurant"):
                try:
                    more, meta = gm.search_page(q, 22.5530, 88.3520, 15, offset=0)
                    line(f"  query '{q}' @Park Street: valid={meta['valid']} with_phone={sum(1 for p in more if p.phone)} "
                         f"with_web={sum(1 for p in more if p.website)}; first: {[p.name[:25] for p in more[:3]]}")
                except Exception as exc:  # noqa: BLE001
                    line(f"  query '{q}': FAILED {type(exc).__name__}: {exc}")
        results["_sample_websites"] = [p.website for p in places if p.website][:6]
    return results


def probe_gmaps_structure(http: Http) -> dict:
    """Print which positions of a result record hold numbers/short strings (to track format changes)."""
    from .providers.gmaps import build_pb, decode_payload, extract_business_arrays
    line("=" * 70)
    line("GOOGLE MAPS RECORD STRUCTURE")
    out = {}
    for q, lat, lng in (("cafe", 22.62, 88.45), ("restaurant", 22.553, 88.352), ("banquet hall", 22.50, 88.36)):
        params = {"tbm": "map", "authuser": "0", "hl": "en", "gl": "in", "q": q, "pb": build_pb(lat, lng, 15)}
        try:
            r = http.get("https://www.google.com/search", params=params, timeout=25, interval=3, retries=0, block_statuses=())
        except Exception as exc:  # noqa: BLE001
            line(f"  {q}: FAILED {exc}")
            continue
        low = r.text.lower()
        kw = {k: low.count(k) for k in ("permanently closed", "temporarily closed", "closed_permanently", "permanently",
                                         "temporarily", "opens soon", "reviews")}
        line(f"  query={q}: HTTP {r.status}, keyword counts in raw body: {kw}")
        try:
            arrays, method = extract_business_arrays(decode_payload(r.text))
        except Exception as exc:  # noqa: BLE001
            line(f"   parse failed: {exc}")
            continue
        for biz in arrays[:2]:
            line(f"   record '{biz[11] if len(biz) > 11 else '?'}' len={len(biz)}")
            for i, v in enumerate(biz):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    line(f"     [{i}] num {v}")
                elif isinstance(v, str) and len(v) <= 80:
                    line(f"     [{i}] str {v!r}")
                elif isinstance(v, list) and i in (4, 34, 88, 203, 32, 100, 142, 157, 175):
                    line(f"     [{i}] list {json.dumps(v)[:300]}")
        for biz in arrays:
            for i in (34, 88, 203):
                pass
        # search for review counts: in [4] array, print full [4] for 3 records
        for biz in arrays[:5]:
            line(f"   [4]={json.dumps(biz[4] if len(biz) > 4 else None)[:200]}")
        out[q] = kw
    return out


def probe_websites(http: Http, urls: list[str]) -> dict:
    line("=" * 70)
    line("BUSINESS WEBSITES (homepage fetch + contact markers)")
    out = {}
    for url in urls[:6]:
        try:
            allowed, delay = http.robots_allowed(url)
            r = http.get(url, timeout=20, max_bytes=2_000_000, retries=0)
            t = r.text
            marks = {"mailto": len(re.findall(r"mailto:", t, re.I)), "tel": len(re.findall(r"tel:", t, re.I)),
                     "wa": len(re.findall(r"wa\.me|api\.whatsapp", t, re.I)),
                     "insta": len(re.findall(r"instagram\.com/", t, re.I)), "ldjson": t.count("application/ld+json")}
            line(f"  {urlsplit(url).hostname}: HTTP {r.status} {len(r.content)}B robots_allowed={allowed} {marks}")
            out[url] = {"status": r.status, **marks}
        except Exception as exc:  # noqa: BLE001
            line(f"  {urlsplit(url).hostname}: FAILED {type(exc).__name__}: {exc}")
            out[url] = {"error": str(exc)[:200]}
    return out


def probe_instagram(http: Http) -> dict:
    line("=" * 70)
    line("INSTAGRAM (logged-out public profile access)")
    out = {}
    for handle in ("instagram", "natgeo"):
        url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={handle}"
        try:
            r = http.get(url, headers={"x-ig-app-id": "936619743392459", "Accept": "*/*", "X-Requested-With": "XMLHttpRequest",
                                       "Referer": f"https://www.instagram.com/{handle}/"},
                         timeout=20, interval=4, retries=0, block_statuses=())
            ok_json = False
            fields = {}
            try:
                data = r.json()
                user = (data.get("data") or {}).get("user") or {}
                ok_json = bool(user)
                fields = {k: (bool(user.get(k))) for k in ("full_name", "biography", "business_email", "business_phone_number", "external_url", "is_business_account")}
            except Exception:  # noqa: BLE001
                pass
            line(f"  web_profile_info @{handle}: HTTP {r.status} final={urlsplit(r.url).path[:40]} json_user={ok_json} fields_present={fields}")
            out[f"api_{handle}"] = {"status": r.status, "json_user": ok_json}
        except Exception as exc:  # noqa: BLE001
            line(f"  web_profile_info @{handle}: FAILED {type(exc).__name__}: {exc}")
            out[f"api_{handle}"] = {"error": str(exc)[:200]}
        try:
            r = http.get(f"https://www.instagram.com/{handle}/", timeout=20, interval=4, retries=0, block_statuses=())
            m = re.search(r'<meta property="og:title" content="([^"]*)"', r.text)
            d = re.search(r'<meta property="og:description" content="([^"]*)"', r.text)
            line(f"  profile html @{handle}: HTTP {r.status} final={urlsplit(r.url).path[:40]} og:title={'yes' if m else 'no'} og:desc={(d.group(1)[:60] if d else 'no')}")
            out[f"html_{handle}"] = {"status": r.status, "og_title": bool(m)}
        except Exception as exc:  # noqa: BLE001
            line(f"  profile html @{handle}: FAILED {type(exc).__name__}: {exc}")
            out[f"html_{handle}"] = {"error": str(exc)[:200]}
    return out


SEARCH_ENGINES = {
    "ddg_html": ("POST", "https://html.duckduckgo.com/html/", "q"),
    "ddg_lite": ("GET", "https://lite.duckduckgo.com/lite/", "q"),
    "bing": ("GET", "https://www.bing.com/search", "q"),
    "yahoo": ("GET", "https://search.yahoo.com/search", "p"),
    "mojeek": ("GET", "https://www.mojeek.com/search", "q"),
    "brave_html": ("GET", "https://search.brave.com/search", "q"),
    "startpage": ("GET", "https://www.startpage.com/sp/search", "query"),
}


def probe_search(http: Http) -> dict:
    line("=" * 70)
    line("WEB SEARCH ENGINES (for finding Instagram/Facebook/LinkedIn pages)")
    out = {}
    query = "Flurys Park Street Kolkata instagram"
    for name, (method, url, qp) in SEARCH_ENGINES.items():
        try:
            if method == "POST":
                r = http.post(url, data={qp: query}, timeout=20, interval=2, retries=0, block_statuses=())
            else:
                r = http.get(url, params={qp: query}, timeout=20, interval=2, retries=0, block_statuses=())
            t = r.text
            insta = set(re.findall(r"instagram\.com(?:/|%2F)([A-Za-z0-9_.]{2,30})", t))
            captcha = any(m in t.lower() for m in ("captcha", "unusual traffic", "are you a robot", "anomaly", "challenge"))
            line(f"  {name:10s}: HTTP {r.status} {len(r.content)}B captcha_marker={captcha} instagram_handles={sorted(insta)[:6]}")
            out[name] = {"status": r.status, "captcha": captcha, "insta": len(insta)}
        except Exception as exc:  # noqa: BLE001
            line(f"  {name:10s}: FAILED {type(exc).__name__}: {exc}")
            out[name] = {"error": str(exc)[:200]}
    return out


def probe_search_parsers(http: Http) -> dict:
    from .enrich.search import WebSearch
    line("=" * 70)
    line("SEARCH PARSERS (parsed results per engine)")
    ws = WebSearch(http, interval=2.5, jitter=1.0)
    out = {}
    queries = ['"Flurys" Park Street Kolkata instagram', '"Afraa Deli" Kolkata instagram', '"Jamuna Banquets" Kolkata facebook',
               '"The Alam Interiors" Kolkata linkedin']
    for engine in ws.engines:
        for q in queries[:3] if engine != "ddg_html" else queries:
            try:
                r, parser = ws._fetch(engine, q)
                res = parser(r.text)
                line(f"  {engine:9s} q={q!r}: HTTP {r.status} parsed={len(res)}")
                for x in res[:6]:
                    line(f"      - {x.title[:70]!r} -> {x.url[:90]}")
                if not res:
                    snippet = re.sub(r"\s+", " ", r.text[:1500])
                    line(f"      (no results) head: {snippet[:400]}")
                out[f"{engine}:{q}"] = len(res)
            except Exception as exc:  # noqa: BLE001
                line(f"  {engine:9s} q={q!r}: FAILED {type(exc).__name__}: {exc}")
                out[f"{engine}:{q}"] = str(exc)[:120]
    return out


def probe_overpass(http: Http) -> dict:
    line("=" * 70)
    line("OPENSTREETMAP OVERPASS")
    out = {}
    q = '[out:json][timeout:60];nwr["amenity"="cafe"](around:5000,22.5726,88.3639);out center 5;'
    for url in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                "https://overpass.private.coffee/api/interpreter"):
        try:
            r = http.post(url, data={"data": q}, timeout=90, interval=1, retries=0, browser=False, block_statuses=(),
                          headers={"User-Agent": "plant-parlour-leadgen/2.0 (github.com/dasravik444-cpu/plant-parlour-v2)"})
            n = len(r.json().get("elements", [])) if r.status == 200 else None
            line(f"  {urlsplit(url).hostname}: HTTP {r.status} elements={n}")
            out[url] = {"status": r.status, "elements": n}
        except Exception as exc:  # noqa: BLE001
            line(f"  {urlsplit(url).hostname}: FAILED {type(exc).__name__}: {exc}")
            out[url] = {"error": str(exc)[:200]}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live diagnostics of external sources")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", default="", help="comma list: gmaps,web,instagram,search,overpass")
    args = ap.parse_args(argv)
    only = set(filter(None, args.only.split(",")))
    line(f"curl_cffi available: {HAVE_CURL_CFFI}")
    http_curl = Http(use_curl_cffi=True) if HAVE_CURL_CFFI else None
    http_plain = Http(use_curl_cffi=False)
    report = {"time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}
    main_http = http_curl or http_plain
    if not only or "gmaps" in only:
        report["gmaps"] = probe_gmaps(http_curl, http_plain, args.quick)
    if "structure" in only:
        report["structure"] = probe_gmaps_structure(main_http)
    if "parsers" in only:
        report["parsers"] = probe_search_parsers(main_http)
    if not only or "web" in only:
        report["web"] = probe_websites(main_http, report.get("gmaps", {}).get("_sample_websites", []) or
                                       ["https://www.flurys.com/", "https://www.peterhook.in/"])
    if not only or "instagram" in only:
        report["instagram"] = probe_instagram(main_http)
    if not only or "search" in only:
        report["search"] = probe_search(main_http)
    if not only or "overpass" in only:
        report["overpass"] = probe_overpass(http_plain)
    line("=" * 70)
    line("PROBE_JSON " + json.dumps(report, default=str)[:20000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
