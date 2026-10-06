"""Phone number parsing/classification with Google's libphonenumber (python `phonenumbers`)."""
from __future__ import annotations

import re

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

_TYPE_LABEL = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "landline",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "mobile/landline",
    PhoneNumberType.TOLL_FREE: "toll-free",
    PhoneNumberType.VOIP: "voip",
    PhoneNumberType.SHARED_COST: "shared-cost",
    PhoneNumberType.UAN: "uan",
}

# Context words that indicate a digit run is NOT a phone number (licence/registration ids etc.)
_NOT_PHONE_CONTEXT = re.compile(r"(fssai|gstin?|cin\b|lic(?:ence|ense)?\s*no|reg(?:istration)?\s*no|pin\s*code|pincode|zip|"
                                r"order\s*id|invoice|account\s*no|a/c|ifsc|isbn|batch)", re.I)


def parse_phone(raw: str, region: str = "IN") -> tuple[str, str] | None:
    """Return (E.164, label) for a valid number, else None."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.lower().startswith("tel:"):
        raw = raw[4:]
    raw = raw.split("?")[0].split(";")[0]
    raw = re.sub(r"%20|%2B", lambda m: " " if m.group(0) == "%20" else "+", raw, flags=re.I)
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8 or len(digits) > 15:
        return None
    try:
        num = phonenumbers.parse(raw, region)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    e164 = phonenumbers.format_number(num, PhoneNumberFormat.E164)
    label = _TYPE_LABEL.get(phonenumbers.number_type(num), "phone")
    return e164, label


def refine_phone_label(e164: str, label: str, local_landline_prefixes) -> str:
    """Display label using the campaign's local landline prefixes (e.g. ["+913"] for West Bengal).

    In India libphonenumber calls some mobile ranges "mobile/landline" because a few far-away STD
    codes overlap them; a 6-9 number that is not a local landline is a mobile here. A landline from
    another region (+91 11 Delhi, +91 22 Mumbai...) on a local business is usually a booking
    platform's call-centre line."""
    if not local_landline_prefixes or not e164.startswith("+91"):
        return label
    parts = [x.strip() for x in (label or "").split(",") if x.strip()]
    if not parts:
        return label
    local = any(e164.startswith(p) for p in local_landline_prefixes)
    if parts[0] == "mobile/landline" and not local and e164[3:4] in "6789":
        parts[0] = "mobile"
    elif parts[0] == "landline" and not local:
        parts[0] = "landline outside the area, often a booking-platform line"
    return ", ".join(parts)


def display_phone(e164: str) -> str:
    try:
        return phonenumbers.format_number(phonenumbers.parse(e164, None), PhoneNumberFormat.INTERNATIONAL)
    except NumberParseException:
        return e164


def find_phones_in_text(text: str, region: str = "IN", require_context: bool = False, max_results: int = 20) -> list[tuple[str, str, str]]:
    """Find valid phone numbers in free text. Returns [(e164, label, context_snippet)].

    With require_context=True only numbers within ~40 characters after a word such as
    "call", "phone", "mobile", "contact", "whatsapp" or "reservation" are accepted.
    """
    out, seen = [], set()
    if not text:
        return out
    ctx_re = re.compile(r"(call|phone|ph\.?|tel\.?|telephone|mobile|mob\.?|contact|whats\s*app|reservations?|bookings?|enquir(y|ies)|helpline|reach us|cell)", re.I)
    for m in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID, max_tries=200):
        start = max(0, m.start - 45)
        before = text[start:m.start]
        pos = [x.end() for x in ctx_re.finditer(before)]
        neg = [x.end() for x in _NOT_PHONE_CONTEXT.finditer(before)]
        # A licence/pincode word right before the digits (and after any phone word) disqualifies it.
        if neg and (not pos or neg[-1] > pos[-1]) and len(before) - neg[-1] <= 20:
            continue
        if require_context and not pos:
            continue
        num = m.number
        e164 = phonenumbers.format_number(num, PhoneNumberFormat.E164)
        if e164 in seen:
            continue
        seen.add(e164)
        label = _TYPE_LABEL.get(phonenumbers.number_type(num), "phone")
        snippet = re.sub(r"\s+", " ", text[start:min(len(text), m.end + 10)]).strip()
        out.append((e164, label, snippet[:120]))
        if len(out) >= max_results:
            break
    return out


def whatsapp_number_from_link(url: str) -> str | None:
    """Extract E.164 from wa.me / api.whatsapp.com links. Numbers in these links carry the country code."""
    m = re.search(r"(?:wa\.me/|whatsapp\.com/send/?\?(?:[^#]*&)?phone=|whatsapp://send\?(?:[^#]*&)?phone=)\+?([0-9][0-9\-\s%20]{7,20})", url, re.I)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1).replace("%20", ""))
    if len(digits) == 10 and digits[0] in "6789":  # Indian number written without country code
        digits = "91" + digits
    parsed = parse_phone("+" + digits)
    return parsed[0] if parsed else None
