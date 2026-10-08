"""USP line: the one thing a business says makes it different, in its own words.

Taken only from the business's own website (title, description, headings, structured data, about page) and
only when a phrase makes a concrete claim - "Kolkata's first rooftop cafe", "serving since 1962",
"500+ weddings", "award-winning", "specialists in ...". Generic phrases ("best restaurant in Kolkata",
"welcome to ...") are not USPs; then the column stays empty. Nothing is written by an AI model.
"""
from __future__ import annotations

import re

CLAIM = re.compile(
    r"\b(first|only|oldest|largest|biggest|award[- ]?winning|awards?|awarded|winners?|renowned|famous|legendary|iconic|"
    r"pioneers?|pioneering|since\s+(?:18|19|20)\d\d|(?:established|founded|started)\s+(?:in\s+)?(?:18|19|20)\d\d|"
    r"est\.?\s*(?:18|19|20)\d\d|heritage|signature|authentic|speciali[sz](?:e|es|ed|ing|ts?)|specialt(?:y|ies)|"
    r"speciality|specialities|exclusive|rooftop|terrace|lake[- ]?(?:view|side)|river[- ]?(?:view|side)|"
    r"24\s?[x/]\s?7|24 hours|round the clock|pure[- ]veg(?:etarian)?|vegan|organic|hand[- ]?made|home[- ]?made|"
    r"family[- ]?(?:run|owned)|generations?|known for|loved for|celebrated for|certified|accredited|nabh|nabl|"
    r"iso[- ]?\d{4,5}|years? of (?:experience|service|excellence|trust)|"
    r"\d[\d,]*\+?\s*(?:[a-z]+\s+)?(?:years|yrs|clients|customers|projects|events|weddings|outlets|branches|rooms|"
    r"seats|covers|members|trainers|doctors|beds|guests|varieties|dishes|cities))\b", re.I)
JUNK = re.compile(r"cookie|javascript|copyright|all rights reserved|click here|log ?in|sign ?in|sign ?up|add to cart|"
                  r"subscribe|newsletter|page not found|\b404\b|coming soon|under construction|lorem ipsum|book now|"
                  r"call now|call us|whatsapp|privacy|terms|gst|pan no|cin\b|@|https?://|www\.", re.I)
SPLIT = re.compile(r"(?<=[.!?])\s+|\s[|•·–—]\s|\s-\s|\n+|;\s")
MIN_LEN, MAX_LEN = 18, 140


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text.strip(" -|•·–—:;,.").strip()


def _drop_name_prefix(sentence: str, business: str) -> str:
    """'Leaf Cafe - Kolkata's first ...' / 'Welcome to Leaf Cafe, ...' -> 'Kolkata's first ...'."""
    s = re.sub(r"^(?:welcome to|about)\s+", "", sentence, flags=re.I)
    name = re.escape((business or "").strip())
    if name:
        s = re.sub(rf"^(?:the\s+)?{name}\s*(?:[,:\-|–—]\s*|is\s+|are\s+)?", "", s, flags=re.I)
    return s[:1].upper() + s[1:] if s else s


def candidates(texts: list[str], business: str = "") -> list[str]:
    out: list[str] = []
    for text in texts:
        for part in SPLIT.split(text or ""):
            s = _drop_name_prefix(_clean(part), business)
            s = _clean(s)
            if not (MIN_LEN <= len(s) <= MAX_LEN) or len(s.split()) < 3 or JUNK.search(s):
                continue
            if re.search(r"\d{7,}", re.sub(r"[\s\-]", "", s)):        # a phone number, not a claim
                continue
            if s.lower() not in (x.lower() for x in out):
                out.append(s)
    return out


def score(sentence: str) -> int:
    hits = {m.group(0).lower() for m in CLAIM.finditer(sentence)}
    sc = 3 * min(len(hits), 2)
    if re.search(r"\bbest\b.*\bin\s+(?:kolkata|calcutta|town|the city)\b", sentence, re.I) and len(hits) <= 1:
        sc -= 2            # "best ... in Kolkata" is an SEO tagline, not a fact
    if sentence.isupper():
        sc -= 1
    return sc


def pick_usp(texts: list[str], business: str = "") -> str:
    """The most concrete claim in the business's own texts, or '' when there is none."""
    best, best_score = "", 0
    for s in candidates(texts, business):
        sc = score(s)
        if sc > best_score or (sc == best_score and sc and len(s) < len(best)):
            best, best_score = s, sc
    return best if best_score >= 3 else ""
