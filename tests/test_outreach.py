"""Outreach: e-mail sequences + WhatsApp queue against a fake Gmail (SMTP/IMAP) and a fake Google Sheet."""
from __future__ import annotations

import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid
from zoneinfo import ZoneInfo

from conftest import make_config
from helpers import FakeSheetsSession

from leadgen.outreach.classify import classify_reply, new_text
from leadgen.outreach.engine import Outreach
from leadgen.outreach.inbox import GmailInbox
from leadgen.outreach.mailer import GmailSender, classify_smtp_error
from leadgen.outreach.store import OutreachStore
from leadgen.sheets import LEAD_COLUMNS, SheetsClient

IST = ZoneInfo("Asia/Kolkata")
SENDER = {"name": "Ravi Kumar", "business": "Green Supply Co", "phone": "+91 90000 00000", "city": "Kolkata",
          "notify_email": "owner@example.com"}


def ts(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


# -- fake Gmail -------------------------------------------------------------------------------
class SmtpWorld:
    def __init__(self, fail=None, auth_ok=True):
        self.sent: list[EmailMessage] = []
        self.logins = 0
        self.fail = fail or (lambda msg: None)
        self.auth_ok = auth_ok

    def factory(self, host, port, timeout=None):
        world = self

        class Conn:
            def ehlo(self):
                pass

            def starttls(self, context=None):
                pass

            def login(self, user, pw):
                if not world.auth_ok:
                    raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
                world.logins += 1

            def send_message(self, msg):
                exc = world.fail(msg)
                if exc:
                    raise exc
                world.sent.append(msg)
                return {}

            def quit(self):
                pass
        return Conn()

    def to(self, addr):
        return [m for m in self.sent if m["To"] == addr]


class ImapWorld:
    def __init__(self):
        self.msgs: dict[int, bytes] = {}
        self.uidnext = 100

    def add(self, raw: bytes) -> int:
        uid = self.uidnext
        self.msgs[uid] = raw
        self.uidnext += 1
        return uid

    def factory(self):
        world = self

        class Conn:
            def login(self, u, p):
                return "OK", [b"ok"]

            def select(self, box, readonly=False):
                return "OK", [str(len(world.msgs)).encode()]

            def status(self, box, what):
                return "OK", [f'"INBOX" (UIDNEXT {world.uidnext} UIDVALIDITY 7)'.encode()]

            def uid(self, cmd, *args):
                if cmd == "SEARCH":
                    lo = int(re.search(r"UID (\d+):", args[-1]).group(1))
                    uids = sorted(u for u in world.msgs if u >= lo) or ([max(world.msgs)] if world.msgs else [])
                    return "OK", [" ".join(map(str, uids)).encode()]
                if cmd == "FETCH":
                    out = []
                    for i, u in enumerate(int(x) for x in args[0].split(",")):
                        raw = world.msgs[u]
                        if "HEADER.FIELDS" in args[1]:
                            head = raw.split(b"\n\n", 1)[0] + b"\n\n"
                            out += [(f"{i + 1} (UID {u} RFC822.SIZE {len(raw)} BODY[HEADER.FIELDS] {{{len(head)}}}".encode(), head), b")"]
                        else:
                            out += [(f"{i + 1} (UID {u} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
                    return "OK", out
                raise AssertionError(cmd)

            def logout(self):
                pass
        return Conn()


def mail(frm, to, subject, body, in_reply_to=None, extra=None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, to, subject, make_msgid()
    if in_reply_to:
        m["In-Reply-To"] = m["References"] = in_reply_to
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    return m.as_bytes()


# -- fake sheet with a Leads tab ---------------------------------------------------------------
def lead(n, name, *, email="", phones="", whatsapp="", priority="Medium", category="Cafe", status="New", person=""):
    return {"Lead ID": f"PP-{n:05d}", "Business Name": name, "Category": category, "Area": "Salt Lake",
            "Phones": phones, "WhatsApp": whatsapp, "Emails": email, "Priority": priority, "Contact Person": person,
            "Key": f"key{n}", "Status": status}


def sheet_with(leads):
    sess = FakeSheetsSession()
    sess.tabs["Leads"] = {"id": 9, "rows": [list(LEAD_COLUMNS)] + [[r.get(c, "") for c in LEAD_COLUMNS] for r in leads]}
    return sess, SheetsClient("sheet", session=sess)


def tab(sess, name):
    rows = sess.tabs[name]["rows"]
    return [dict(zip(rows[0], r)) for r in rows[1:] if any(str(x) for x in r)]


def status_of(sess, key):
    return next(r["Status"] for r in tab(sess, "Leads") if r["Key"] == key)


def cfg_with(**email):
    e = {"start_per_day": 3, "step": 0, "max_per_day": 40, "max_per_run": 8, **email}
    return make_config(outreach={"sender": SENDER, "email": e,
                                 "whatsapp": {"start_per_day": 2, "step": 0, "max_per_day": 2, "include_mobiles": False}})


def runner(cfg, store, client, clock, smtp, imap=None, live=True, **kw):
    sender = GmailSender("me@gmail.com", "abcd efgh ijkl mnop", smtp_factory=smtp.factory)
    inbox = GmailInbox("me@gmail.com", "abcd efgh ijkl mnop", imap_factory=imap.factory) if imap else None
    return Outreach(cfg, store, client=client, sender=sender, inbox=inbox, live=live, now_fn=clock, sleep_fn=clock.sleep, **kw)


LEADS = [
    lead(1, "Leaf Cafe", email="hello@leafcafe.in", phones="+91 98300 11111 (mobile)", priority="High", person="Asha Roy (Owner)"),
    lead(2, "Brew Corner", email="brew@gmail.com", phones="+91 33 2222 3333 (landline)", priority="Low"),
    lead(3, "Royal Banquet", email="events@royalbanquet.in", phones="+91 98300 33333 (mobile)", priority="Medium",
         category="Banquet"),
    lead(4, "Leaf Cafe Branch 2", email="info@leafcafe.in", priority="High"),      # same domain as lead 1
    lead(5, "Tea Stall", phones="+91 98300 55555 (mobile)", whatsapp="+91 98300 55555", priority="High"),
    lead(6, "Customer Cafe", email="owner@customercafe.in", priority="High", status="Customer"),
]


# ==============================================================================================
def test_dry_run_previews_and_sends_nothing(tmp_path):
    sess, client = sheet_with(LEADS)
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 11))
    o = runner(cfg_with(), OutreachStore(str(tmp_path / "o.sqlite")), client, clock, smtp, live=False)
    code, s = o.run()
    assert code == 0, s
    assert smtp.sent == [] and smtp.logins == 1                  # only a login check
    preview = tab(sess, "Email Preview")
    assert [p["Business"] for p in preview] == ["Leaf Cafe", "Royal Banquet", "Brew Corner"]   # priority order, 3/day
    body = preview[0]["Body"]
    assert body.startswith("Hi Asha,") and "Green Supply Co" in body and 'reply "no"' in body
    assert "http" not in body and "www." not in body             # no links in cold e-mails
    assert all(status_of(sess, f"key{i}") in ("New", "Customer") for i in range(1, 7))
    assert "Gmail login OK" in "; ".join(s["notes"])


def test_live_sends_paced_in_priority_order_and_writes_status(tmp_path):
    sess, client = sheet_with(LEADS)
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 18))       # last hour of the window: whole quota now
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    code, s = runner(cfg_with(), store, client, clock, smtp).run()
    assert code == 0, s
    sent_to = [m["To"] for m in smtp.sent]
    assert sent_to == ["hello@leafcafe.in", "events@royalbanquet.in", "brew@gmail.com"]
    assert "info@leafcafe.in" not in sent_to                   # one conversation per company domain
    assert "owner@customercafe.in" not in sent_to              # owner's own Status = hands off
    assert len(clock.slept) == 2 and all(75 <= g <= 210 for g in clock.slept)
    m = smtp.sent[0]
    assert m["List-Unsubscribe"] and m.get_content_type() == "text/plain"
    assert status_of(sess, "key1") == "Emailed" and status_of(sess, "key6") == "Customer"
    assert len(tab(sess, "Outreach")) == 3 and tab(sess, "Outreach")[0]["Stage"] == "Waiting for reply"
    # same day again: the 3/day limit is used up
    code, s = runner(cfg_with(), store, client, clock, smtp).run()
    assert len(smtp.sent) == 3


def test_follow_ups_are_threaded_then_the_sequence_ends(tmp_path):
    sess, client = sheet_with(LEADS[:1])
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 18))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    runner(cfg_with(), store, client, clock, smtp).run()
    first = smtp.sent[0]
    clock.t = ts(2026, 10, 17, 18)                               # +3 days (Saturday)
    runner(cfg_with(), store, client, clock, smtp).run()
    fu1 = smtp.sent[1]
    assert fu1["Subject"] == "Re: " + first["Subject"] and fu1["In-Reply-To"] == first["Message-ID"]
    assert status_of(sess, "key1") == "Emailed (follow-up 1)"
    clock.t = ts(2026, 10, 21, 18)                               # +7 days
    runner(cfg_with(), store, client, clock, smtp).run()
    assert len(smtp.sent) == 3 and first["Message-ID"] in smtp.sent[2]["References"]
    clock.t = ts(2026, 10, 28, 18)
    runner(cfg_with(), store, client, clock, smtp).run()
    assert len(smtp.sent) == 3                                   # no fourth e-mail
    assert status_of(sess, "key1") == "Emailed - no reply"


def test_positive_reply_stops_sequence_alerts_owner_and_queues_whatsapp(tmp_path):
    sess, client = sheet_with(LEADS[:1])
    smtp, imap, clock = SmtpWorld(), ImapWorld(), Clock(ts(2026, 10, 14, 18))
    imap.add(mail("friend@x.com", "me@gmail.com", "hi", "unrelated mail"))   # old mail is never read
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    runner(cfg_with(), store, client, clock, smtp, imap).run()
    first = smtp.sent[0]
    imap.add(mail("Asha <hello@leafcafe.in>", "me@gmail.com", "Re: " + first["Subject"],
                  "Yes please send the price list.\n\nOn Wed, Ravi wrote:\n> plants for Leaf Cafe", in_reply_to=first["Message-ID"]))
    clock.t = ts(2026, 10, 15, 11)
    code, s = runner(cfg_with(), store, client, clock, smtp, imap).run()
    assert s["positive"] == 1
    assert status_of(sess, "key1") == "Interested - call now"
    reply = tab(sess, "Replies")[0]
    assert reply["Reply Type"] == "Interested" and reply["Reply"].startswith("Yes please") and "wa.me/919830011111" in reply["WhatsApp Chat"]
    assert "facebook.com/ads/library" in reply["Their Ads (Meta)"]
    wa = tab(sess, "WhatsApp Queue")
    assert len(wa) == 1 and wa[0]["Why"].startswith("Replied 'yes'") and wa[0]["Open Chat"].startswith("https://wa.me/919830011111?text=")
    alert = smtp.to("owner@example.com")
    assert len(alert) == 1 and "Leaf Cafe" in alert[0].get_content()
    clock.t = ts(2026, 10, 17, 18)
    runner(cfg_with(), store, client, clock, smtp, imap).run()
    assert len(smtp.to("hello@leafcafe.in")) == 1                 # replied: no follow-up


def test_not_interested_reply_opts_out_everywhere(tmp_path):
    sess, client = sheet_with(LEADS[:1])
    smtp, imap, clock = SmtpWorld(), ImapWorld(), Clock(ts(2026, 10, 14, 18))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    runner(cfg_with(), store, client, clock, smtp, imap).run()
    imap.add(mail("hello@leafcafe.in", "me@gmail.com", "Re: x", "Not interested, please remove us."))
    clock.t = ts(2026, 10, 17, 18)
    runner(cfg_with(), store, client, clock, smtp, imap).run()
    assert status_of(sess, "key1") == "Not interested (opted out)"
    dnc = {r["Email or Phone"] for r in tab(sess, "Do Not Contact")}
    assert {"hello@leafcafe.in", "+919830011111"} <= dnc
    assert len(smtp.to("hello@leafcafe.in")) == 1


def test_bad_address_and_bounce_message_are_handled_without_stopping(tmp_path):
    def fail(msg):
        if msg["To"] == "hello@leafcafe.in":
            return smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"5.1.1 The email account that you tried to reach does not exist")})
    sess, client = sheet_with(LEADS)
    smtp, imap, clock = SmtpWorld(fail=fail), ImapWorld(), Clock(ts(2026, 10, 14, 18))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    code, s = runner(cfg_with(), store, client, clock, smtp, imap).run()
    assert [m["To"] for m in smtp.sent] == ["events@royalbanquet.in", "brew@gmail.com"]   # carried on after the bad one
    assert status_of(sess, "key1") == "Email bounced"
    imap.add(mail("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "me@gmail.com",
                  "Delivery Status Notification (Failure)", "Address not found: brew@gmail.com",
                  extra={"X-Failed-Recipients": "brew@gmail.com"}))
    clock.t = ts(2026, 10, 15, 11)
    code, s = runner(cfg_with(), store, client, clock, smtp, imap).run()
    assert s["bounces"] == 1 and status_of(sess, "key2") == "Email bounced"
    assert {"hello@leafcafe.in", "brew@gmail.com"} <= {r["Email or Phone"] for r in tab(sess, "Do Not Contact")}


def test_gmail_limit_pauses_sending_until_tomorrow(tmp_path):
    def fail(msg):
        if msg["To"] == "events@royalbanquet.in":
            return smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded.")
    sess, client = sheet_with(LEADS)
    smtp, clock = SmtpWorld(fail=fail), Clock(ts(2026, 10, 14, 18))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    code, s = runner(cfg_with(), store, client, clock, smtp).run()
    assert code == 2 and len(smtp.sent) == 1 and any("PAUSED" in n for n in s["notes"])
    clock.t = ts(2026, 10, 14, 18, 20)
    code, s = runner(cfg_with(), store, client, clock, smtp).run()
    assert len(smtp.sent) == 1 and any("paused until" in n for n in s["notes"])


def test_nothing_is_sent_outside_business_hours_or_on_sunday(tmp_path):
    sess, client = sheet_with(LEADS)
    smtp = SmtpWorld()
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    for when in (ts(2026, 10, 18, 12), ts(2026, 10, 14, 21), ts(2026, 10, 14, 8)):     # Sunday, evening, early
        code, s = runner(cfg_with(), store, client, Clock(when), smtp).run()
        assert smtp.sent == [] and any("no e-mails this run" in n for n in s["notes"])


def test_unexpected_error_skips_one_lead_only(tmp_path):
    def fail(msg):
        if msg["To"] == "events@royalbanquet.in":
            return ValueError("odd character")
    sess, client = sheet_with(LEADS)
    smtp, clock = SmtpWorld(fail=fail), Clock(ts(2026, 10, 14, 18))
    code, s = runner(cfg_with(), OutreachStore(str(tmp_path / "o.sqlite")), client, clock, smtp).run()
    assert [m["To"] for m in smtp.sent] == ["hello@leafcafe.in", "brew@gmail.com"]
    assert any("skipped" in n for n in s["notes"])


def test_whatsapp_queue_results_and_not_on_whatsapp(tmp_path):
    leads = [lead(i, f"Shop {i}", whatsapp=f"+91 98300 0000{i}", priority="High") for i in range(1, 5)]
    leads.append(lead(9, "Mobile Only", phones="+91 98300 99999 (mobile)", priority="High"))
    sess, client = sheet_with(leads)
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 11))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    runner(cfg_with(), store, client, clock, smtp, live=False).run()
    wa = tab(sess, "WhatsApp Queue")
    assert [r["Business"] for r in wa] == ["Shop 1", "Shop 2"]                   # daily cap 2
    assert wa[0]["Open Chat"].startswith("https://wa.me/919830000001?text=") and "STOP" in wa[0]["Message"]
    # owner marks the results in the sheet
    rows = sess.tabs["WhatsApp Queue"]["rows"]
    res_col = rows[0].index("Result")
    rows[1][res_col], rows[2][res_col] = "Not on WhatsApp", "Not interested"
    clock.t = ts(2026, 10, 15, 11)
    runner(cfg_with(), store, client, clock, smtp, live=False).run()
    wa = tab(sess, "WhatsApp Queue")
    assert [r["Business"] for r in wa] == ["Shop 1", "Shop 2", "Shop 3", "Shop 4"]   # next two; none queued twice
    assert store.one("SELECT result FROM wa WHERE phone='+919830000001'")["result"] == "not_on_whatsapp"
    assert "+919830000002" in {r["Email or Phone"] for r in tab(sess, "Do Not Contact")}
    assert "Mobile Only" not in [r["Business"] for r in wa]              # mobiles only when include_mobiles = true
    # next day nothing new is added while two rows are still waiting for the owner
    clock.t = ts(2026, 10, 16, 11)
    runner(cfg_with(), store, client, clock, smtp, live=False).run()
    assert len(tab(sess, "WhatsApp Queue")) == 4


def test_whatsapp_mobiles_queued_and_next_number_tried_when_not_on_whatsapp(tmp_path):
    leads = [lead(1, "Two Mobiles", phones="+91 98300 11111 (mobile)\n+91 98300 22222 (mobile)", priority="High"),
             lead(2, "Landline Only", phones="+91 33 2222 3333 (landline)", priority="High"),
             lead(3, "Has Email", email="a@hasemail.in", phones="+91 98300 33333 (mobile)", priority="High"),
             lead(4, "Third Shop", phones="+91 98300 44444 (mobile)")]
    sess, client = sheet_with(leads)
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 11))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    cfg = cfg_with()
    cfg["outreach"]["whatsapp"].update(include_mobiles=True, start_per_day=1, max_per_day=1)
    runner(cfg, store, client, clock, smtp, live=False).run()
    wa = tab(sess, "WhatsApp Queue")
    assert [(r["Business"], r["Number"]) for r in wa] == [("Two Mobiles", "+91 98300 11111")]
    assert wa[0]["Why"].startswith("Mobile number")
    rows = sess.tabs["WhatsApp Queue"]["rows"]
    rows[1][rows[0].index("Result")] = "Not on WhatsApp"
    clock.t = ts(2026, 10, 15, 11)
    runner(cfg, store, client, clock, smtp, live=False).run()
    # the same lead's other mobile is tried next; landlines and e-mail leads are never queued
    assert [r["Number"] for r in tab(sess, "WhatsApp Queue")] == ["+91 98300 11111", "+91 98300 22222"]
    rows = sess.tabs["WhatsApp Queue"]["rows"]
    rows[2][rows[0].index("Result")] = "Sent"
    clock.t = ts(2026, 10, 16, 11)
    runner(cfg, store, client, clock, smtp, live=False).run()
    assert [r["Business"] for r in tab(sess, "WhatsApp Queue")] == ["Two Mobiles", "Two Mobiles", "Third Shop"]


def test_whatsapp_queue_adds_one_day_s_batch_and_tops_up_when_the_limit_rises(tmp_path):
    leads = [lead(i, f"Shop {i}", whatsapp=f"+91 98300 0000{i}", priority="High") for i in range(1, 6)]
    sess, client = sheet_with(leads)
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 11))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    cfg = cfg_with()                                                   # 2 a day
    runner(cfg, store, client, clock, smtp, live=False).run()
    rows = sess.tabs["WhatsApp Queue"]["rows"]
    col = rows[0].index("Result")
    rows[1][col] = rows[2][col] = "Sent"
    clock.t = ts(2026, 10, 14, 15)
    runner(cfg, store, client, clock, smtp, live=False).run()
    assert len(tab(sess, "WhatsApp Queue")) == 2                       # today's 2 are done: nothing more today
    cfg["outreach"]["whatsapp"].update(start_per_day=4, max_per_day=4)
    clock.t = ts(2026, 10, 14, 16)
    runner(cfg, store, client, clock, smtp, live=False).run()
    assert [r["Business"] for r in tab(sess, "WhatsApp Queue")] == ["Shop 1", "Shop 2", "Shop 3", "Shop 4"]


def test_whatsapp_limit_warms_up_with_days_actually_sent(tmp_path):
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    cfg = make_config(outreach={"sender": SENDER})       # defaults: 20/day, +10 every 3 days with sends, max 50
    assert cfg["outreach"]["whatsapp"]["include_mobiles"] is True
    o = Outreach(cfg, store, live=False, use_sheet=False, sender=None, inbox=None)
    assert o._wa_cap("2026-11-01") == 20

    def day(d, result):
        store.conn.execute("INSERT INTO wa(phone,lead_key,lead_id,business,reason,queued_day,result,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?)", (f"+91983000{d:04d}", f"k{d}", "", "", "mobile", f"2026-10-{d:02d}", result, 0))
    for d in range(1, 4):
        day(d, "not_on_whatsapp")                        # nothing actually sent: no warm-up
    assert o._wa_cap("2026-11-01") == 20
    for d in range(4, 10):
        day(d, "sent")
    assert o._wa_cap("2026-11-01") == 40
    for d in range(10, 25):
        day(d, "replied")
    assert o._wa_cap("2026-11-01") == 50


def test_owner_do_not_contact_and_lost_memory_never_double_email(tmp_path):
    leads = [lead(1, "Already Emailed", email="a@shop1.in", status="Emailed", priority="High"),
             lead(2, "Blocked Shop", email="b@shop2.in", priority="High"),
             lead(3, "Fine Shop", email="c@shop3.in")]
    sess, client = sheet_with(leads)
    sess.tabs["Do Not Contact"] = {"id": 20, "rows": [["Email or Phone", "Reason", "Added", "Source"], ["b@shop2.in", "asked", "", ""]]}
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 18))
    runner(cfg_with(), OutreachStore(str(tmp_path / "o.sqlite")), client, clock, smtp).run()
    assert [m["To"] for m in smtp.sent] == ["c@shop3.in"]


def test_daily_limit_warms_up_slowly(tmp_path):
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    cfg = make_config(outreach={"sender": SENDER})       # defaults: 15/day, +5 every 3 sending days, max 40
    o = Outreach(cfg, store, live=True, use_sheet=False, sender=None, inbox=None)
    assert o._daily_cap("2026-11-01") == 15
    for d in range(1, 13):
        store.conn.execute("INSERT INTO sends(email,lead_key,step,sent_at,day,status) VALUES('x','k',1,0,?,'sent')", (f"2026-10-{d:02d}",))
    assert o._daily_cap("2026-11-01") == 35
    for d in range(13, 31):
        store.conn.execute("INSERT INTO sends(email,lead_key,step,sent_at,day,status) VALUES('x','k',1,0,?,'sent')", (f"2026-10-{d:02d}",))
    assert o._daily_cap("2026-11-01") == 40


def test_reply_classification():
    assert classify_reply("Re: plants", "Yes, please send the price list") == "positive"
    assert classify_reply("Re: plants", "haan ji, bhejiye") == "positive"
    assert classify_reply("Re: plants", "No") == "negative"
    assert classify_reply("Re: plants", "no, but send me your catalogue") == "positive"
    assert classify_reply("Re: plants", "Not interested, thanks") == "negative"
    assert classify_reply("unsubscribe", "") == "negative"
    assert classify_reply("Automatic reply: away", "I am out of office") == "auto"
    assert classify_reply("Re: plants", "Who gave you my number?") == "other"
    assert new_text("Sure\n\nOn Mon, X wrote:\n> old") == "Sure"


def test_smtp_errors_are_classified():
    assert classify_smtp_error(550, "5.4.5 Daily user sending limit exceeded") == "limit"
    assert classify_smtp_error(550, "5.1.1 The email account that you tried to reach does not exist") == "invalid"
    assert classify_smtp_error(421, "4.7.0 Try again later") == "temp"


def test_live_mode_refuses_to_start_without_sender_details(tmp_path):
    sess, client = sheet_with(LEADS)
    cfg = make_config()                                   # empty [outreach.sender]
    smtp = SmtpWorld()
    code, s = runner(cfg, OutreachStore(str(tmp_path / "o.sqlite")), client, Clock(ts(2026, 10, 14, 18)), smtp).run()
    assert code == 1 and smtp.sent == [] and "sender" in s["notes"][0]
