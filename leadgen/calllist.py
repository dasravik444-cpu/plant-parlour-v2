"""The owner's daily call list: one tab per day in the Google Sheet ("Calls 2026-11-09").

It holds the leads added that day that have a phone number, best first, with the contact person, all
numbers and e-mails, and two columns for the owner: Call Result (a drop-down) and Notes. Leads that
replied "yes" or wrote back by e-mail are put at the top of every day's list until the owner changes
their Status in the Leads tab. Making the list again (after a later lead run, or `pp calls`) adds the new
leads and keeps whatever the owner already typed in Call Result and Notes.

Only the newest KEEP_DAYS lists stay as tabs; an older one is removed only if nobody wrote anything in it
(every lead stays in the Leads tab in any case)."""
from __future__ import annotations

import re

from .sheets import SheetsError, _cell, _tab_ref, col_letter
from .util import get_logger

log = get_logger("calllist")

PREFIX = "Calls "
KEEP_DAYS = 14
COLUMNS = ["#", "Priority", "Business", "Contact Person", "Phone", "Other Phones", "WhatsApp", "E-mail", "Category",
           "Area", "Address", "Website", "E-mail Status", "Call Result", "Notes", "Lead ID", "Key"]
OWNER_COLS = ("Call Result", "Notes")
RESULTS = ["Interested - send price list", "Call back later", "Not interested", "No answer", "Wrong number",
           "Already has a supplier"]
READ = ["Lead ID", "Date Added", "Business Name", "Category", "Area", "Address", "Phones", "WhatsApp", "Emails",
        "Contact Person", "Website", "Priority", "Status", "Key"]
HOT = {"interested - call now": "REPLIED YES - call first", "replied - read it": "Replied by e-mail - call"}
SKIP = {"not interested (opted out)"}
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def tab_name(day: str) -> str:
    return PREFIX + day


def _lines(v) -> list[str]:
    return [x.strip() for x in str(v or "").split("\n") if x.strip()]


def _phone_rank(line: str) -> int:
    low = line.lower()
    return 0 if "mobile" in low and "landline" not in low else 1 if "mobile" in low else 2


def _read_leads(client, leads_tab: str) -> list[dict]:
    ref = _tab_ref(leads_tab)
    head = client.get_values(f"{ref}!A1:AZ1")
    header = [h.strip() for h in (head[0] if head else [])]
    present = [c for c in READ if c in header]
    if "Key" not in present or "Date Added" not in present:
        raise SheetsError(f"the '{leads_tab}' tab has no Key / Date Added column")
    cols = client.batch_get_columns([f"{ref}!{col_letter(header.index(c))}2:{col_letter(header.index(c))}" for c in present])
    n = max((len(c) for c in cols), default=0)
    return [{name: (str(cols[j][i]) if i < len(cols[j]) else "") for j, name in enumerate(present)} for i in range(n)]


def build_rows(leads: list[dict], day: str, owner: dict[str, dict]) -> tuple[list[list], dict]:
    """The day's rows (without the # column filled yet) and counts for the summary."""
    hot, fresh = [], []
    stats = {"today": 0, "with_phone": 0, "hot": 0, "skipped_opt_out": 0}
    for r in leads:
        key = r.get("Key", "").strip()
        if not key:
            continue
        status = r.get("Status", "").strip()
        phones = sorted(_lines(r.get("Phones")), key=_phone_rank)
        is_today = r.get("Date Added", "").strip().startswith(day)
        if is_today:
            stats["today"] += 1
        if status.lower() in SKIP:
            stats["skipped_opt_out"] += int(is_today)
            continue
        label = HOT.get(status.lower())
        if not phones or not (is_today or label):
            continue
        row = {
            "Priority": label or r.get("Priority", ""), "Business": r.get("Business Name", ""),
            "Contact Person": "\n".join(_lines(r.get("Contact Person"))), "Phone": phones[0],
            "Other Phones": "\n".join(phones[1:]), "WhatsApp": "\n".join(_lines(r.get("WhatsApp"))),
            "E-mail": "\n".join(x.split(" ")[0] for x in _lines(r.get("Emails"))), "Category": r.get("Category", ""),
            "Area": r.get("Area", ""), "Address": r.get("Address", ""), "Website": r.get("Website", ""),
            "E-mail Status": status or "New", "Lead ID": r.get("Lead ID", ""), "Key": key,
        }
        row.update(owner.get(key, {}))
        if label:
            hot.append((0 if "yes" in label.lower() else 1, row))
        else:
            stats["with_phone"] += 1
            rank = (PRIORITY_RANK.get(row["Priority"].strip().lower(), 3), 0 if row["Contact Person"] else 1,
                    _phone_rank(row["Phone"]), _id_num(row["Lead ID"]))
            fresh.append((rank, row))
    stats["hot"] = len(hot)
    ordered = [r for _, r in sorted(hot, key=lambda x: x[0])] + [r for _, r in sorted(fresh, key=lambda x: x[0])]
    out = []
    for i, row in enumerate(ordered, 1):
        row["#"] = i
        out.append([row.get(c, "") for c in COLUMNS])
    return out, stats


def _id_num(lead_id: str) -> int:
    digits = re.sub(r"\D", "", lead_id or "")
    return int(digits) if digits else 10**9


def _owner_input(client, title: str) -> dict[str, dict]:
    """What the owner already typed in a day's tab, by lead Key."""
    rows = client.get_values(f"{_tab_ref(title)}!A1:{col_letter(len(COLUMNS) - 1)}")
    if not rows:
        return {}
    header = [h.strip() for h in rows[0]]
    if "Key" not in header:
        return {}
    k = header.index("Key")
    idx = {c: header.index(c) for c in OWNER_COLS if c in header}
    out = {}
    for r in rows[1:]:
        key = str(r[k]).strip() if k < len(r) else ""
        vals = {c: str(r[i]).strip() for c, i in idx.items() if i < len(r) and str(r[i]).strip()}
        if key and vals:
            out[key] = vals
    return out


def make_call_list(cfg, client, day: str) -> dict:
    """Create or refresh the day's call-list tab. Returns counts for the log."""
    leads_tab = cfg["sheets"]["leads_tab"]
    title = tab_name(day)
    tabs = client.tabs()
    owner = _owner_input(client, title) if title in tabs else {}
    rows, stats = build_rows(_read_leads(client, leads_tab), day, owner)
    if title not in tabs:
        client.batch_update([{"addSheet": {"properties": {"title": title, "index": 1,
                                                          "gridProperties": {"rowCount": max(1000, len(rows) + 200),
                                                                             "columnCount": len(COLUMNS) + 1,
                                                                             "frozenRowCount": 1, "frozenColumnCount": 3}}}}])
        tabs = client.tabs()
    props = tabs[title]
    sid = props["sheetId"]
    have = int((props.get("gridProperties") or {}).get("rowCount") or 0)
    if have and have < len(rows) + 1:
        client.batch_update([{"appendDimension": {"sheetId": sid, "dimension": "ROWS", "length": len(rows) + 1 - have + 200}}])
    ref, last = _tab_ref(title), col_letter(len(COLUMNS) - 1)
    client.clear(f"{ref}!A1:{last}")
    client.update_values(f"{ref}!A1", [COLUMNS] + [[_cell(v) for v in r] for r in rows])
    res = COLUMNS.index("Call Result")
    client.batch_update([
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                       "backgroundColor": {"red": 0.85, "green": 0.93, "blue": 0.85}}},
                        "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
        {"setDataValidation": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": max(2, len(rows) + 1),
                                         "startColumnIndex": res, "endColumnIndex": res + 1},
                               "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in RESULTS]},
                                        "showCustomUi": True, "strict": False}}},
    ])
    removed = _tidy_old_tabs(client, day)
    stats.update(tab=title, rows=len(rows), removed_old=removed, kept_notes=len(owner))
    log.info("call list %s: %d to call (%d replied, %d new leads with a phone of %d added that day)",
             title, len(rows), stats["hot"], stats["with_phone"], stats["today"])
    return stats


def _tidy_old_tabs(client, day: str) -> list[str]:
    tabs = client.tabs()
    days = sorted((t for t in tabs if re.fullmatch(re.escape(PREFIX) + r"\d{4}-\d{2}-\d{2}", t) and t[len(PREFIX):] <= day),
                  reverse=True)
    removed = []
    for title in days[KEEP_DAYS:]:
        if _owner_input(client, title):
            continue                     # the owner wrote in it: keep
        client.batch_update([{"deleteSheet": {"sheetId": tabs[title]["sheetId"]}}])
        removed.append(title)
    return removed
