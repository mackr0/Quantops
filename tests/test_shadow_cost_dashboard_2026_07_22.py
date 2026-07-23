"""2026-07-22 — shadow-eval spend on the dashboard.

The operator enabled Shadow Model Evaluation and reasonably expected
to SEE its cost next to the AI Cost section — it was only reachable
via the daily digest email. Shadow spend stays in its own ledger
(ai_shadow_calls — it must never inflate the trading-cost numbers)
but now renders as its own live line under the by-model breakdown.

Pins:
  1. ai_cost_ledger.shadow_by_model_today reads ai_shadow_calls with
     the same row shape as by_model_today (mergeable), [] on missing
     table.
  2. /api/dashboard-totals payload carries shadow_cost_by_model.
  3. The dashboard template renders the ai-cost-shadow line and the
     poll JS refreshes it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestShadowByModelToday:

    def test_reads_shadow_ledger(self, tmp_path):
        from journal import init_db
        from ai_cost_ledger import shadow_by_model_today
        db = str(tmp_path / "p.db")
        init_db(db)
        with closing(sqlite3.connect(db)) as c:
            c.execute(
                "INSERT INTO ai_shadow_calls (call_id, provider, model, "
                " cost_usd) VALUES ('c1', 'anthropic', "
                " 'claude-haiku-4-5', 0.011)")
            c.execute(
                "INSERT INTO ai_shadow_calls (call_id, provider, model, "
                " cost_usd) VALUES ('c2', 'anthropic', "
                " 'claude-haiku-4-5', 0.009)")
            c.execute(
                "INSERT INTO ai_shadow_calls (call_id, provider, model, "
                " cost_usd) VALUES ('c3', 'openai', 'gpt-4o-mini', "
                " 0.005)")
            c.commit()
        out = shadow_by_model_today(db)
        by_model = {r["model"]: r for r in out}
        assert by_model["claude-haiku-4-5"]["calls"] == 2
        assert abs(by_model["claude-haiku-4-5"]["usd"] - 0.02) < 1e-9
        assert by_model["gpt-4o-mini"]["calls"] == 1

    def test_missing_table_returns_empty(self, tmp_path):
        from ai_cost_ledger import shadow_by_model_today
        db = str(tmp_path / "empty.db")
        sqlite3.connect(db).close()
        assert shadow_by_model_today(db) == []

    def test_mergeable_with_operational_breakdowns(self, tmp_path):
        from ai_cost_ledger import merge_model_breakdowns
        merged = merge_model_breakdowns([
            [{"provider": "openai", "model": "gpt-4o-mini",
              "calls": 1, "usd": 0.005}],
            [{"provider": "openai", "model": "gpt-4o-mini",
              "calls": 2, "usd": 0.010}],
        ])
        assert merged[0]["calls"] == 3
        assert abs(merged[0]["usd"] - 0.015) < 1e-9


class TestDashboardWiring:

    def test_endpoint_payload_carries_shadow_breakdown(self):
        src = open(os.path.join(REPO, "views.py")).read()
        idx = src.index('"cost_by_model": merge_model_breakdowns(model_breakdowns)')
        assert "shadow_cost_by_model" in src[idx:idx + 500], (
            "/api/dashboard-totals must ship the shadow breakdown for "
            "the live refresh")
        assert "shadow_breakdowns.append(" in src

    def test_template_renders_and_refreshes_shadow_line(self):
        tpl = open(os.path.join(REPO, "templates",
                                "dashboard.html")).read()
        assert 'id="ai-cost-shadow"' in tpl
        assert "shadow eval (today):" in tpl
        assert "d.shadow_cost_by_model" in tpl, (
            "the 30s poll must refresh the shadow line, not just the "
            "initial render")
