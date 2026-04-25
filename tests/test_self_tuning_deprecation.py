"""Tests for the tuner -> alpha_decay auto-deprecation pipeline.

When the self-tuner finds a modular strategy (one with no profile-level
toggle, e.g., insider_cluster) underperforming, it should now ACT — not
emit a 'Recommendation:' string and call it a day. The action goes
through alpha_decay's deprecation pipeline, which already provides
auto-restoration when rolling Sharpe recovers.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_db(tmp_path):
    """Test DB with ai_predictions, deprecated_strategies, and trades."""
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL,
            regime_at_prediction TEXT, strategy_type TEXT,
            features_json TEXT, resolved_at TEXT, resolution_price REAL,
            days_held INTEGER
        );
        CREATE TABLE deprecated_strategies (
            strategy_type TEXT PRIMARY KEY,
            deprecated_at TEXT,
            reason TEXT,
            rolling_sharpe_at_deprecation REAL,
            lifetime_sharpe REAL,
            consecutive_bad_days INTEGER,
            restored_at TEXT
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, qty REAL,
            price REAL, pnl REAL
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _ctx(db, **overrides):
    defaults = dict(
        profile_id=1, user_id=1, db_path=db, enable_self_tuning=True,
        display_name="Test", segment="small",
        ai_confidence_threshold=25, max_position_pct=0.10,
        stop_loss_pct=0.03, take_profit_pct=0.10,
        strategy_momentum_breakout=True, strategy_volume_spike=True,
        strategy_mean_reversion=True, strategy_gap_and_go=True,
        enable_short_selling=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _insert_preds(db, strategy, total, wins):
    conn = sqlite3.connect(db)
    for i in range(wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, actual_return_pct, strategy_type) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'win', 2.0, ?)",
            (f"S{i}", strategy),
        )
    for i in range(total - wins):
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, price_at_prediction, "
            " status, actual_outcome, actual_return_pct, strategy_type) "
            "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss', -2.0, ?)",
            (f"L{i}", strategy),
        )
    conn.commit()
    conn.close()


class TestDeprecationAutoAction:
    def test_underperforming_modular_strategy_gets_deprecated(self, tmp_path):
        """The classic case: insider_cluster has 17% win rate (3/18) vs
        a healthy overall baseline. The tuner should auto-deprecate it
        instead of returning a 'Recommendation:' string."""
        db = _make_db(tmp_path)
        # 18 insider_cluster predictions, 3 wins -> 17% win rate
        _insert_preds(db, "insider_cluster", total=18, wins=3)
        # 50 momentum_breakout predictions, 25 wins -> 50% win rate
        # (push overall_wr well above the bad strategy)
        _insert_preds(db, "momentum_breakout", total=50, wins=25)

        ctx = _ctx(db)
        from self_tuning import _optimize_strategy_toggles, _get_conn
        conn = _get_conn(db)
        with patch("models.log_tuning_change") as mock_log:
            with patch("self_tuning._get_recent_adjustment", return_value=None):
                with patch("self_tuning._was_adjustment_effective", return_value=None):
                    msg = _optimize_strategy_toggles(
                        conn, ctx, profile_id=1, user_id=1,
                        overall_wr=50.0, resolved=68,
                    )
        conn.close()

        # Should have returned a "Deprecated" message, not a "Recommendation"
        assert msg is not None
        assert msg.startswith("Deprecated"), f"Expected 'Deprecated...', got: {msg!r}"
        assert "Recommendation" not in msg

        # Should have written to deprecated_strategies
        c = sqlite3.connect(db)
        rows = c.execute(
            "SELECT strategy_type FROM deprecated_strategies "
            "WHERE restored_at IS NULL"
        ).fetchall()
        c.close()
        assert ("insider_cluster",) in rows

        # Should have logged to tuning history
        mock_log.assert_called()
        args, kwargs = mock_log.call_args
        # adjustment_type should be the new deprecation type
        assert "strategy_deprecate" in args or kwargs.get("adjustment_type") == "strategy_deprecate"

    def test_cooldown_prevents_repeated_deprecation(self, tmp_path):
        """If the tuner already deprecated this strategy in the last 3
        days, don't re-fire — cooldown applies."""
        db = _make_db(tmp_path)
        _insert_preds(db, "insider_cluster", total=18, wins=3)
        _insert_preds(db, "momentum_breakout", total=50, wins=25)

        ctx = _ctx(db)
        from self_tuning import _optimize_strategy_toggles, _get_conn
        conn = _get_conn(db)
        # Pretend a cooldown is active for this strategy.
        with patch("self_tuning._get_recent_adjustment") as mock_recent:
            mock_recent.side_effect = lambda pid, key, days: (
                {"id": 1} if "deprecate:insider_cluster" in key else None
            )
            msg = _optimize_strategy_toggles(
                conn, ctx, profile_id=1, user_id=1,
                overall_wr=50.0, resolved=68,
            )
        conn.close()
        # Should have skipped this strategy and returned None (no other
        # strategies are bad in this fixture).
        assert msg is None

        # And nothing should have been added to deprecated_strategies.
        c = sqlite3.connect(db)
        n = c.execute("SELECT COUNT(*) FROM deprecated_strategies").fetchone()[0]
        c.close()
        assert n == 0

    def test_already_deprecated_strategy_skipped(self, tmp_path):
        """If alpha_decay already deprecated it (via rolling-Sharpe path),
        the tuner shouldn't re-deprecate."""
        db = _make_db(tmp_path)
        _insert_preds(db, "insider_cluster", total=18, wins=3)
        _insert_preds(db, "momentum_breakout", total=50, wins=25)
        # Pre-mark as deprecated.
        c = sqlite3.connect(db)
        c.execute(
            "INSERT INTO deprecated_strategies "
            "(strategy_type, deprecated_at, reason, "
            " rolling_sharpe_at_deprecation, lifetime_sharpe, "
            " consecutive_bad_days, restored_at) "
            "VALUES ('insider_cluster', datetime('now'), 'pre-existing', "
            " NULL, NULL, 0, NULL)"
        )
        c.commit()
        c.close()

        ctx = _ctx(db)
        from self_tuning import _optimize_strategy_toggles, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_strategy_toggles(
                    conn, ctx, profile_id=1, user_id=1,
                    overall_wr=50.0, resolved=68,
                )
        conn.close()
        # Should skip because already deprecated.
        assert msg is None

    def test_toggleable_strategy_still_uses_toggle_path(self, tmp_path):
        """Sanity: the existing 4-strategy toggle path still works for
        the strategies that DO have profile-level toggles. The new
        deprecation path is for strategies WITHOUT toggles only."""
        db = _make_db(tmp_path)
        # mean_reversion has a profile toggle. Make it bad.
        _insert_preds(db, "mean_reversion", total=18, wins=3)
        _insert_preds(db, "momentum_breakout", total=50, wins=25)

        ctx = _ctx(db)
        from self_tuning import _optimize_strategy_toggles, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_update:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_strategy_toggles(
                            conn, ctx, profile_id=1, user_id=1,
                            overall_wr=50.0, resolved=68,
                        )
                        mock_update.assert_called_with(
                            1, strategy_mean_reversion=0)
        conn.close()
        assert msg is not None
        assert msg.startswith("Disabled"), f"Expected 'Disabled...', got: {msg!r}"
        # Importantly: should not have been deprecated via alpha_decay.
        c = sqlite3.connect(db)
        n = c.execute("SELECT COUNT(*) FROM deprecated_strategies").fetchone()[0]
        c.close()
        assert n == 0
