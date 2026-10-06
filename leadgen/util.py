"""Small shared helpers: time zones, hashing, text normalisation, logging."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone, tzinfo

_LOGGER_CONFIGURED = False


def get_logger(name: str = "leadgen") -> logging.Logger:
    global _LOGGER_CONFIGURED
    if not _LOGGER_CONFIGURED:
        level = os.environ.get("PP_LOG_LEVEL", "INFO").upper()
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        root = logging.getLogger("leadgen")
        root.handlers[:] = [handler]
        root.setLevel(getattr(logging, level, logging.INFO))
        root.propagate = False
        _LOGGER_CONFIGURED = True
    return logging.getLogger(name if name.startswith("leadgen") else "leadgen." + name)


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------
_FIXED_OFFSETS = {"Asia/Kolkata": timedelta(hours=5, minutes=30), "Asia/Calcutta": timedelta(hours=5, minutes=30), "UTC": timedelta(0)}


def get_tz(name: str) -> tzinfo:
    """Return a tzinfo; falls back to a fixed offset when tzdata is missing (some Android setups)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # pragma: no cover - depends on the platform's tz database
        return timezone(_FIXED_OFFSETS.get(name, timedelta(0)), name)


def now_ts() -> float:
    return time.time()


def local_now(tz_name: str, ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(now_ts() if ts is None else ts, get_tz(tz_name))


def local_date(tz_name: str, ts: float | None = None) -> str:
    return local_now(tz_name, ts).date().isoformat()


def fmt_local(tz_name: str, ts: float | None) -> str:
    if not ts:
        return ""
    return local_now(tz_name, ts).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# Hashing / JSON
# --------------------------------------------------------------------------
def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def jload(text, default=None):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------
_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_text(s: str | None) -> str:
    """Lowercase, accent-free, punctuation-free text with single spaces."""
    if not s:
        return ""
    s = strip_accents(str(s)).lower().replace("&", " and ").replace("'", "")
    return _WS.sub(" ", _NON_ALNUM.sub(" ", s)).strip()


def clean_space(s: str | None) -> str:
    return _WS.sub(" ", s or "").strip()


def truncate(s: str | None, n: int) -> str:
    s = clean_space(s)
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


# --------------------------------------------------------------------------
# Masking for public logs (GitHub Actions logs of a public repo are public)
# --------------------------------------------------------------------------
def mask_value(kind: str, value: str) -> str:
    if not value:
        return value
    if kind in ("phone", "whatsapp") and value.startswith("+"):
        digits = re.sub(r"\D", "", value)
        return "+" + digits[:2] + "*" * max(0, len(digits) - 5) + digits[-3:]
    if kind == "email" and "@" in value:
        local, _, domain = value.partition("@")
        return local[:1] + "***@" + domain
    return value
