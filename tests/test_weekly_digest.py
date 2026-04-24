"""Tests for the weekly AI digest (ai_weekly_summary + multi_scheduler
_task_weekly_digest task).

Covers:
  - build_weekly_summary gracefully handles empty/missing tables
  - render_html produces a subject + HTML body and doesn't crash
  - Top/bottom trades pick the right trades and include AI reasoning
  - _task_weekly_digest skips on non-Fridays and before 17:00
  - Idempotency marker prevents double-send on the same day
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_master_db(tmp_path, monkeypatch):
    """Create a minimal master DB with profiles and tuning_history tables."""
    master = tmp_path / "master.db"
    c = sqlite3.connect(str(master))
    c.executescript(
        """
        CREATE TABLE trading_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            market_type TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE tuning_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            adjustment_type TEXT NOT NULL,
            parameter_name TEXT NOT NULL,
            old_value TEXT NOT NULL,
            new_value TEXT NOT NULL,
            reason TEXT NOT NULL,
            win_rate_at_change REAL,
            predictions_resolved INTEGER,
            outcome_after TEXT DEFAULT 'pending',
            win_rate_after REAL,
            reviewed_at TEXT
        );
        """
    )
    c.executescript(
        """
        INSERT INTO trading_profiles(id, user_id, name, market_type, enabled)
        VALUES (1, 1, 'Mid Cap', 'midcap', 1),
               (2, 1, 'Small Cap', 'small', 1),
               (3, 1, 'Disabled', 'largecap', 0);
        """
    )
    c.commit()
    c.close()
    monkeypatch.chdir(tmp_path)
    return str(master)


def _make_profile_db(tmp_path, pid, trades=None, predictions=None,
                      cost_rows=None, deprecated=None):
    """Build a profile DB at the expected path with supplied fixture data."""
    db = tmp_path / f"quantopsai_profile_{pid}.db"
    c = sqlite3.connect(str(db))
    c.executescript(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            strategy TEXT,
            reason TEXT,
            ai_reasoning TEXT,
            ai_confidence REAL,
            stop_loss REAL,
            take_profit REAL,
            pnl REAL,
            status TEXT
        );
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            predicted_signal TEXT NOT NULL,
            confidence INTEGER,
            reasoning TEXT,
            price_at_prediction REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            actual_outcome TEXT,
            resolved_at TEXT
        );
        CREATE TABLE ai_cost_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            purpose TEXT,
            estimated_cost_usd REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE deprecated_strategies (
            strategy_type TEXT PRIMARY KEY,
            deprecated_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            rolling_sharpe_at_deprecation REAL,
            lifetime_sharpe REAL,
            consecutive_bad_days INTEGER,
            restored_at TEXT
        );
        """
    )
    for t in (trades or []):
        c.execute(
            "INSERT INTO trades(timestamp,symbol,side,qty,price,signal_type,"
            "ai_reasoning,ai_confidence,pnl,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            t,
        )
    for p in (predictions or []):
        c.execute(
            "INSERT INTO ai_predictions(timestamp,symbol,predicted_signal,"
            "confidence,price_at_prediction,actual_outcome,resolved_at) "
            "VALUES (?,?,?,?,?,?,?)",
            p,
        )
    for row in (cost_rows or []):
        c.execute(
            "INSERT INTO ai_cost_ledger(timestamp,provider,model,"
            "input_tokens,output_tokens,purpose,estimated_cost_usd) "
            "VALUES (?,?,?,?,?,?,?)",
            row,
        )
    for d in (deprecated or []):
        c.execute(
            "INSERT INTO deprecated_strategies("
            "strategy_type,deprecated_at,reason,rolling_sharpe_at_deprecation,"
            "lifetime_sharpe,consecutive_bad_days) VALUES (?,?,?,?,?,?)",
            d,
        )
    c.commit()
    c.close()
    return str(db)


# ---------------------------------------------------------------------------
# build_weekly_summary
# ---------------------------------------------------------------------------

class TestBuildSummary:
    def test_empty_profile_dbs_dont_crash(self, fresh_master_db, tmp_path):
        # No per-profile DBs exist at all
        from ai_weekly_summary import build_weekly_summary
        summary = build_weekly_summary(master_db_path=fresh_master_db)
        # 2 enabled profiles; disabled excluded
        assert len(summary["profiles"]) == 2
        assert summary["totals"]["realized_pnl"] == 0.0
        assert summary["totals"]["buys"] == 0
        assert summary["totals"]["tuning_changes"] == 0

    def test_aggregates_trades_and_pnl(self, fresh_master_db, tmp_path):
        now = datetime.utcnow()
        three_days_ago = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        _make_profile_db(tmp_path, 1, trades=[
            (three_days_ago, "AAPL", "buy", 10, 180, "momentum", "strong breakout", 75, None, "open"),
            (three_days_ago, "AAPL", "sell", 10, 185, "trailing_stop", "target hit", 75, 50.0, "closed"),
            (three_days_ago, "MSFT", "sell", 5, 420, "stop_loss", "reversed", 60, -30.0, "closed"),
        ])
        _make_profile_db(tmp_path, 2)  # empty

        from ai_weekly_summary import build_weekly_summary
        summary = build_weekly_summary(master_db_path=fresh_master_db)
        p1 = next(p for p in summary["profiles"] if p["profile_id"] == 1)
        assert p1["buys"] == 1
        assert p1["sells"] == 2
        # pnl only counts sell rows (20 net = 50 - 30)
        assert p1["realized_pnl"] == 20.0
        assert summary["totals"]["realized_pnl"] == 20.0

    def test_pulls_tuning_history_from_master(self, fresh_master_db, tmp_path):
        now = datetime.utcnow()
        two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        c = sqlite3.connect(fresh_master_db)
        c.execute(
            "INSERT INTO tuning_history(profile_id,user_id,timestamp,"
            "adjustment_type,parameter_name,old_value,new_value,reason,"
            "outcome_after,win_rate_after) "
            "VALUES (1,1,?,?,?,?,?,?,?,?)",
            (two_days_ago, "confidence_threshold", "ai_confidence_threshold",
             "25", "60", "win rate at <60% conf was 28% (7/25)",
             "improved", 0.55),
        )
        c.commit()
        c.close()
        _make_profile_db(tmp_path, 1)
        _make_profile_db(tmp_path, 2)

        from ai_weekly_summary import build_weekly_summary
        summary = build_weekly_summary(master_db_path=fresh_master_db)
        p1 = next(p for p in summary["profiles"] if p["profile_id"] == 1)
        assert len(p1["tuning_changes"]) == 1
        change = p1["tuning_changes"][0]
        assert change["old_value"] == "25"
        assert change["new_value"] == "60"
        assert change["outcome_after"] == "improved"

    def test_ai_cost_rollup(self, fresh_master_db, tmp_path):
        now = datetime.utcnow()
        ts = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        _make_profile_db(tmp_path, 1, cost_rows=[
            (ts, "anthropic", "haiku", 1000, 500, "batch_select", 0.01),
            (ts, "anthropic", "haiku", 2000, 300, "ensemble:risk_assessor", 0.005),
        ])
        _make_profile_db(tmp_path, 2)

        from ai_weekly_summary import build_weekly_summary
        summary = build_weekly_summary(master_db_path=fresh_master_db)
        p1 = next(p for p in summary["profiles"] if p["profile_id"] == 1)
        assert p1["ai_cost"] == pytest.approx(0.015)
        assert p1["cost_by_purpose"]["batch_select"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------

class TestRender:
    def test_empty_summary_renders(self, fresh_master_db, tmp_path):
        from ai_weekly_summary import build_weekly_summary, render_html
        summary = build_weekly_summary(master_db_path=fresh_master_db)
        subject, html = render_html(summary)
        assert "QuantOpsAI Weekly Digest" in subject
        assert "<html>" in html
        assert "No self-tuning changes" in html

    def test_subject_contains_date_range(self, fresh_master_db):
        from ai_weekly_summary import build_weekly_summary, render_html
        summary = build_weekly_summary(master_db_path=fresh_master_db)
        subject, _ = render_html(summary)
        # Week label formatting
        assert "→" in subject

    def test_trading_narrative_includes_reasoning(self, fresh_master_db,
                                                    tmp_path):
        now = datetime.utcnow()
        two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        _make_profile_db(tmp_path, 1, trades=[
            (two_days_ago, "NVDA", "sell", 10, 900, "trailing_stop",
             "Strong momentum + insider buying set up", 85, 250.0, "closed"),
        ])
        _make_profile_db(tmp_path, 2)

        from ai_weekly_summary import build_weekly_summary, render_html
        summary = build_weekly_summary(master_db_path=fresh_master_db)
        _, html = render_html(summary)
        assert "NVDA" in html
        assert "Strong momentum" in html
        assert "TOP 5 WINNERS" in html

    def test_losers_section_appears(self, fresh_master_db, tmp_path):
        now = datetime.utcnow()
        ts = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        _make_profile_db(tmp_path, 1, trades=[
            (ts, "BADCO", "sell", 10, 50, "stop_loss",
             "Stopped out on reversal despite bullish setup", 70, -500.0, "closed"),
        ])
        _make_profile_db(tmp_path, 2)

        from ai_weekly_summary import build_weekly_summary, render_html
        summary = build_weekly_summary(master_db_path=fresh_master_db)
        _, html = render_html(summary)
        assert "BOTTOM 3 LOSERS" in html
        assert "BADCO" in html


# ---------------------------------------------------------------------------
# _task_weekly_digest behavior (day/time gating + idempotency)
# ---------------------------------------------------------------------------

class TestTaskGating:
    def _mock_datetime(self, monkeypatch, year, month, day, hour):
        """Patch datetime.now() in multi_scheduler to return a fixed moment."""
        fixed = datetime(year, month, day, hour, 0)
        import multi_scheduler

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed
        monkeypatch.setattr(multi_scheduler, "datetime", FakeDT, raising=False)
        return fixed

    def test_skips_on_non_friday(self, fresh_master_db, monkeypatch, tmp_path):
        from datetime import datetime as real_dt
        # Wednesday at 18:00
        wed = real_dt(2026, 4, 22, 18, 0)

        import multi_scheduler
        import ai_weekly_summary

        sent = {"n": 0}
        def fake_send(*a, **kw):
            sent["n"] += 1
            return True
        monkeypatch.setattr("notifications.send_email", fake_send)

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return wed.replace(tzinfo=tz) if tz else wed

        with patch("multi_scheduler.datetime", FakeDT):
            multi_scheduler._task_weekly_digest(fresh_master_db)
        assert sent["n"] == 0, "Weekly digest must not fire on a Wednesday"

    def test_skips_before_1600(self, fresh_master_db, monkeypatch, tmp_path):
        from datetime import datetime as real_dt
        # Friday at 15:30 — before market close (16:00 ET), digest waits
        fri_early = real_dt(2026, 4, 24, 15, 30)

        import multi_scheduler
        sent = {"n": 0}
        monkeypatch.setattr(
            "notifications.send_email",
            lambda *a, **kw: (sent.update({"n": sent["n"] + 1}), True)[1],
        )

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return fri_early.replace(tzinfo=tz) if tz else fri_early

        with patch("multi_scheduler.datetime", FakeDT):
            multi_scheduler._task_weekly_digest(fresh_master_db)
        assert sent["n"] == 0, "Weekly digest must not fire before 17:00"

    def test_fires_on_friday_at_or_after_1600(self, fresh_master_db,
                                                monkeypatch, tmp_path):
        from datetime import datetime as real_dt
        # Friday at 16:03 ET — right after market close, snapshot block fires
        fri_evening = real_dt(2026, 4, 24, 16, 3)

        import multi_scheduler
        sent_subjects = []
        def fake_send(subject, html, ctx=None):
            sent_subjects.append(subject)
            return True
        monkeypatch.setattr("notifications.send_email", fake_send)

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return fri_evening.replace(tzinfo=tz) if tz else fri_evening

        with patch("multi_scheduler.datetime", FakeDT):
            multi_scheduler._task_weekly_digest(fresh_master_db)
        assert len(sent_subjects) == 1
        assert "Weekly Digest" in sent_subjects[0]

    def test_idempotency_marker_prevents_double_send(self, fresh_master_db,
                                                      monkeypatch, tmp_path):
        """Simulate 10 profiles hitting the task sequentially — only one email."""
        from datetime import datetime as real_dt
        fri_evening = real_dt(2026, 4, 24, 16, 5)

        import multi_scheduler
        sent = {"n": 0}
        def fake_send(subject, html, ctx=None):
            sent["n"] += 1
            return True
        monkeypatch.setattr("notifications.send_email", fake_send)

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return fri_evening.replace(tzinfo=tz) if tz else fri_evening

        with patch("multi_scheduler.datetime", FakeDT):
            for _ in range(10):
                multi_scheduler._task_weekly_digest(fresh_master_db)
        assert sent["n"] == 1, (
            f"Idempotency broken: task fired {sent['n']} times, expected 1"
        )

    def test_no_marker_retry_on_send_failure(self, fresh_master_db,
                                              monkeypatch, tmp_path):
        """If the send fails, the marker should NOT be written — next cycle
        retries instead of silently skipping."""
        from datetime import datetime as real_dt
        fri_evening = real_dt(2026, 4, 24, 16, 5)

        import multi_scheduler
        attempts = {"n": 0}
        def fake_send(subject, html, ctx=None):
            attempts["n"] += 1
            return False  # simulate send failure
        monkeypatch.setattr("notifications.send_email", fake_send)

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return fri_evening.replace(tzinfo=tz) if tz else fri_evening

        with patch("multi_scheduler.datetime", FakeDT):
            multi_scheduler._task_weekly_digest(fresh_master_db)
            multi_scheduler._task_weekly_digest(fresh_master_db)
            multi_scheduler._task_weekly_digest(fresh_master_db)
        assert attempts["n"] == 3, (
            "Failed sends must trigger retries, not be suppressed by marker"
        )
