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
    "enrich": {"website": True, "max_pages_per_site": 4, "social_search": True,
               "social_kinds": ["instagram", "facebook", "linkedin"], "instagram_profile": True,
               "check_email_mx": True, "workers": 6, "search_interval_s": 4.5, "site_interval_s": 2.0,
               "api_enrich": True},
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
    if not (isinstance(p["daily_target"], int) and 1 <= p["daily_target"] <= 2000):
        errors.append("plan.daily_target must be an integer 1..2000")
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
    if errors:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))
