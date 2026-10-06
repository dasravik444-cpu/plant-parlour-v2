import pytest

from helpers import FakeSheetsSession
from leadgen.sheets import KEY_COL, LEAD_COLUMNS, STATUS_COL, SheetsClient, SheetsError, SheetsSync


def row(key, name, lead="PP-00001", phones="+91 98300 12345"):
    r = {c: "" for c in LEAD_COLUMNS}
    r.update({"Lead ID": lead, "Business Name": name, "Key": key, "Phones": phones, "Status": "New"})
    return r


def make():
    sess = FakeSheetsSession()
    return sess, SheetsSync(SheetsClient("sheet123", session=sess))


def test_tabs_and_headers_created():
    sess, sync = make()
    sync.ensure_tabs()
    assert {"Leads", "Plan", "Daily Report"} <= set(sess.tabs)
    assert sess.tabs["Leads"]["rows"][0] == LEAD_COLUMNS
    sync.ensure_tabs()  # idempotent
    assert len(sess.tabs["Leads"]["rows"]) == 1


def test_upsert_is_idempotent_and_preserves_user_status():
    sess, sync = make()
    sync.ensure_tabs()
    added, updated, _ = sync.upsert_leads([row("g:1", "A Cafe"), row("g:2", "B Cafe", "PP-00002")])
    assert (added, updated) == (2, 0)
    # user edits the Status column and adds a note to the right
    leads = sess.tabs["Leads"]["rows"]
    leads[1][STATUS_COL] = "Contacted"
    leads[1].append("call back Monday")
    added, updated, _ = sync.upsert_leads([row("g:1", "A Cafe (renamed)"), row("g:3", "C Cafe", "PP-00003")])
    assert (added, updated) == (1, 1)
    leads = sess.tabs["Leads"]["rows"]
    assert len([r for r in leads[1:] if any(r)]) == 3                 # no duplicate row
    assert leads[1][2] == "A Cafe (renamed)" and leads[1][STATUS_COL] == "Contacted"
    assert leads[1][-1] == "call back Monday"


def test_rows_found_after_user_sorts_sheet():
    sess, sync = make()
    sync.ensure_tabs()
    sync.upsert_leads([row("g:1", "A"), row("g:2", "B", "PP-00002")])
    leads = sess.tabs["Leads"]["rows"]
    leads[1], leads[2] = leads[2], leads[1]                             # user sorted the rows
    added, updated, _ = sync.upsert_leads([row("g:1", "A2")])
    assert (added, updated) == (0, 1)
    assert sess.tabs["Leads"]["rows"][2][2] == "A2" and sess.tabs["Leads"]["rows"][2][KEY_COL] == "g:1"


def test_existing_lead_id_is_adopted():
    sess, sync = make()
    sync.ensure_tabs()
    sync.upsert_leads([row("g:1", "A", "PP-00007")])
    _, _, adopted = sync.upsert_leads([row("g:1", "A", "PP-00001")])  # e.g. state database was rebuilt
    assert adopted == {"g:1": "PP-00007"} and sess.tabs["Leads"]["rows"][1][0] == "PP-00007"


def test_refuses_foreign_tab_layout():
    sess, sync = make()
    sess.tabs["Leads"] = {"id": 9, "rows": [["ID", "Date", "Name", "Email"]]}
    with pytest.raises(SheetsError):
        sync.ensure_tabs()
    assert sess.tabs["Leads"]["rows"] == [["ID", "Date", "Name", "Email"]]   # untouched


def test_plan_and_report_tabs():
    sess, sync = make()
    sync.ensure_tabs()
    sync.write_plan([[1, "Salt Lake"], [2, "Howrah"]])
    sync.write_plan([[1, "Salt Lake"]])
    plan = [r for r in sess.tabs["Plan"]["rows"][1:] if any(str(x) for x in r)]
    assert plan == [[1, "Salt Lake"]]
    sync.append_report(["2026-10-06", "06:07"])
    sync.append_report(["2026-10-07", "06:07"])
    assert [r[0] for r in sess.tabs["Daily Report"]["rows"][1:]] == ["2026-10-06", "2026-10-07"]
