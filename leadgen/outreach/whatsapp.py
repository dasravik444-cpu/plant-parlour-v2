"""WhatsApp send queue (free, within WhatsApp's rules).

WhatsApp gives no free sending API: the only official API (the WhatsApp Business Platform) charges per
message, and automating the WhatsApp or WhatsApp Business app breaks WhatsApp's terms and gets numbers
banned. So the system prepares a short daily queue of personalised messages as wa.me links: tapping a
link opens the chat in WhatsApp Business on the owner's phone with the message already typed, and the
owner presses send. Numbers that are not on WhatsApp are simply marked and never queued again.
"""
from __future__ import annotations

from urllib.parse import quote

RESULTS = ["Sent", "Not on WhatsApp", "Replied", "Interested", "Not interested", "Skip"]
RESULT_CODES = {"sent": "sent", "not on whatsapp": "not_on_whatsapp", "replied": "replied", "interested": "interested",
                "not interested": "not_interested", "stop": "not_interested", "skip": "skipped", "skipped": "skipped"}
FINAL_RESULTS = {"sent", "not_on_whatsapp", "replied", "interested", "not_interested", "skipped"}

REASONS = {
    "opted_in": "Replied 'yes' to our e-mail - send the price list",
    "listed": "Publishes this number as WhatsApp",
    "mobile": "Mobile number (not confirmed on WhatsApp)",
}


def wa_link(e164: str, text: str) -> str:
    digits = "".join(ch for ch in e164 if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(text, safe='')}"


def result_code(cell: str) -> str:
    """Owner's entry in the Result column -> stored code ('' = still to do)."""
    return RESULT_CODES.get((cell or "").strip().lower(), "")


def ads_library_link(business: str, country: str = "IN") -> str:
    """Meta Ad Library search for a business (for a person to open - never scraped)."""
    return ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            f"&country={country}&q={quote(business or '', safe='')}&search_type=keyword_unordered")
