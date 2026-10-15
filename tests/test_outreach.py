"""Outreach: e-mail sequences + WhatsApp queue against a fake Gmail (SMTP/IMAP) and a fake Google Sheet."""
from __future__ import annotations

import re
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from zoneinfo import ZoneInfo

from conftest import make_config
from helpers import FakeSheetsSession

from leadgen.outreach.classify import classify_reply, new_text
from leadgen.outreach.engine import Outreach
from leadgen.outreach.inbox import GmailInbox
from leadgen.outreach.mailer import GmailSender, build_message, classify_smtp_error
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
def lead(n, name, *, email="", phones="", whatsapp="", priority="Medium", category="Cafe", status="New", person="", usp=""):
    return {"Lead ID": f"PP-{n:05d}", "Business Name": name, "Category": category, "Area": "Salt Lake",
            "Phones": phones, "WhatsApp": whatsapp, "Emails": email, "Priority": priority, "Contact Person": person,
            "Key": f"key{n}", "Status": status, "USP": usp}


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
    kw.setdefault("mx_check", lambda domain: True)
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


def test_sender_phone_and_alert_address_can_come_from_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_SENDER_PHONE", "+91 98765 43210")
    sess, client = sheet_with(LEADS[:1])
    smtp, imap, clock = SmtpWorld(), ImapWorld(), Clock(ts(2026, 10, 14, 18))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    cfg = cfg_with()
    cfg["outreach"]["sender"] = {**SENDER, "phone": "", "notify_email": ""}
    runner(cfg, store, client, clock, smtp, imap).run()
    first = smtp.sent[0]
    assert "+91 98765 43210" in first.get_content()                     # the secret fills the signature
    imap.add(mail("Asha <hello@leafcafe.in>", "me@gmail.com", "Re: " + first["Subject"], "Yes, send rates",
                  in_reply_to=first["Message-ID"]))
    clock.t = ts(2026, 10, 15, 11)
    runner(cfg, store, client, clock, smtp, imap).run()
    assert len(smtp.to("me@gmail.com")) == 1                            # no alert address: the outreach Gmail gets it
    monkeypatch.setenv("OUTREACH_NOTIFY_EMAIL", "boss@example.com")
    assert Outreach(cfg, store, live=False, use_sheet=False).o["sender"]["notify_email"] == "boss@example.com"
    assert cfg["outreach"]["sender"]["notify_email"] == ""              # the config itself is not changed


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


RAMP = {"start_per_day": 40, "step": 30, "step_every_days": 1, "max_per_day": 350}


def level_engine(tmp_path, clock=None, **email):
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    cfg = make_config(outreach={"sender": SENDER, "email": {**RAMP, **email}})
    clock = clock or Clock(ts(2026, 10, 16, 11))
    return store, Outreach(cfg, store, live=True, use_sheet=False, sender=None, inbox=None, now_fn=clock, sleep_fn=clock.sleep)


def sent_on(store, day, n, status="sent", at=None):
    at = at if at is not None else datetime.fromisoformat(day + "T12:00:00+05:30").timestamp()
    with store.tx():
        store.conn.executemany("INSERT INTO sends(email,lead_key,step,sent_at,day,status) VALUES(?,?,1,?,?,?)",
                               [(f"x{day}{i}{status}@d.in", "k", at, day, status) for i in range(n)])


def next_day(o, day):
    o._save_level(o._level(day))
    return o._daily_cap(day)


def test_daily_limit_rises_after_each_good_day_up_to_the_maximum(tmp_path):
    store, o = level_engine(tmp_path)
    caps, days = [], [f"2026-10-{d:02d}" for d in (16, 17, 19, 20, 21, 22, 23, 24, 26, 27, 28, 29, 30)]
    for day in days:
        caps.append(next_day(o, day))
        sent_on(store, day, caps[-1])                       # the whole limit went out
    assert caps == [40, 70, 100, 130, 160, 190, 220, 250, 280, 310, 340, 350, 350]
    assert [r[1] for r in store.get("email_level_log")] == caps
    assert store.get("email_level_log")[1][2] == "good day - raised"


def test_daily_limit_only_rises_when_the_day_used_it(tmp_path):
    store, o = level_engine(tmp_path)
    assert next_day(o, "2026-10-16") == 40
    sent_on(store, "2026-10-16", 20)                        # half: no evidence Gmail takes more
    assert next_day(o, "2026-10-17") == 40 and "20 of 40 sent" in o._level("2026-10-17")["why"]
    assert next_day(o, "2026-10-19") == 40                  # nothing sent on the 17th (e.g. tablet off)
    sent_on(store, "2026-10-19", 33)                        # 80% or more counts as a good day
    assert next_day(o, "2026-10-20") == 70
    assert o._daily_cap("2026-10-20") == 70                 # fixed for the day, however often asked


def test_gmail_limit_cuts_the_limit_then_it_settles_and_rises_again(tmp_path):
    store, o = level_engine(tmp_path)
    store.set("email_level", {"day": "2026-10-16", "cap": 250, "good": 0, "hold": 0, "flag": "limit", "why": ""})
    sent_on(store, "2026-10-16", 180)
    caps = []
    for day in ("2026-10-17", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"):
        caps.append(next_day(o, day))
        sent_on(store, day, caps[-1])
    assert caps == [150, 150, 150, 150, 180]                # 60%, three calm days, then up again
    store.set("email_level", {"day": "2026-10-22", "cap": 180, "good": 0, "hold": 0, "flag": "blocked", "why": ""})
    assert next_day(o, "2026-10-23") == 90                  # a block from Gmail: half


def test_limit_never_drops_below_the_start_and_restarts_lower_after_a_long_break(tmp_path):
    store, o = level_engine(tmp_path)
    store.set("email_level", {"day": "2026-10-16", "cap": 50, "good": 0, "hold": 0, "flag": "blocked", "why": ""})
    assert next_day(o, "2026-10-17") == 40
    store.set("email_level", {"day": "2026-10-01", "cap": 300, "good": 0, "hold": 0, "flag": "", "why": ""})
    sent_on(store, "2026-10-01", 300)
    assert next_day(o, "2026-10-17") == 150 and "no sending for 16 days" in o._level("2026-10-17")["why"]


def test_owner_can_lower_the_maximum_any_time(tmp_path):
    store, o = level_engine(tmp_path)
    store.set("email_level", {"day": "2026-10-16", "cap": 300, "good": 0, "hold": 0, "flag": "", "why": ""})
    sent_on(store, "2026-10-16", 300)
    o.o["email"]["max_per_day"] = 120
    assert next_day(o, "2026-10-17") == 120


def test_high_bounce_rate_stops_the_rise(tmp_path):
    clock = Clock(ts(2026, 10, 17, 11))
    store, o = level_engine(tmp_path, clock)
    store.set("email_level", {"day": "2026-10-16", "cap": 100, "good": 0, "hold": 0, "flag": "", "why": ""})
    sent_on(store, "2026-10-16", 100)
    sent_on(store, "2026-10-16", 5, status="bounced")       # 5% >= hold_bounce_rate 4%
    assert next_day(o, "2026-10-17") == 100 and "bounce rate 5.0%" in o._level("2026-10-17")["why"]


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


def test_first_email_quotes_the_usp_stays_short_and_usp_leads_go_first(tmp_path):
    leads = [lead(1, "Plain Cafe", email="hi@plaincafe.in", priority="High"),
             lead(2, "Peter Cat", email="info@petercat.in", priority="High", category="Restaurant",
                  usp="Iconic Park Street restaurant famous for its Chelo Kebab since 1975")]
    sess, client = sheet_with(leads)
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 11))
    runner(cfg_with(start_per_day=1), OutreachStore(str(tmp_path / "o.sqlite")), client, clock, smtp, live=False).run()
    preview = tab(sess, "Email Preview")
    assert [p["Business"] for p in preview] == ["Peter Cat"]                 # the personalised one first
    body = preview[0]["Body"]
    assert 'liked this line on your website: "Iconic Park Street restaurant famous for its Chelo Kebab since 1975".' in body
    assert 50 <= len(body.split()) <= 150 and "http" not in body


def test_paused_email_sends_only_a_manual_test_batch(tmp_path):
    sess, client = sheet_with(LEADS)
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    cfg = cfg_with()
    cfg["outreach"]["email"]["enabled"] = False
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 11))
    code, s = runner(cfg, store, client, clock, smtp).run()                   # the scheduled run: nothing goes out
    assert smtp.sent == [] and any("paused" in n for n in s["notes"])
    code, s = runner(cfg, store, client, clock, smtp, max_emails=1).run()     # the owner's test: exactly one
    assert len(smtp.sent) == 1 and s["new"] == 1


def test_one_business_listed_twice_gets_one_email(tmp_path):
    leads = [lead(1, "Mayur Residency", email="res@itsindia.in", priority="High"),
             lead(2, "Mayur Residency Kolkata", email="kolkata@mayurhotels.in", priority="High"),
             lead(3, "Other Hotel", email="hi@otherhotel.in", priority="High")]
    leads[0]["Website"] = leads[1]["Website"] = "https://mayurhotels.in/"
    sess, client = sheet_with(leads)
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 11))
    runner(cfg_with(start_per_day=5), OutreachStore(str(tmp_path / "o.sqlite")), client, clock, smtp, max_emails=3).run()
    assert sorted(m["To"] for m in smtp.sent) == ["hi@otherhotel.in", "res@itsindia.in"]


def test_wrong_gmail_password_touches_no_lead(tmp_path):
    sess, client = sheet_with(LEADS)
    smtp, clock = SmtpWorld(auth_ok=False), Clock(ts(2026, 10, 14, 11))
    code, s = runner(cfg_with(), OutreachStore(str(tmp_path / "o.sqlite")), client, clock, smtp, max_emails=2).run()
    assert code == 2 and smtp.sent == [] and any("Gmail refused the login" in n for n in s["notes"])
    assert all(status_of(sess, f"key{i}") in ("New", "Customer") for i in range(1, 7))


def test_unreachable_gmail_stops_at_the_first_failure(tmp_path):
    sess, client = sheet_with(LEADS)
    tries = []

    def fail(msg):
        tries.append(msg["To"])
        return OSError("timed out")
    smtp, clock = SmtpWorld(fail=fail), Clock(ts(2026, 10, 14, 11))
    code, s = runner(cfg_with(), OutreachStore(str(tmp_path / "o.sqlite")), client, clock, smtp, max_emails=3).run()
    assert code == 2 and len(set(tries)) == 1 and any("could not reach Gmail" in n for n in s["notes"])


def test_gmail_hanging_up_during_login_is_reported_as_a_password_problem():
    class HangUp(SmtpWorld):
        def factory(self, host, port, timeout=None):
            conn = super().factory(host, port, timeout)

            def login(user, pw):
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            conn.login = login
            return conn
    sender = GmailSender("me@gmail.com", "my normal password", smtp_factory=HangUp().factory)
    problem = sender.check()
    assert "Gmail refused the login" in problem and "App Password" in problem
    res = sender.send(build_message(sender_name="x", sender_addr="me@gmail.com", to="a@b.in", subject="s", body="b"))
    assert res.status == "auth"


def test_sales_type_address_is_written_to_first(tmp_path):
    leads = [lead(1, "ITC Royal Bengal", email="grievanceofficer@itchotels.com\nreservations@itchotels.com", priority="High")]
    sess, client = sheet_with(leads)
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 14, 11))
    runner(cfg_with(), OutreachStore(str(tmp_path / "o.sqlite")), client, clock, smtp, max_emails=1).run()
    assert [m["To"] for m in smtp.sent] == ["reservations@itchotels.com"]


def test_dry_run_copy_lists_the_emails_it_would_send(tmp_path, monkeypatch):
    """The maintainer's encrypted copy of a dry-run shows the e-mails a live run would send."""
    import csv
    import types

    import leadgen.outreach.engine as engine_mod
    import leadgen.outreach.store as store_mod
    from leadgen import cli

    class FakeEngine:
        def __init__(self, cfg, store, live=None, max_emails=None):
            self.sent_log = []
            self.preview = [["2026-10-09", "L1", "Leaf Cafe", "hello@leafcafe.example", "Plants for Leaf Cafe",
                             "Hello Leaf Cafe team,", "k1"]]

        def run(self):
            return 0, {"mode": "dry-run", "status": "ok", "notes": []}

    monkeypatch.setattr(engine_mod, "Outreach", FakeEngine)
    monkeypatch.setattr(store_mod, "OutreachStore", lambda path: types.SimpleNamespace(close=lambda: None))
    out = tmp_path / "sent.csv"
    monkeypatch.setenv("OUTREACH_SENT_CSV", str(out))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    args = types.SimpleNamespace(db=str(tmp_path / "o.sqlite"), mode="dry-run", max_emails=2)
    assert cli.cmd_outreach(None, args) == 0
    rows = list(csv.reader(out.open(encoding="utf-8")))
    assert rows == [["Lead ID", "Business", "To", "Result", "Subject", "Body"],
                    ["L1", "Leaf Cafe", "hello@leafcafe.example", "dry-run (not sent)", "Plants for Leaf Cafe",
                     "Hello Leaf Cafe team,"]]


# -- automatic sending at volume: limits, brakes, alerts ---------------------------------------
def test_gmail_limit_is_remembered_as_a_ceiling_that_rises_slowly(tmp_path):
    clock = Clock(ts(2026, 10, 16, 15))
    store, o = level_engine(tmp_path, clock)
    store.set("email_level", {"day": "2026-10-16", "cap": 250, "good": 0, "hold": 0, "flag": "", "why": ""})
    sent_on(store, "2026-10-16", 200, at=clock.t - 3600)
    o._flag_day("limit")                                    # Gmail stopped us after 200 in 24 hours
    lvl = store.get("email_level")
    assert lvl["flag"] == "limit" and lvl["ceiling"] == 180
    caps = []
    for d in (17, 19, 20, 21, 22, 23, 24, 26, 27, 28, 29, 30):
        day = f"2026-10-{d:02d}"
        caps.append(next_day(o, day))
        sent_on(store, day, caps[-1])
    assert caps[0] == 150 and max(caps) <= 210
    assert caps[:6] == [150, 150, 150, 150, 180, 180]       # cut, settle, then up to the ceiling only
    assert 210 in caps                                      # after a week at the ceiling: one careful step
    assert "Gmail's limit for this account" in store.get("email_level_log")[5][2]


def bounce_world(bad_addrs):
    def fail(msg):
        if msg["To"] in bad_addrs:
            return smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"5.1.1 The email account that you tried to reach does not exist")})
    return SmtpWorld(fail=fail)


def many_leads(n, start=1):
    return [lead(i, f"Shop {i}", email=f"hi@shop{i}.in", priority="High") for i in range(start, start + n)]


def test_many_bounces_in_a_day_pause_until_tomorrow_and_count_again_after(tmp_path):
    leads = many_leads(40)
    sess, client = sheet_with(leads)
    smtp = bounce_world({f"hi@shop{i}.in" for i in range(1, 11)})          # the first 10 do not exist
    clock = Clock(ts(2026, 10, 16, 18, 15))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    cfg = cfg_with(start_per_day=30, max_per_day=30, max_per_run=30, max_bounces_per_day=10, pause_day_bounce_rate=0.1,
                   min_gap_seconds=20, max_gap_seconds=30, window_end="18:59")
    code, s = runner(cfg, store, client, clock, smtp).run()
    assert code == 2 and s["bounces"] == 10 and s["new"] == 0
    assert any("SENDING PAUSED" in n and "10 bounces" in n for n in s["notes"])
    assert store.get("email_level")["flag"] == "bounces_day"
    alert = smtp.to("owner@example.com")
    assert len(alert) == 1 and "paused until" in alert[0]["Subject"]       # the owner hears it at once
    clock.t = ts(2026, 10, 17, 11)                          # next day: the same 10 bounces do not stop it again
    code, s = runner(cfg, store, client, clock, smtp).run()
    assert code == 0 and len(smtp.to("hi@shop11.in")) == 1
    assert "many bounces on the last sending day - kept" in store.get("email_level")["why"]


def test_high_weekly_bounce_rate_pauses_two_days_once(tmp_path):
    clock = Clock(ts(2026, 10, 16, 11))
    sess, client = sheet_with(many_leads(5))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    sent_on(store, "2026-10-14", 100, at=clock.t - 2 * 86400)
    sent_on(store, "2026-10-14", 7, status="bounced", at=clock.t - 2 * 86400)   # 7% of 100 in the week
    smtp = SmtpWorld()
    code, s = runner(cfg_with(), store, client, clock, smtp).run()
    assert code == 2 and smtp.sent == [s for s in smtp.sent if s["To"] == "owner@example.com"]
    assert any("bounce rate 7.0%" in n for n in s["notes"])
    clock.t = ts(2026, 10, 17, 11)
    runner(cfg_with(), store, client, clock, smtp).run()
    assert not [m for m in smtp.sent if m["To"] != "owner@example.com"]  # still paused (2 days)
    clock.t = ts(2026, 10, 19, 18, 15)                      # Monday: the old bounces no longer count
    code, s = runner(cfg_with(), store, client, clock, smtp).run()
    assert code == 0 and len([m for m in smtp.sent if m["To"] != "owner@example.com"]) == 3
    assert store.get("email_level")["why"] == "too many addresses bounced - lowered"


def test_gmail_limit_alert_waits_until_gmail_sends_again(tmp_path):
    state = {"limit": True}

    def fail(msg):
        if state["limit"]:
            return smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded.")
    sess, client = sheet_with(many_leads(5))
    smtp, clock = SmtpWorld(fail=fail), Clock(ts(2026, 10, 16, 12))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    code, s = runner(cfg_with(), store, client, clock, smtp).run()
    assert code == 2 and smtp.sent == [] and len(store.get("owner_alerts")) == 1
    state["limit"] = False
    clock.t = ts(2026, 10, 16, 15)
    runner(cfg_with(), store, client, clock, smtp).run()
    assert smtp.sent == [] and len(store.get("owner_alerts")) == 1     # still paused: not even the alert is tried
    clock.t = ts(2026, 10, 17, 11)
    runner(cfg_with(), store, client, clock, smtp).run()
    assert len(smtp.to("owner@example.com")) == 1 and store.get("owner_alerts") == []
    assert "Gmail's daily limit was reached" in store.get("email_level")["why"]


def test_domain_without_mail_server_is_skipped_and_never_tried_again(tmp_path):
    leads = [lead(1, "Gone Cafe", email="hi@expired.in", priority="High"), lead(2, "Live Cafe", email="hi@live.in")]
    sess, client = sheet_with(leads)
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 16, 18))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    checked = []

    def mx(domain):
        checked.append(domain)
        return domain != "expired.in"
    runner(cfg_with(), store, client, clock, smtp, mx_check=mx).run()
    assert [m["To"] for m in smtp.sent] == ["hi@live.in"]
    assert store.suppressed("hi@expired.in")
    assert "Do Not Contact" in sess.tabs and any(r["Email or Phone"] == "hi@expired.in" for r in tab(sess, "Do Not Contact"))


def test_stop_request_ends_the_batch_after_the_current_email(tmp_path):
    sess, client = sheet_with(many_leads(10))
    smtp, clock = SmtpWorld(), Clock(ts(2026, 10, 16, 18))
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    o = runner(cfg_with(start_per_day=10, max_per_run=10), store, client, clock, smtp)
    real_sleep = clock.sleep

    def sleep(sec):
        real_sleep(sec)
        if len(smtp.sent) == 3:
            o.stop_requested = True                        # the tablet asks it to stop
    o.sleep = sleep
    code, s = o.run()
    assert len(smtp.sent) == 3 and s["new"] == 3 and any("stopped on request" in n for n in s["notes"])
    assert sum(1 for r in tab(sess, "Leads") if r["Status"] == "Emailed") == 3     # the sheet is still written


def test_automatic_sending_over_two_weeks(tmp_path):
    """The whole loop (scripts/simulate_outreach.py runs the same at full size), hourly runs Monday-Saturday: the limit climbs, follow-ups go out once, bounces and
    replies stop conversations, nothing goes out twice or outside business hours."""
    import random as _random

    rnd = _random.Random(7)
    leads = [lead(i, f"Shop {i}", email=f"hi@shop{i}.in" if i % 10 < 6 else "", priority="High") for i in range(1, 501)]
    bad = {f"hi@shop{i}.in" for i in range(1, 501) if i % 37 == 0}
    sess, client = sheet_with(leads)
    imap, log = ImapWorld(), []
    clock = Clock(ts(2026, 10, 16, 10, 11))
    pending = []

    def fail(msg):
        to = msg["To"]
        log.append((clock.t, to, msg.get("In-Reply-To", "")))
        if to in bad:
            pending.append((clock.t + 600, mail("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "me@gmail.com",
                                                "Delivery Status Notification (Failure)", f"Address not found: {to}",
                                                extra={"X-Failed-Recipients": to})))
        elif not msg.get("In-Reply-To") and to != "owner@example.com" and rnd.random() < 0.03:
            pending.append((clock.t + 7200, mail(to, "me@gmail.com", "Re: " + msg["Subject"], "Yes please, send the list",
                                                 in_reply_to=msg["Message-ID"])))
    smtp = SmtpWorld(fail=fail)
    store = OutreachStore(str(tmp_path / "o.sqlite"))
    cfg = cfg_with(start_per_day=10, step=5, step_every_days=1, max_per_day=40, max_per_run=10, min_gap_seconds=30,
                   max_gap_seconds=80, run_budget_minutes=45, window_end="19:00", follow_up_days=[4],
                   pause_min_sends=100, max_bounces_per_day=10)
    per_day = {}
    for d in range(16):
        day = datetime(2026, 10, 16, tzinfo=IST) + timedelta(days=d)
        if day.weekday() == 6:
            continue
        for h in range(10, 19):
            clock.t = max(clock.t, ts(day.year, day.month, day.day, h, 11))
            for item in [x for x in pending if x[0] <= clock.t]:
                imap.add(item[1])
            pending[:] = [x for x in pending if x[0] > clock.t]
            start = clock.t
            code, s = runner(cfg, store, client, clock, smtp, imap).run()
            assert clock.t - start <= 47 * 60, "a run must end well before the next one"
            assert code in (0, 2), s
        per_day[day.date().isoformat()] = store.get("email_level")["cap"]
    to_leads = [x for x in log if x[1] != "owner@example.com"]
    caps = list(per_day.values())
    assert caps[:5] == [10, 15, 20, 25, 30] and caps[-1] == 40
    for t, *_ in to_leads:                                   # business hours, never on Sunday
        lt = datetime.fromtimestamp(t, IST)
        assert lt.weekday() != 6 and "10:00" <= lt.strftime("%H:%M") < "19:00"
    by_day = {}
    for t, *_ in to_leads:
        k = datetime.fromtimestamp(t, IST).date().isoformat()
        by_day[k] = by_day.get(k, 0) + 1
    assert all(by_day[k] <= per_day[k] for k in by_day)      # never above the day's limit
    firsts = [to for _, to, irt in to_leads if not irt]
    assert len(firsts) == len(set(firsts))                   # nobody gets the first e-mail twice
    counts = {}
    for _, to, _ in to_leads:
        counts[to] = counts.get(to, 0) + 1
    assert max(counts.values()) == 2                          # first e-mail + one follow-up, never more
    replied = {r["email"] for r in store.q("SELECT email FROM replies")}
    assert replied and all(counts[a] <= 2 for a in replied)
    for r in store.q("SELECT email, received_at FROM replies"):
        assert not [x for x in to_leads if x[1] == r["email"] and x[0] > r["received_at"]]   # a reply ends it
    bounced = {r["email"] for r in store.q("SELECT email FROM threads WHERE stage='bounced'")}
    assert bounced and all(counts[a] == 1 for a in bounced)  # a bounce ends it
    assert not [x for x in to_leads if x[1] not in {f"hi@shop{i}.in" for i in range(1, 501)}]
