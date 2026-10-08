"""Read the Leads tab of the Google Sheet into Lead records (the sheet is the outreach system's input)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..enrich.phones import parse_phone
from ..sheets import _tab_ref, col_letter

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}$")
NO_REPLY_RE = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|abuse|privacy|bounce)", re.I)
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "hotmail.com", "outlook.com", "live.com",
             "rediffmail.com", "icloud.com", "aol.com", "protonmail.com", "proton.me", "ymail.com", "zoho.com", "msn.com"}

# Columns outreach reads (by header name, so column order does not matter).
COLUMNS = ["Lead ID", "Business Name", "Category", "Area", "Phones", "WhatsApp", "Emails", "Contact Person", "Website",
           "Priority", "Key", "Status", "Other Contacts (unverified)", "Signals"]

# Statuses written by the system. A lead whose Status is blank, "New" or one of these is handled by the
# automation; anything else typed by the owner ("Customer", "Called", "Do not contact"...) means hands off.
STATUS_EMAILED = "Emailed"
STATUS_FOLLOWUP = "Emailed (follow-up {n})"
STATUS_REPLIED = "Replied - read it"
STATUS_INTERESTED = "Interested - call now"
STATUS_OPTED_OUT = "Not interested (opted out)"
STATUS_BOUNCED = "Email bounced"
STATUS_DONE = "Emailed - no reply"
SYSTEM_STATUSES = {"", "new", STATUS_EMAILED.lower(), STATUS_REPLIED.lower(), STATUS_INTERESTED.lower(),
                   STATUS_OPTED_OUT.lower(), STATUS_BOUNCED.lower(), STATUS_DONE.lower()} | \
                  {STATUS_FOLLOWUP.format(n=n).lower() for n in range(1, 6)}


@dataclass
class Lead:
    key: str
    lead_id: str = ""
    row: int = 0
    business: str = ""
    category_label: str = ""
    category_key: str = ""
    area: str = ""
    emails: list[str] = field(default_factory=list)
    unverified_emails: list[str] = field(default_factory=list)
    phones: list[tuple[str, str]] = field(default_factory=list)     # (E.164, type label)
    whatsapp: list[str] = field(default_factory=list)                # E.164 numbers the business publishes as WhatsApp
    person: str = ""
    website: str = ""
    priority: str = ""
    status: str = ""
    signals: str = ""

    @property
    def system_managed(self) -> bool:
        return self.status.strip().lower() in SYSTEM_STATUSES

    @property
    def first_name(self) -> str:
        name = re.sub(r"\(.*?\)", "", self.person.split("\n")[0]).strip()
        first = name.split(" ")[0].strip(" ,.") if name else ""
        return first if first.isalpha() and len(first) > 1 else ""

    def mobiles(self) -> list[str]:
        """Phone numbers that are mobiles (WhatsApp is only likely on these)."""
        out = []
        for e164, label in self.phones:
            lab = label.lower()
            if lab.startswith("mobile") and not lab.startswith("mobile/landline"):
                out.append(e164)
            elif lab.startswith("mobile/landline") and e164.startswith("+91") and e164[3:4] in "6789":
                out.append(e164)
        return out


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def usable_email(email: str) -> bool:
    email = (email or "").strip()
    return bool(EMAIL_RE.match(email)) and not NO_REPLY_RE.match(email.split("@")[0])


def _lines(cell) -> list[str]:
    return [x.strip() for x in str(cell or "").split("\n") if x.strip()]


def _parse_numbers(cell, region: str) -> list[tuple[str, str]]:
    out = []
    for line in _lines(cell):
        number, _, rest = line.partition("(")
        parsed = parse_phone(number.strip(), region)
        if not parsed:
            continue
        label = rest.rstrip(")").strip() or parsed[1]
        if parsed[0] not in [e for e, _ in out]:
            out.append((parsed[0], label))
    return out


def parse_lead(values: dict, row: int, cfg) -> Lead | None:
    key = str(values.get("Key") or "").strip()
    if not key:
        return None
    region = cfg["campaign"].get("country", "IN")
    label = str(values.get("Category") or "").strip()
    cat_key = ""
    for c in cfg["categories"]:
        if c.get("label", "").lower() == label.lower() or c["key"] == label:
            cat_key = c["key"]
            break
    emails = []
    for e in _lines(values.get("Emails")):
        e = e.split(" ")[0].strip().lower()
        if usable_email(e) and e not in emails:
            emails.append(e)
    unverified = []
    for line in _lines(values.get("Other Contacts (unverified)")):
        if line.lower().startswith("email:"):
            e = line.split(":", 1)[1].strip().split(" ")[0].strip().lower()
            if usable_email(e) and e not in emails and e not in unverified:
                unverified.append(e)
    return Lead(key=key, lead_id=str(values.get("Lead ID") or "").strip(), row=row,
                business=str(values.get("Business Name") or "").strip(), category_label=label, category_key=cat_key,
                area=str(values.get("Area") or "").strip(), emails=emails, unverified_emails=unverified,
                phones=_parse_numbers(values.get("Phones"), region),
                whatsapp=[e for e, _ in _parse_numbers(values.get("WhatsApp"), region)],
                person=str(values.get("Contact Person") or "").strip(), website=str(values.get("Website") or "").strip(),
                priority=str(values.get("Priority") or "").strip(), status=str(values.get("Status") or "").strip(),
                signals=str(values.get("Signals") or "").strip())


def read_leads(client, tab: str, cfg) -> list[Lead]:
    """Read only the columns outreach needs (one batched request)."""
    ref = _tab_ref(tab)
    head = client.get_values(f"{ref}!A1:AZ1")
    header = [h.strip() for h in (head[0] if head else [])]
    present = [c for c in COLUMNS if c in header]
    if "Key" not in present:
        return []
    ranges = []
    for c in present:
        col = col_letter(header.index(c))
        ranges.append(f"{ref}!{col}2:{col}")
    cols = client.batch_get_columns(ranges)
    n = max((len(c) for c in cols), default=0)
    leads = []
    for i in range(n):
        values = {name: (cols[j][i] if i < len(cols[j]) else "") for j, name in enumerate(present)}
        lead = parse_lead(values, i + 2, cfg)
        if lead:
            leads.append(lead)
    return leads
