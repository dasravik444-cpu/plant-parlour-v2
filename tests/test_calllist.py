"""The owner's daily call list tab, against a fake Google Sheet."""
from __future__ import annotations

from conftest import make_config
from helpers import FakeSheetsSession

from leadgen.calllist import COLUMNS, KEEP_DAYS, make_call_list
from leadgen.sheets import LEAD_COLUMNS, SheetsClient


def lead(n, name, day, *, phones="", person="", email="", whatsapp="", priority="Medium", status="New"):
    return {"Lead ID": f"PP-{n:05d}", "Date Added": day, "Business Name": name, "Category": "Cafe", "Area": "Salt Lake",
            "Address": f"{n} Test Road", "Phones": phones, "WhatsApp": whatsapp, "Emails": email, "Contact Person": person,
            "Priority": priority, "Key": f"k{n}", "Status": status, "Website": f"https://shop{n}.in"}


def sheet(leads):
    sess = FakeSheetsSession()
    sess.tabs["Leads"] = {"id": 9, "rows": [list(LEAD_COLUMNS)] + [[r.get(c, "") for c in LEAD_COLUMNS] for r in leads]}
    return sess, SheetsClient("sheet", session=sess)


def tab(sess, name):
    rows = sess.tabs[name]["rows"]
    return [dict(zip(rows[0], r)) for r in rows[1:] if any(str(x) for x in r)]


TODAY = "2026-11-09"
LEADS = [
    lead(1, "Low Cafe", TODAY, phones="+91 33 2222 1111 (landline)", priority="Low"),
    lead(2, "Best Hotel", TODAY, phones="+91 33 2222 2222 (landline)\n+91 98300 22222 (mobile)", priority="High",
         person="Rina Sen (Owner)", email="hello@besthotel.in (website)", whatsapp="+91 98300 22222"),
    lead(3, "No Phone Salon", TODAY, email="hi@salon.in", priority="High"),
    lead(4, "Old Cafe", "2026-11-08", phones="+91 98300 44444 (mobile)", priority="High"),
    lead(5, "Said Yes Studio", "2026-11-02", phones="+91 98300 55555 (mobile)", status="Interested - call now"),
    lead(6, "Said No Bar", TODAY, phones="+91 98300 66666 (mobile)", status="Not interested (opted out)"),
    lead(7, "Mid Cafe", TODAY, phones="+91 98300 77777 (mobile)", priority="Medium", status="Emailed"),
]


def test_todays_call_list_best_first_with_replies_on_top():
    sess, client = sheet(LEADS)
    s = make_call_list(make_config(), client, TODAY)
    rows = tab(sess, "Calls " + TODAY)
    assert sess.tabs["Calls " + TODAY]["rows"][0] == COLUMNS
    assert [r["Business"] for r in rows] == ["Said Yes Studio", "Best Hotel", "Mid Cafe", "Low Cafe"]
    assert rows[0]["Priority"] == "REPLIED YES - call first"
    best = rows[1]
    assert best["Contact Person"] == "Rina Sen (Owner)" and best["Phone"] == "+91 98300 22222 (mobile)"   # mobile first
    assert best["Other Phones"] == "+91 33 2222 2222 (landline)" and best["E-mail"] == "hello@besthotel.in"
    assert best["WhatsApp"] == "+91 98300 22222" and best["#"] == 2
    assert rows[2]["E-mail Status"] == "Emailed"
    assert s["rows"] == 4 and s["hot"] == 1 and s["with_phone"] == 3 and s["today"] == 5


def test_topping_up_keeps_the_owners_results_and_notes():
    sess, client = sheet(LEADS)
    cfg = make_config()
    make_call_list(cfg, client, TODAY)
    rows = sess.tabs["Calls " + TODAY]["rows"]
    head = rows[0]
    best = next(r for r in rows if "Best Hotel" in r)
    best[head.index("Call Result")] = "Call back later"
    best[head.index("Notes")] = "owner in Delhi till Friday"
    # a later lead run adds a lead for today
    sess.tabs["Leads"]["rows"].append([lead(8, "New High Cafe", TODAY, phones="+91 98300 88888 (mobile)",
                                            priority="High").get(c, "") for c in LEAD_COLUMNS])
    s = make_call_list(cfg, client, TODAY)
    rows = tab(sess, "Calls " + TODAY)
    assert [r["Business"] for r in rows][:3] == ["Said Yes Studio", "Best Hotel", "New High Cafe"]
    assert rows[1]["Call Result"] == "Call back later" and rows[1]["Notes"] == "owner in Delhi till Friday"
    assert s["kept_notes"] == 1


def test_only_two_weeks_of_lists_stay_unless_the_owner_wrote_in_them():
    sess, client = sheet(LEADS)
    cfg = make_config()
    make_call_list(cfg, client, "2026-10-10")
    noted = sess.tabs["Calls 2026-10-10"]["rows"]
    noted[1][COLUMNS.index("Call Result")] = "No answer"          # the owner called someone on that day's list
    for d in range(11, 31):
        make_call_list(cfg, client, f"2026-10-{d:02d}")
    make_call_list(cfg, client, TODAY)
    lists = sorted(t for t in sess.tabs if t.startswith("Calls "))
    assert "Calls 2026-10-10" in lists                          # has the owner's note: kept
    assert len(lists) == KEEP_DAYS + 1 and lists[-1] == "Calls " + TODAY
    assert "Calls 2026-10-11" not in lists                       # empty and old: removed
