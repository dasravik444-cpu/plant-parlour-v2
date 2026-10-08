"""Read replies and bounces from the Gmail inbox over IMAP (read-only).

Privacy: only message headers are read to find what concerns outreach; a message body is opened only
when it comes from a lead we e-mailed, refers to one of our messages, or is a bounce about one of them.
Nothing is marked as read, moved or deleted."""
from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from email import policy
from email.utils import parseaddr

from ..util import get_logger
from .classify import bounced_addresses, classify_reply, is_bounce, new_text

log = get_logger("outreach.inbox")

IMAP_HOST = "imap.gmail.com"
HEADER_FIELDS = "FROM SUBJECT IN-REPLY-TO REFERENCES X-FAILED-RECIPIENTS AUTO-SUBMITTED X-AUTOREPLY PRECEDENCE"
MAX_BODY_BYTES = 3_000_000


@dataclass
class InboxEvent:
    kind: str            # reply | bounce
    email: str           # our recipient this is about (lower-case)
    reply_kind: str = ""  # positive | negative | other | auto (for replies)
    text: str = ""
    uid: int = 0


def _headers(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw or b"", policy=policy.default)
    return {k.lower(): str(v) for k, v in msg.items()}


def _body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        # bounces are multipart/report: use every text part
        texts = []
        for p in msg.walk():
            if p.get_content_maintype() == "text" or p.get_content_type() == "message/delivery-status":
                try:
                    texts.append(p.get_content() if p.get_content_type() != "message/delivery-status" else str(p))
                except Exception:
                    texts.append(str(p.get_payload()))
        return "\n".join(t if isinstance(t, str) else str(t) for t in texts)
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
    return text


class GmailInbox:
    def __init__(self, address: str, app_password: str, *, imap_factory=None):
        self.address = address.lower()
        self.password = app_password.replace(" ", "")
        self.factory = imap_factory or (lambda: imaplib.IMAP4_SSL(IMAP_HOST, 993))

    def scan(self, *, known_emails: set[str], known_msgids: dict[str, str], last_uid: int | None, uidvalidity: str | None,
             max_messages: int = 400) -> tuple[list[InboxEvent], int, str]:
        """New events since last_uid. known_msgids: our Message-ID -> the recipient it was sent to.
        Returns (events, new last_uid, uidvalidity)."""
        m = self.factory()
        try:
            m.login(self.address, self.password)
            typ, _ = m.select("INBOX", readonly=True)
            if typ != "OK":
                raise imaplib.IMAP4.error("cannot open INBOX")
            typ, st = m.status("INBOX", "(UIDNEXT UIDVALIDITY)")
            info = st[0].decode() if st and isinstance(st[0], bytes) else str(st[0] if st else "")
            uidnext = int(re.search(r"UIDNEXT (\d+)", info).group(1))
            validity = re.search(r"UIDVALIDITY (\d+)", info).group(1)
            if last_uid is None or uidvalidity != validity:
                # First look (or the mailbox was rebuilt): start from now, never read old mail.
                return [], uidnext - 1, validity
            if uidnext - 1 <= last_uid:
                return [], last_uid, validity
            typ, data = m.uid("SEARCH", None, f"UID {last_uid + 1}:*")
            uids = sorted(int(x) for x in (data[0] or b"").split() if int(x) > last_uid)[:max_messages]
            events: list[InboxEvent] = []
            newest = last_uid
            for i in range(0, len(uids), 50):
                batch = uids[i:i + 50]
                typ, data = m.uid("FETCH", ",".join(map(str, batch)), f"(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
                for item in data or []:
                    if not isinstance(item, tuple):
                        continue
                    meta = item[0].decode("latin-1")
                    uid = int(re.search(r"UID (\d+)", meta).group(1))
                    ms = re.search(r"RFC822\.SIZE (\d+)", meta)
                    size = int(ms.group(1)) if ms else 0
                    newest = max(newest, uid)
                    ev = self._consider(m, uid, size, _headers(item[1]), known_emails, known_msgids)
                    if ev:
                        events.extend(ev)
            return events, max(newest, uids[-1] if uids else last_uid), validity
        finally:
            try:
                m.logout()
            except Exception:
                pass

    def _consider(self, m, uid: int, size: int, h: dict, known_emails: set[str], known_msgids: dict[str, str]) -> list[InboxEvent]:
        sender = parseaddr(h.get("from", ""))[1].lower()
        subject = h.get("subject", "")
        refs = re.findall(r"<[^>]+>", (h.get("in-reply-to", "") + " " + h.get("references", "")))
        bounce = is_bounce(h.get("from", ""), subject)
        # The conversation this message belongs to: by sender, or by our Message-ID in its reply chain
        # (a reply can come from another address of the same business).
        thread_email = sender if sender in known_emails else next((known_msgids[r] for r in refs if r in known_msgids), "")
        if sender == self.address or not (bounce or thread_email) or size > MAX_BODY_BYTES:
            return []
        typ, data = m.uid("FETCH", str(uid), "(BODY.PEEK[])")
        raw = next((x[1] for x in data or [] if isinstance(x, tuple)), b"")
        msg = email.message_from_bytes(raw, policy=policy.default)
        body = _body_text(msg)
        if bounce:
            hits = bounced_addresses(h, body, known_emails)
            if not hits and thread_email:
                hits = {thread_email}
            return [InboxEvent("bounce", e, uid=uid) for e in sorted(hits)]
        kind = classify_reply(subject, body, h)
        return [InboxEvent("reply", thread_email, kind, new_text(body), uid)]
