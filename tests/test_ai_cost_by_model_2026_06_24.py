"""Dashboard cost-per-LLM breakdown (2026-06-24).

The operator could see only one book-wide "AI Cost Total" and had no way to
tell whether the spend was the cheap Gemini primary or an expensive Claude
fallback. The dashboard now shows cost broken down per model (today), summed
across all profiles.

Pins:
  1. by_model_today() groups a profile's ledger by (provider, model) for today.
  2. merge_model_breakdowns() sums per-profile breakdowns book-wide.
  3. /api/dashboard-totals returns a `cost_by_model` list (happy path).
  4. The dashboard template renders the breakdown rows.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_LEDGER_DDL = """
CREATE TABLE ai_cost_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    purpose TEXT,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    call_id TEXT
);
"""


def _make_ledger_db(path, rows):
    """rows: list of (provider, model, usd, ts) — ts None => today (ET)."""
    et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    conn = sqlite3.connect(path)
    conn.executescript(_LEDGER_DDL)
    for provider, model, usd, ts in rows:
        conn.execute(
            "INSERT INTO ai_cost_ledger (timestamp, provider, model, "
            "estimated_cost_usd) VALUES (?, ?, ?, ?)",
            (ts or (et_today + " 12:00:00"), provider, model, usd),
        )
    conn.commit()
    conn.close()


class TestByModelToday:
    def test_groups_by_provider_model(self, tmp_path):
        from ai_cost_ledger import by_model_today
        db = str(tmp_path / "p1.db")
        _make_ledger_db(db, [
            ("google", "gemini-2.5-flash-lite", 0.01, None),
            ("google", "gemini-2.5-flash-lite", 0.02, None),
            ("anthropic", "claude-haiku-4-5", 0.50, None),
        ])
        out = by_model_today(db)
        by_model = {r["model"]: r for r in out}
        assert by_model["claude-haiku-4-5"]["usd"] == pytest.approx(0.50)
        assert by_model["claude-haiku-4-5"]["calls"] == 1
        assert by_model["gemini-2.5-flash-lite"]["usd"] == pytest.approx(0.03)
        assert by_model["gemini-2.5-flash-lite"]["calls"] == 2
        # sorted by usd desc — Haiku first
        assert out[0]["model"] == "claude-haiku-4-5"

    def test_excludes_other_days(self, tmp_path):
        from ai_cost_ledger import by_model_today
        db = str(tmp_path / "p2.db")
        _make_ledger_db(db, [
            ("google", "gemini-2.5-flash", 0.05, None),
            ("google", "gemini-2.5-flash", 9.99, "2020-01-01 12:00:00"),
        ])
        out = by_model_today(db)
        assert len(out) == 1
        assert out[0]["usd"] == pytest.approx(0.05)

    def test_safe_on_missing_table(self, tmp_path):
        from ai_cost_ledger import by_model_today
        db = str(tmp_path / "empty.db")
        sqlite3.connect(db).close()  # no ai_cost_ledger table
        assert by_model_today(db) == []

    def test_safe_on_missing_db(self, tmp_path):
        from ai_cost_ledger import by_model_today
        assert by_model_today(str(tmp_path / "nope.db")) == []


class TestMergeModelBreakdowns:
    def test_sums_across_profiles(self):
        from ai_cost_ledger import merge_model_breakdowns
        merged = merge_model_breakdowns([
            [{"provider": "google", "model": "gemini-2.5-flash-lite",
              "calls": 2, "usd": 0.03}],
            [{"provider": "google", "model": "gemini-2.5-flash-lite",
              "calls": 1, "usd": 0.01},
             {"provider": "anthropic", "model": "claude-haiku-4-5",
              "calls": 5, "usd": 1.10}],
        ])
        by_model = {r["model"]: r for r in merged}
        assert by_model["gemini-2.5-flash-lite"]["calls"] == 3
        assert by_model["gemini-2.5-flash-lite"]["usd"] == pytest.approx(0.04)
        assert by_model["claude-haiku-4-5"]["usd"] == pytest.approx(1.10)
        # sorted by usd desc — Haiku dominates
        assert merged[0]["model"] == "claude-haiku-4-5"

    def test_empty(self):
        from ai_cost_ledger import merge_model_breakdowns
        assert merge_model_breakdowns([]) == []
        assert merge_model_breakdowns([[], []]) == []


class TestDashboardTotalsEndpoint:
    @pytest.fixture
    def logged_in_client(self, tmp_main_db):
        import config
        config.DB_PATH = tmp_main_db
        from app import create_app
        from models import create_user
        create_user("cost@test.com", "password123", "Cost", is_admin=True)
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.test_client() as client:
            client.post("/login", data={"email": "cost@test.com",
                                        "password": "password123"},
                        follow_redirects=True)
            yield client

    def test_endpoint_includes_cost_by_model(self, logged_in_client):
        resp = logged_in_client.get("/api/dashboard-totals")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "cost_by_model" in data
        assert isinstance(data["cost_by_model"], list)


def test_dashboard_template_renders_breakdown():
    """Static pin: the breakdown row + live-update hook are present."""
    tpl = open(os.path.join(REPO, "templates", "dashboard.html")).read()
    assert 'id="ai-cost-by-model"' in tpl
    assert "by model (today)" in tpl
    assert "cost_by_model" in tpl  # both the Jinja loop and the JS updater
