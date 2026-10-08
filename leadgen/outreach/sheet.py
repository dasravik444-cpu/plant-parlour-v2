"""Outreach tabs in the campaign's Google Sheet.

Leads, Plan and Daily Report belong to lead generation and are only read here, except the Status
column of leads the automation contacts. Tabs created by outreach:
  Outreach        one row per e-mailed lead: stage, e-mails sent, next follow-up, reply
  Email Preview   dry-run only: the exact e-mails that would be sent today
  Replies         every reply, with the next step (call, WhatsApp, remove)
  WhatsApp Queue  today's messages to send by tapping the link; owner fills Result
  Do Not Contact  opt-outs and bounces; the owner can add e-mails or numbers too
  Outreach Report one line per run
"""
from __future__ import annotations

from ..sheets import SheetsError, _cell, _tab_ref, col_letter
from ..util import get_logger
from .whatsapp import RESULTS

log = get_logger("outreach.sheet")

TABS = {
    "outreach": ["Lead ID", "Business", "Category", "Area", "Email", "Stage", "Emails Sent", "Last Sent", "Next Follow-up",
                 "Reply", "Phone", "Key"],
    "preview": ["Planned", "Lead ID", "Business", "To", "Subject", "Body", "Key"],
    "replies": ["Received", "Lead ID", "Business", "Email", "Phone", "Reply Type", "Reply", "Next Step", "WhatsApp Chat",
                "Their Ads (Meta)", "Key"],
    "whatsapp": ["Date", "Lead ID", "Business", "Category", "Area", "Number", "Why", "Message", "Open Chat", "Result", "Key"],
    "dnc": ["Email or Phone", "Reason", "Added", "Source"],
    "report": ["Date", "Time", "Mode", "Daily Limit", "Sent Today", "New E-mails", "Follow-ups", "Interested", "Not Interested",
               "Other Replies", "Bounces", "Bounce Rate (7 days)", "WhatsApp Queued", "WhatsApp Done", "Notes"],
}
WA_RESULT_COL = TABS["whatsapp"].index("Result")
WA_ROWS = 10000          # the WhatsApp tab is made this tall so the Result dropdown covers months of queue


class OutreachSheet:
    def __init__(self, client, names: dict, leads_tab: str = "Leads"):
        self.c = client
        self.names = names            # logical tab -> title in the sheet
        self.leads_tab = leads_tab

    def ref(self, tab: str) -> str:
        return _tab_ref(self.names[tab])

    def ensure_tabs(self) -> None:
        tabs = self.c.tabs()
        missing = [t for t in TABS if self.names[t] not in tabs]
        if missing:
            self.c.batch_update([{"addSheet": {"properties": {"title": self.names[t],
                                                              "gridProperties": {"rowCount": WA_ROWS if t == "whatsapp" else 1000,
                                                                                 "columnCount": len(TABS[t]) + 2,
                                                                                 "frozenRowCount": 1}}}} for t in missing])
            tabs = self.c.tabs()
        for t, cols in TABS.items():
            last = col_letter(len(cols) - 1)
            head = self.c.get_values(f"{self.ref(t)}!A1:{last}1")
            current = [x.strip() for x in (head[0] if head else [])]
            if not any(current):
                sid = tabs[self.names[t]]["sheetId"]
                self.c.update_values(f"{self.ref(t)}!A1", [cols])
                reqs = [{"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                                       "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.98}}},
                                        "fields": "userEnteredFormat(textFormat,backgroundColor)"}}]
                if t == "whatsapp":
                    reqs.append({"setDataValidation": {
                        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": WA_ROWS,
                                  "startColumnIndex": WA_RESULT_COL, "endColumnIndex": WA_RESULT_COL + 1},
                        "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in RESULTS]},
                                 "showCustomUi": True, "strict": False}}})
                self.c.batch_update(reqs)
            elif current[:len(cols)] != cols:
                raise SheetsError(f"tab '{self.names[t]}' has unexpected headers {current[:5]}... - rename that tab; "
                                  "outreach will not overwrite it")

    # -- reading owner input -------------------------------------------------------
    def read_dnc(self) -> list[tuple[str, str]]:
        rows = self.c.get_values(f"{self.ref('dnc')}!A2:B")
        return [(str(r[0]).strip(), str(r[1]).strip() if len(r) > 1 else "") for r in rows if r and str(r[0]).strip()]

    def read_whatsapp_results(self) -> list[tuple[str, str, str]]:
        """[(key, number, result cell)] for every queue row."""
        n_col, r_col, k_col = (col_letter(TABS["whatsapp"].index(c)) for c in ("Number", "Result", "Key"))
        nums, results, keys = self.c.batch_get_columns([f"{self.ref('whatsapp')}!{n_col}2:{n_col}",
                                                        f"{self.ref('whatsapp')}!{r_col}2:{r_col}",
                                                        f"{self.ref('whatsapp')}!{k_col}2:{k_col}"])
        n = max(len(nums), len(results), len(keys))
        get = lambda col, i: str(col[i]).strip() if i < len(col) else ""  # noqa: E731
        return [(get(keys, i), get(nums, i), get(results, i)) for i in range(n)]

    # -- writing -------------------------------------------------------------------
    def _ensure_rows(self, tab: str, needed: int) -> None:
        props = self.c.tabs().get(self.names[tab], {})
        have = int((props.get("gridProperties") or {}).get("rowCount") or 0)
        if props and have < needed:
            self.c.batch_update([{"appendDimension": {"sheetId": props["sheetId"], "dimension": "ROWS",
                                                      "length": needed - have + 500}}])

    def rewrite(self, tab: str, rows: list[list]) -> None:
        last = col_letter(len(TABS[tab]) - 1)
        self._ensure_rows(tab, len(rows) + 1)
        self.c.clear(f"{self.ref(tab)}!A2:{last}")
        for i in range(0, len(rows), 2000):
            self.c.update_values(f"{self.ref(tab)}!A{i + 2}", [[_cell(v) for v in r] for r in rows[i:i + 2000]])

    def append(self, tab: str, rows: list[list]) -> None:
        for i in range(0, len(rows), 500):
            self.c.append_values(f"{self.ref(tab)}!A1", [[_cell(v) for v in r] for r in rows[i:i + 500]])

    def set_lead_statuses(self, statuses: dict[str, str]) -> int:
        """Write the Status cell of leads by Key (row numbers are re-read so a sorted sheet stays correct)."""
        if not statuses:
            return 0
        ref = _tab_ref(self.leads_tab)
        head = self.c.get_values(f"{ref}!A1:AZ1")
        header = [h.strip() for h in (head[0] if head else [])]
        if "Key" not in header or "Status" not in header:
            return 0
        k_col, s_col = col_letter(header.index("Key")), col_letter(header.index("Status"))
        (keys,) = self.c.batch_get_columns([f"{ref}!{k_col}2:{k_col}"])
        data = []
        for i, k in enumerate(keys):
            k = str(k).strip()
            if k in statuses:
                data.append({"range": f"{ref}!{s_col}{i + 2}", "values": [[statuses[k]]]})
        for i in range(0, len(data), 300):
            self.c.batch_update_values(data[i:i + 300])
        return len(data)
