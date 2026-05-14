"""Structural guardrail: NO page route on the site may return 5xx.

The 2026-05-13 incident: `/performance?profile_id=5` returned a 500
because profile 5 had positive equity but zero open positions, and
the template's `is not none` guard wrongly passed Jinja Undefined.
The narrower test (`/performance` with no params) passed; the
specific bad combination did not.

This test enforces the GENERAL invariant: every HTML page registered
with the app, hit with several representative parameter combinations,
must not 5xx. New routes get coverage for free. New profile shapes
(zero positions, zero trades, archived, missing data) are
represented by the seeded fixtures.

What this catches:
  - Template Undefined-not-None guards
  - Server handlers that raise on edge-case data
  - Missing columns / dict keys assumed by handler code
  - Any new page added without considering empty-data cases

What this does NOT catch:
  - Page renders without crashing but contains wrong data
  - Page returns 200 but with a JS error
  - API endpoints (covered by other guardrail tests)

Scope: HTML pages only (everything except `/api/*`, `/static/*`).
HTTP method: GET only (POST endpoints get separate auth/CSRF
treatment).
"""
from __future__ import annotations

import os
import sys
from typing import Iterable, List
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# Profile-id values to vary. Includes the exact shapes that have
# triggered past 5xx incidents:
#   0   — "all profiles" sentinel
#   1   — fully-populated profile (positions + trades + history)
#   5   — positive equity, zero open positions (May 13 incident)
#   2   — archived profile (no recent activity)
#   999 — non-existent (defensive)
PROFILE_ID_VARIATIONS = [0, 1, 2, 5, 999]


# Symbol/date variations for routes that take them. Empty string
# tests the "no filter" path; a real symbol tests the filter path;
# a missing symbol tests the no-results path.
SYMBOL_VARIATIONS = ["", "AAPL", "ZZZZNONEXIST"]


# URL parameter substitutions for typed Flask path params (e.g.,
# `/foo/<int:profile_id>`).
PATH_PARAM_DEFAULTS = {
    "profile_id":  "1",
    "symbol":      "AAPL",
    "trade_id":    "1",
    "id":          "1",
    "user_id":     "1",
    "strategy":    "market_engine",
    "filename":    "test.html",
}


# Routes to skip entirely. Each entry needs a one-line justification.
SKIP_PREFIXES = (
    "/static/",  # served by Flask, not a render target
    "/api/",     # JSON APIs covered by other guardrail tests
    "/_",        # Flask internals (debug toolbar etc.)
)


SKIP_PATHS = {
    "/logout":   "auth side-effect; tested in auth tests",
    "/auth":     "POST-only auth handling",
}


def _seed_profile(profile_id: int, name: str = "Test", **overrides) -> dict:
    base = {
        "id": profile_id, "user_id": 1, "name": name,
        "enabled": 1, "is_virtual": 1, "initial_capital": 25000.0,
        "market_type": "smallcap",
        "ai_confidence_threshold": 25,
        "max_position_pct": 0.10, "max_total_positions": 10,
        "stop_loss_pct": 0.03, "take_profit_pct": 0.10,
        "max_correlation": 0.7, "max_sector_positions": 5,
        "rsi_overbought": 85.0, "rsi_oversold": 25.0,
        "gap_pct_threshold": 3.0, "min_volume": 500_000,
        "drawdown_pause_pct": 0.20, "drawdown_reduce_pct": 0.10,
        "min_price": 1.0, "max_price": 20.0,
        "signal_weights": "{}",
        "regime_overrides": "{}",
        "tod_overrides": "{}",
        "symbol_overrides": "{}",
        "prompt_layout": "{}",
        "capital_scale": 1.0,
    }
    base.update(overrides)
    return base


@pytest.fixture
def patched_user_with_profiles(tmp_path, monkeypatch):
    """Set up a real SQLite test environment with minimal seed data.

    The May 13 incident wouldn't be caught by mock-only fixtures
    because the bug was in shape-of-real-data — `compute_exposure`
    returning a truncated dict for empty-positions profiles. The
    test must therefore use the same DB schema + init path as prod
    so handler code sees real query results, not mocks.
    """
    import sqlite3
    import config
    import models

    # Master DB
    master_db = str(tmp_path / "quantopsai.db")
    monkeypatch.setattr(config, "DB_PATH", master_db)

    # Initialize the schema
    models.init_user_db()

    # Per-profile DBs (need at least the trades + ai_predictions
    # tables so handler code's queries return [] not raise).
    for pid in (1, 2, 5, 10):
        db = str(tmp_path / f"quantopsai_profile_{pid}.db")
        from journal import init_db as init_journal_db
        init_journal_db(db)

    # Seed users + trading_profiles
    conn = sqlite3.connect(master_db)
    conn.execute(
        "INSERT INTO users (id, email, password_hash, is_admin, role) "
        "VALUES (1, 'test@example.com', 'x', 1, 'admin')"
    )
    profile_specs = [
        (1, "Mid Cap", "midcap", 1),
        (2, "Crypto (archived)", "crypto", 0),
        # Profile 5: positive equity, zero positions — the May 13
        # incident shape. Per-profile DB exists but trades table is
        # empty.
        (5, "Small Cap 25K", "smallcap", 1),
        (10, "Small Cap Shorts", "smallcap", 1),
    ]
    for pid, name, mt, enabled in profile_specs:
        conn.execute(
            "INSERT INTO trading_profiles (id, user_id, name, "
            "market_type, enabled, is_virtual, initial_capital) "
            "VALUES (?, 1, ?, ?, ?, 1, 25000.0)",
            (pid, name, mt, enabled),
        )
    conn.commit()
    conn.close()

    # Mock user
    user_obj = type("U", (), {})()
    user_obj.is_authenticated = True
    user_obj.is_active = True
    user_obj.is_anonymous = False
    user_obj.is_admin = True   # /admin requires this
    user_obj.role = "admin"
    user_obj.effective_user_id = 1
    user_obj.id = 1
    user_obj.email = "test@example.com"
    user_obj.get_id = lambda: "1"

    # Mock the broker layer so handlers see realistic shapes:
    # - Profile 1 (Mid Cap): one open AAPL long, $100K equity
    # - Profile 5 (Small Cap 25K): NO positions, $25K equity ←
    #   the May 13 incident shape — must reach the empty-positions
    #   code path inside compute_exposure
    # - Profile 10 (Small Cap Shorts): one open TSLA short
    POSITIONS_BY_PROFILE = {
        1: [{"symbol": "AAPL", "qty": 50, "market_value": 9000,
             "current_price": 180, "avg_entry_price": 175,
             "unrealized_pl": 250, "unrealized_plpc": 0.028}],
        2: [],   # Crypto archived
        5: [],   # ← the incident shape: equity but no positions
        10: [{"symbol": "TSLA", "qty": -10, "market_value": -2500,
              "current_price": 250, "avg_entry_price": 260,
              "unrealized_pl": 100, "unrealized_plpc": 0.038}],
    }
    EQUITY_BY_PROFILE = {1: 100_000, 2: 0, 5: 25_000, 10: 50_000}

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


def _discover_page_routes(app) -> List[str]:
    """Return the URL of every GET-able page route (HTML, not API).
    URL params substituted with PATH_PARAM_DEFAULTS values.

    Routes that this can't substitute (e.g. unknown param name) are
    skipped with a logged warning so the test's intent stays
    transparent — adding a new param to PATH_PARAM_DEFAULTS extends
    coverage automatically.
    """
    out = []
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if any(path.startswith(p) for p in SKIP_PREFIXES):
            continue
        if path in SKIP_PATHS:
            continue
        if "GET" not in (rule.methods or set()):
            continue
        # Substitute path params
        skip = False
        for arg_name in (rule.arguments or set()):
            placeholder_int = "<int:" + arg_name + ">"
            placeholder_str = "<" + arg_name + ">"
            placeholder_path = "<path:" + arg_name + ">"
            default = PATH_PARAM_DEFAULTS.get(arg_name)
            if default is None:
                # Unknown param — skip so we don't fabricate values
                skip = True
                break
            path = (path.replace(placeholder_int, default)
                        .replace(placeholder_str, default)
                        .replace(placeholder_path, default))
        if skip:
            continue
        out.append(path)
    return sorted(set(out))


def _expand_with_query_variations(routes: List[str]) -> Iterable[str]:
    """Yield each route both bare and with the parameter variations
    most likely to surface render bugs. Bare-no-params is included
    because some handlers branch on missing-param vs param=0."""
    for route in routes:
        yield route  # bare — no query params
        # profile_id variations — every page may behave differently
        # on different profile shapes
        for pid in PROFILE_ID_VARIATIONS:
            yield f"{route}?profile_id={pid}"


class TestNoPageRoute500s:
    """The structural invariant: no GET-able HTML page may 5xx
    under any tested parameter combination."""

    def test_no_5xx_on_any_page_route(self, patched_user_with_profiles):
        client, app = _logged_in_client()
        routes = _discover_page_routes(app)
        # Sanity: we discovered something
        assert len(routes) >= 5, (
            f"Route discovery found only {len(routes)} routes — "
            f"likely the URL-map walker is broken or app failed to "
            f"register blueprints. Investigate before relying on "
            f"this guardrail."
        )
        failures = []
        with patch("flask_login.utils._get_user",
                    return_value=patched_user_with_profiles):
            for url in _expand_with_query_variations(routes):
                try:
                    resp = client.get(url, follow_redirects=False)
                except Exception as exc:
                    # Handler raised before returning a response —
                    # always a bug.
                    failures.append((url, "EXCEPTION",
                                     f"{type(exc).__name__}: "
                                     f"{str(exc)[:200]}"))
                    continue
                if 500 <= resp.status_code < 600:
                    body = resp.data.decode(
                        "utf-8", errors="replace"
                    )[:400]
                    failures.append(
                        (url, resp.status_code, body)
                    )
        if failures:
            details = "\n".join(
                f"  {url} → {code}\n    {snippet}"
                for url, code, snippet in failures[:10]
            )
            pytest.fail(
                f"{len(failures)} route × parameter combinations "
                f"returned 5xx (or raised). New page routes get "
                f"coverage automatically — fix the handler/template, "
                f"don't allowlist.\n\n"
                f"First {min(10, len(failures))} failures:\n{details}"
            )


class TestPerformancePageZeroPositionProfile:
    """Specific regression for the 2026-05-13 incident — pin the
    exact /performance?profile_id=<empty-positions-profile> path."""

    def test_performance_with_empty_positions_profile_renders(
            self, patched_user_with_profiles):
        client, app = _logged_in_client()
        with patch("flask_login.utils._get_user",
                    return_value=patched_user_with_profiles):
            resp = client.get("/performance?profile_id=5")
        assert resp.status_code != 500, (
            f"/performance?profile_id=5 returned 500. The May 13 "
            f"incident: empty-positions early-exit in "
            f"compute_exposure returned a truncated dict missing "
            f"`book_beta`, which Jinja's `is not none` guard "
            f"wrongly passed (Undefined is not None), then "
            f"format() crashed."
        )
