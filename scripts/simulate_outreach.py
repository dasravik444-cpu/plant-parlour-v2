"""Multi-week simulation of automatic outreach against a fake Gmail (SMTP+IMAP) and a fake Google Sheet.

usage: python scripts/simulate_outreach.py [--days 24] [--leads 8000] [--bad 0.03] [--quota 0] [--block-day N] [--seed 1]
"""
import argparse, os, random, smtplib, sys, time, tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO); sys.path.insert(0, REPO + "/tests")
os.environ["OUTREACH_SENDER_PHONE"] = "+91 90000 00000"

from helpers import FakeSheetsSession                      # noqa: E402
from test_outreach import SmtpWorld, ImapWorld, mail, Clock  # noqa: E402
from leadgen.config import load_config                     # noqa: E402
from leadgen.outreach.engine import Outreach               # noqa: E402
from leadgen.outreach.inbox import GmailInbox              # noqa: E402
from leadgen.outreach.mailer import GmailSender            # noqa: E402
from leadgen.outreach.store import OutreachStore           # noqa: E402
from leadgen.sheets import LEAD_COLUMNS, SheetsClient      # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=24)
ap.add_argument("--leads", type=int, default=8000)
ap.add_argument("--bad", type=float, default=0.03, help="share of addresses that bounce")
ap.add_argument("--quota", type=int, default=0, help="fake Gmail rolling-24h quota (0 = none)")
ap.add_argument("--block-day", type=int, default=0, help="day index on which Gmail answers 5.7.1 spam block once")
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--start", default="2026-10-16")
args = ap.parse_args()
rnd = random.Random(args.seed)

cfg = load_config(REPO + "/config/plant-parlour.toml")
cfg["outreach"]["sender"].update({"phone": "+91 90000 00000"})
E = cfg["outreach"]["email"]
cats = ["Cafe", "Restaurant", "Hotel", "Banquet Venue", "Office", "Salon / Spa", "Clinic", "Interior Designer"]

rows, bad, emails_of = [], set(), {}
for i in range(1, args.leads + 1):
    r = {c: "" for c in LEAD_COLUMNS}
    r.update({"Lead ID": f"PP-{i:05d}", "Business Name": f"Business {i} {rnd.choice(['Cafe','Hotel','Studio','Clinic','House'])}",
              "Category": rnd.choice(cats), "Area": "Salt Lake", "Key": f"k{i}", "Status": "New",
              "Priority": rnd.choice(["High", "Medium", "Medium", "Low"]),
              "Phones": f"+91 98{rnd.randint(10000000, 99999999)} (mobile)"})
    u = rnd.random()
    if u < 0.50:
        r["Emails"] = f"info@biz{i}.in"
    elif u < 0.56:
        r["Emails"] = f"biz{i}owner@gmail.com"
    if r["Emails"]:
        emails_of[r["Key"]] = r["Emails"]
        if rnd.random() < args.bad:
            bad.add(r["Emails"])
    rows.append([r[c] for c in LEAD_COLUMNS])
sess = FakeSheetsSession()
sess.tabs["Leads"] = {"id": 9, "rows": [list(LEAD_COLUMNS)] + rows}
client = SheetsClient("sheet", session=sess)

clock = Clock(datetime.fromisoformat(args.start + "T10:11:00+05:30").timestamp())
imap = ImapWorld()
sent_log = []          # (ts, to, subject, msgid, in_reply_to)
owner_mail = []
pending = []           # (deliver_ts, raw)
block_once = {"armed": False}


def fail(msg):
    now = clock.t
    if block_once["armed"]:
        block_once["armed"] = False
        return smtplib.SMTPDataError(550, b"5.7.1 Our system has detected an unusual rate of unsolicited mail. blocked")
    if args.quota:
        n24 = sum(1 for t, *_ in sent_log if t > now - 86400)
        if n24 >= args.quota:
            return smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded.")
    to = msg["To"]
    if to == "me@gmail.com":          # alerts to the owner (no notify address set)
        owner_mail.append((now, msg["Subject"]))
        return None
    sent_log.append((now, to, msg["Subject"], msg["Message-ID"], msg.get("In-Reply-To", "")))
    if to in bad:
        pending.append((now + 300, mail("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "me@gmail.com",
                                        "Delivery Status Notification (Failure)", f"Address not found: {to}",
                                        extra={"X-Failed-Recipients": to})))
    elif not msg.get("In-Reply-To") and to.endswith((".in", "gmail.com")) and "Lead outreach" not in str(msg["From"]):
        u = rnd.random()
        if u < 0.008:
            pending.append((now + rnd.uniform(2, 30) * 3600, mail(to, "me@gmail.com", "Re: " + msg["Subject"],
                                                                  "Yes please send the price list", in_reply_to=msg["Message-ID"])))
        elif u < 0.012:
            pending.append((now + rnd.uniform(2, 30) * 3600, mail(to, "me@gmail.com", "Re: " + msg["Subject"],
                                                                  "Not interested, please remove us", in_reply_to=msg["Message-ID"])))
    return None


smtp = SmtpWorld(fail=fail)
_orig_factory = smtp.factory
store = OutreachStore(os.path.join(tempfile.mkdtemp(), "outreach.sqlite"))

day0 = datetime.fromisoformat(args.start).date()
report = []
slots = [f"{h:02d}:11" for h in range(10, 19)]
violations = []
for d in range(args.days):
    day = day0 + timedelta(days=d)
    if day.weekday() == 6:
        continue
    if args.block_day and d == args.block_day:
        block_once["armed"] = True
    runs, max_run = 0, 0.0
    for sl in slots:
        start = datetime.fromisoformat(f"{day}T{sl}:00+05:30").timestamp()
        clock.t = max(clock.t, start)
        # deliver mail that has arrived by now
        for item in sorted([p for p in pending if p[0] <= clock.t]):
            imap.add(item[1])
        pending[:] = [p for p in pending if p[0] > clock.t]
        t0 = clock.t
        sender = GmailSender("me@gmail.com", "abcd efgh ijkl mnop", smtp_factory=smtp.factory)
        inbox = GmailInbox("me@gmail.com", "abcd efgh ijkl mnop", imap_factory=imap.factory)
        o = Outreach(cfg, store, client=client, sender=sender, inbox=inbox, live=True, now_fn=clock, sleep_fn=clock.sleep,
                     rng=rnd, mx_check=lambda dom: True)
        w0 = time.time()
        code, s = o.run()
        runs += 1
        max_run = max(max_run, clock.t - t0)
        if clock.t - t0 > (E["run_budget_minutes"] + 2) * 60:
            violations.append(f"{day} {sl}: run took {(clock.t - t0) / 60:.1f} min")
        if code not in (0,) and not any("PAUSED" in n for n in s["notes"]):
            violations.append(f"{day} {sl}: code {code} {s['notes']}")
    ds = str(day)
    lvl = store.get("email_level") or {}
    today_sends = [x for x in sent_log if datetime.fromtimestamp(x[0], IST).date() == day and "Lead outreach" not in x[2]]
    firsts = sum(1 for x in today_sends if not x[4])
    fus = sum(1 for x in today_sends if x[4])
    b = store.scalar("SELECT COUNT(*) FROM sends WHERE day=? AND status IN ('bounced','invalid')", (ds,), 0)
    rep = store.scalar("SELECT COUNT(*) FROM replies WHERE date(received_at,'unixepoch','+5 hours','+30 minutes')=?", (ds,), 0)
    times = [datetime.fromtimestamp(x[0], IST) for x in today_sends]
    for t in times:
        if not ("10:00" <= t.strftime("%H:%M") < E["window_end"]):
            violations.append(f"sent outside window at {t}")
    report.append((ds, day.strftime("%a"), lvl.get("cap") if lvl.get("day") == ds else "-", len(today_sends), firsts, fus, b, rep,
                   times[0].strftime("%H:%M") if times else "-", times[-1].strftime("%H:%M") if times else "-",
                   round(max_run / 60, 1), (lvl.get("why") or "")[:44], (store.get("pause_reason") or "")[:40]
                   if float(store.get("paused_until", 0) or 0) > clock.t else ""))

print(f"{'day':10} {'':3} {'limit':>5} {'sent':>5} {'first':>5} {'f-up':>5} {'bnc':>4} {'repl':>4} {'from':>5} {'to':>5} {'maxrun':>6}  why / pause")
for r in report:
    print(f"{r[0]:10} {r[1]:3} {str(r[2]):>5} {r[3]:>5} {r[4]:>5} {r[5]:>5} {r[6]:>4} {r[7]:>4} {r[8]:>5} {r[9]:>5} {r[10]:>6}  {r[11]} {('| PAUSED: ' + r[12]) if r[12] else ''}")

# integrity checks
firsts_to = Counter(x[1] for x in sent_log if not x[4] and "Lead outreach" not in x[2] and "paused" not in x[2].lower())
dups = [a for a, n in firsts_to.items() if n > 1]
per_addr = Counter(x[1] for x in sent_log if "Lead outreach" not in x[2] and "paused" not in x[2].lower())
over = [a for a, n in per_addr.items() if n > 1 + len(E["follow_up_days"])]
neg = {r["email"] for r in store.q("SELECT email FROM replies WHERE kind='negative'")}
after_neg = []
for r in store.q("SELECT email, received_at FROM replies"):
    after_neg += [x for x in sent_log if x[1] == r["email"] and x[0] > r["received_at"]]
bounced = {r["email"] for r in store.q("SELECT email FROM threads WHERE stage='bounced'")}
after_bounce = [x for x in sent_log if x[1] in bounced and x[4]]
dom = Counter(x[1].split("@")[1] for x in sent_log if not x[4] and not x[1].endswith("gmail.com") and "Lead outreach" not in x[2])
alerts = [x for x in owner_mail if "paused" in x[1].lower()]
print()
print("total e-mails to leads:", sum(per_addr.values()), "| businesses reached:", len(firsts_to), "| bounced:", len(bounced),
      "| replies:", store.scalar("SELECT COUNT(*) FROM replies"), "| owner pause alerts:", len(alerts), "| reply alerts:", len(owner_mail) - len(alerts))
print("first e-mail twice to the same address:", len(dups), dups[:3])
print("more e-mails than the sequence allows:", len(over))
print("e-mails after a reply:", len(after_neg), "| follow-ups after a bounce:", len(after_bounce))
print("first e-mails to the same company domain twice:", sum(1 for n in dom.values() if n > 1))
max_day = max((r[3] for r in report), default=0)
print("most e-mails in one day:", max_day, "| violations:", len(violations))
for v in violations[:15]:
    print("  !", v)
