"""Campaign configuration (TOML) with defaults and validation.

Secrets never live in the config file. They come from environment variables:
  PP_SHEET_ID                    Google spreadsheet id (or [sheets].spreadsheet_id)
  GOOGLE_SERVICE_ACCOUNT_JSON    service-account key JSON *content*
  GOOGLE_SERVICE_ACCOUNT_FILE    ...or a path to the key file
  GOOGLE_PLACES_API_KEY          optional official Places API key
"""
from __future__ import annotations

import copy
import os
from datetime import date

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


class ConfigError(ValueError):
    pass


DEFAULTS: dict = {
    "campaign": {"id": "campaign", "client": "", "timezone": "Asia/Kolkata", "country": "IN",
                 "start_date": "", "language": "en", "region": "in"},
    "area": {"name": "", "aliases": [], "center": None, "radius_km": 25.0, "local_landline_prefixes": []},
    "plan": {"days": 30, "daily_target": 150, "order": "center_out", "min_cell_km": 1.0, "max_cell_km": 10.0,
             "split_threshold": 60, "finish_scheduled_part": True},
    "discovery": {"providers": ["gmaps", "places_api", "osm"], "max_pages": 3, "gmaps_interval_s": 4.0,
                  "gmaps_jitter_s": 3.0, "gmaps_variant": "gosom", "places_api_daily_cap": 30,
                  "places_api_monthly_cap": 900, "keep_outside_cell": True},
    "categories": [],
    "filters": {"exclude_chains": [], "exclude_name_words": [], "require_contact": True,
                "exclude_closed": True, "allow_unmatched_categories": False},
    "enrich": {"website": True, "max_pages_per_site": 6, "social_search": True,
               "social_kinds": ["instagram", "facebook", "linkedin"], "instagram_profile": True,
               "check_email_mx": True, "workers": 6, "search_interval_s": 4.5, "site_interval_s": 2.0,
               "api_enrich": True, "role_email_candidates": True},
    "sheets": {"enabled": True, "spreadsheet_id": "", "leads_tab": "Leads", "plan_tab": "Plan",
               "report_tab": "Daily Report", "checkpoint_minutes": 20},
    "runtime": {"time_budget_minutes": 80, "use_curl_cffi": True, "safety_margin_minutes": 6},
    # open-data: only openly licensed data (Overture Maps, OpenStreetMap) + the businesses' own websites,
    #            crawled openly as a named bot. No scraping of Google Maps, search engines or Instagram.
    # standard:  also Google Maps and web search engines (more complete, but against Google's terms).
    "compliance": {"mode": "open-data"},
    "open_data": {"min_confidence": 0.4, "refresh_days": 30,
                  "exclude_codes": ["internet_cafe", "event_photography_service", "photographer", "party_supply_store",
                                    "hostel"]},
    # Outreach (python -m leadgen outreach): e-mail sequences from a free Gmail account + a WhatsApp send queue.
    "outreach": {
        "enabled": True,
        "mode": "dry-run",          # dry-run: plan + preview only | live: send (or repository variable OUTREACH_LIVE=true)
        "sender": {"name": "", "business": "", "phone": "", "city": "", "notify_email": ""},
        "email": {"enabled": True, "start_per_day": 15, "step": 5, "step_every_days": 3, "max_per_day": 40,
                  "max_per_run": 8, "min_gap_seconds": 75, "max_gap_seconds": 210,
                  "window_start": "10:00", "window_end": "18:30", "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
                  "skip_dates": [], "follow_up_days": [3, 7], "include_unverified": False, "one_per_domain": True,
                  "pause_bounce_rate": 0.05, "pause_min_sends": 20, "max_bounces_per_day": 3, "run_budget_minutes": 35,
                  "subjects": [], "first": "", "follow_ups": []},
        "whatsapp": {"enabled": True, "start_per_day": 20, "step": 10, "step_every_days": 3, "max_per_day": 50,
                     "include_mobiles": True, "message": "", "opted_in_message": ""},
        "boost_categories": ["event_planner", "banquet_venue", "nursery_florist", "interior_designer", "landscaper", "hotel"],
        "hooks": {}, "audience": {},
        "tabs": {"outreach": "Outreach", "preview": "Email Preview", "replies": "Replies", "whatsapp": "WhatsApp Queue",
                 "dnc": "Do Not Contact", "report": "Outreach Report"},
    },
}

OPEN_DATA_PROVIDERS = ("overture", "osm")
BOT_NAME = "PlantParlourLeadBot"
BOT_UA = f"Mozilla/5.0 (compatible; {BOT_NAME}/2.0; +https://github.com/dasravik444-cpu/plant-parlour-v2)"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config(dict):
    """Validated configuration (a dict with a few helpers)."""

    path: str = ""

    @property
    def tz(self) -> str:
        return self["campaign"]["timezone"]

    @property
    def categories(self) -> list[dict]:
        return [c for c in self["categories"] if c.get("enabled", True)]

    def category(self, key: str) -> dict | None:
        for c in self["categories"]:
            if c["key"] == key:
                return c
        return None

    def secret(self, name: str) -> str:
        return os.environ.get(name, "").strip()

    @property
    def open_data(self) -> bool:
        return self["compliance"]["mode"] == "open-data"

    @property
    def sheet_id(self) -> str:
        return self.secret("PP_SHEET_ID") or str(self["sheets"].get("spreadsheet_id") or "").strip()

    def plan_fingerprint(self) -> dict:
        """Fields that define the geographic plan. Changing them requires an explicit re-plan."""
        a, p = self["area"], self["plan"]
        return {"center": [round(float(a["center"][0]), 5), round(float(a["center"][1]), 5)],
                "radius_km": float(a["radius_km"]), "days": p["days"], "min_cell_km": float(p["min_cell_km"]),
                "max_cell_km": float(p["max_cell_km"]), "split_threshold": int(p["split_threshold"]),
                "order": p["order"], "queries": [[c["key"], list(c["queries"])] for c in self.categories]}


def load_config(path: str) -> Config:
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}") from exc
    cfg = Config(_merge(DEFAULTS, raw))
    cfg.path = path
    validate(cfg)
    apply_compliance(cfg)
    return cfg


def apply_compliance(cfg: Config) -> None:
    """Shape the pipeline to the chosen compliance mode.

    open-data (default): only openly licensed data (Overture, OpenStreetMap) + the businesses' own
        websites, crawled as a named bot. No Google, no search engines, no Instagram. Fully within terms.
    hybrid: the same open-data discovery (reliable, complete) PLUS web-search and Instagram enrichment
        for richer contacts (grey-area terms; no Google Maps scraping).
    standard: adds Google Maps and web-search scraping (uses [discovery].providers as written).
        More complete, but against Google's terms of service - the owner's explicit opt-in.
    """
    override = os.environ.get("PP_MODE", "").strip()
    if override in ("open-data", "hybrid", "standard"):
        cfg["compliance"]["mode"] = override
    mode = cfg["compliance"]["mode"]
    if mode == "open-data":
        cfg["discovery"]["providers"] = list(OPEN_DATA_PROVIDERS)
        cfg["enrich"]["social_search"] = False        # no scraping of search engines
        cfg["enrich"]["instagram_profile"] = False    # no scraping of Instagram
        cfg["runtime"]["use_curl_cffi"] = False        # no browser disguise: we crawl as a named bot
    elif mode == "hybrid":
        # Open-data discovery, but enrichment (website + web search for socials + Instagram) stays on.
        cfg["discovery"]["providers"] = list(OPEN_DATA_PROVIDERS)
    # standard: leave [discovery].providers and [enrich] exactly as written in the config.


def _outreach_errors(o: dict) -> list[str]:
    import re

    errors = []
    if o.get("mode") not in ("dry-run", "live"):
        errors.append('outreach.mode must be "dry-run" or "live"')
    e = o["email"]

    def int_in(name, lo, hi):
        v = e.get(name)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            errors.append(f"outreach.email.{name} must be a whole number {lo}..{hi}")

    int_in("start_per_day", 1, 200)
    int_in("step", 0, 50)
    int_in("step_every_days", 1, 30)
    int_in("max_per_day", 1, 400)      # a free Gmail account allows ~500/day; stay well below
    int_in("max_per_run", 1, 50)
    int_in("max_bounces_per_day", 1, 50)
    int_in("pause_min_sends", 1, 1000)
    try:
        if not 20 <= float(e["min_gap_seconds"]) <= float(e["max_gap_seconds"]) <= 3600:
            errors.append("outreach.email gaps must satisfy 20 <= min_gap_seconds <= max_gap_seconds <= 3600")
    except (TypeError, ValueError):
        errors.append("outreach.email gaps must be numbers")
    times = []
    for name in ("window_start", "window_end"):
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(e.get(name, "")))
        if not m:
            errors.append(f"outreach.email.{name} must be HH:MM")
        else:
            times.append(int(m.group(1)) * 60 + int(m.group(2)))
    if len(times) == 2 and times[0] >= times[1]:
        errors.append("outreach.email.window_start must be before window_end")
    if not set(e.get("days") or []) <= {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} or not e.get("days"):
        errors.append("outreach.email.days must list days like [\"mon\", \"tue\"]")
    f = e.get("follow_up_days")
    if not (isinstance(f, list) and len(f) <= 5 and all(isinstance(x, int) and 1 <= x <= 60 for x in f)
            and f == sorted(set(f))):
        errors.append("outreach.email.follow_up_days must be increasing whole days 1..60 (at most 5), e.g. [3, 7]")
    if not 0 < float(e.get("pause_bounce_rate", 0)) < 1:
        errors.append("outreach.email.pause_bounce_rate must be between 0 and 1")
    w = o["whatsapp"]
    if "daily_cap" in w:
        errors.append("outreach.whatsapp.daily_cap was replaced by start_per_day / step / step_every_days / max_per_day")
    for name, lo, hi in (("start_per_day", 0, 200), ("step", 0, 50), ("step_every_days", 1, 30), ("max_per_day", 0, 200)):
        v = w.get(name)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            errors.append(f"outreach.whatsapp.{name} must be a whole number {lo}..{hi}")
    from .outreach.templates import unknown_placeholders

    texts = [("email.subjects", s) for s in e.get("subjects") or []] + [("email.first", e.get("first") or "")]
    texts += [("email.follow_ups", s) for s in e.get("follow_ups") or []]
    texts += [("whatsapp.message", w.get("message") or ""), ("whatsapp.opted_in_message", w.get("opted_in_message") or "")]
    texts += [(f"hooks.{k}", v) for k, v in (o.get("hooks") or {}).items()]
    for where, text in texts:
        bad = unknown_placeholders(str(text))
        if bad:
            errors.append(f"outreach.{where} uses unknown placeholder(s) {sorted(bad)}")
    return errors


def validate(cfg: Config) -> None:
    errors = []
    c = cfg["campaign"]
    try:
        from .util import get_tz

        get_tz(c["timezone"])
    except Exception:
        errors.append(f"campaign.timezone '{c['timezone']}' is not a known time zone")
    if c.get("start_date"):
        try:
            date.fromisoformat(str(c["start_date"]))
        except ValueError:
            errors.append("campaign.start_date must be YYYY-MM-DD")
    area = cfg["area"]
    center = area.get("center")
    if not (isinstance(center, list) and len(center) == 2 and all(isinstance(x, (int, float)) for x in center)):
        errors.append("area.center must be [latitude, longitude]")
    elif not (-90 <= center[0] <= 90 and -180 <= center[1] <= 180):
        errors.append("area.center is outside valid latitude/longitude ranges")
    try:
        r = float(area["radius_km"])
        if not 0.5 <= r <= 300:
            errors.append("area.radius_km must be between 0.5 and 300")
    except (TypeError, ValueError):
        errors.append("area.radius_km must be a number")
    p = cfg["plan"]
    if not (p["days"] == "auto" or (isinstance(p["days"], int) and 1 <= p["days"] <= 365)):
        errors.append("plan.days must be an integer 1..365 or \"auto\"")
    if not (p["daily_target"] == "auto" or (isinstance(p["daily_target"], int) and 1 <= p["daily_target"] <= 5000)):
        errors.append('plan.daily_target must be an integer 1..5000 or "auto"')
    if p["order"] not in ("center_out", "dense_first", "as_planned"):
        errors.append("plan.order must be center_out, dense_first or as_planned")
    if not 0.3 <= float(p["min_cell_km"]) <= float(p["max_cell_km"]) <= 50:
        errors.append("plan cell sizes must satisfy 0.3 <= min_cell_km <= max_cell_km <= 50")
    keys = set()
    if not cfg["categories"]:
        errors.append("at least one [[categories]] entry is required")
    for i, cat in enumerate(cfg["categories"]):
        k = cat.get("key")
        if not k or not isinstance(k, str):
            errors.append(f"categories[{i}] needs a key")
            continue
        if k in keys:
            errors.append(f"duplicate category key '{k}'")
        keys.add(k)
        if not cat.get("queries") or not all(isinstance(q, str) and q.strip() for q in cat["queries"]):
            errors.append(f"category '{k}' needs a non-empty list of queries")
        cat.setdefault("label", k.replace("_", " ").title())
        cat.setdefault("match", [])
        cat.setdefault("osm", [])
        cat.setdefault("overture", [])
        cat.setdefault("enabled", True)
    for prov in cfg["discovery"]["providers"]:
        if prov not in ("gmaps", "places_api", "osm", "overture"):
            errors.append(f"unknown discovery provider '{prov}'")
    if cfg["compliance"]["mode"] not in ("open-data", "hybrid", "standard"):
        errors.append('compliance.mode must be "open-data", "hybrid" or "standard"')
    if not isinstance(cfg["enrich"]["workers"], int) or not 1 <= cfg["enrich"]["workers"] <= 16:
        errors.append("enrich.workers must be 1..16")
    if not 5 <= float(cfg["runtime"]["time_budget_minutes"]) <= 340:
        errors.append("runtime.time_budget_minutes must be 5..340")
    errors += _outreach_errors(cfg["outreach"])
    if errors:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))
