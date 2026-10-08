"""Recorder agent: Google Sheets output (Leads, Plan, Daily Report tabs).

* The local database stays the source of truth; the sheet is a view of it.
* Rows are matched by the hidden-ish `Key` column, so re-running, sorting the
  sheet, or a crash between writing and recording never creates duplicates.
* The `Status` and any columns you add to the right are yours: the system writes
  `Status` only when it first adds a row and never touches columns after it.
"""
from __future__ import annotations

import json
import os
import time

from .util import get_logger

log = get_logger("sheets")

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

LEAD_COLUMNS = ["Lead ID", "Date Added", "Business Name", "Category", "Area", "Address", "Phones", "WhatsApp", "Emails",
                "Instagram", "Facebook", "LinkedIn", "Contact Person", "Website", "Google Maps", "Rating", "Priority",
                "Signals", "USP", "Description", "Contact Sources", "Other Contacts (unverified)", "Plan Part",
                "Last Updated", "Key", "Status"]
# Earlier layouts: a sheet created with one of these is upgraded in place (columns inserted, data kept).
OLD_LEAD_LAYOUTS = [
    ["Lead ID", "Date Added", "Business Name", "Category", "Area", "Address", "Phones", "WhatsApp", "Emails",
     "Instagram", "Facebook", "LinkedIn", "Website", "Google Maps", "Rating", "Priority", "Description",
     "Contact Sources", "Other Contacts (unverified)", "Plan Part", "Last Updated", "Key", "Status"],
    ["Lead ID", "Date Added", "Business Name", "Category", "Area", "Address", "Phones", "WhatsApp", "Emails",
     "Instagram", "Facebook", "LinkedIn", "Contact Person", "Website", "Google Maps", "Rating", "Priority",
     "Description", "Contact Sources", "Other Contacts (unverified)", "Plan Part", "Last Updated", "Key", "Status"],
    ["Lead ID", "Date Added", "Business Name", "Category", "Area", "Address", "Phones", "WhatsApp", "Emails",
     "Instagram", "Facebook", "LinkedIn", "Contact Person", "Website", "Google Maps", "Rating", "Priority",
     "Signals", "Description", "Contact Sources", "Other Contacts (unverified)", "Plan Part", "Last Updated",
     "Key", "Status"],
]
STATUS_COL = LEAD_COLUMNS.index("Status")
KEY_COL = LEAD_COLUMNS.index("Key")
PLAN_COLUMNS = ["Part", "Area", "Main localities", "Areas seen in results", "Search squares", "Scheduled date", "Status",
                "Started", "Finished", "Leads found", "Searches done", "Searches total"]
REPORT_COLUMNS = ["Date", "Run started", "Minutes", "Plan day", "Scheduled part", "Parts worked", "Searches run",
                  "Places found (new)", "New leads", "Leads with phone", "With WhatsApp", "With email", "With Instagram",
                  "Leads total", "Sheet rows added", "Sheet rows updated", "Plan progress", "Health", "Warnings"]


class SheetsError(RuntimeError):
    pass


def col_letter(idx: int) -> str:
    s, n = "", idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def load_credentials():
    """Service-account credentials from GOOGLE_SERVICE_ACCOUNT_JSON (content) or *_FILE / GOOGLE_APPLICATION_CREDENTIALS (path)."""
    from google.oauth2 import service_account

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            info = json.loads(raw)
        except ValueError as exc:
            raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON (paste the whole key file content)") from exc
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and os.path.exists(path):
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    raise SheetsError("no Google service-account key configured (set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE)")


class SheetsClient:
    def __init__(self, spreadsheet_id: str, credentials=None, session=None):
        if not spreadsheet_id:
            raise SheetsError("no spreadsheet id configured (set the PP_SHEET_ID secret)")
        self.sid = spreadsheet_id
        if session is None:
            from google.auth.transport.requests import AuthorizedSession

            session = AuthorizedSession(credentials or load_credentials())
        self.s = session
        self.calls = 0

    def _req(self, method: str, url: str, **kw):
        delay = 5
        for attempt in range(4):
            self.calls += 1
            try:
                r = self.s.request(method, url, timeout=60, **kw)
            except Exception as exc:  # network trouble
                if attempt == 3:
                    raise SheetsError(f"Sheets API unreachable: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                msg = r.text[:400]
                if r.status_code == 403:
                    msg += " -- share the spreadsheet with the service-account email as Editor"
                if r.status_code == 404:
                    msg += " -- check PP_SHEET_ID"
                raise SheetsError(f"Sheets API HTTP {r.status_code}: {msg}")
            return r.json() if r.content else {}
        raise SheetsError("Sheets API retries exhausted")

    def metadata(self) -> dict:
        return self._req("GET", f"{API}/{self.sid}", params={"fields": "properties.title,sheets.properties(sheetId,title,gridProperties)"})

    def tabs(self) -> dict:
        return {s["properties"]["title"]: s["properties"] for s in self.metadata().get("sheets", [])}

    def batch_update(self, requests: list) -> dict:
        return self._req("POST", f"{API}/{self.sid}:batchUpdate", json={"requests": requests})

    def get_values(self, rng: str) -> list:
        return self._req("GET", f"{API}/{self.sid}/values/{_q(rng)}", params={"majorDimension": "ROWS"}).get("values", [])

    def batch_get_columns(self, ranges: list[str]) -> list[list]:
        data = self._req("GET", f"{API}/{self.sid}/values:batchGet", params=[("ranges", r) for r in ranges] + [("majorDimension", "COLUMNS")])
        out = []
        for vr in data.get("valueRanges", []):
            vals = vr.get("values") or [[]]
            out.append(vals[0] if vals else [])
        return out

    def update_values(self, rng: str, values: list) -> dict:
        return self._req("PUT", f"{API}/{self.sid}/values/{_q(rng)}", params={"valueInputOption": "RAW"}, json={"values": values})

    def batch_update_values(self, data: list[dict]) -> dict:
        return self._req("POST", f"{API}/{self.sid}/values:batchUpdate", json={"valueInputOption": "RAW", "data": data})

    def append_values(self, rng: str, values: list) -> dict:
        return self._req("POST", f"{API}/{self.sid}/values/{_q(rng)}:append",
                         params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, json={"values": values})

    def clear(self, rng: str) -> dict:
        return self._req("POST", f"{API}/{self.sid}/values/{_q(rng)}:clear", json={})


def _q(rng: str) -> str:
    from urllib.parse import quote

    return quote(rng, safe="!:")


def _tab_ref(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


class SheetsSync:
    def __init__(self, client: SheetsClient, leads_tab: str = "Leads", plan_tab: str = "Plan", report_tab: str = "Daily Report"):
        self.c = client
        self.leads_tab, self.plan_tab, self.report_tab = leads_tab, plan_tab, report_tab

    # -- layout ---------------------------------------------------------------
    def ensure_tabs(self) -> None:
        tabs = self.c.tabs()
        wanted = [(self.leads_tab, LEAD_COLUMNS), (self.plan_tab, PLAN_COLUMNS), (self.report_tab, REPORT_COLUMNS)]
        missing = [t for t, _ in wanted if t not in tabs]
        if missing:
            self.c.batch_update([{"addSheet": {"properties": {"title": t, "gridProperties": {"rowCount": 1000, "columnCount": 30, "frozenRowCount": 1}}}}
                                 for t in missing])
            tabs = self.c.tabs()
        for tab, cols in wanted:
            head = self.c.get_values(f"{_tab_ref(tab)}!A1:{col_letter(len(cols) - 1)}1")
            current = head[0] if head else []
            if tab == self.leads_tab and current:
                current = self._upgrade_layout(tabs[tab]["sheetId"], [x.strip() for x in current], cols)
            if not any(x.strip() for x in current):
                self.c.update_values(f"{_tab_ref(tab)}!A1", [cols])
                sheet_id = tabs[tab]["sheetId"]
                self.c.batch_update([
                    {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.85, "green": 0.93, "blue": 0.83}}},
                                    "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
                    {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                                               "fields": "gridProperties.frozenRowCount"}},
                ])
            elif [x.strip() for x in current[:len(cols)]] != cols:
                raise SheetsError(f"tab '{tab}' has unexpected headers {current[:6]}... - rename that tab or set a different "
                                  f"tab name in the config; the system will not overwrite it")

    def _upgrade_layout(self, sheet_id: int, current: list[str], cols: list[str]) -> list[str]:
        """Insert columns added in a newer version into a sheet made with an older layout (data moves along)."""
        for old in OLD_LEAD_LAYOUTS:
            if current[:len(old)] == old and current[:len(cols)] != cols:
                layout = list(old)
                for idx, name in enumerate(cols):
                    if idx >= len(layout) or layout[idx] != name:
                        if name in layout:
                            break      # a reordering, not an insertion: leave it to the header check
                        self.c.batch_update([{"insertDimension": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                                            "startIndex": idx, "endIndex": idx + 1},
                                                                  "inheritFromBefore": True}}])
                        layout.insert(idx, name)
                self.c.update_values(f"{_tab_ref(self.leads_tab)}!A1", [cols])
                log.info("Leads tab upgraded to the current column layout")
                return list(cols)
        return current

    # -- leads ------------------------------------------------------------------
    def existing_rows(self) -> dict[str, tuple[int, str]]:
        """key -> (row number, lead id) for rows already in the Leads tab."""
        key_col = col_letter(KEY_COL)
        ids, keys = self.c.batch_get_columns([f"{_tab_ref(self.leads_tab)}!A2:A", f"{_tab_ref(self.leads_tab)}!{key_col}2:{key_col}"])
        out = {}
        for i, k in enumerate(keys):
            k = (k or "").strip()
            if k and k not in out:
                out[k] = (i + 2, ids[i] if i < len(ids) else "")
        return out

    def upsert_leads(self, rows: list[dict]) -> tuple[int, int, dict]:
        """rows: dicts keyed by LEAD_COLUMNS. Returns (added, updated, key->lead_id adopted from sheet)."""
        existing = self.existing_rows()
        updates, appends, adopted = [], [], {}
        last_col_no_status = col_letter(STATUS_COL - 1)
        for row in rows:
            key = row["Key"]
            if key in existing:
                rownum, sheet_id = existing[key]
                if sheet_id and sheet_id != row["Lead ID"]:
                    adopted[key] = sheet_id  # keep the id the user already sees
                    row = {**row, "Lead ID": sheet_id}
                values = [_cell(row.get(c, "")) for c in LEAD_COLUMNS[:STATUS_COL]]
                updates.append({"range": f"{_tab_ref(self.leads_tab)}!A{rownum}:{last_col_no_status}{rownum}", "values": [values]})
            else:
                appends.append([_cell(row.get(c, "")) for c in LEAD_COLUMNS])
        for i in range(0, len(updates), 200):
            self.c.batch_update_values(updates[i:i + 200])
        for i in range(0, len(appends), 400):
            self.c.append_values(f"{_tab_ref(self.leads_tab)}!A1", appends[i:i + 400])
        return len(appends), len(updates), adopted

    # -- plan / report -----------------------------------------------------------
    def write_plan(self, rows: list[list]) -> None:
        self.c.clear(f"{_tab_ref(self.plan_tab)}!A2:{col_letter(len(PLAN_COLUMNS) - 1)}")
        if rows:
            self.c.update_values(f"{_tab_ref(self.plan_tab)}!A2", [[_cell(v) for v in r] for r in rows])

    def append_report(self, row: list) -> None:
        self.c.append_values(f"{_tab_ref(self.report_tab)}!A1", [[_cell(v) for v in row]])


def _cell(v) -> str | float | int:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return s[:49000]  # Sheets cell limit is 50,000 characters
