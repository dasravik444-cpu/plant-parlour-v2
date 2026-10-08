"""Send e-mail through Gmail's SMTP server with an app password (free Gmail account).

Every outcome is classified, so one bad address never stops a run, while a Gmail limit or a block stops
sending at once (continuing would put the account at risk)."""
from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from ..util import get_logger

log = get_logger("outreach.mail")

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587


@dataclass
class SendResult:
    status: str          # sent | invalid | limit | blocked | temp | auth | network
    code: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "sent"


def build_message(*, sender_name: str, sender_addr: str, to: str, subject: str, body: str,
                  in_reply_to: str = "", references: list[str] | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_addr)) if sender_name else sender_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_addr.rsplit("@", 1)[-1] if "@" in sender_addr else None)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    # Lets mail apps show an "unsubscribe" button; the request arrives as an e-mail and is honoured.
    msg["List-Unsubscribe"] = f"<mailto:{sender_addr}?subject=unsubscribe>"
    msg.set_content(body)          # text/plain, utf-8; no HTML, links, images or tracking pixels
    return msg


def classify_smtp_error(code: int, text: str) -> str:
    t = (text or "").lower()
    if "5.4.5" in t or "sending limit" in t or "quota" in t or "too many" in t or (code == 421 and "rate" in t):
        return "limit"
    if "5.7." in t and ("spam" in t or "unsolicited" in t or "suspend" in t or "blocked" in t or "policy" in t):
        return "blocked"
    if code in (550, 551, 553) and ("5.1.1" in t or "does not exist" in t or "no such user" in t or "invalid" in t
                                    or "user unknown" in t or "address rejected" in t or "not found" in t):
        return "invalid"
    if 500 <= code < 600:
        return "invalid" if "5.1." in t else "blocked"
    return "temp"


class GmailSender:
    def __init__(self, address: str, app_password: str, *, smtp_factory=smtplib.SMTP, timeout: float = 40):
        self.address = address
        self.password = app_password.replace(" ", "")      # Google shows the app password in groups of 4
        self.factory = smtp_factory
        self.timeout = timeout
        self.smtp = None

    def _connect(self) -> None:
        s = self.factory(SMTP_HOST, SMTP_PORT, timeout=self.timeout)
        s.ehlo()
        s.starttls(context=ssl.create_default_context())
        s.ehlo()
        try:
            s.login(self.address, self.password)
        except smtplib.SMTPServerDisconnected as exc:
            # Gmail answers a refused password (e.g. the normal password instead of an App Password) and then
            # hangs up; smtplib's next login method then sees the closed connection. It is a login refusal.
            raise smtplib.SMTPAuthenticationError(534, b"Gmail closed the connection during login (password refused; "
                                                       b"Application-specific password required)") from exc
        self.smtp = s

    def check(self) -> str:
        """Log in without sending. Returns "" if OK, else a readable reason."""
        try:
            self._connect()
            return ""
        except smtplib.SMTPAuthenticationError as exc:
            said = _txt(exc.smtp_error).replace("\n", " ")[:160]
            hint = (" - the secret must be a Gmail App Password (16 letters from myaccount.google.com/apppasswords, "
                    "2-Step Verification on), not the account's normal password") if "application-specific" in said.lower() \
                else " - check the address and the app password"
            return f"Gmail refused the login ({exc.smtp_code}: {said}){hint}"
        except (smtplib.SMTPException, OSError, socket.timeout) as exc:
            return f"could not reach Gmail's sending server (smtp.gmail.com:{SMTP_PORT}): {type(exc).__name__}: {exc}"
        finally:
            self.close()

    def send(self, msg: EmailMessage) -> SendResult:
        for attempt in (1, 2):
            try:
                if self.smtp is None:
                    self._connect()
                refused = self.smtp.send_message(msg)
                if refused:
                    code, text = next(iter(refused.values()))
                    return SendResult(classify_smtp_error(code, _txt(text)), code, _txt(text))
                return SendResult("sent")
            except smtplib.SMTPAuthenticationError as exc:
                self.close()
                return SendResult("auth", exc.smtp_code, _txt(exc.smtp_error))
            except smtplib.SMTPRecipientsRefused as exc:
                code, text = next(iter(exc.recipients.values()))
                return SendResult(classify_smtp_error(code, _txt(text)), code, _txt(text))
            except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
                return SendResult(classify_smtp_error(exc.smtp_code, _txt(exc.smtp_error)), exc.smtp_code, _txt(exc.smtp_error))
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError, socket.timeout) as exc:
                self.close()
                if attempt == 2:
                    return SendResult("network", 0, f"{type(exc).__name__}: {exc}")
            except smtplib.SMTPResponseException as exc:
                return SendResult(classify_smtp_error(exc.smtp_code, _txt(exc.smtp_error)), exc.smtp_code, _txt(exc.smtp_error))
            except smtplib.SMTPException as exc:
                return SendResult("temp", 0, f"{type(exc).__name__}: {exc}")
        return SendResult("network")

    def close(self) -> None:
        if self.smtp is not None:
            try:
                self.smtp.quit()
            except Exception:
                pass
            self.smtp = None


def _txt(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v or "")
