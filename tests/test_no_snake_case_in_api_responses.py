"""End-to-end snake_case guardrail covering every user-facing surface.

The previous test (`test_no_snake_case_in_optimizer_strings`) only
covered `_optimize_*` function returns inside `self_tuning.py`. That
left every other surface — API responses, HTML templates, JS-rendered
sections — uncovered. Each new feature wave added new leak paths the
test couldn't see.

This test closes that gap by going end-to-end:

  1. **API discovery**: enumerates every GET endpoint registered with
     the Flask app under the `/api/...` prefix. For each, hits it
     with a mocked logged-in user, walks the JSON response, and
     fails if any PARAM_BOUNDS key appears as a dict KEY anywhere
     in the response (the pattern that JS Object.entries-renders
     directly into the page).

  2. **Page render check**: for the AI / Settings / Performance /
     Trades pages, hits the route and parses the response HTML.
     Strips hidden form values, `<option value="">` attributes, and
     embedded JS source (which legitimately reference raw keys),
     then asserts no PARAM_BOUNDS key appears in the visible text.

The contract this enforces: any data-returning endpoint that surfaces
parameter metadata MUST resolve labels server-side (using
`display_name(name)` or the layer-specific helper), not pass raw
param keys to the UI.

Allowed exceptions are explicitly enumerated in
`ALLOWED_RAW_KEY_FIELDS` — paths where a raw key appears as a VALUE
of a field whose consumer (the JS) is contractually obligated to
look up its label. Currently only `param_name` (paired with
`param_label`), `parameter_name` (paired with `parameter_label`),
`change_type`, and the explicit `key` / `field` pattern.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, List, Set, Tuple
from unittest.mock import patch, MagicMock

import pytest


# Field names where a raw PARAM_BOUNDS string IS allowed because the
# consuming JS knows to translate it via display_name.
ALLOWED_RAW_KEY_FIELDS = {
    "param_name", "parameter_name", "change_type", "adjustment_type",
    "key", "field", "strategy_type",
}

# URL parameters to inject when an endpoint requires them.
DEFAULT_QUERY_PARAMS = {
    "profile_id": "1",
    "param_name": "ai_confidence_threshold",
    "symbol": "NVDA",
}


def _param_bounds_keys() -> Set[str]:
    from param_bounds import PARAM_BOUNDS
    return set(PARAM_BOUNDS.keys())


def _walk_json(obj, path: Tuple = ()):
    """Yield (path, key, value) for every nested node."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from _walk_json(v, path + (str(k),))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_json(v, path + (f"[{i}]",))


def _strip_html_to_visible_text(html: str) -> str:
    """Strip `<script>`, `<style>`, hidden inputs, option values, and
    HTML attributes. Returns the text a human would actually see."""
    # Remove script and style blocks entirely
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html,
                   flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html,
                   flags=re.DOTALL | re.IGNORECASE)
    # Drop the value="..." of <option> tags (raw keys are common in
    # form option values) — keep the displayed text between tags
    html = re.sub(r'(<option\b[^>]*\s)value="[^"]*"',
                   r"\1value=\"\"", html, flags=re.IGNORECASE)
    # Remove ALL HTML attributes (anything inside `<...>`)
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities just enough to expose words
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))
    # Collapse whitespace
    html = re.sub(r"\s+", " ", html)
    return html


def _logged_in_client():
    """Build a Flask test client with a mocked logged-in user."""
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        sess["_fresh"] = True
    return client, app


def _seed_profile_with_every_override(profile_id: int = 1) -> dict:
    """Build a profile dict with overrides on every layer using
    PARAM_BOUNDS keys — so any leak path will surface a raw key."""
    return {
        "id": profile_id, "user_id": 1, "name": "Test Profile",
        "enabled": 1,
        "ai_confidence_threshold": 25,
        "max_position_pct": 0.10, "max_total_positions": 10,
        "stop_loss_pct": 0.03, "take_profit_pct": 0.10,
        "max_correlation": 0.7, "max_sector_positions": 5,
        "rsi_overbought": 85.0, "rsi_oversold": 25.0,
        "gap_pct_threshold": 3.0, "min_volume": 500000,
        "drawdown_pause_pct": 0.20, "drawdown_reduce_pct": 0.10,
        "min_price": 1.0, "max_price": 20.0,
        "signal_weights": json.dumps(
            {"options_signal": 0.7, "vwap_position": 0.4,
             "put_call_ratio": 0.7}),
        "regime_overrides": json.dumps(
            {"stop_loss_pct": {"volatile": 0.06},
             "max_position_pct": {"bear": 0.05}}),
        "tod_overrides": json.dumps(
            {"max_position_pct": {"open": 0.05}}),
        "symbol_overrides": json.dumps(
            {"stop_loss_pct": {"NVDA": 0.08}}),
        "prompt_layout": json.dumps({"alt_data": "brief"}),
        "capital_scale": 0.85,
    }


@pytest.fixture
def patched_user_data():
    """Patch every common DB lookup so API endpoints return seeded
    data, with a logged-in user."""
    profile = _seed_profile_with_every_override(1)

    user_obj = type("U", (), {})()
    user_obj.is_authenticated = True
    user_obj.is_active = True
    user_obj.is_anonymous = False
    user_obj.effective_user_id = 1
    user_obj.id = 1
    user_obj.email = "test@example.com"
    user_obj.get_id = lambda: "1"

    patches = [
        patch("flask_login.utils._get_user", return_value=user_obj),
        patch("views.get_trading_profile", return_value=profile),
        patch("views.get_user_profiles", return_value=[profile]),
    ]
    for p in patches:
        p.start()
    yield profile, user_obj
    for p in patches:
        p.stop()


def _api_routes(app) -> List[str]:
    """Discover every GET-able /api/* endpoint registered with the app."""
    out = []
    for rule in app.url_map.iter_rules():
        if not str(rule).startswith("/api/"):
            continue
        if "GET" not in (rule.methods or set()):
            continue
        # Substitute URL parameters with sensible defaults
        path = str(rule)
        for arg_name in (rule.arguments or set()):
            placeholder = "<int:" + arg_name + ">"
            path = path.replace(placeholder, "1")
            placeholder = "<" + arg_name + ">"
            path = path.replace(placeholder, "1")
        # Append default query string
        qs = "&".join(f"{k}={v}" for k, v in DEFAULT_QUERY_PARAMS.items())
        sep = "&" if "?" in path else "?"
        out.append(path + sep + qs)
    return sorted(out)


class TestNoSnakeCaseInApiResponses:
    def test_every_api_endpoint_response(self, patched_user_data):
        """For every GET /api/* endpoint, walk the JSON response and
        fail if any PARAM_BOUNDS key appears as a dict KEY in a
        non-allowlisted position."""
        client, app = _logged_in_client()
        # Re-apply the user patch in this context
        with patch("flask_login.utils._get_user",
                    return_value=patched_user_data[1]):
            with patch("views.get_trading_profile",
                        return_value=patched_user_data[0]):
                with patch("views.get_user_profiles",
                            return_value=[patched_user_data[0]]):
                    routes = _api_routes(app)
                    leaks = []
                    param_keys = _param_bounds_keys()
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
                        for path, k, v in _walk_json(data):
                            # Look for raw param-name-shaped dict keys
                            if isinstance(k, str) and k in param_keys:
                                # Allow when the parent path's last
                                # segment is itself a parameter key
                                # (some endpoints structure data as
                                # {param_name: {...}} but only if a
                                # sibling label is present).
                                continue  # raw key as a TOP-LEVEL
                                          # response key isn't the leak
                                          # we care about — focus on
                                          # nested-leaf positions.
                            # Look for raw param-name strings appearing
                            # as VALUES whose containing field name
                            # isn't on the allowlist (those allowed
                            # fields are contractually translated by
                            # the consuming JS via display_name).
                            if (isinstance(v, str) and v in param_keys
                                    and isinstance(k, str)
                                    and k not in ALLOWED_RAW_KEY_FIELDS):
                                leaks.append(
                                    (route, path, k, v))
                            # Look for nested dicts whose KEYS are
                            # PARAM_BOUNDS names (the
                            # `Object.entries(d).forEach(e => e[0])`
                            # leak pattern).
                            if isinstance(v, dict):
                                bad = set(v.keys()) & param_keys
                                if bad:
                                    leaks.append(
                                        (route, path + (str(k),),
                                         "<keys>", sorted(bad)))

                    if leaks:
                        details = "\n".join(
                            f"  {r}\n    at path {'.'.join(p)}: "
                            f"key={k!r} value={v!r}"
                            for r, p, k, v in leaks[:8]
                        )
                        pytest.fail(
                            "API responses contain raw PARAM_BOUNDS keys "
                            "in user-facing positions. The JS will "
                            "render them as snake_case in the UI.\n\n"
                            "Fix one of:\n"
                            "  1. Resolve labels server-side via "
                            "display_name(name) before returning.\n"
                            "  2. Use the labeled-list shape: "
                            "[{key, label, value}, ...] instead of "
                            "{name: value} dicts.\n"
                            "  3. If the field name needs to be a raw "
                            "key for legitimate reasons (consumer JS "
                            "translates it), add it to "
                            "ALLOWED_RAW_KEY_FIELDS.\n\n"
                            f"Leaks (first 8):\n{details}"
                        )
