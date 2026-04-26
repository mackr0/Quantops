"""Tests for losing-week post-mortem and false-negative tuner rule."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────
# post_mortem.analyze_recent_week
# ─────────────────────────────────────────────────────────────────────

def _make_pred_db(tmp_path):
    db = str(tmp_path / "pm.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL,
            features_json TEXT, resolved_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _seed_baseline(db, n_total, n_wins, days_ago=60):
    conn = sqlite3.connect(db)
    for i in range(n_wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, resolved_at) "
            f"VALUES (?, 'BUY', 70, 100, 'resolved', 'win', "
            f" datetime('now', '-{days_ago} days'))",
            (f"BW{i}",))
    for i in range(n_total - n_wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, resolved_at) "
            f"VALUES (?, 'BUY', 70, 100, 'resolved', 'loss', "
            f" datetime('now', '-{days_ago} days'))",
            (f"BL{i}",))
    conn.commit()
    conn.close()


def _seed_recent_losses(db, n, dominant_feature, dominant_value):
    """Insert n loss predictions in last 7 days, all sharing one feature/value."""
    conn = sqlite3.connect(db)
    for i in range(n):
        feats = {dominant_feature: dominant_value, "noise": i}
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, features_json, resolved_at) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss', ?, "
            " datetime('now', '-2 days'))",
            (f"L{i}", json.dumps(feats)))
    conn.commit()
    conn.close()


class TestAnalyzeRecentWeek:
    def test_no_op_when_baseline_too_thin(self, tmp_path):
        from post_mortem import analyze_recent_week
        db = _make_pred_db(tmp_path)
        _seed_baseline(db, 10, 5)  # below 30-row baseline minimum
        result = analyze_recent_week(db)
        assert result is None

    def test_no_op_when_recent_week_healthy(self, tmp_path):
        from post_mortem import analyze_recent_week
        db = _make_pred_db(tmp_path)
        _seed_baseline(db, 100, 50)
        # Recent week with same WR as baseline
        conn = sqlite3.connect(db)
        for i in range(10):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome, resolved_at) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'win', "
                " datetime('now', '-2 days'))",
                (f"R{i}",))
        for i in range(10):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome, resolved_at) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss', "
                " datetime('now', '-2 days'))",
                (f"R{i}L",))
        conn.commit()
        conn.close()
        result = analyze_recent_week(db)
        assert result is None  # 50% WR matches baseline

    def test_pattern_extracted_when_bad_week_with_dominant_feature(self, tmp_path):
        from post_mortem import analyze_recent_week
        db = _make_pred_db(tmp_path)
        # Baseline 50% WR
        _seed_baseline(db, 100, 50)
        # Recent week: 10 losses, all with insider_cluster=1
        _seed_recent_losses(db, 10, "insider_cluster", 1)
        result = analyze_recent_week(db)
        assert result is not None
        assert result["losing_trade_count"] == 10
        # The dominant feature should be detected
        dominant = result["dominant_features"]
        assert any(d["feature"] == "insider_cluster" for d in dominant)
        # Pattern stored in DB
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT pattern_text, still_active FROM learned_patterns"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][1] == 1  # still_active

    def test_subsequent_run_marks_prior_inactive(self, tmp_path):
        from post_mortem import analyze_recent_week
        db = _make_pred_db(tmp_path)
        _seed_baseline(db, 100, 50)
        _seed_recent_losses(db, 10, "insider_cluster", 1)
        analyze_recent_week(db)
        # Add MORE losses with a different dominant feature
        _seed_recent_losses(db, 10, "options_signal", "bearish")
        analyze_recent_week(db)
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT still_active FROM learned_patterns ORDER BY id"
        ).fetchall()
        conn.close()
        assert rows[0][0] == 0  # First inactive
        assert rows[1][0] == 1  # Second active


class TestGetActivePatterns:
    def test_returns_active_patterns(self, tmp_path):
        from post_mortem import analyze_recent_week, get_active_patterns
        db = _make_pred_db(tmp_path)
        _seed_baseline(db, 100, 50)
        _seed_recent_losses(db, 10, "insider_cluster", 1)
        analyze_recent_week(db)
        patterns = get_active_patterns(db)
        assert len(patterns) == 1
        assert "insider_cluster" in patterns[0].lower() or "Insider" in patterns[0]

    def test_returns_empty_when_no_patterns(self, tmp_path):
        from post_mortem import get_active_patterns
        db = _make_pred_db(tmp_path)
        # No predictions, no patterns
        patterns = get_active_patterns(db)
        assert patterns == []


# ─────────────────────────────────────────────────────────────────────
# False-negative tuner rule
# ─────────────────────────────────────────────────────────────────────

def _seed_hold_losses(db, n, confidence_value):
    """Insert n HOLD-loss predictions with given confidence."""
    conn = sqlite3.connect(db)
    for i in range(n):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, resolved_at) "
            "VALUES (?, 'HOLD', ?, 100, 'resolved', 'loss', "
            " datetime('now', '-3 days'))",
            (f"H{i}", confidence_value))
    conn.commit()
    conn.close()


class TestFalseNegatives:
    def test_lowers_threshold_when_marginal_misses_dominate(self, tmp_path):
        from self_tuning import _optimize_false_negatives, _get_conn
        db = _make_pred_db(tmp_path)
        # 12 HOLD-losses at confidence 22 (within band 15-25 just below threshold 25)
        _seed_hold_losses(db, 12, 22)
        # 3 HOLD-losses way below threshold (background noise)
        _seed_hold_losses(db, 3, 5)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            ai_confidence_threshold=25,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_false_negatives(
                            conn, ctx, 1, 1,
                            overall_wr=50.0, resolved=30)
                        mock_up.assert_called_with(1, ai_confidence_threshold=20)
        conn.close()
        assert msg is not None
        assert "20" in msg

    def test_no_op_when_threshold_at_floor(self, tmp_path):
        from self_tuning import _optimize_false_negatives, _get_conn
        db = _make_pred_db(tmp_path)
        _seed_hold_losses(db, 12, 8)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            ai_confidence_threshold=10,  # at floor
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_false_negatives(
                    conn, ctx, 1, 1,
                    overall_wr=50.0, resolved=30)
        conn.close()
        assert msg is None

    def test_no_op_when_misses_dont_cluster_marginally(self, tmp_path):
        from self_tuning import _optimize_false_negatives, _get_conn
        db = _make_pred_db(tmp_path)
        # All HOLD-losses at low confidence, none in marginal band
        _seed_hold_losses(db, 15, 5)
        ctx = SimpleNamespace(
            profile_id=1, user_id=1, db_path=db,
            ai_confidence_threshold=25,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_false_negatives(
                    conn, ctx, 1, 1,
                    overall_wr=50.0, resolved=30)
        conn.close()
        assert msg is None
