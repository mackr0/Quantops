"""Structural guardrail: no ALLCAPS_SNAKE_CASE in user-facing API text.

The existing `test_no_snake_case_in_api_responses.py` catches lowercase
PARAM_BOUNDS keys (like `ai_confidence_threshold`). This file closes
the other half: ALLCAPS_SNAKE_CASE tokens that the LLM and internal
enums routinely emit and that leak into the UI when an endpoint
forgets to call `humanize()`.

Examples this catches:
  - STRONG_SELL, STRONG_BUY, MULTILEG_OPEN, PAIR_TRADE
  - BULL_PUT_SPREAD, IRON_CONDOR, COVERED_CALL
  - Anything matching `\\b[A-Z]{2,}(_[A-Z]+)+\\b`

Tests the CLASS, not the instance. If the AI invents a new
ALLCAPS_SNAKE token tomorrow (e.g., `BUTTERFLY_OPEN`) and it leaks
into a humanized field, this test catches it.

Scope: focused on TEXT-shaped fields (title, detail, reasoning,
message, summary, name) where the consumer JS renders the string
raw via `escapeHtml`. Raw-enum fields (signal_type, action,
strategy) whose JS consumers humanize before rendering are
allowlisted by FIELD NAME — see `RAW_ENUM_FIELDS`.

The 2026-05-12 incident: the Strategy Activity ticker showed
"STRONG_SELL signal (-2/4 score)..." because `api_activity`'s
`detail` field passed the raw AI reasoning through unhumanized.
This test fires on that exact shape.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, List, Tuple
from unittest.mock import patch

import pytest


# Regex catching uppercase identifier tokens with at least one
# underscore: `STRONG_SELL`, `MULTILEG_OPEN`, `BULL_PUT_SPREAD`.
# Anchored on word boundaries so it doesn't match within URLs or
# OCC-symbol strings (which lack underscores).
ALLCAPS_SNAKE_RE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z]+)+\b")


# Fields where a raw ALLCAPS_SNAKE token is ACCEPTABLE because the
# JS consumer is contractually obligated to map it before rendering.
# Adding a field name here is a deliberate decision — the consuming
# JS path MUST resolve the label (via the `display_name` filter or
# a JS-side mapping table). Otherwise the leak will surface visually.
#
# Tight allowlist: `signal_type`, `signal`, and `action` were
# REMOVED because the JS rendering paths
# (templates/dashboard.html: `c.signal`, `t.action`) inline these
# values into the DOM via `escapeHtml` — there is no JS-side
# humanize step. They MUST be humanized server-side. The Candidates
# Considered panel showing "STRONG_BUY" on 2026-05-12 was the leak.
RAW_ENUM_FIELDS = {
    # Raw enum tags consumed by JS that maps them before display.
    "predicted_signal",
    "activity_type", "type", "strategy", "strategy_type",
    "option_strategy", "spread_strategy", "side", "status",
    "rejection_code", "adjustment_type", "regime",
    # Substring-matchable raw-enum patterns — fields ending in
    # `_code` or `_type` are conventionally raw and JS-humanized.
    "_code", "_type",
}


# URL parameters to inject when an endpoint requires them.
DEFAULT_QUERY_PARAMS = {
    "profile_id": "1",
    "param_name": "ai_confidence_threshold",
    "symbol": "NVDA",
}


def _is_raw_enum_field(field_name: str) -> bool:
    """True if this field name is allowed to carry raw ALLCAPS_SNAKE."""
    if not field_name:
        return False
    if field_name in RAW_ENUM_FIELDS:
        return True
    for suffix in ("_code", "_type"):
        if field_name.endswith(suffix):
            return True
    return False


def _walk_strings(obj: Any,
                   path: Tuple = (),
                   parent_key: str = "") -> Iterable[Tuple[Tuple, str, str]]:
    """Yield (path, parent_key, string_value) for every string leaf."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, path + (str(k),), str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, path + (f"[{i}]",), parent_key)
    elif isinstance(obj, str):
        yield path, parent_key, obj


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


def _seed_profile(profile_id: int = 1) -> dict:
    return {
        "id": profile_id, "user_id": 1, "name": "Test Profile",
        "enabled": 1,
        "ai_confidence_threshold": 25,
        "max_position_pct": 0.10, "max_total_positions": 10,
        "stop_loss_pct": 0.03, "take_profit_pct": 0.10,
        "max_correlation": 0.7, "max_sector_positions": 5,
        "min_price": 1.0, "max_price": 20.0,
        "min_volume": 500000,
        "rsi_overbought": 85.0, "rsi_oversold": 25.0,
        "gap_pct_threshold": 3.0,
        "drawdown_pause_pct": 0.20, "drawdown_reduce_pct": 0.10,
        "signal_weights": "{}",
        "regime_overrides": "{}",
        "tod_overrides": "{}",
        "symbol_overrides": "{}",
        "prompt_layout": "{}",
        "capital_scale": 0.85,
    }


@pytest.fixture
def patched_user():
    user_obj = type("U", (), {})()
    user_obj.is_authenticated = True
    user_obj.is_active = True
    user_obj.is_anonymous = False
    user_obj.effective_user_id = 1
    user_obj.id = 1
    user_obj.email = "test@example.com"
    user_obj.get_id = lambda: "1"
    profile = _seed_profile()
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


# ---------------------------------------------------------------------------
# Unit test — the regex catches what it should, ignores what it shouldn't
# ---------------------------------------------------------------------------

class TestAllcapsSnakeRegex:
    def test_catches_obvious_leaks(self):
        assert ALLCAPS_SNAKE_RE.search("STRONG_SELL signal")
        assert ALLCAPS_SNAKE_RE.search("MULTILEG_OPEN executed")
        assert ALLCAPS_SNAKE_RE.search("BULL_PUT_SPREAD on AAPL")
        assert ALLCAPS_SNAKE_RE.search("Trade executed: STRONG_BUY F")

    def test_ignores_single_word_uppercase(self):
        # Plain uppercase ticker symbols / acronyms are NOT a leak
        assert not ALLCAPS_SNAKE_RE.search("BUY 100 AAPL")
        assert not ALLCAPS_SNAKE_RE.search("VIX 18.5")
        assert not ALLCAPS_SNAKE_RE.search("HOLD")
        assert not ALLCAPS_SNAKE_RE.search("SPY")

    def test_ignores_humanized_form(self):
        # After humanize() runs, "STRONG_SELL" → "Strong Sell"
        assert not ALLCAPS_SNAKE_RE.search("Strong Sell signal")
        assert not ALLCAPS_SNAKE_RE.search("Multileg Open executed")

    def test_ignores_occ_symbol(self):
        # OCC symbols don't have underscores
        assert not ALLCAPS_SNAKE_RE.search("AAPL  250620C00150000")


# ---------------------------------------------------------------------------
# Activity feed — the specific endpoint that leaked on 2026-05-12
# ---------------------------------------------------------------------------

class TestActivityFeedHumanizes:
    """The exact incident: a STRONG_SELL token in the activity feed's
    detail field rendered raw in the Strategy Activity ticker. Force
    a snake_case payload into the underlying activity_log and verify
    `humanize()` strips it before the JSON response is returned."""

    def test_api_activity_humanizes_detail(self, patched_user):
        client, app = _logged_in_client()
        fake_entries = [{
            "id": 1,
            "profile_id": 1,
            "user_id": 1,
            "timestamp": "2026-05-12T17:41:02",
            "activity_type": "trade_executed",
            "title": "SELL 139 F @ $11.91",
            "detail": "Trade executed: SELL F\nSTRONG_SELL signal (-2/4 score) "
                       "with 87% personal win rate on F. MULTILEG_OPEN bull_put_spread "
                       "next cycle.",
            "symbol": "F",
            "profile_name": "Small Cap",
        }]
        with patch("flask_login.utils._get_user",
                    return_value=patched_user[1]):
            with patch("views.get_activity_feed",
                        return_value=fake_entries):
                with patch("views.get_activity_count", return_value=1):
                    resp = client.get("/api/activity?limit=10")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Walk every string in the response — there must be no
        # ALLCAPS_SNAKE leak in TEXT-shaped fields
        leaks = []
        for path, parent_key, value in _walk_strings(data):
            if _is_raw_enum_field(parent_key):
                continue
            if ALLCAPS_SNAKE_RE.search(value):
                m = ALLCAPS_SNAKE_RE.search(value)
                leaks.append((".".join(path), parent_key,
                              m.group(0), value[:80]))
        if leaks:
            details = "\n".join(
                f"  path={p!r} key={k!r} token={t!r} in: {v!r}"
                for p, k, t, v in leaks
            )
            pytest.fail(
                "ALLCAPS_SNAKE_CASE leaked through /api/activity:\n"
                + details + "\n\nFix: apply humanize() to title/detail "
                "in views.api_activity before returning."
            )


# ---------------------------------------------------------------------------
# Cycle data — Candidates Considered shortlist (the 2026-05-12 leak)
# ---------------------------------------------------------------------------

class TestCycleDataHumanizes:
    """The Candidates Considered table renders `c.signal` raw via
    `td>' + c.signal + '<` in templates/dashboard.html. Without a
    server-side humanize, STRONG_BUY shows up in the UI verbatim."""

    def test_api_cycle_data_humanizes_shortlist_signal(
            self, patched_user, tmp_path, monkeypatch):
        client, app = _logged_in_client()
        monkeypatch.chdir(tmp_path)
        fake_cycle = {
            "profile_id": 1,
            "profile_name": "Test",
            "timestamp": 1778608138.81,
            "ai_reasoning": "No candidates met threshold",
            "trades_selected": [],
            "shortlist": [{
                "symbol": "UPST", "signal": "STRONG_BUY",
                "score": 3, "rsi": 40, "adx": 21, "mfi": 40,
                "volume_ratio": 0.4,
                "track_record": "21W/49L overall (30%) STRONG_BUY 0W/1L",
                "options_signal": "BULLISH_FLOW",
                "options_oracle_summary": "PCR=0.08(BULLISH_FLOW)",
            }],
        }
        (tmp_path / "cycle_data_1.json").write_text(json.dumps(fake_cycle))
        with patch("flask_login.utils._get_user",
                    return_value=patched_user[1]):
            resp = client.get("/api/cycle-data/1")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        leaks = []
        for path, parent_key, value in _walk_strings(data):
            if _is_raw_enum_field(parent_key):
                continue
            if ALLCAPS_SNAKE_RE.search(value):
                m = ALLCAPS_SNAKE_RE.search(value)
                leaks.append((".".join(path), parent_key,
                              m.group(0), value[:80]))
        if leaks:
            details = "\n".join(
                f"  path={p!r} key={k!r} token={t!r} in: {v!r}"
                for p, k, t, v in leaks
            )
            pytest.fail(
                "ALLCAPS_SNAKE_CASE leaked through /api/cycle-data:\n"
                + details + "\n\nFix: apply humanize() to shortlist "
                "fields in views.api_cycle_data."
            )


class TestCycleDataExecutionOutcome:
    """When the AI proposes SHORT F but F was already held long, the
    trade pipeline closes the long instead of opening a new short.
    The brain ticker MUST surface that mismatch — otherwise it says
    "SHORT F" and the operator goes looking for a non-existent
    short. The 2026-05-12 F incident."""

    def test_intent_short_executed_long_close_gets_badged(
            self, patched_user, tmp_path, monkeypatch):
        import sqlite3
        client, app = _logged_in_client()
        monkeypatch.chdir(tmp_path)
        # Build a profile DB with a recent SELL row for F (long-close)
        db_file = tmp_path / "quantopsai_profile_1.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT,
            side TEXT, qty REAL, price REAL, signal_type TEXT,
            status TEXT, occ_symbol TEXT
        )""")
        from datetime import datetime
        ts = datetime.utcnow().isoformat()
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "signal_type, status) VALUES (?, 'F', 'sell', 139, 11.915, "
            "'STRONG_SELL', 'closed')", (ts,))
        conn.commit()
        conn.close()
        # cycle_data shows AI proposed "Short" intent on F
        fake_cycle = {
            "profile_id": 1, "profile_name": "Small Cap",
            "timestamp": 1778608138.81,
            "ai_reasoning": "Short F based on SEC alert",
            "trades_selected": [{
                "symbol": "F",
                "action": "Short",  # already humanized at this point
                "size_pct": 1.25, "confidence": 62,
                "reasoning": "SEC alert + sector outflow",
            }],
            "shortlist": [],
        }
        (tmp_path / "cycle_data_1.json").write_text(json.dumps(fake_cycle))
        with patch("flask_login.utils._get_user",
                    return_value=patched_user[1]):
            resp = client.get("/api/cycle-data/1")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        trades = data.get("trades_selected") or []
        assert len(trades) == 1
        t = trades[0]
        assert t["symbol"] == "F"
        # Executed action stamped — Long Close, not Short Open
        assert t.get("executed_action") == "Long Close", t
        # The conversion is flagged so the brain ticker can badge it
        assert t.get("execution_outcome") == "converted_to_close"
        assert "already held long" in t.get(
            "execution_outcome_display", "")


# ---------------------------------------------------------------------------
# Broad sweep — every GET /api/* endpoint, raw-enum-field-aware
# ---------------------------------------------------------------------------

def _api_routes(app) -> List[str]:
    out = []
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if not path.startswith("/api/"):
            continue
        if "GET" not in (rule.methods or set()):
            continue
        for arg_name in (rule.arguments or set()):
            path = path.replace("<int:" + arg_name + ">", "1")
            path = path.replace("<" + arg_name + ">", "1")
        qs = "&".join(f"{k}={v}" for k, v in DEFAULT_QUERY_PARAMS.items())
        sep = "&" if "?" in path else "?"
        out.append(path + sep + qs)
    return sorted(out)


class TestAllApiEndpointsHumanized:
    """Hit every GET /api/* endpoint, walk the response, and fail if
    ALLCAPS_SNAKE_CASE appears in a non-raw-enum field. Bulk coverage
    so the next field added gets caught by the test, not the operator."""

    def test_no_allcaps_snake_in_text_fields(self, patched_user):
        client, app = _logged_in_client()
        with patch("flask_login.utils._get_user",
                    return_value=patched_user[1]):
            with patch("views.get_trading_profile",
                        return_value=patched_user[0]):
                with patch("views.get_user_profiles",
                            return_value=[patched_user[0]]):
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
                        for path, parent_key, value in _walk_strings(data):
                            if _is_raw_enum_field(parent_key):
                                continue
                            m = ALLCAPS_SNAKE_RE.search(value)
                            if m:
                                leaks.append((route, ".".join(path),
                                              parent_key, m.group(0),
                                              value[:80]))
                    if leaks:
                        details = "\n".join(
                            f"  {r}\n    path={p!r} key={k!r} "
                            f"token={t!r}\n    in: {v!r}"
                            for r, p, k, t, v in leaks[:8]
                        )
                        pytest.fail(
                            "ALLCAPS_SNAKE_CASE leaked through API "
                            "responses in non-raw-enum fields.\n\n"
                            "Fix one of:\n"
                            "  1. Apply humanize() server-side before "
                            "returning the field.\n"
                            "  2. If the field name is contractually a "
                            "raw enum (JS humanizes it), add the field "
                            "name to RAW_ENUM_FIELDS in this test.\n\n"
                            f"Leaks (first 8):\n{details}"
                        )
