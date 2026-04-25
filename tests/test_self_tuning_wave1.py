"""Wave 1 of autonomous tuning — extended-coverage optimizers (Group A
concentration/risk + Group D timing/flag). Each new tuner function gets:
- a triggers-correctly test (action fires when signal is present)
- a respects-bounds test (clamped to PARAM_BOUNDS)
- a respects-cooldown test (no action when recent adjustment exists)
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_db(tmp_path):
    db = str(tmp_path / "w1.db")
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
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, side TEXT, qty REAL, price REAL, pnl REAL,
            stop_loss REAL, take_profit REAL
        );
        CREATE TABLE deprecated_strategies (
            strategy_type TEXT PRIMARY KEY,
            deprecated_at TEXT, reason TEXT,
            rolling_sharpe_at_deprecation REAL, lifetime_sharpe REAL,
            consecutive_bad_days INTEGER, restored_at TEXT
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
        ai_confidence_threshold=25,
        max_position_pct=0.10,
        max_total_positions=10,
        max_correlation=0.7,
        max_sector_positions=5,
        drawdown_pause_pct=0.20,
        drawdown_reduce_pct=0.10,
        min_price=1.0, max_price=20.0,
        stop_loss_pct=0.03, take_profit_pct=0.10,
        short_stop_loss_pct=0.08,
        avoid_earnings_days=2,
        skip_first_minutes=0,
        maga_mode=True,
        enable_short_selling=False,
        strategy_momentum_breakout=True, strategy_volume_spike=True,
        strategy_mean_reversion=True, strategy_gap_and_go=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _seed_trades(db, rows):
    conn = sqlite3.connect(db)
    for r in rows:
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, timestamp) "
            "VALUES (?, 'buy', 100, ?, ?, ?)",
            (
                r.get("symbol", "X"), r.get("price", 10.0),
                r.get("pnl", 0.0),
                r.get("timestamp", "2026-04-01 12:00:00"),
            ),
        )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# param_bounds.clamp
# ─────────────────────────────────────────────────────────────────────

class TestClamp:
    def test_below_min_clamped_up(self):
        from param_bounds import clamp
        assert clamp("max_correlation", 0.10) == 0.30
        assert clamp("max_position_pct", 0.001) == 0.01

    def test_above_max_clamped_down(self):
        from param_bounds import clamp
        assert clamp("max_correlation", 1.0) == 0.95
        assert clamp("max_total_positions", 50) == 25

    def test_in_range_passthrough(self):
        from param_bounds import clamp
        assert clamp("max_correlation", 0.6) == 0.6
        assert clamp("rsi_overbought", 80) == 80

    def test_unknown_param_passthrough(self):
        from param_bounds import clamp
        assert clamp("not_a_real_param", 99) == 99

    def test_int_in_int_out(self):
        from param_bounds import clamp
        result = clamp("max_total_positions", 100)
        assert result == 25
        assert isinstance(result, int)


# ─────────────────────────────────────────────────────────────────────
# _optimize_max_total_positions
# ─────────────────────────────────────────────────────────────────────

class TestMaxTotalPositions:
    def test_reduces_on_deep_losses_and_low_wr(self, tmp_path):
        db = _make_db(tmp_path)
        # 10 deep losses
        _seed_trades(db, [{"pnl": -300, "price": 10} for _ in range(10)])
        ctx = _ctx(db, max_total_positions=10)
        from self_tuning import _optimize_max_total_positions, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_max_total_positions(
                            conn, ctx, 1, 1, overall_wr=30.0, resolved=30)
                        mock_up.assert_called_with(1, max_total_positions=9)
        conn.close()
        assert msg is not None
        assert "9" in msg

    def test_increases_on_strong_edge(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_trades(db, [{"pnl": 200, "price": 10} for _ in range(10)])
        ctx = _ctx(db, max_total_positions=10)
        from self_tuning import _optimize_max_total_positions, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_max_total_positions(
                            conn, ctx, 1, 1, overall_wr=65.0, resolved=30)
                        mock_up.assert_called_with(1, max_total_positions=11)
        conn.close()
        assert "11" in msg

    def test_respects_lower_bound(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_trades(db, [{"pnl": -300} for _ in range(10)])
        ctx = _ctx(db, max_total_positions=3)  # already at floor
        from self_tuning import _optimize_max_total_positions, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_max_total_positions(
                    conn, ctx, 1, 1, overall_wr=30.0, resolved=30)
        conn.close()
        assert msg is None  # Can't go below 3

    def test_respects_cooldown(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_trades(db, [{"pnl": -300} for _ in range(10)])
        ctx = _ctx(db, max_total_positions=10)
        from self_tuning import _optimize_max_total_positions, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment",
                   return_value={"id": 1}):
            msg = _optimize_max_total_positions(
                conn, ctx, 1, 1, overall_wr=30.0, resolved=30)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# _optimize_max_correlation
# ─────────────────────────────────────────────────────────────────────

class TestMaxCorrelation:
    def test_tightens_on_loss_clusters(self, tmp_path):
        db = _make_db(tmp_path)
        # 4 weeks total, 3 of them with 3+ losses (cluster_rate = 75%)
        rows = []
        for week, ts in enumerate(
            ["2026-03-02 12:00:00", "2026-03-09 12:00:00",
             "2026-03-16 12:00:00", "2026-03-23 12:00:00"]
        ):
            n_losses = 3 if week < 3 else 0
            for i in range(n_losses):
                rows.append({"pnl": -100, "timestamp": ts, "symbol": f"S{i}"})
            rows.append({"pnl": 50, "timestamp": ts})  # at least one trade per week
        _seed_trades(db, rows)
        ctx = _ctx(db, max_correlation=0.70)
        from self_tuning import _optimize_max_correlation, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_max_correlation(
                            conn, ctx, 1, 1, overall_wr=40.0, resolved=30)
                        mock_up.assert_called_with(1, max_correlation=0.65)
        conn.close()
        assert msg is not None
        assert "0.65" in msg

    def test_loosens_on_clean_history_and_high_wr(self, tmp_path):
        db = _make_db(tmp_path)
        # 6 weeks, no clusters, high WR
        rows = []
        for ts in ["2026-03-02 12:00:00", "2026-03-09 12:00:00",
                   "2026-03-16 12:00:00", "2026-03-23 12:00:00",
                   "2026-03-30 12:00:00", "2026-04-06 12:00:00"]:
            rows.extend([{"pnl": 100, "timestamp": ts}] * 3)
        _seed_trades(db, rows)
        ctx = _ctx(db, max_correlation=0.70)
        from self_tuning import _optimize_max_correlation, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_max_correlation(
                            conn, ctx, 1, 1, overall_wr=60.0, resolved=30)
                        mock_up.assert_called_with(1, max_correlation=0.75)
        conn.close()
        assert msg is not None
        assert "0.75" in msg

    def test_respects_upper_bound(self, tmp_path):
        db = _make_db(tmp_path)
        for ts in ["2026-03-02 12:00:00", "2026-03-09 12:00:00",
                   "2026-03-16 12:00:00", "2026-03-23 12:00:00"]:
            _seed_trades(db, [{"pnl": 100, "timestamp": ts}] * 3)
        ctx = _ctx(db, max_correlation=0.95)  # already at ceiling
        from self_tuning import _optimize_max_correlation, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_max_correlation(
                    conn, ctx, 1, 1, overall_wr=60.0, resolved=30)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# _optimize_max_sector_positions
# ─────────────────────────────────────────────────────────────────────

class TestMaxSectorPositions:
    def test_tightens_on_low_wr(self, tmp_path):
        db = _make_db(tmp_path)
        ctx = _ctx(db, max_sector_positions=5)
        from self_tuning import _optimize_max_sector_positions, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_max_sector_positions(
                            conn, ctx, 1, 1, overall_wr=30.0, resolved=30)
                        mock_up.assert_called_with(1, max_sector_positions=4)
        conn.close()
        assert msg is not None

    def test_no_action_when_healthy(self, tmp_path):
        db = _make_db(tmp_path)
        ctx = _ctx(db, max_sector_positions=5)
        from self_tuning import _optimize_max_sector_positions, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_max_sector_positions(
                    conn, ctx, 1, 1, overall_wr=55.0, resolved=30)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# _optimize_drawdown_thresholds  +  _optimize_drawdown_reduce
# ─────────────────────────────────────────────────────────────────────

class TestDrawdownThresholds:
    def test_tightens_pause_in_drift_zone(self, tmp_path):
        db = _make_db(tmp_path)
        ctx = _ctx(db, drawdown_pause_pct=0.20)
        from self_tuning import _optimize_drawdown_thresholds, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_drawdown_thresholds(
                            conn, ctx, 1, 1, overall_wr=40.0, resolved=30)
                        mock_up.assert_called_with(1, drawdown_pause_pct=0.18)
        conn.close()
        assert msg is not None

    def test_no_action_outside_drift_zone(self, tmp_path):
        db = _make_db(tmp_path)
        ctx = _ctx(db, drawdown_pause_pct=0.20)
        from self_tuning import _optimize_drawdown_thresholds, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg_high = _optimize_drawdown_thresholds(
                conn, ctx, 1, 1, overall_wr=55.0, resolved=30)
            msg_low = _optimize_drawdown_thresholds(
                conn, ctx, 1, 1, overall_wr=20.0, resolved=30)
        conn.close()
        assert msg_high is None
        assert msg_low is None

    def test_tightens_reduce_in_drift_zone(self, tmp_path):
        db = _make_db(tmp_path)
        ctx = _ctx(db, drawdown_reduce_pct=0.10)
        from self_tuning import _optimize_drawdown_reduce, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_drawdown_reduce(
                            conn, ctx, 1, 1, overall_wr=40.0, resolved=30)
                        mock_up.assert_called_with(1, drawdown_reduce_pct=0.09)
        conn.close()
        assert msg is not None


# ─────────────────────────────────────────────────────────────────────
# _optimize_price_band
# ─────────────────────────────────────────────────────────────────────

class TestPriceBand:
    def test_raises_min_when_bottom_band_fails(self, tmp_path):
        db = _make_db(tmp_path)
        # 6 trades in bottom band ($1-$1.50), all losers
        _seed_trades(db, [{"price": 1.20, "pnl": -50} for _ in range(6)])
        ctx = _ctx(db, min_price=1.0, max_price=20.0)
        from self_tuning import _optimize_price_band, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_price_band(
                            conn, ctx, 1, 1, overall_wr=45.0, resolved=30)
                        mock_up.assert_called_with(1, min_price=1.25)
        conn.close()
        assert msg is not None
        assert "1.25" in msg

    def test_lowers_max_when_top_band_fails(self, tmp_path):
        db = _make_db(tmp_path)
        # 6 trades in top band ($17+), all losers
        _seed_trades(db, [{"price": 18.0, "pnl": -50} for _ in range(6)])
        ctx = _ctx(db, min_price=1.0, max_price=20.0)
        from self_tuning import _optimize_price_band, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_price_band(
                            conn, ctx, 1, 1, overall_wr=45.0, resolved=30)
                        mock_up.assert_called_with(1, max_price=17.0)
        conn.close()
        assert msg is not None


# ─────────────────────────────────────────────────────────────────────
# _optimize_maga_mode (binary auto-disable)
# ─────────────────────────────────────────────────────────────────────

class TestMagaMode:
    def test_auto_disables_when_political_signal_underperforms(self, tmp_path):
        db = _make_db(tmp_path)
        # 25 resolved predictions with political_context set, 10 wins -> 40% WR
        # vs 60% overall — clearly underperforming
        conn = sqlite3.connect(db)
        for i in range(10):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome, features_json) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'win', "
                " '{\"political_context\":1}')",
                (f"W{i}",),
            )
        for i in range(15):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome, features_json) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss', "
                " '{\"political_context\":1}')",
                (f"L{i}",),
            )
        conn.commit()
        conn.close()

        ctx = _ctx(db, maga_mode=True)
        from self_tuning import _optimize_maga_mode, _get_conn
        conn2 = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_maga_mode(
                            conn2, ctx, 1, 1, overall_wr=60.0, resolved=25)
                        mock_up.assert_called_with(1, maga_mode=0)
        conn2.close()
        assert msg is not None
        assert "Disabled" in msg

    def test_no_action_when_already_off(self, tmp_path):
        db = _make_db(tmp_path)
        ctx = _ctx(db, maga_mode=False)
        from self_tuning import _optimize_maga_mode, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_maga_mode(
                conn, ctx, 1, 1, overall_wr=60.0, resolved=25)
        conn.close()
        assert msg is None

    def test_no_action_when_signal_not_underperforming(self, tmp_path):
        db = _make_db(tmp_path)
        # 25 with political_context, 18 wins -> 72% WR vs 60% overall
        conn = sqlite3.connect(db)
        for i in range(18):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome, features_json) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'win', "
                " '{\"political_context\":1}')",
                (f"W{i}",),
            )
        for i in range(7):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome, features_json) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss', "
                " '{\"political_context\":1}')",
                (f"L{i}",),
            )
        conn.commit()
        conn.close()

        ctx = _ctx(db, maga_mode=True)
        from self_tuning import _optimize_maga_mode, _get_conn
        conn2 = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_maga_mode(
                    conn2, ctx, 1, 1, overall_wr=60.0, resolved=25)
        conn2.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# Orchestrator registration
# ─────────────────────────────────────────────────────────────────────

class TestOptimizerRegistration:
    def test_all_w1_optimizers_registered(self):
        """All Wave 1 optimizers must be in the orchestrator's list."""
        import self_tuning
        import inspect
        src = inspect.getsource(self_tuning._apply_upward_optimizations)
        for fname in [
            "_optimize_max_total_positions",
            "_optimize_max_correlation",
            "_optimize_max_sector_positions",
            "_optimize_drawdown_thresholds",
            "_optimize_drawdown_reduce",
            "_optimize_price_band",
            "_optimize_avoid_earnings_days",
            "_optimize_skip_first_minutes",
            "_optimize_maga_mode",
        ]:
            assert fname in src, f"{fname} not registered in orchestrator"
