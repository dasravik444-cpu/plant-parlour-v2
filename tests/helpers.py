"""Test doubles: a fake internet (HTTP router) and a fake Google Sheets API. Synthetic data only."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from leadgen.net import Http, Response, Transient


# ---------------------------------------------------------------------------
# Google Maps tbm=map response builder (same record layout the parser expects)
# ---------------------------------------------------------------------------
def biz(name, lat, lng, place_id, *, phone_local=None, phone_intl=None, website=None, cats=("Cafe",), area="Salt Lake",
        address=None, rating=4.5, reviews=120, closed=False, tag=None):
    b = [None] * 260
    b[11] = name
    b[9] = [None, None, lat, lng]
    b[78] = place_id
    b[10] = "0x39f8:" + place_id[-6:]
    b[13] = list(cats)
    b[39] = address or f"12 Test Road, {area}, Kolkata, West Bengal 700091"
    b[14] = area
    b[166] = "Kolkata, West Bengal"
    b[4] = [None] * 7 + [rating, reviews]
    if phone_local:
        b[178] = [[phone_local, [[phone_local, 1], [phone_intl or phone_local, 2]]]]
    if website:
        b[7] = [website, urlsplit(website).hostname]
    if closed:
        b[203] = [[["Tuesday", 2, [2026, 10, 6], [["Permanently closed", [[], []]]], 0, 1]]]
    if tag:
        b[88] = [tag, "SearchResult.TYPE_CAFE"]
    return b


def gmaps_payload(records: list) -> str:
    items = [["meta"]] + [[None] * 14 + [r] for r in records]
    return ")]}'\n" + json.dumps([[None, items]])


def yahoo_html(results: list[tuple[str, str, str]]) -> str:
    blocks = []
    for title, url, snippet in results:
        ru = url.replace(":", "%3a").replace("/", "%2f")
        blocks.append(f'<div class="dd algo algo-sr"><div class="compTitle"><h3 class="title"><a href="https://r.search.yahoo.com/_ylt=X/RV=2/RE=1/RO=10/RU={ru}/RK=2/RS=abc-">'
                      f'<span class="d-ib">www.example.com › x</span>{title}</a></h3></div><div class="compText aAbs"><p>{snippet}</p></div></div>')
    return "<html><body><ol>" + "".join(f"<li>{b}</li>" for b in blocks) + "</ol></body></html>"


class FakeHttp(Http):
    """Real Http logic (breakers, robots, classification) with a routed fake transport and no waiting."""

    def __init__(self, router, **kw):
        super().__init__(use_curl_cffi=False, default_interval=0.0, **kw)
        self.router = router
        self.calls: list[tuple[str, str]] = []
        self._pacer.reserve = lambda key, interval: time.time()

    def _do(self, method, url, headers, params, data, json_body, timeout, max_bytes, browser, allow_redirects, cookies):
        full = url + ("?" + urlencode(params) if params else "")
        self.calls.append((method, full))
        out = self.router(method, url, params or {}, data)
        if isinstance(out, Exception):
            raise out
        status, body, ctype, final = (out + (None,))[:4] if len(out) == 3 else out
        content = body.encode("utf-8") if isinstance(body, str) else body
        return Response(url=final or url, status=status, headers={"content-type": ctype}, content=content[:max_bytes], elapsed=0.001,
                        truncated=len(content) > max_bytes)

    def network_ok(self) -> bool:
        return True


def not_found(*_a):
    return (404, "not found", "text/html")


# ---------------------------------------------------------------------------
# Fake Google Sheets REST API (subset used by leadgen.sheets)
# ---------------------------------------------------------------------------
def _col_idx(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def parse_range(rng: str):
    """'Tab'!A2:V -> (tab, c1, r1, c2|None, r2|None) with zero-based indexes; None = open-ended."""
    rng = unquote(rng)
    tab, _, cells = rng.rpartition("!")
    if tab.startswith("'") and tab.endswith("'"):
        tab = tab[1:-1].replace("''", "'")
    m = re.fullmatch(r"([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?", cells)
    if not m:
        raise ValueError(f"bad range {rng}")
    c1, r1, c2, r2 = m.groups()
    return (tab, _col_idx(c1), int(r1) - 1 if r1 else 0, _col_idx(c2) if c2 else None, int(r2) - 1 if r2 else None)


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


class FakeSheetsSession:
    def __init__(self):
        self.tabs: dict[str, dict] = {"Sheet1": {"id": 0, "rows": []}}
        self.next_id = 1
        self.log = []

    # grid helpers
    def _rows(self, tab):
        return self.tabs[tab]["rows"]

    def _set(self, tab, r, c, v):
        rows = self._rows(tab)
        while len(rows) <= r:
            rows.append([])
        row = rows[r]
        while len(row) <= c:
            row.append("")
        row[c] = v

    def _write(self, rng, values):
        tab, c1, r1, _c2, _r2 = parse_range(rng)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                self._set(tab, r1 + i, c1 + j, v)

    def _read(self, rng):
        tab, c1, r1, c2, r2 = parse_range(rng)
        rows = self._rows(tab)
        r2 = len(rows) - 1 if r2 is None else r2
        out = []
        for r in range(r1, min(r2, len(rows) - 1) + 1):
            row = rows[r]
            hi = len(row) - 1 if c2 is None else c2
            out.append([row[c] if c < len(row) else "" for c in range(c1, hi + 1)])
        while out and not any(str(x) for x in out[-1]):
            out.pop()
        return [[str(x) if not isinstance(x, (int, float)) else x for x in r] for r in out]

    def request(self, method, url, timeout=None, params=None, json=None):
        self.log.append((method, url))
        path = urlsplit(url).path
        m = re.match(r"^/v4/spreadsheets/([^/:]+)(.*)$", path)
        rest = m.group(2)
        if method == "GET" and rest == "":
            return _Resp(200, {"properties": {"title": "Fake"}, "sheets": [
                {"properties": {"sheetId": t["id"], "title": name, "gridProperties": {"rowCount": 1000}}} for name, t in self.tabs.items()]})
        if method == "POST" and rest == ":batchUpdate":
            replies = []
            for req in json["requests"]:
                if "addSheet" in req:
                    title = req["addSheet"]["properties"]["title"]
                    self.tabs[title] = {"id": self.next_id, "rows": []}
                    replies.append({"addSheet": {"properties": {"sheetId": self.next_id, "title": title}}})
                    self.next_id += 1
                elif "insertDimension" in req:
                    rng = req["insertDimension"]["range"]
                    tab = next(k for k, v in self.tabs.items() if v["id"] == rng["sheetId"])
                    n = rng["endIndex"] - rng["startIndex"]
                    for r in self.tabs[tab]["rows"]:
                        if len(r) >= rng["startIndex"]:
                            r[rng["startIndex"]:rng["startIndex"]] = [""] * n
                    replies.append({})
                elif "deleteSheet" in req:
                    sid = req["deleteSheet"]["sheetId"]
                    self.tabs = {k: v for k, v in self.tabs.items() if v["id"] != sid}
                    replies.append({})
                else:
                    replies.append({})
            return _Resp(200, {"replies": replies})
        if method == "GET" and rest == "/values:batchGet":
            ranges = [v for k, v in params if k == "ranges"]
            vrs = []
            for rng in ranges:
                rows = self._read(rng)  # single-column ranges only in this app
                col = [r[0] if r else "" for r in rows]
                while col and col[-1] == "":
                    col.pop()
                vrs.append({"range": rng, "values": [col]} if col else {"range": rng})
            return _Resp(200, {"valueRanges": vrs})
        if method == "POST" and rest == "/values:batchUpdate":
            for d in json["data"]:
                self._write(d["range"], d["values"])
            return _Resp(200, {})
        mv = re.match(r"^/values/(.+?)(:append|:clear)?$", rest)
        if mv:
            rng, action = unquote(mv.group(1)), mv.group(2)
            if method == "GET":
                return _Resp(200, {"values": self._read(rng)})
            if method == "PUT":
                self._write(rng, json["values"])
                return _Resp(200, {})
            if action == ":append":
                tab = parse_range(rng)[0]
                rows = self._rows(tab)
                last = max((i for i, r in enumerate(rows) if any(str(x) for x in r)), default=-1)
                for i, row in enumerate(json["values"]):
                    for j, v in enumerate(row):
                        self._set(tab, last + 1 + i, j, v)
                return _Resp(200, {})
            if action == ":clear":
                tab, c1, r1, c2, r2 = parse_range(rng)
                rows = self._rows(tab)
                for r in range(r1, len(rows) if r2 is None else min(r2 + 1, len(rows))):
                    hi = len(rows[r]) - 1 if c2 is None else c2
                    for c in range(c1, min(hi + 1, len(rows[r]))):
                        rows[r][c] = ""
                return _Resp(200, {})
        return _Resp(400, {"error": f"unsupported {method} {url}"})
