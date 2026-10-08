"""One outreach run: read the sheet and the inbox, send today's e-mails, build the WhatsApp queue, write back.

Safe by default: dry-run (nothing is sent) until the owner switches to live. In live mode the daily
e-mail limit starts low and rises slowly (warm-up), e-mails go out only in business hours with random
gaps, every reply/bounce/opt-out stops the conversation, and sending pauses by itself when Gmail signals
a limit or too many addresses bounce."""
from __future__ import annotations

import math
import os
import random
import re
import time
from datetime import datetime, timedelta

from ..enrich.phones import display_phone, parse_phone
from ..sheets import SheetsClient, SheetsError
from ..util import fmt_local, get_logger, jdump, local_now
from . import templates as T
from .classify import snippet
from .inbox import GmailInbox
from .leads import (FREE_MAIL, STATUS_BOUNCED, STATUS_DONE, STATUS_EMAILED, STATUS_FOLLOWUP, STATUS_INTERESTED,
                    STATUS_OPTED_OUT, STATUS_REPLIED, Lead, email_domain, read_leads)
from .mailer import GmailSender, build_message
from .sheet import OutreachSheet
from .whatsapp import REASONS, ads_library_link, result_code, wa_link

log = get_logger("outreach")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
REPLY_LABEL = {"positive": "Interested", "negative": "Not interested", "other": "Replied"}
NEXT_STEP = {"positive": "Call them now (then send the price list on WhatsApp)",
             "negative": "Removed - they will not be contacted again",
             "other": "Read the reply and answer it yourself"}
NO_REPLY_AFTER_DAYS = 5


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _id_num(lead_id: str) -> int:
    digits = re.sub(r"\D", "", lead_id or "")
    return int(digits) if digits else 10**9


class Outreach:
    def __init__(self, cfg, store, *, client=None, sender=None, inbox=None, live: bool | None = None,
                 max_emails: int | None = None, now_fn=time.time, sleep_fn=time.sleep, rng=None, use_sheet: bool = True):
        self.cfg, self.store = cfg, store
        self.o = cfg["outreach"]
        # The repository is public: the owner's number and alert address can come from GitHub secrets instead.
        env_sender = {k: os.environ.get(v, "").strip() for k, v in (("phone", "OUTREACH_SENDER_PHONE"),
                                                                    ("notify_email", "OUTREACH_NOTIFY_EMAIL"))}
        if any(env_sender.values()):
            self.o = {**self.o, "sender": {**self.o["sender"], **{k: v for k, v in env_sender.items() if v}}}
        self.now, self.sleep = now_fn, sleep_fn
        self.rng = rng or random.Random()
        self.max_emails = max_emails
        if live is None:
            live = self.o["mode"] == "live" or os.environ.get("OUTREACH_LIVE", "").strip().lower() in ("1", "true", "yes")
        self.live = live
        addr = os.environ.get("OUTREACH_GMAIL_ADDRESS", "").strip()
        pw = os.environ.get("OUTREACH_GMAIL_APP_PASSWORD", "").strip()
        self.sender = sender if sender is not None else (GmailSender(addr, pw) if addr and pw else None)
        self.inbox = inbox if inbox is not None else (GmailInbox(addr, pw) if addr and pw else None)
        self.client = client
        self.use_sheet = use_sheet
        self.notes: list[str] = []
        self.code = 0
        self.stats = {"new": 0, "followups": 0, "bounces": 0, "positive": 0, "negative": 0, "other": 0,
                      "wa_queued": 0, "wa_done": 0, "planned": 0}
        self.status_updates: dict[str, str] = {}
        self.new_replies: list[dict] = []
        self.preview: list[list] = []
        self.wa_rows: list[list] = []
        self.sent_log: list[list] = []       # (lead id, business, to, result, subject, body) of this run's e-mails
        self.leads: list[Lead] = []
        self.by_key: dict[str, Lead] = {}

    # ------------------------------------------------------------------ helpers
    def _local(self, ts=None):
        return local_now(self.cfg.tz, self.now() if ts is None else ts)

    def _today(self) -> str:
        return self._local().date().isoformat()

    def _set_status(self, lead: Lead | None, status: str) -> None:
        if lead and lead.system_managed:
            self.status_updates[lead.key] = status
            lead.status = status

    def _suppress_lead(self, lead: Lead | None, reason: str, source: str) -> None:
        if not lead:
            return
        for e in lead.emails + lead.unverified_emails:
            self.store.suppress(e, "email", reason, source)
        for e164, _ in lead.phones:
            self.store.suppress(e164, "phone", reason, source)
        for e164 in lead.whatsapp:
            self.store.suppress(e164, "phone", reason, source)
        self.store.conn.execute("UPDATE threads SET stage='opted_out', next_due=NULL, updated_at=? WHERE lead_key=? AND stage='active'",
                                (self.now(), lead.key))

    def _score(self, lead: Lead) -> tuple:
        boost = set(self.o.get("boost_categories") or [])
        return (PRIORITY_RANK.get(lead.priority.lower(), 3), 0 if lead.category_key in boost else 1,
                0 if lead.signals else 1, 0 if lead.usp else 1, _id_num(lead.lead_id))

    def _sender_problem(self) -> str:
        s = self.o["sender"]
        missing = [k for k in ("name", "business", "phone", "city") if not str(s.get(k, "")).strip()]
        if missing:
            hint = " (the phone can be the OUTREACH_SENDER_PHONE secret)" if "phone" in missing else ""
            return f"fill in [outreach.sender] {', '.join(missing)} in the config before going live{hint}"
        if self.sender is None:
            return "set the OUTREACH_GMAIL_ADDRESS and OUTREACH_GMAIL_APP_PASSWORD secrets before going live"
        return ""

    # ------------------------------------------------------------------ run
    def run(self) -> tuple[int, dict]:
        if not self.o.get("enabled", True):
            return 0, {"status": "outreach disabled in the config"}
        if self.live:
            problem = self._sender_problem()
            if problem:
                self.notes.append(problem)
                return 1, self._summary("not started")
        sheet = None
        if self.use_sheet:
            if self.client is None:
                if not self.cfg.sheet_id:
                    self.notes.append("no Google Sheet configured (PP_SHEET_ID)")
                    return 1, self._summary("not started")
                self.client = SheetsClient(self.cfg.sheet_id)
            sheet = OutreachSheet(self.client, self.o["tabs"], self.cfg["sheets"]["leads_tab"])
            try:
                sheet.ensure_tabs()
                self.leads = read_leads(self.client, self.cfg["sheets"]["leads_tab"], self.cfg)
                self.by_key = {lead.key: lead for lead in self.leads}
                self._sync_dnc(sheet)
                self._sync_whatsapp(sheet)
            except SheetsError as exc:
                self.notes.append(f"Google Sheet error: {exc}")
                return 2, self._summary("sheet error")
        self.by_key = {lead.key: lead for lead in self.leads}
        self._check_inbox()
        self._mark_no_reply()
        self._send_emails()
        self._whatsapp_queue()
        status = "ok"
        if sheet is not None:
            try:
                self._write_sheet(sheet)
            except SheetsError as exc:
                self.notes.append(f"Google Sheet error while saving results: {exc}")
                self.code = max(self.code, 2)
                status = "sheet error"
        self._notify_owner()
        summary = self._summary(status)
        if sheet is not None and (self._did_something() or self.notes):
            try:
                sheet.append("report", [self._report_row()])
            except SheetsError:
                pass
        return self.code, summary

    def _did_something(self) -> bool:
        s = self.stats
        return any(s[k] for k in ("new", "followups", "bounces", "positive", "negative", "other", "wa_queued", "planned"))

    # ------------------------------------------------------------------ owner input from the sheet
    def _sync_dnc(self, sheet: OutreachSheet) -> None:
        for value, reason in sheet.read_dnc():
            v = value.strip()
            if v.startswith("@") and "." in v:
                self.store.suppress(v, "domain", reason or "added by owner", "sheet", in_sheet=True)
            elif "@" in v:
                self.store.suppress(v, "email", reason or "added by owner", "sheet", in_sheet=True)
            else:
                p = parse_phone(v, self.cfg["campaign"].get("country", "IN"))
                if p:
                    self.store.suppress(p[0], "phone", reason or "added by owner", "sheet", in_sheet=True)
            # anything already listed in the tab needs no second row
            self.store.conn.execute("UPDATE suppression SET in_sheet=1 WHERE value=?", (v.lower(),))

    def _sync_whatsapp(self, sheet: OutreachSheet) -> None:
        region = self.cfg["campaign"].get("country", "IN")
        for key, number, cell in sheet.read_whatsapp_results():
            code = result_code(cell)
            p = parse_phone(number, region) if number else None
            if not p:
                continue
            row = self.store.one("SELECT result FROM wa WHERE phone=?", (p[0],))
            if row is None:
                # Listed in the tab but unknown here (memory lost): remember it so it is never queued twice.
                lead = self.by_key.get(key)
                self.store.conn.execute("INSERT OR IGNORE INTO wa(phone,lead_key,lead_id,business,reason,queued_day,result,updated_at) "
                                        "VALUES(?,?,?,?,?,?,?,?)", (p[0], key, lead.lead_id if lead else "", lead.business if lead else "",
                                                                    "listed", "", code, self.now()))
                if code == "not_interested":
                    self.store.suppress(p[0], "phone", "not interested (WhatsApp)", "whatsapp")
                    self._suppress_lead(lead, "not interested (WhatsApp)", "whatsapp")
                continue
            if not code or row["result"] == code:
                continue
            self.store.conn.execute("UPDATE wa SET result=?, updated_at=? WHERE phone=?", (code, self.now(), p[0]))
            self.stats["wa_done"] += 1
            lead = self.by_key.get(key)
            if code == "not_interested":
                self.store.suppress(p[0], "phone", "not interested (WhatsApp)", "whatsapp")
                self._suppress_lead(lead, "not interested (WhatsApp)", "whatsapp")
                self._set_status(lead, STATUS_OPTED_OUT)
            elif code == "interested":
                self._set_status(lead, STATUS_INTERESTED)

    # ------------------------------------------------------------------ inbox
    def _check_inbox(self) -> None:
        if self.inbox is None:
            if self.live:
                self.notes.append("inbox not checked: Gmail secrets missing")
            return
        threads = self.store.q("SELECT email, msgids FROM threads")
        known = {t["email"] for t in threads}
        msgids = {}
        for t in threads:
            for m in self.store.msgids(t):
                msgids[m] = t["email"]
        try:
            events, last_uid, validity = self.inbox.scan(known_emails=known, known_msgids=msgids,
                                                         last_uid=self.store.get("imap_last_uid"),
                                                         uidvalidity=self.store.get("imap_uidvalidity"))
        except Exception as exc:  # noqa: BLE001 - an inbox problem must not stop sending or the sheet update
            self.notes.append(f"could not read the Gmail inbox: {type(exc).__name__}: {exc}"[:300])
            if self.live:
                self.code = max(self.code, 2)
            return
        now, today = self.now(), self._today()
        with self.store.tx():
            for ev in events:
                t = self.store.thread(ev.email)
                lead = self.by_key.get(t["lead_key"]) if t else None
                if ev.kind == "bounce":
                    if t and t["stage"] != "bounced":
                        self.store.conn.execute("UPDATE threads SET stage='bounced', next_due=NULL, updated_at=? WHERE email=?",
                                                (now, ev.email))
                        self.store.conn.execute("INSERT INTO sends(email,lead_key,step,sent_at,day,status,error) VALUES(?,?,?,?,?,?,?)",
                                                (ev.email, t["lead_key"], t["step"], now, today, "bounced", "bounce message"))
                        self.store.suppress(ev.email, "email", "bounced (address does not exist)", "bounce")
                        self.stats["bounces"] += 1
                        self._set_status(lead, STATUS_BOUNCED)
                    continue
                if ev.reply_kind == "auto" or t is None:
                    continue
                stage = "opted_out" if ev.reply_kind == "negative" else "replied"
                self.store.conn.execute("UPDATE threads SET stage=?, next_due=NULL, reply_kind=?, reply_at=?, reply_snippet=?, updated_at=? "
                                        "WHERE email=?", (stage, ev.reply_kind, now, snippet(ev.text), now, ev.email))
                self.store.conn.execute("INSERT INTO replies(email,lead_key,kind,received_at,snippet,imap_uid) VALUES(?,?,?,?,?,?)",
                                        (ev.email, t["lead_key"], ev.reply_kind, now, snippet(ev.text, 1000), ev.uid))
                self.stats[ev.reply_kind] += 1
                if ev.reply_kind == "negative":
                    self.store.suppress(ev.email, "email", "asked not to be contacted", "reply")
                    self._suppress_lead(lead, "asked not to be contacted", "reply")
                    self._set_status(lead, STATUS_OPTED_OUT)
                elif ev.reply_kind == "positive":
                    self._set_status(lead, STATUS_INTERESTED)
                    self._queue_opted_in(lead, t)
                else:
                    self._set_status(lead, STATUS_REPLIED)
                self.new_replies.append({"email": ev.email, "kind": ev.reply_kind, "text": ev.text, "lead": lead,
                                         "lead_key": t["lead_key"], "lead_id": t["lead_id"], "business": t["business"]})
            self.store.set("imap_last_uid", last_uid)
            self.store.set("imap_uidvalidity", validity)

    def _queue_opted_in(self, lead: Lead | None, t) -> None:
        """They said yes by e-mail: put their mobile at the top of today's WhatsApp queue."""
        if not lead or not self.o["whatsapp"]["enabled"]:
            return
        numbers = lead.whatsapp + [m for m in lead.mobiles() if m not in lead.whatsapp]
        if not numbers:
            return
        phone = numbers[0]
        row = self.store.one("SELECT result, reason FROM wa WHERE phone=?", (phone,))
        if row is not None and (row["reason"] == "opted_in" or row["result"] == "not_interested"):
            return
        self.store.conn.execute("INSERT INTO wa(phone,lead_key,lead_id,business,reason,queued_day,result,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                                "ON CONFLICT(phone) DO UPDATE SET reason='opted_in', queued_day=excluded.queued_day, result='', "
                                "updated_at=excluded.updated_at",
                                (phone, lead.key, lead.lead_id, lead.business, "opted_in", self._today(), "", self.now()))
        self.wa_rows.append(self._wa_row(lead, phone, "opted_in"))
        self.stats["wa_queued"] += 1

    def _mark_no_reply(self) -> None:
        """Conversations that finished all follow-ups without a reply: tell the owner (a call may still work)."""
        cutoff = self.now() - NO_REPLY_AFTER_DAYS * 86400
        for t in self.store.q("SELECT email, lead_key FROM threads WHERE stage='completed' AND last_sent < ?", (cutoff,)):
            self.store.conn.execute("UPDATE threads SET stage='no_reply', updated_at=? WHERE email=?", (self.now(), t["email"]))
            self._set_status(self.by_key.get(t["lead_key"]), STATUS_DONE)

    # ------------------------------------------------------------------ e-mail
    def _daily_cap(self, today: str) -> int:
        e = self.o["email"]
        days = self.store.scalar("SELECT COUNT(DISTINCT day) FROM sends WHERE status='sent' AND day < ?", (today,), 0)
        return min(e["max_per_day"], e["start_per_day"] + e["step"] * (days // e["step_every_days"]))

    def _window(self, local: datetime) -> tuple[bool, str, datetime]:
        e = self.o["email"]
        end = local.replace(hour=_minutes(e["window_end"]) // 60, minute=_minutes(e["window_end"]) % 60, second=0, microsecond=0)
        if DAY_NAMES[local.weekday()] not in e["days"]:
            return False, "not a sending day", end
        if local.date().isoformat() in [str(d) for d in e.get("skip_dates") or []]:
            return False, "date listed in skip_dates", end
        now_m = local.hour * 60 + local.minute
        if not _minutes(e["window_start"]) <= now_m < _minutes(e["window_end"]):
            return False, f"outside sending hours ({e['window_start']}-{e['window_end']})", end
        return True, "", end

    def _send_emails(self) -> None:
        e = self.o["email"]
        if not e["enabled"] and self.max_emails is None:
            # Paused by the owner; a manual run with "max_emails" still sends that many (test e-mails).
            self.notes.append("e-mail sending paused in the config ([outreach.email] enabled = false)")
            return
        local = self._local()
        today = local.date().isoformat()
        cap = self._daily_cap(today)
        used = self.store.scalar("SELECT COUNT(*) FROM sends WHERE day=? AND status IN ('sent','invalid')", (today,), 0)
        remaining = max(0, cap - used)
        self.cap, self.used_today = cap, used
        if not self.live:
            n = remaining if self.max_emails is None else min(self.max_emails, remaining)
            for item in self._pick(n):
                msg = self._compose(item)
                self.preview.append([today, item["lead"].lead_id, item["lead"].business, item["email"],
                                     msg["Subject"], msg.get_content().strip(), item["lead"].key])
            self.stats["planned"] = len(self.preview)
            self._dry_run_login_check()
            return
        paused = float(self.store.get("paused_until", 0) or 0)
        if paused > self.now():
            self.notes.append(f"sending paused until {fmt_local(self.cfg.tz, paused)} ({self.store.get('pause_reason', '')})")
            return
        ok, why, end = self._window(local)
        if not ok:
            self.notes.append(f"no e-mails this run: {why}")
            return
        if remaining <= 0:
            return
        runs_left = max(1, math.ceil((end - local).total_seconds() / 3600))
        n = min(e["max_per_run"], math.ceil(remaining / runs_left))
        if self.max_emails is not None:
            n = min(self.max_emails, remaining)
        batch = self._pick(n)
        self._send_batch(batch)

    def _dry_run_login_check(self) -> None:
        if self.sender is None:
            self.notes.append("dry-run: nothing sent (Gmail secrets not set yet)")
            return
        problem = self.sender.check()
        self.notes.append("dry-run: nothing sent; " + (f"Gmail login FAILED - {problem}" if problem else "Gmail login OK"))
        if problem:
            self.code = max(self.code, 2)

    def _pick(self, n: int) -> list[dict]:
        if n <= 0:
            return []
        e = self.o["email"]
        now = self.now()
        out: list[dict] = []
        sup = {r["value"] for r in self.store.q("SELECT value FROM suppression")}
        blocked = lambda *vals: any(v and v.lower() in sup for v in vals)  # noqa: E731
        # 1. follow-ups that are due
        for t in self.store.q("SELECT * FROM threads WHERE stage='active' AND next_due IS NOT NULL AND next_due <= ? ORDER BY next_due",
                              (now,)):
            if len(out) >= n:
                break
            lead = self.by_key.get(t["lead_key"])
            dom = email_domain(t["email"])
            if (self.use_sheet and lead is None) or (lead and not lead.system_managed) or blocked(t["email"], "@" + dom):
                self.store.conn.execute("UPDATE threads SET stage='stopped', next_due=NULL, updated_at=? WHERE email=?", (now, t["email"]))
                continue
            if lead and blocked(*[p for p, _ in lead.phones], *lead.whatsapp):
                self.store.conn.execute("UPDATE threads SET stage='opted_out', next_due=NULL, updated_at=? WHERE email=?", (now, t["email"]))
                continue
            out.append({"kind": "followup", "step": t["step"] + 1, "email": t["email"], "lead": lead or self._stub(t), "thread": t})
        # 2. new conversations, best leads first
        used_emails = {r["email"] for r in self.store.q("SELECT email FROM threads")}
        used_leads = {r["lead_key"] for r in self.store.q("SELECT DISTINCT lead_key FROM threads")}
        used_domains = {r["domain"] for r in self.store.q("SELECT DISTINCT domain FROM threads WHERE domain IS NOT NULL")}
        for lead in sorted(self.leads, key=self._score):
            if len(out) >= n:
                break
            # Only leads never contacted: the sheet's Status is the durable record, so even if the outreach
            # memory were lost, nobody gets the first e-mail twice.
            if lead.status.strip().lower() not in ("", "new") or lead.key in used_leads:
                continue
            if blocked(*[p for p, _ in lead.phones], *lead.whatsapp):
                continue
            pool = lead.emails + (lead.unverified_emails if e["include_unverified"] else [])
            for addr in pool:
                dom = email_domain(addr)
                if addr in used_emails or blocked(addr, "@" + dom):
                    continue
                if e["one_per_domain"] and dom not in FREE_MAIL and dom in used_domains:
                    continue
                out.append({"kind": "new", "step": 1, "email": addr, "lead": lead, "thread": None})
                used_emails.add(addr)
                used_leads.add(lead.key)
                if dom not in FREE_MAIL:
                    used_domains.add(dom)
                break
        return out

    def _stub(self, t) -> Lead:
        return Lead(key=t["lead_key"], lead_id=t["lead_id"] or "", business=t["business"] or "")

    def _compose(self, item: dict):
        e, s = self.o["email"], self.o["sender"]
        lead = item["lead"]
        ctx = T.context(lead, self.o)
        addr = self.sender.address if self.sender else "you@example.com"
        if item["kind"] == "new":
            subject = T.render(T.pick(e.get("subjects") or T.SUBJECTS, lead.key), ctx)
            body = T.render(e.get("first") or T.FIRST_EMAIL, ctx)
            return build_message(sender_name=s.get("name", ""), sender_addr=addr, to=item["email"], subject=subject, body=body)
        t = item["thread"]
        follow_ups = e.get("follow_ups") or T.FOLLOW_UPS
        body = T.render(follow_ups[min(item["step"] - 2, len(follow_ups) - 1)], ctx)
        subject = t["subject"] if (t["subject"] or "").lower().startswith("re:") else f"Re: {t['subject']}"
        ids = self.store.msgids(t)
        return build_message(sender_name=s.get("name", ""), sender_addr=addr, to=item["email"], subject=subject, body=body,
                             in_reply_to=ids[-1] if ids else "", references=ids)

    def _send_batch(self, batch: list[dict]) -> None:
        e = self.o["email"]
        started = self.now()
        budget = float(e["run_budget_minutes"]) * 60
        temp_failures = 0
        bounces_today = self.store.scalar("SELECT COUNT(*) FROM sends WHERE day=? AND status IN ('invalid','bounced')",
                                          (self._today(),), 0)
        try:
            for i, item in enumerate(batch):
                if i:
                    gap = self.rng.uniform(float(e["min_gap_seconds"]), float(e["max_gap_seconds"]))
                    if self.now() - started + gap > budget:
                        self.notes.append("run time budget reached - the rest goes out in the next run")
                        break
                    self.sleep(gap)
                    if not self._window(self._local())[0]:
                        break
                try:
                    msg = self._compose(item)
                    res = self.sender.send(msg)
                except Exception as exc:  # noqa: BLE001 - one odd lead must never stop the run
                    self.notes.append(f"skipped {item['email']}: {type(exc).__name__}: {exc}"[:200])
                    continue
                self._record(item, msg, res)
                self.sent_log.append([item["lead"].lead_id, item["lead"].business, item["email"], res.status,
                                      msg["Subject"], msg.get_content().strip()])
                if res.ok:
                    temp_failures = 0
                    continue
                if res.status == "invalid":
                    bounces_today += 1
                    if bounces_today >= e["max_bounces_per_day"]:
                        self._pause(1, f"{bounces_today} addresses bounced today")
                        break
                elif res.status in ("limit", "blocked"):
                    days = 1 if res.status == "limit" else 2
                    self._pause(days, f"Gmail said: {res.code} {res.detail[:150]}")
                    break
                elif res.status == "auth":
                    self.notes.append("Gmail refused the login - check OUTREACH_GMAIL_APP_PASSWORD (it changes when the Google password changes)")
                    self.code = max(self.code, 2)
                    break
                else:
                    temp_failures += 1
                    if temp_failures >= 3:
                        self.notes.append(f"Gmail had temporary problems ({res.detail[:120]}) - trying again next run")
                        break
        finally:
            self.sender.close()
        self._check_bounce_rate()

    def _record(self, item: dict, msg, res) -> None:
        now, today = self.now(), self._today()
        lead, addr = item["lead"], item["email"]
        msgid = msg["Message-ID"]
        status = {"sent": "sent", "invalid": "invalid"}.get(res.status, "failed")
        if status == "failed":
            return       # nothing changed; the same e-mail is tried again in a later run
        fu = self.o["email"]["follow_up_days"]
        with self.store.tx():
            self.store.conn.execute("INSERT INTO sends(email,lead_key,step,msgid,sent_at,day,status,error) VALUES(?,?,?,?,?,?,?,?)",
                                    (addr, lead.key, item["step"], msgid, now, today, status, res.detail[:300] if status != "sent" else None))
            if status == "invalid":
                self.store.suppress(addr, "email", f"rejected by the mail server ({res.code})", "bounce")
                if item["thread"] is None:
                    self.store.conn.execute("INSERT INTO threads(email,lead_key,lead_id,business,domain,stage,step,updated_at) "
                                            "VALUES(?,?,?,?,?,?,?,?)", (addr, lead.key, lead.lead_id, lead.business,
                                                                        email_domain(addr), "bounced", 0, now))
                else:
                    self.store.conn.execute("UPDATE threads SET stage='bounced', next_due=NULL, updated_at=? WHERE email=?", (now, addr))
                self.stats["bounces"] += 1
                self._set_status(lead, STATUS_BOUNCED)
                return
            if item["thread"] is None:
                next_due = now + fu[0] * 86400 if fu else None
                self.store.conn.execute(
                    "INSERT INTO threads(email,lead_key,lead_id,business,domain,stage,step,subject,msgids,first_sent,last_sent,next_due,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (addr, lead.key, lead.lead_id, lead.business, email_domain(addr), "active" if fu else "completed", 1,
                     msg["Subject"], jdump([msgid]), now, now, next_due, now))
                self.stats["new"] += 1
                self._set_status(lead, STATUS_EMAILED)
            else:
                t = item["thread"]
                step = t["step"] + 1
                ids = self.store.msgids(t) + [msgid]
                more = step - 1 < len(fu)        # follow-ups sent so far = step - 1
                next_due = (t["first_sent"] + fu[step - 1] * 86400) if more else None
                self.store.conn.execute("UPDATE threads SET step=?, msgids=?, last_sent=?, next_due=?, stage=?, updated_at=? WHERE email=?",
                                        (step, jdump(ids), now, next_due, "active" if more else "completed", now, addr))
                self.stats["followups"] += 1
                self._set_status(lead, STATUS_FOLLOWUP.format(n=step - 1))

    def _pause(self, days: int, reason: str) -> None:
        local = self._local()
        until = (local + timedelta(days=days)).replace(hour=0, minute=5, second=0, microsecond=0)
        self.store.set("paused_until", until.timestamp())
        self.store.set("pause_reason", reason)
        self.notes.append(f"SENDING PAUSED until {until.strftime('%Y-%m-%d %H:%M')}: {reason}")
        self.code = max(self.code, 2)

    def _check_bounce_rate(self) -> None:
        e = self.o["email"]
        since = self.now() - 7 * 86400
        sent = self.store.scalar("SELECT COUNT(*) FROM sends WHERE sent_at>=? AND status IN ('sent','invalid')", (since,), 0)
        bad = self.store.scalar("SELECT COUNT(*) FROM sends WHERE sent_at>=? AND status IN ('invalid','bounced')", (since,), 0)
        if sent >= e["pause_min_sends"] and bad / max(sent, 1) >= e["pause_bounce_rate"]:
            if float(self.store.get("paused_until", 0) or 0) <= self.now():
                self._pause(2, f"bounce rate {bad}/{sent} in 7 days is too high - check the e-mail addresses")

    def bounce_rate(self) -> str:
        since = self.now() - 7 * 86400
        sent = self.store.scalar("SELECT COUNT(*) FROM sends WHERE sent_at>=? AND status IN ('sent','invalid')", (since,), 0)
        bad = self.store.scalar("SELECT COUNT(*) FROM sends WHERE sent_at>=? AND status IN ('invalid','bounced')", (since,), 0)
        return f"{100 * bad / sent:.1f}% ({bad}/{sent})" if sent else ""

    # ------------------------------------------------------------------ WhatsApp
    def _wa_row(self, lead: Lead, phone: str, reason: str) -> list:
        w = self.o["whatsapp"]
        ctx = T.context(lead, self.o)
        tpl = (w.get("opted_in_message") or T.WHATSAPP_OPTED_IN) if reason == "opted_in" else (w.get("message") or T.WHATSAPP_COLD)
        text = T.render(tpl, ctx)
        return [self._today(), lead.lead_id, lead.business, lead.category_label, lead.area, display_phone(phone),
                REASONS[reason], text, wa_link(phone, text), "", lead.key]

    def _wa_cap(self, today: str) -> int:
        """Warm-up for a new WhatsApp number: start small and grow with the days the owner actually sent messages."""
        w = self.o["whatsapp"]
        days = self.store.scalar("SELECT COUNT(DISTINCT queued_day) FROM wa WHERE queued_day!='' AND queued_day<? "
                                 "AND result IN ('sent','replied','interested','not_interested')", (today,), 0)
        return min(w["max_per_day"], w["start_per_day"] + w["step"] * (days // w["step_every_days"]))

    def _whatsapp_queue(self) -> None:
        """Add new numbers up to today's limit: at most that many a day, and never more than that waiting."""
        w = self.o["whatsapp"]
        today = self._today()
        cap = self._wa_cap(today)
        if not w["enabled"] or cap <= 0 or not self.leads:
            return
        pending = self.store.scalar("SELECT COUNT(*) FROM wa WHERE result='' AND reason!='opted_in'", (), 0)
        added_today = self.store.scalar("SELECT COUNT(*) FROM wa WHERE queued_day=? AND reason!='opted_in'", (today,), 0)
        room = max(0, min(cap - pending, cap - added_today))
        if room == 0:
            return
        sup = {r["value"] for r in self.store.q("SELECT value FROM suppression")}
        queued_phones = {r["phone"] for r in self.store.q("SELECT phone FROM wa")}
        # A lead is tried once; only when its number turned out not to be on WhatsApp is its next number tried.
        queued_leads = {r["lead_key"] for r in self.store.q("SELECT DISTINCT lead_key FROM wa WHERE result!='not_on_whatsapp'")}
        # E-mail covers leads with an address; WhatsApp reaches the others (and those whose e-mail sequence ended).
        emailing = {r["lead_key"] for r in self.store.q("SELECT DISTINCT lead_key FROM threads WHERE stage IN ('active','replied','opted_out')")}
        ended = {r["lead_key"] for r in self.store.q(
            "SELECT DISTINCT lead_key FROM threads WHERE stage IN ('completed','no_reply','bounced')")}
        added = 0
        with self.store.tx():
            for lead in sorted(self.leads, key=self._score):
                if added >= room:
                    break
                if not lead.system_managed or lead.key in queued_leads or lead.key in emailing:
                    continue
                if any(x.lower() in sup for x in lead.emails):
                    continue
                if lead.emails and lead.key not in ended:
                    continue          # e-mail will reach this lead first
                choices = [(p, "listed") for p in lead.whatsapp]
                if w["include_mobiles"]:
                    choices += [(p, "mobile") for p in lead.mobiles() if p not in lead.whatsapp]
                for phone, reason in choices:
                    if phone in queued_phones or phone.lower() in sup:
                        continue
                    self.store.conn.execute("INSERT INTO wa(phone,lead_key,lead_id,business,reason,queued_day,result,updated_at) "
                                            "VALUES(?,?,?,?,?,?,?,?)", (phone, lead.key, lead.lead_id, lead.business, reason, today, "", self.now()))
                    self.wa_rows.append(self._wa_row(lead, phone, reason))
                    queued_phones.add(phone)
                    queued_leads.add(lead.key)
                    added += 1
                    break
        self.stats["wa_queued"] += added

    # ------------------------------------------------------------------ sheet + owner
    def _outreach_rows(self) -> list[list]:
        order = {"replied": 0, "opted_out": 1, "active": 2, "completed": 3, "no_reply": 4, "bounced": 5, "stopped": 6}
        rows = []
        tz = self.cfg.tz
        for t in sorted(self.store.q("SELECT * FROM threads"), key=lambda r: (order.get(r["stage"], 9), -(r["last_sent"] or 0))):
            lead = self.by_key.get(t["lead_key"])
            stage = {"active": "Waiting for reply", "replied": f"Replied ({REPLY_LABEL.get(t['reply_kind'] or '', 'reply')})",
                     "opted_out": "Opted out", "completed": "All e-mails sent", "no_reply": "No reply after follow-ups",
                     "bounced": "Bounced (bad address)", "stopped": "Stopped (owner took over or removed)"}.get(t["stage"], t["stage"])
            phone = ""
            if lead:
                nums = lead.whatsapp + lead.mobiles() + [p for p, _ in lead.phones]
                phone = display_phone(nums[0]) if nums else ""
            rows.append([t["lead_id"] or "", t["business"] or "", lead.category_label if lead else "", lead.area if lead else "",
                         t["email"], stage, t["step"], fmt_local(tz, t["last_sent"]), fmt_local(tz, t["next_due"]),
                         t["reply_snippet"] or "", phone, t["lead_key"]])
        return rows

    def _write_sheet(self, sheet: OutreachSheet) -> None:
        tz = self.cfg.tz
        sheet.rewrite("outreach", self._outreach_rows())
        sheet.rewrite("preview", self.preview)
        if self.new_replies:
            rows = []
            for r in self.new_replies:
                lead = r["lead"]
                phone_e164 = (lead.whatsapp + lead.mobiles() + [p for p, _ in lead.phones])[0] if lead and (lead.whatsapp or lead.phones) else ""
                chat = ""
                if r["kind"] == "positive" and phone_e164 and lead:
                    chat = wa_link(phone_e164, T.render(self.o["whatsapp"].get("opted_in_message") or T.WHATSAPP_OPTED_IN,
                                                        T.context(lead, self.o)))
                rows.append([fmt_local(tz, self.now()), r["lead_id"] or "", r["business"] or "", r["email"],
                             display_phone(phone_e164) if phone_e164 else "", REPLY_LABEL.get(r["kind"], r["kind"]),
                             snippet(r["text"], 1000), NEXT_STEP.get(r["kind"], ""), chat,
                             ads_library_link(r["business"] or "", self.cfg["campaign"].get("country", "IN")), r["lead_key"]])
            sheet.append("replies", rows)
        if self.wa_rows:
            sheet.append("whatsapp", self.wa_rows)
        new_dnc = self.store.q("SELECT * FROM suppression WHERE in_sheet=0 ORDER BY added_at")
        if new_dnc:
            sheet.append("dnc", [[r["value"], r["reason"] or "", fmt_local(tz, r["added_at"]), r["source"] or ""] for r in new_dnc])
            self.store.conn.execute("UPDATE suppression SET in_sheet=1 WHERE in_sheet=0")
        sheet.set_lead_statuses(self.status_updates)

    def _notify_owner(self) -> None:
        hot = [r for r in self.new_replies if r["kind"] in ("positive", "other")]
        if not (hot and self.live and self.sender):
            return
        # No alert address set: the alert goes to the outreach Gmail itself (its phone app shows it).
        to = str(self.o["sender"].get("notify_email") or "").strip() or self.sender.address
        lines = []
        for r in hot:
            lead = r["lead"]
            nums = (lead.whatsapp + lead.mobiles() + [p for p, _ in lead.phones]) if lead else []
            lines.append(f"- {r['business']} ({r['email']}) - {REPLY_LABEL.get(r['kind'])}\n  Phone: "
                         f"{display_phone(nums[0]) if nums else 'not listed'}\n  They wrote: {snippet(r['text'], 400)}")
        body = ("New replies from your leads. Best next step: call them today.\n\n" + "\n\n".join(lines)
                + "\n\nAll replies are in the Replies tab of the Google Sheet.")
        subject = f"{len(hot)} new {'reply' if len(hot) == 1 else 'replies'} from leads - call them"
        res = self.sender.send(build_message(sender_name="Lead outreach", sender_addr=self.sender.address, to=to,
                                             subject=subject, body=body))
        self.sender.close()
        if not res.ok:
            self.notes.append(f"could not e-mail the reply alert to {to}: {res.status}")

    # ------------------------------------------------------------------ report
    def _report_row(self) -> list:
        local = self._local()
        s = self.stats
        used = self.store.scalar("SELECT COUNT(*) FROM sends WHERE day=? AND status IN ('sent','invalid')", (local.date().isoformat(),), 0)
        done = self.store.scalar("SELECT COUNT(*) FROM wa WHERE result!=''", (), 0)
        return [local.date().isoformat(), local.strftime("%H:%M"), "live" if self.live else "dry-run",
                getattr(self, "cap", ""), used, s["new"], s["followups"], s["positive"], s["negative"], s["other"],
                s["bounces"], self.bounce_rate(), s["wa_queued"], done, "; ".join(self.notes)[:900]]

    def _summary(self, status: str) -> dict:
        return {"status": status, "mode": "live" if self.live else "dry-run", **self.stats,
                "daily_limit": getattr(self, "cap", None), "notes": self.notes}
