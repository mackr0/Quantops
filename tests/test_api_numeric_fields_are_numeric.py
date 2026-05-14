"""Structural guardrail: every numeric-looking field in an `/api/*`
JSON response is `int`/`float`/None — never a `str`.

The bug class.
Backend serializes a `Decimal`, `numpy.float64`, or some computed
value as `str` (e.g. `str(Decimal("0.05"))` returns `"0.05"`). The
JS dashboard then does `value + 1` and gets `"0.051"` instead of
`1.05`. The dashboard renders the wrong number — silently — and
operators trust it. This is a class of bug that DOES NOT surface as
a 500 / parse error; it surfaces as wrong P&L, wrong percentages,
wrong allocation displays, days later (when someone notices a chart
is wrong).

The acceptable patterns are:
  1. Field is `int` or `float` → ok
  2. Field is `None` → ok ONLY when the route documents it via the
     ALLOWED_NULL_FIELDS allowlist (e.g., a not-yet-computed value)
  3. Field is `str` ONLY when the route documents it via the
     ALLOWED_STRING_VALUES_FOR allowlist (e.g., a status enum that
     happens to look numeric, or a pre-formatted display string)

Default-deny: any unallowed string-in-numeric-field fails.

This test reuses the per-profile fixture pattern from
`test_every_api_returns_valid_json.py` (inlined; see that file's
docstring for why inlining is preferred over a shared conftest).
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, List, Optional, Set, Tuple
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


PROFILE_ID_VARIATIONS = [0, 1, 2, 5, 999]


# Field names that look numeric and SHOULD be int/float.
# The set is curated, not pattern-only — too many false positives
# from a bare regex.
NUMERIC_FIELDS: Set[str] = {
    "equity", "cash", "buying_power", "portfolio_value",
    "pnl", "pnl_pct", "unrealized_pl", "unrealized_plpc",
    "realized_pl", "realized_pnl",
    "qty", "shares", "size", "position_size",
    "price", "current_price", "avg_entry_price", "limit_price",
    "stop_price", "fill_price", "filled_avg_price",
    "market_value", "cost_basis", "notional",
    "win_rate", "win_rate_pct", "loss_rate",
    "drawdown", "drawdown_pct", "max_drawdown",
    "confidence", "confidence_pct", "score", "weight",
    "sharpe", "sortino", "alpha", "beta", "volatility",
    "ai_calls_today", "trades_today", "candidates_today",
    "fees", "commission", "slippage",
    "delta", "gamma", "theta", "vega", "iv", "iv_rank",
    "spread", "spread_pct", "bid", "ask", "mid",
    "rsi", "atr", "macd",
    "volume", "avg_volume",
}

# Suffixes that strongly imply the field is numeric.
NUMERIC_SUFFIXES: Tuple[str, ...] = (
    "_pct", "_count", "_qty", "_price", "_pnl", "_score",
    "_ratio", "_rate", "_vol", "_amount", "_value",
)


def _looks_numeric(field_name: str) -> bool:
    if field_name in NUMERIC_FIELDS:
        return True
    for suf in NUMERIC_SUFFIXES:
        if field_name.endswith(suf):
            return True
    return False


# (route_prefix, field_name) → rationale.
# Default-deny; entries here legitimately return strings even though
# the field name LOOKS numeric. Each entry needs written rationale.
ALLOWED_STRING_VALUES_FOR: dict = {
    # status enums that contain digits or look numeric in name
    ("/api/", "status"):
        "Status fields are enum strings ('ACTIVE', 'PAUSED'), not numeric.",
    ("/api/", "name"):
        "Display name fields are strings — never numeric.",
    # Date/time fields ending in _at can include unix-time-ish strings
    ("/api/", "fetched_at"):
        "ISO-8601 timestamp string, not a unix epoch.",
    ("/api/", "created_at"):
        "ISO-8601 timestamp string.",
    ("/api/", "updated_at"):
        "ISO-8601 timestamp string.",
    ("/api/", "submitted_at"):
        "ISO-8601 timestamp string.",
    ("/api/", "filled_at"):
        "ISO-8601 timestamp string.",
    # Action / verdict / direction enums
    ("/api/", "action"):
        "Trade action enum (BUY/SELL/...), not numeric.",
    ("/api/", "verdict"):
        "Specialist verdict enum (BUY/SELL/HOLD/VETO).",
    ("/api/", "direction"):
        "Direction enum (long/short).",
    ("/api/", "side"):
        "Order side enum (buy/sell).",
    ("/api/", "kind"):
        "Kind/category enum (e.g. cache_kind).",
    # Confidence is sometimes returned formatted as a percent string
    # (e.g. '78%') by the AI display payload — these endpoints are
    # display-only; numeric confidence is exposed via raw_confidence.
    ("/api/ai-summary", "confidence"):
        "AI-summary endpoint returns confidence as a pre-formatted "
        "'NN%' display string. The numeric form is exposed elsewhere.",
}


# (route_prefix, field_name) → rationale.
# Fields that legitimately return None (not-yet-computed, no data
# for this profile). Each entry needs written rationale.
ALLOWED_NULL_FIELDS: dict = {
    ("/api/", "veto_reason"):
        "Null when no specialist vetoed the trade.",
    ("/api/", "vetoed_by"):
        "Null when no specialist vetoed the trade.",
    ("/api/", "filled_avg_price"):
        "Null until an order fills (PENDING/CANCELLED orders).",
    ("/api/", "filled_at"):
        "Null until an order fills.",
    ("/api/", "fill_price"):
        "Null until an order fills.",
    ("/api/", "stop_price"):
        "Null when no protective stop is set on the position.",
    ("/api/", "limit_price"):
        "Null on market orders (no limit price).",
    ("/api/", "exit_price"):
        "Null on still-open positions.",
    ("/api/", "realized_pnl"):
        "Null on still-open positions (only known at close).",
    ("/api/", "realized_pl"):
        "Null on still-open positions (only known at close).",
    ("/api/", "max_drawdown"):
        "Null when insufficient history to compute.",
    ("/api/", "sharpe"):
        "Null when insufficient history to compute.",
    ("/api/", "sortino"):
        "Null when insufficient history to compute.",
    ("/api/", "alpha"):
        "Null when insufficient history to compute.",
    ("/api/", "beta"):
        "Null when insufficient history to compute.",
    ("/api/", "pnl"):
        "Null on still-open positions (P&L only realized at close); "
        "the open-position display uses unrealized_pl instead.",
    ("/api/", "decision_price"):
        "Null on positions that pre-date the meta-tracking schema or "
        "were entered manually without recording a decision price.",
    ("/api/", "slippage_pct"):
        "Null until both decision_price and fill_price are recorded "
        "and the slippage post-fill computation has run.",
}


def _allowlisted_string(route: str, field_name: str) -> Optional[str]:
    for (prefix, name), rationale in ALLOWED_STRING_VALUES_FOR.items():
        if name == field_name and route.startswith(prefix):
            return rationale
    return None


def _allowlisted_null(route: str, field_name: str) -> Optional[str]:
    for (prefix, name), rationale in ALLOWED_NULL_FIELDS.items():
        if name == field_name and route.startswith(prefix):
            return rationale
    return None


def _walk_response(node: Any, route: str,
                   path: str = "") -> List[Tuple[str, str, Any]]:
    """Walk a JSON response and yield (path, kind, bad_value) for
    every numeric-looking field whose value is the wrong type and
    not allowlisted."""
    issues: List[Tuple[str, str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            sub_path = f"{path}.{k}" if path else k
            if isinstance(v, (dict, list)):
                issues.extend(_walk_response(v, route, sub_path))
                continue
            if not isinstance(k, str):
                continue
            if not _looks_numeric(k):
                continue
            if isinstance(v, bool):
                # Booleans subclass int; avoid flagging as numeric
                # — but no numeric field should also be a bool.
                # Treat as type mismatch only if the field isn't
                # known-bool-shaped (e.g. `*_count`); skip for now.
                continue
            if isinstance(v, (int, float)):
                continue
            if v is None:
                if _allowlisted_null(route, k):
                    continue
                issues.append((sub_path, "null", v))
                continue
            if isinstance(v, str):
                if _allowlisted_string(route, k):
                    continue
                issues.append((sub_path, "string", v))
                continue
            # Other unexpected types
            issues.append((sub_path, type(v).__name__, v))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            sub_path = f"{path}[{i}]"
            issues.extend(_walk_response(item, route, sub_path))
    return issues


def _logged_in_client():
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


@pytest.fixture
def patched_user_with_profiles(tmp_path, monkeypatch):
    """Mirrors the fixture in test_every_api_returns_valid_json.py.
    See that file's docstring for the rationale on inlining."""
    import sqlite3
    import config
    import models
    master_db = str(tmp_path / "quantopsai.db")
    monkeypatch.setattr(config, "DB_PATH", master_db)
    models.init_user_db()
    for pid in (1, 2, 5, 10):
        from journal import init_db as init_journal_db
        init_journal_db(str(tmp_path / f"quantopsai_profile_{pid}.db"))
    conn = sqlite3.connect(master_db)
    try:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, is_admin, role) "
            "VALUES (1, 'test@example.com', 'x', 1, 'admin')"
        )
        for pid, name, mt, enabled in [
            (1, "Mid Cap", "midcap", 1),
            (2, "Crypto (archived)", "crypto", 0),
            (5, "Small Cap 25K", "smallcap", 1),
            (10, "Small Cap Shorts", "smallcap", 1),
        ]:
            conn.execute(
                "INSERT INTO trading_profiles (id, user_id, name, "
                "market_type, enabled, is_virtual, initial_capital) "
                "VALUES (?, 1, ?, ?, ?, 1, 25000.0)",
                (pid, name, mt, enabled),
            )
        conn.commit()
    finally:
        conn.close()

    user_obj = type("U", (), {})()
    user_obj.is_authenticated = True
    user_obj.is_active = True
    user_obj.is_anonymous = False
    user_obj.is_admin = True
    user_obj.role = "admin"
    user_obj.effective_user_id = 1
    user_obj.id = 1
    user_obj.email = "test@example.com"
    user_obj.get_id = lambda: "1"

    EQUITY_BY_PROFILE = {1: 100_000, 2: 0, 5: 25_000, 10: 50_000}
    POSITIONS_BY_PROFILE = {
        1: [{"symbol": "AAPL", "qty": 50, "market_value": 9000,
             "current_price": 180, "avg_entry_price": 175,
             "unrealized_pl": 250, "unrealized_plpc": 0.028}],
        2: [], 5: [],
        10: [{"symbol": "TSLA", "qty": -10, "market_value": -2500,
              "current_price": 250, "avg_entry_price": 260,
              "unrealized_pl": 100, "unrealized_plpc": 0.038}],
    }

    def _fake_account_info(ctx):
        pid = getattr(ctx, "profile_id", 0) or 0
        return {"equity": EQUITY_BY_PROFILE.get(pid, 0),
                "cash": EQUITY_BY_PROFILE.get(pid, 0),
                "buying_power": EQUITY_BY_PROFILE.get(pid, 0),
                "status": "ACTIVE"}

    def _fake_positions(ctx):
        pid = getattr(ctx, "profile_id", 0) or 0
        return POSITIONS_BY_PROFILE.get(pid, [])

    patches = [
        patch("flask_login.utils._get_user", return_value=user_obj),
        patch("views._safe_account_info", side_effect=_fake_account_info),
        patch("views._safe_positions", side_effect=_fake_positions),
    ]
    for p in patches:
        p.start()
    yield user_obj
    for p in patches:
        p.stop()


PATH_PARAM_DEFAULTS = {
    "profile_id": "1",
    "param_name": "ai_confidence_threshold",
    "symbol": "NVDA",
    "trade_id": "1",
}


def _discover_api_routes(app) -> List[str]:
    out = []
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if not path.startswith("/api/"):
            continue
        if "GET" not in (rule.methods or set()):
            continue
        skip = False
        for arg_name in (rule.arguments or set()):
            default = PATH_PARAM_DEFAULTS.get(arg_name)
            if default is None:
                skip = True
                break
            path = (path.replace("<int:" + arg_name + ">", default)
                        .replace("<" + arg_name + ">", default)
                        .replace("<path:" + arg_name + ">", default))
        if skip:
            continue
        out.append(path)
    return sorted(set(out))


# Routes that are intentionally NOT JSON; skip them.
ALLOWLIST_NON_JSON_ROUTES = {
    "/api/positions-html":
        "Returns HTML fragment, not JSON. Skipped by this test.",
}


def _is_allowlisted_route(url: str) -> bool:
    bare = url.split("?")[0].rstrip("/")
    for prefix in ALLOWLIST_NON_JSON_ROUTES:
        if bare.startswith(prefix):
            return True
    return False


class TestApiNumericFieldsAreNumeric:
    """Walks every successful /api/* JSON response × profile_id
    variation and asserts numeric-looking fields are int/float (or
    None when allowlisted)."""

    def test_no_string_in_numeric_field(self, patched_user_with_profiles):
        client, app = _logged_in_client()
        routes = _discover_api_routes(app)
        violations: List[Tuple[str, str, str, Any]] = []
        with patch("flask_login.utils._get_user",
                   return_value=patched_user_with_profiles):
            for route in routes:
                if _is_allowlisted_route(route):
                    continue
                for pid in [None] + PROFILE_ID_VARIATIONS:
                    url = (route if pid is None
                           else f"{route}?profile_id={pid}")
                    try:
                        resp = client.get(url, follow_redirects=False)
                    except Exception:
                        continue
                    if resp.status_code != 200:
                        continue
                    ct = (resp.content_type or "").lower()
                    if "application/json" not in ct:
                        continue
                    try:
                        data = json.loads(resp.data)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    issues = _walk_response(data, route)
                    for path, kind, val in issues:
                        violations.append((url, path, kind, val))
        if violations:
            details = "\n".join(
                f"  {url}\n    {path}: type={kind}, value={val!r}"
                for url, path, kind, val in violations[:25]
            )
            pytest.fail(
                f"{len(violations)} numeric-looking fields in API "
                f"responses had wrong type (string or unallowed "
                f"None).\n\nFirst {min(25, len(violations))} "
                f"failures:\n{details}\n\nFix one of:\n"
                f"  1. Convert backend value to int/float before "
                f"jsonify (e.g. float(decimal_value))\n"
                f"  2. If the field IS legitimately a string, add "
                f"to ALLOWED_STRING_VALUES_FOR with rationale\n"
                f"  3. If the field IS legitimately None, add to "
                f"ALLOWED_NULL_FIELDS with rationale"
            )
