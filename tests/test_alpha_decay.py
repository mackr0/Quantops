"""Tests for alpha_decay.py — Phase 3 of Quant Fund Evolution.

Covers:
  - Rolling/lifetime metric computation
  - Snapshot persistence
  - Decay detection (insufficient data, healthy, decayed)
  - Auto-deprecation and restoration
  - Pipeline filter for deprecated strategies
"""

import sqlite3
from datetime import datetime, timedelta

import pytest


def _make_prediction_db(tmp_path, rows, tables_from_journal=True):
    """Create a per-profile DB with ai_predictions populated for testing.

    `rows` is a list of (strategy_type, outcome, return_pct, days_ago).
    """
    db_path = str(tmp_path / "test_profile.db")
    if tables_from_journal:
        from journal import init_db
        init_db(db_path)
    else:
        # Minimal ai_predictions table for unit tests
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY,
                timestamp TEXT, symbol TEXT, predicted_signal TEXT,
                confidence REAL, reasoning TEXT,
                price_at_prediction REAL,
                status TEXT, actual_outcome TEXT, actual_return_pct REAL,
                resolved_at TEXT, strategy_type TEXT
            )
        """)
        conn.commit()
        conn.close()

    conn = sqlite3.connect(db_path)
    for i, (stype, outcome, ret, days_ago) in enumerate(rows):
        resolved_at = (datetime.now() - timedelta(days=days_ago)).isoformat()
        timestamp = (datetime.now() - timedelta(days=days_ago + 3)).isoformat()
        conn.execute(
            """INSERT INTO ai_predictions
               (timestamp, symbol, predicted_signal, confidence, reasoning,
                price_at_prediction, status, actual_outcome,
                actual_return_pct, resolved_at, strategy_type)
               VALUES (?, 'TEST', 'BUY', 50, 'test', 100.0,
                       'resolved', ?, ?, ?, ?)""",
            (timestamp, outcome, ret, resolved_at, stype),
        )
    conn.commit()
    conn.close()
    return db_path


class TestRollingMetrics:
    def test_empty_db_returns_zeros(self, tmp_path):
        from alpha_decay import compute_rolling_metrics
        db = _make_prediction_db(tmp_path, [])
        m = compute_rolling_metrics(db, "momentum", window_days=30)
        assert m["n_predictions"] == 0
        assert m["sharpe_ratio"] == 0.0

    def test_profitable_signals_positive_sharpe(self, tmp_path):
        from alpha_decay import compute_rolling_metrics
        rows = [("momentum", "win", 3.0, d) for d in range(20)]
        db = _make_prediction_db(tmp_path, rows)
        m = compute_rolling_metrics(db, "momentum", window_days=30)
        assert m["n_predictions"] == 20
        assert m["win_rate"] == 100.0
        # All same return means stdev=0 and Sharpe=0 (no variance to normalize)
        # Use mixed returns for meaningful Sharpe:
        rows2 = [("mix", "win", 2.0 + i * 0.1, d) for i, d in enumerate(range(20))]
        db2 = _make_prediction_db(tmp_path, rows2)
        m2 = compute_rolling_metrics(db2, "mix", window_days=30)
        assert m2["sharpe_ratio"] > 0

    def test_window_filters_old_predictions(self, tmp_path):
        from alpha_decay import compute_rolling_metrics, compute_lifetime_metrics
        rows = (
            [("s1", "win", 2.0, d) for d in range(5)]       # recent wins
            + [("s1", "loss", -3.0, d) for d in range(60, 70)]  # old losses
        )
        db = _make_prediction_db(tmp_path, rows)

        rolling = compute_rolling_metrics(db, "s1", window_days=30)
        lifetime = compute_lifetime_metrics(db, "s1")

        assert rolling["n_predictions"] == 5
        assert rolling["win_rate"] == 100.0
        assert lifetime["n_predictions"] == 15


class TestSnapshot:
    def test_snapshot_writes_row_per_strategy(self, tmp_path):
        from alpha_decay import snapshot_all_strategies
        rows = (
            [("momentum", "win", 2.0, d) for d in range(15)]
            + [("mean_rev", "loss", -1.5, d) for d in range(12)]
        )
        db = _make_prediction_db(tmp_path, rows)
        types = snapshot_all_strategies(db, window_days=30)
        assert set(types) == {"momentum", "mean_rev"}

        conn = sqlite3.connect(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM signal_performance_history"
        ).fetchone()[0]
        conn.close()
        assert count == 2

    def test_snapshot_idempotent_same_day(self, tmp_path):
        from alpha_decay import snapshot_all_strategies
        rows = [("s1", "win", 2.0, d) for d in range(15)]
        db = _make_prediction_db(tmp_path, rows)
        # Run twice on same day
        snapshot_all_strategies(db, window_days=30)
        snapshot_all_strategies(db, window_days=30)
        conn = sqlite3.connect(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM signal_performance_history"
        ).fetchone()[0]
        conn.close()
        assert count == 1  # UNIQUE constraint enforced


class TestDecayDetection:
    def test_insufficient_lifetime_data_returns_no_decay(self, tmp_path):
        from alpha_decay import detect_decay
        rows = [("s1", "win", 2.0, d) for d in range(10)]
        db = _make_prediction_db(tmp_path, rows)
        result = detect_decay(db, "s1")
        assert result["decay_detected"] is False
        assert "Insufficient" in result["reason"]

    def test_no_snapshots_yet(self, tmp_path):
        from alpha_decay import detect_decay
        # Seed 90 days of resolved predictions. Wave 3 / Fix #8
        # excludes the most-recent 30 days from the "lifetime"
        # baseline to keep rolling-vs-lifetime disjoint, so we need
        # ≥ 50 predictions OLDER than 30 days for the lifetime_min
        # gate to pass — meaning we reach the snapshot-checking path
        # this test is exercising. Use 90 days to be safe.
        rows = [("s1", "win", 2.0 + i * 0.05, d) for i, d in enumerate(range(90))]
        db = _make_prediction_db(tmp_path, rows)
        # No snapshots written yet
        result = detect_decay(db, "s1")
        assert result["decay_detected"] is False
        assert "snapshot" in result["reason"].lower()

    def test_healthy_strategy_not_flagged(self, tmp_path):
        from alpha_decay import detect_decay, snapshot_all_strategies

        # 100 resolved predictions with consistent positive Sharpe
        rows = [("s1", "win", 2.0 + i * 0.02, d) for i, d in enumerate(range(100))]
        db = _make_prediction_db(tmp_path, rows)
        snapshot_all_strategies(db, window_days=30)

        result = detect_decay(db, "s1")
        # Rolling is the same as lifetime in this synthetic data, so no decay
        assert result["decay_detected"] is False


class TestDeprecation:
    def test_deprecate_and_is_deprecated(self, tmp_path):
        from alpha_decay import deprecate_strategy, is_deprecated
        rows = [("s1", "win", 1.0, d) for d in range(5)]
        db = _make_prediction_db(tmp_path, rows)

        assert is_deprecated(db, "s1") is False
        deprecate_strategy(db, "s1", {"reason": "test decay",
                                       "current_rolling_sharpe": 0.2,
                                       "lifetime_sharpe": 1.0,
                                       "consecutive_bad_days": 30})
        assert is_deprecated(db, "s1") is True

    def test_restore_removes_deprecation(self, tmp_path):
        from alpha_decay import deprecate_strategy, restore_strategy, is_deprecated
        rows = [("s1", "win", 1.0, d) for d in range(5)]
        db = _make_prediction_db(tmp_path, rows)

        deprecate_strategy(db, "s1", {"reason": "test",
                                       "current_rolling_sharpe": 0.2,
                                       "lifetime_sharpe": 1.0,
                                       "consecutive_bad_days": 30})
        assert is_deprecated(db, "s1") is True
        restore_strategy(db, "s1")
        assert is_deprecated(db, "s1") is False

    def test_list_deprecated(self, tmp_path):
        from alpha_decay import deprecate_strategy, list_deprecated
        rows = [("s1", "win", 1.0, d) for d in range(5)]
        db = _make_prediction_db(tmp_path, rows)
        deprecate_strategy(db, "s1", {"reason": "x",
                                       "current_rolling_sharpe": 0.1,
                                       "lifetime_sharpe": 1.0,
                                       "consecutive_bad_days": 30})
        deprecate_strategy(db, "s2", {"reason": "y",
                                       "current_rolling_sharpe": 0.1,
                                       "lifetime_sharpe": 1.0,
                                       "consecutive_bad_days": 30})
        deprecated = list_deprecated(db)
        assert len(deprecated) == 2


class TestRunDecayCycle:
    def test_full_cycle_runs_without_error(self, tmp_path):
        from alpha_decay import run_decay_cycle
        rows = [("s1", "win", 2.0 + i * 0.02, d) for i, d in enumerate(range(60))]
        db = _make_prediction_db(tmp_path, rows)
        summary = run_decay_cycle(db)
        assert "strategies_snapshotted" in summary
        assert "newly_deprecated" in summary
        assert "restored" in summary
        assert "errors" in summary


class TestPipelineIntegration:
    def test_rank_candidates_skips_deprecated_strategies(self):
        from trade_pipeline import _rank_candidates
        signals = [
            {"symbol": "AAPL", "signal": "BUY", "score": 2,
             "votes": {"momentum": "BUY", "mean_rev": "HOLD"}},
            {"symbol": "MSFT", "signal": "BUY", "score": 2,
             "votes": {"mean_rev": "BUY", "momentum": "HOLD"}},
        ]
        # Deprecate the "momentum" strategy
        result = _rank_candidates(signals, held_symbols=set(), enable_shorts=False,
                                   deprecated_strategies={"momentum"})
        symbols = [s["symbol"] for s in result]
        assert "AAPL" not in symbols  # was filtered out (momentum was deprecated)
        assert "MSFT" in symbols      # mean_rev is still active

    def test_rank_candidates_no_deprecated_is_unchanged(self):
        from trade_pipeline import _rank_candidates
        signals = [
            {"symbol": "AAPL", "signal": "BUY", "score": 2,
             "votes": {"momentum": "BUY"}},
        ]
        # No deprecated set
        result = _rank_candidates(signals, held_symbols=set(), enable_shorts=False)
        assert len(result) == 1
