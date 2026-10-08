"""Understand incoming mail: is it a reply (interested / not interested / other), an auto-reply or a bounce?"""
from __future__ import annotations

import re

# Strong "stop" wording anywhere in the new text means: never contact again.
NEGATIVE = re.compile(
    r"\b(not\s+interested|no\s+thanks?|no\s+thank\s+you|unsubscribe|remove\s+(me|us|my|our)|stop\s+(e-?mailing|sending|messaging|contacting)"
    r"|do\s*n[o']?t\s+(e-?mail|contact|message|write|send)|not\s+(required|needed|looking)|no\s+requirement|no\s+need"
    r"|nahi\s+chahiye|nahin\s+chahiye|dorkar\s+nei|lagbe\s+na|spam)\b", re.I)
# A reply that is only "no" / "nope" / "nahi" is also an opt-out.
NEGATIVE_SHORT = re.compile(r"^\s*(no|nope|na|nah|nahi|nahin|stop|not now)[\s.!]*$", re.I)
POSITIVE = re.compile(
    r"\b(yes|yeah|yep|sure|ok(ay)?|interested|please\s+(send|share|call)|send\s+(it|me|us|the|your)|share\s+(it|the|your)"
    r"|price\s*list|prices?|rates?|quote|quotation|catalogu?e|brochure|call\s+(me|us)|whats\s*app|details|let'?s\s+talk"
    r"|haan|ha\s+ji|haanji|ji\s+haan|bhejo|bhejiye|pathan|pathiye)\b", re.I)
AUTO_SUBJECT = re.compile(r"(out\s+of\s+(the\s+)?office|automatic\s+reply|auto-?reply|autoreply|away\s+from|on\s+leave|vacation"
                          r"|thank\s+you\s+for\s+(your\s+)?(e-?mail|message|contacting)|we\s+have\s+received\s+your)", re.I)
BOUNCE_FROM = re.compile(r"(mailer-daemon|postmaster|mail\s+delivery\s+(subsystem|system)|delivery\s+status)", re.I)
BOUNCE_SUBJECT = re.compile(r"(delivery\s+status\s+notification|undeliver|delivery\s+(has\s+)?failed|mail\s+delivery\s+failed"
                            r"|returned\s+mail|failure\s+notice|address\s+not\s+found|message\s+not\s+delivered|could\s+not\s+be\s+delivered)", re.I)
QUOTE_START = re.compile(r"^(on\s.{3,200}\swrote:|-{2,}\s*original\s+message\s*-{2,}|from:\s.+|sent\s+from\s+my\s.+|_{5,}|>.*)$", re.I)
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")


def new_text(body: str) -> str:
    """The part of a reply the person actually wrote (drop quoted history and signatures)."""
    lines = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if QUOTE_START.match(s):
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def is_auto_reply(headers: dict, subject: str) -> bool:
    auto = (headers.get("auto-submitted") or "").lower()
    if auto and auto != "no":
        return True
    if headers.get("x-autoreply") or headers.get("x-autorespond") or (headers.get("precedence") or "").lower() in ("auto_reply", "bulk", "junk"):
        return True
    return bool(AUTO_SUBJECT.search(subject or ""))


def classify_reply(subject: str, body: str, headers: dict | None = None) -> str:
    """auto | negative | positive | other"""
    headers = headers or {}
    if is_auto_reply(headers, subject):
        return "auto"
    text = new_text(body)
    if re.search(r"\bunsubscribe\b", subject or "", re.I) or NEGATIVE.search(text) or NEGATIVE_SHORT.match(text.split("\n")[0] if text else ""):
        return "negative"
    if POSITIVE.search(text):
        return "positive"
    return "other"


def is_bounce(from_addr: str, subject: str) -> bool:
    return bool(BOUNCE_FROM.search(from_addr or "")) or bool(BOUNCE_SUBJECT.search(subject or ""))


def bounced_addresses(headers: dict, body: str, known: set[str]) -> set[str]:
    """Which of our recipients a bounce message is about."""
    found = set()
    for h in ("x-failed-recipients", "final-recipient", "original-recipient"):
        for e in EMAIL_IN_TEXT.findall(headers.get(h) or ""):
            if e.lower() in known:
                found.add(e.lower())
    if not found:
        for e in EMAIL_IN_TEXT.findall(body or ""):
            if e.lower() in known:
                found.add(e.lower())
    return found


def snippet(text: str, limit: int = 300) -> str:
    one = re.sub(r"\s+", " ", text or "").strip()
    return one if len(one) <= limit else one[:limit - 1] + "…"
