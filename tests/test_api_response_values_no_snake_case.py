"""Broader snake_case guardrail: walk every API response and fail
if any STRING VALUE looks like raw `snake_case` in a user-facing
field.

Born 2026-05-07: existing `test_no_snake_case_in_api_responses.py`
only checked for PARAM_BOUNDS keys. The slippage-model API was
returning `"source": "insufficient_history"` — a raw snake_case
string that rendered as `insufficient_history` in the dashboard.
PARAM_BOUNDS-based test missed it because the value isn't a
parameter name.

This test catches the broader class: ANY snake_case string value
in a field that the JS renders to the UI without server-side
resolution.

Allowlist conventions:
- INTERNAL_VALUE_FIELDS: fields whose values are internal codes
  consumed by the JS for switch/case logic (e.g., `prediction_type`,
  `regime`). The consumer JS translates them. Listed here.
- SNAKE_CASE_VALUE_ALLOWED_PATTERNS: specific value patterns that
  are legitimately raw (e.g., OCC option symbols are uppercase
  letters + digits; ticker symbols are uppercase).
"""

from __future__ import annotations
import json
import re
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# Match strings that look like raw snake_case identifiers: at least
# one underscore between lowercase words, no whitespace. Tolerates a
# trailing digit (e.g., "phase_1"). Excludes camelCase, ALL_CAPS,
# strings with whitespace, and generic words like "open"/"closed".
SNAKE_CASE = re.compile(r"^[a-z]+(?:_[a-z0-9]+)+$")


# Fields whose VALUES are intentionally raw internal codes — the
# JS that consumes them maps them to display strings via switch /
# Object.entries / display_name. If you add a field here, document
# WHY (e.g., the JS does `if (d.regime === "bull") {...}`).
INTERNAL_VALUE_FIELDS = {
    # JS switch values
    "regime",                  # bull / bear / sideways / volatile
    "prediction_type",         # directional_long / directional_short / exit_*
    "predicted_signal",        # STRONG_BUY / BUY / HOLD / SELL / SHORT / etc.
    "actual_outcome",          # win / loss / neutral / pending
    "status",                  # open / closed / canceled / filled
    "side",                    # buy / sell / sell_short / buy_to_cover
    "signal_type",             # BUY / SELL / SHORT / OPTIONS / MULTILEG / PAIR_TRADE
    "action",                  # BUY / SHORT / OPTIONS_OPEN / etc.
    "option_strategy",         # covered_call / protective_put / etc.
    "pair_action",             # ENTER_LONG_A_SHORT_B / EXIT
    "type",                    # market / limit
    "time_in_force",           # day / gtc / etc.
    "schedule_type",           # market_hours / extended / custom
    "ai_provider",             # anthropic / openai / google
    "market_type",             # smallcap / largecap / midcap / crypto / etc.
    "segment",                 # same as market_type
    "ai_model",                # claude-haiku-4-5-20251001 etc. (kebab + digit)
    "level",                   # normal / elevated / crisis / severe
    "crisis_level",            # same
    "severity",                # warning / critical / catastrophic / etc.
    "approximation_quality",   # low / medium / high
    "right",                   # C / P (option type)
    "_curve_status",           # normal / flat / inverted
    "_market_gex_regime",      # pinning / expansion / balanced
    "_rotation_phase",         # risk_on / risk_off / mixed
    "vwap_position",           # above / at / below
    "sector_trend",            # inflow / outflow / flat
    "insider_direction",       # buying / selling / neutral
    "options_signal",          # bullish_flow / bearish_flow / neutral
    "congress_direction",      # buying / selling / neutral
    "eps_revision_direction",  # up / down / flat
    "insider_near_earnings",   # bullish / bearish / neutral
    "earnings_surprise_direction",  # beats / misses / mixed
    "google_trends_direction", # rising / flat / falling
    "trigger",                 # cooldown / wash_cooldown / blacklist / etc.
    "intent",                  # buy_to_open / sell_to_open / etc.
    "position_intent",         # same
}


# API endpoints to skip (require complex setup or aren't user-facing)
SKIP_ROUTES = {
    "/api/dashboard-totals",  # heavy upstream calls
    "/api/scheduler-status",
    "/api/cycle-data",        # not always populated
    "/api/scan-status",
}


def _walk_json(obj, path=(), parent=None):
    """Yield (path, key, value, parent_dict) for every leaf value.
    The `parent_dict` lets callers detect labeled-list pattern
    (a dict that has both `name` and `label` — `name` is the
    form-action key, `label` is what's rendered)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_json(v, path + (str(k),), parent=obj)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _walk_json(item, path + (f"[{i}]",), parent=parent)
    else:
        yield path, (path[-1] if path else None), obj, parent


def _logged_in_client():
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["LOGIN_DISABLED"] = True
    return app.test_client(), app


def _api_routes(app):
    """Return GET routes under /api/ that take int profile_id only
    (the simplest case to fuzz)."""
    out = []
    for rule in app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()):
            continue
        if not rule.rule.startswith("/api/"):
            continue
        if "<int:profile_id>" not in rule.rule:
            continue
        if any(rule.rule.startswith(s) for s in SKIP_ROUTES):
            continue
        out.append(rule.rule.replace("<int:profile_id>", "1"))
    return sorted(set(out))


@pytest.fixture
def patched_user_data():
    profile = {
        "id": 1, "user_id": 1, "name": "Test", "market_type": "smallcap",
        "alpaca_account_id": 1, "enabled": 1,
        "ai_api_key_enc": "", "consensus_api_key_enc": "",
        "alpaca_api_key_enc": "", "alpaca_secret_key_enc": "",
    }
    user = MagicMock()
    user.is_authenticated = True
    user.id = 1
    user.effective_user_id = 1
    return profile, user


class TestNoRawSnakeCaseInAPIValues:
    def test_no_snake_case_string_values_in_user_facing_fields(self, patched_user_data):
        """Walk every /api/<route>/1 GET response. Any string VALUE
        that matches snake_case pattern in a non-allowlisted field
        is a real leak (the JS will render it raw). Catch it before
        users notice."""
        client, app = _logged_in_client()
        with patch("flask_login.utils._get_user",
                    return_value=patched_user_data[1]), \
             patch("views.get_trading_profile",
                    return_value=patched_user_data[0]), \
             patch("views.get_user_profiles",
                    return_value=[patched_user_data[0]]):

            routes = _api_routes(app)
            leaks = []
            for route in routes:
                try:
                    resp = client.get(route)
                except Exception:
                    continue
                if resp.status_code != 200:
                    continue
                try:
                    data = json.loads(resp.data)
                except (ValueError, json.JSONDecodeError):
                    continue
                for path, k, v, parent in _walk_json(data):
                    if not isinstance(v, str):
                        continue
                    if not isinstance(k, str):
                        continue
                    # Skip allowlisted fields
                    if k in INTERNAL_VALUE_FIELDS:
                        continue
                    # Skip fields ending in `_raw` (caller convention
                    # for "the raw enum, label is in <field> sibling")
                    if k.endswith("_raw"):
                        continue
                    # Labeled-list pattern: if the parent dict has BOTH
                    # `name` and `label`, then `name` is the form-action
                    # key (intentionally raw) and `label` is what's
                    # rendered. Same for `key`+`label` and `id`+`name`.
                    if parent and isinstance(parent, dict):
                        if (k == "name" and "label" in parent) or \
                           (k == "key" and "label" in parent):
                            continue
                    # Skip OCC option symbols (start with letters,
                    # have leading uppercase + digits + C/P) and
                    # ticker symbols (all uppercase).
                    if v.isupper() or " " in v:
                        continue
                    if SNAKE_CASE.match(v):
                        leaks.append((route, ".".join(path), k, v))

            if leaks:
                details = "\n".join(
                    f"  {r}  path={p}  field={k!r}  value={v!r}"
                    for r, p, k, v in leaks[:15]
                )
                pytest.fail(
                    "API endpoints return raw snake_case STRING VALUES "
                    "in non-allowlisted fields. The dashboard JS renders "
                    "these directly to the UI as snake_case.\n\n"
                    "Fix one of:\n"
                    "  1. Resolve via display_name() server-side and return "
                    "the human label.\n"
                    "  2. If the field is consumed as a switch/case code, "
                    "add it to INTERNAL_VALUE_FIELDS in this test with a "
                    "comment explaining why it's raw.\n"
                    "  3. Rename the field to <name>_raw and add a sibling "
                    "<name> with the resolved label.\n\n"
                    f"Leaks (first 15):\n{details}"
                )
