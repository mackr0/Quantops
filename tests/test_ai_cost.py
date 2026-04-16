"""Tests for ai_pricing + ai_cost_ledger.

Covers: pricing math, unknown-model fallback, ledger writes, and
1d/7d/30d aggregation in spend_summary.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

class TestPricing:
    def test_known_model_costs_correctly(self):
        from ai_pricing import estimate_cost_usd
        # Haiku: $1/M input, $5/M output
        # 100k input + 50k output → 0.10 + 0.25 = $0.35
        cost = estimate_cost_usd("claude-haiku-4-5-20251001", 100_000, 50_000)
        assert cost == pytest.approx(0.35, abs=1e-6)

    def test_zero_tokens_returns_zero(self):
        from ai_pricing import estimate_cost_usd
        assert estimate_cost_usd("claude-haiku-4-5-20251001", 0, 0) == 0.0

    def test_unknown_model_falls_back(self):
        from ai_pricing import estimate_cost_usd, FALLBACK_PRICING
        cost = estimate_cost_usd("gpt-99-nonexistent", 1_000_000, 1_000_000)
        expected = FALLBACK_PRICING["input"] + FALLBACK_PRICING["output"]
        assert cost == pytest.approx(expected, abs=1e-6)

    def test_none_model_falls_back(self):
        from ai_pricing import estimate_cost_usd
        # Should not crash on None — just use fallback
        cost = estimate_cost_usd(None, 1000, 1000)
        assert cost > 0

    def test_negative_tokens_clamped_to_zero(self):
        from ai_pricing import estimate_cost_usd
        # Defensive: negative values shouldn't produce negative cost
        assert estimate_cost_usd("claude-haiku-4-5", -100, -100) == 0.0


# ---------------------------------------------------------------------------
# Ledger writes
# ---------------------------------------------------------------------------

class TestLogAiCall:
    def test_writes_row_with_cost(self, tmp_profile_db):
        from ai_cost_ledger import log_ai_call
        log_ai_call(tmp_profile_db, "anthropic", "claude-haiku-4-5-20251001",
                    input_tokens=10_000, output_tokens=2_000,
                    purpose="ensemble:earnings_analyst")

        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT provider, model, input_tokens, output_tokens, "
            "       purpose, estimated_cost_usd "
            "FROM ai_cost_ledger ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row[0] == "anthropic"
        assert row[1] == "claude-haiku-4-5-20251001"
        assert row[2] == 10_000
        assert row[3] == 2_000
        assert row[4] == "ensemble:earnings_analyst"
        # 10k * 1/1M + 2k * 5/1M = 0.01 + 0.01 = 0.02
        assert row[5] == pytest.approx(0.02, abs=1e-6)

    def test_silent_on_missing_db_path(self):
        """No db_path → log silently no-ops; never raises."""
        from ai_cost_ledger import log_ai_call
        log_ai_call(None, "anthropic", "model", 100, 100, "test")
        log_ai_call("", "anthropic", "model", 100, 100, "test")

    def test_silent_on_bad_db(self, tmp_path):
        """Pointing at a file without the table must not crash."""
        from ai_cost_ledger import log_ai_call
        empty_db = str(tmp_path / "empty.db")
        # Create empty db without the table
        conn = sqlite3.connect(empty_db)
        conn.close()
        # Should not raise
        log_ai_call(empty_db, "anthropic", "model", 100, 100, "test")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestSpendSummary:
    def _insert(self, db_path, *, days_ago, cost, purpose="test", model="m"):
        conn = sqlite3.connect(db_path)
        ts = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO ai_cost_ledger
                 (timestamp, provider, model, input_tokens, output_tokens,
                  purpose, estimated_cost_usd)
               VALUES (?, 'anthropic', ?, 100, 50, ?, ?)""",
            (ts, model, purpose, cost),
        )
        conn.commit()
        conn.close()

    def test_empty_ledger_returns_zeros(self, tmp_profile_db):
        from ai_cost_ledger import spend_summary
        summary = spend_summary(tmp_profile_db)
        assert summary["today"]["calls"] == 0
        assert summary["today"]["usd"] == 0.0
        assert summary["7d"]["usd"] == 0.0
        assert summary["30d"]["usd"] == 0.0

    def test_window_aggregation(self, tmp_profile_db):
        from ai_cost_ledger import spend_summary
        # Insert calls at varying ages
        self._insert(tmp_profile_db, days_ago=0,  cost=0.10)  # today
        self._insert(tmp_profile_db, days_ago=3,  cost=0.20)  # in 7d
        self._insert(tmp_profile_db, days_ago=20, cost=0.30)  # in 30d only
        self._insert(tmp_profile_db, days_ago=45, cost=0.99)  # outside window

        s = spend_summary(tmp_profile_db)
        assert s["today"]["usd"] == pytest.approx(0.10, abs=1e-6)
        assert s["today"]["calls"] == 1
        assert s["7d"]["usd"]    == pytest.approx(0.30, abs=1e-6)  # today + 3d
        assert s["7d"]["calls"]  == 2
        assert s["30d"]["usd"]   == pytest.approx(0.60, abs=1e-6)  # all 3 within 30d
        assert s["30d"]["calls"] == 3

    def test_breakdown_by_purpose(self, tmp_profile_db):
        from ai_cost_ledger import spend_summary
        self._insert(tmp_profile_db, days_ago=1, cost=0.50,
                     purpose="ensemble:risk_assessor")
        self._insert(tmp_profile_db, days_ago=1, cost=0.30,
                     purpose="ensemble:risk_assessor")
        self._insert(tmp_profile_db, days_ago=1, cost=0.40,
                     purpose="batch_select")

        s = spend_summary(tmp_profile_db)
        purposes = {r["purpose"]: r for r in s["by_purpose_30d"]}
        assert purposes["ensemble:risk_assessor"]["usd"] == pytest.approx(0.80, abs=1e-6)
        assert purposes["ensemble:risk_assessor"]["calls"] == 2
        assert purposes["batch_select"]["usd"] == pytest.approx(0.40, abs=1e-6)

    def test_breakdown_by_model(self, tmp_profile_db):
        from ai_cost_ledger import spend_summary
        self._insert(tmp_profile_db, days_ago=1, cost=0.10, model="haiku")
        self._insert(tmp_profile_db, days_ago=1, cost=0.50, model="opus")
        self._insert(tmp_profile_db, days_ago=1, cost=0.05, model="haiku")

        s = spend_summary(tmp_profile_db)
        models = {r["model"]: r for r in s["by_model_30d"]}
        assert models["haiku"]["usd"] == pytest.approx(0.15, abs=1e-6)
        assert models["opus"]["usd"]  == pytest.approx(0.50, abs=1e-6)

    def test_handles_missing_table_gracefully(self, tmp_path):
        """Old profile DB without the table → return zeros, don't crash."""
        from ai_cost_ledger import spend_summary
        empty_db = str(tmp_path / "old.db")
        conn = sqlite3.connect(empty_db)
        conn.close()
        s = spend_summary(empty_db)
        assert s["today"]["usd"] == 0.0
        assert s["by_purpose_30d"] == []
