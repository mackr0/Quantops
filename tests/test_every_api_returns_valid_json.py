"""Structural guardrail: every `/api/*` endpoint must return
valid JSON with `Content-Type: application/json`, regardless of
profile_id input.

The bug class.
A route returns HTML (template render with wrong content type),
or returns Python `repr(dict)` instead of `jsonify(...)`, or has
an unhandled error producing werkzeug's HTML 500 page. The
dashboard JS then:
  - Fails to parse → silent empty panel
  - Misinterprets HTML as JSON → JS error in console (operator
    doesn't see)
  - Shows a stale value indefinitely

The May 13 page-route 500 incident's API analog: APIs returning
500 don't surface to operators because the JS just doesn't render
the panel. No alarm.

This test extends the page-route no-500 work to the API layer
with stricter shape contract: response must be (a) status 2xx
or 4xx (no 5xx), (b) Content-Type starting with application/json
(unless 4xx with body), (c) parseable as JSON, (d) top-level dict
or list. Iterates profile_id variations including the empty-
positions edge case.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

# Inline copy of the fixture from test_no_500_per_profile.py
# (cross-test imports require shared conftest setup; inlining is
# the simpler path for now).
PROFILE_ID_VARIATIONS = [0, 1, 2, 5, 999]


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
    """Set up a real SQLite test environment with seeded profiles
    that mirror production shapes — including profile 5 with
    positive equity and zero positions (the May 13 incident shape)."""
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


# Routes that are intentionally NOT JSON (return text or HTML
# fragments). Each entry needs a written rationale.
ALLOWLIST_NON_JSON_ROUTES = {
    "/api/positions-html":
        "Returns HTML fragment for server-side rendering of the "
        "Open Positions table — not a data API.",
}


def _discover_api_routes(app):
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


def _is_allowlisted(url: str) -> bool:
    """True iff the URL prefix matches an allowlisted non-JSON
    route. Allows the test to skip endpoints that legitimately
    return HTML/text."""
    bare = url.split("?")[0].rstrip("/")
    for prefix in ALLOWLIST_NON_JSON_ROUTES:
        if bare.startswith(prefix):
            return True
    return False


class TestEveryApiReturnsValidJson:
    def test_every_api_response_is_valid_json(
            self, patched_user_with_profiles):
        client, app = _logged_in_client()
        routes = _discover_api_routes(app)
        assert len(routes) >= 5, (
            f"Discovered only {len(routes)} /api/* routes — "
            f"likely a Flask blueprint registration regression."
        )
        violations = []
        with patch("flask_login.utils._get_user",
                    return_value=patched_user_with_profiles):
            for route in routes:
                if _is_allowlisted(route):
                    continue
                # Test each profile_id variation
                for pid in [None] + PROFILE_ID_VARIATIONS:
                    url = (route if pid is None
                           else f"{route}?profile_id={pid}")
                    try:
                        resp = client.get(url, follow_redirects=False)
                    except Exception as exc:
                        violations.append(
                            (url, "raised",
                             f"{type(exc).__name__}: {str(exc)[:120]}")
                        )
                        continue
                    # 5xx = bug, period
                    if 500 <= resp.status_code < 600:
                        violations.append(
                            (url, f"{resp.status_code}",
                             resp.data[:120].decode(
                                 "utf-8", errors="replace"))
                        )
                        continue
                    # Auth redirect (302) is fine — endpoint
                    # delegates to login flow
                    if resp.status_code in (302, 401, 403, 404):
                        continue
                    # Content-Type must be JSON-ish
                    ct = (resp.content_type or "").lower()
                    if "application/json" not in ct:
                        violations.append(
                            (url, "wrong-content-type",
                             f"Content-Type={ct!r}, expected "
                             f"application/json")
                        )
                        continue
                    # Body must parse as JSON
                    try:
                        data = json.loads(resp.data)
                    except (ValueError, json.JSONDecodeError) as exc:
                        violations.append(
                            (url, "invalid-json",
                             f"{exc}: body[:80]={resp.data[:80]!r}")
                        )
                        continue
                    # Top-level should be dict or list
                    if not isinstance(data, (dict, list)):
                        violations.append(
                            (url, "wrong-shape",
                             f"top-level is {type(data).__name__}, "
                             f"expected dict or list")
                        )
                        continue
        if violations:
            details = "\n".join(
                f"  {url} → {kind}\n    {detail}"
                for url, kind, detail in violations[:15]
            )
            pytest.fail(
                f"{len(violations)} API responses violated the "
                f"valid-JSON contract.\n\nFirst {min(15, len(violations))} "
                f"failures:\n{details}\n\nFix:\n"
                f"  - 5xx → fix the handler exception\n"
                f"  - wrong-content-type → use jsonify(...) or "
                f"return Response(json.dumps(d), mimetype='application/json')\n"
                f"  - invalid-json → check for bare strings / "
                f"repr() returns\n"
                f"  - wrong-shape → wrap in {{...}} or [...]\n"
                f"  - HTML-returning routes → add to "
                f"ALLOWLIST_NON_JSON_ROUTES with rationale"
            )
