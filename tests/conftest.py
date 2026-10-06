import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

from leadgen.config import Config, DEFAULTS, _merge, validate  # noqa: E402

TEST_CONFIG = {
    "campaign": {"id": "test-campaign", "client": "Test", "timezone": "Asia/Kolkata", "country": "IN", "start_date": ""},
    "area": {"name": "Kolkata", "center": [22.58, 88.42], "radius_km": 3},
    "plan": {"days": 3, "daily_target": 5, "min_cell_km": 1.0, "max_cell_km": 2.0, "split_threshold": 60},
    "discovery": {"providers": ["gmaps", "osm"], "max_pages": 2, "gmaps_interval_s": 0.0, "gmaps_jitter_s": 0.0},
    "filters": {"exclude_chains": ["starbucks"], "require_contact": True},
    "enrich": {"workers": 2, "check_email_mx": False, "social_kinds": ["instagram", "facebook", "linkedin"]},
    "sheets": {"enabled": True, "spreadsheet_id": "", "leads_tab": "Leads", "plan_tab": "Plan", "report_tab": "Daily Report"},
    "runtime": {"time_budget_minutes": 5, "safety_margin_minutes": 0.5, "use_curl_cffi": False},
    "categories": [
        {"key": "cafe", "label": "Cafe", "queries": ["cafe", "coffee shop"], "match": ["cafe", "coffee", "bakery"],
         "osm": ['["amenity"="cafe"]']},
        {"key": "banquet_venue", "label": "Banquet", "queries": ["banquet hall"], "match": ["banquet", "event venue", "wedding"],
         "osm": ['["amenity"="events_venue"]']},
    ],
}


def make_config(**overrides) -> Config:
    raw = _merge(TEST_CONFIG, overrides)
    cfg = Config(_merge(DEFAULTS, raw))
    validate(cfg)
    return cfg


@pytest.fixture
def cfg():
    return make_config()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("PP_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
              "GOOGLE_PLACES_API_KEY", "BRAVE_API_KEY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
