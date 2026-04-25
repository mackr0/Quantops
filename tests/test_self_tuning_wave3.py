"""Wave 3 — exit parameter optimizers (Layer 1 Group B)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch


def _make_db(tmp_path):
    db = str(tmp_path / "w3.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL,
            features_json TEXT, resolved_at TEXT, days_held INTEGER
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, side TEXT, qty REAL, price REAL, pnl REAL,
            stop_loss REAL, take_profit REAL
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
        short_take_profit_pct=0.08,
        atr_multiplier_sl=2.0,
        atr_multiplier_tp=3.0,
        trailing_atr_multiplier=1.5,
        use_atr_stops=True,
        use_trailing_stops=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _seed_trades(db, rows):
    conn = sqlite3.connect(db)
    for r in rows:
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, "
            " stop_loss, take_profit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                r.get("symbol", "X"), r.get("side", "buy"),
                r.get("qty", 100), r.get("price", 10.0),
                r.get("pnl", 0.0),
                r.get("stop_loss"),
                r.get("take_profit"),
            ),
        )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# atr_multiplier_sl — widen on near-stop loss clusters
# ─────────────────────────────────────────────────────────────────────

class TestAtrMultiplierSl:
    def test_widens_when_losses_cluster_at_stop(self, tmp_path):
        db = _make_db(tmp_path)
        # 12 losses, all roughly the same magnitude (cluster near max)
        rows = [{"side": "buy", "price": 10, "qty": 100, "pnl": -50}
                for _ in range(12)]
        _seed_trades(db, rows)
        ctx = _ctx(db, atr_multiplier_sl=2.0, use_atr_stops=True)
        from self_tuning import _optimize_atr_multiplier_sl, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_atr_multiplier_sl(
                            conn, ctx, 1, 1, overall_wr=45.0, resolved=20)
                        mock_up.assert_called_with(1, atr_multiplier_sl=2.25)
        conn.close()
        assert msg is not None

    def test_no_op_when_atr_stops_off(self, tmp_path):
        db = _make_db(tmp_path)
        ctx = _ctx(db, use_atr_stops=False)
        from self_tuning import _optimize_atr_multiplier_sl, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_atr_multiplier_sl(
                conn, ctx, 1, 1, overall_wr=45.0, resolved=20)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# atr_multiplier_tp — tighten when avg winner << best winner
# ─────────────────────────────────────────────────────────────────────

class TestAtrMultiplierTp:
    def test_tightens_when_avg_winner_far_below_best(self, tmp_path):
        db = _make_db(tmp_path)
        # 1 big winner, 11 small winners — avg should be << max
        rows = [{"side": "buy", "price": 10, "qty": 100, "pnl": 100}]  # 10%
        rows.extend([{"side": "buy", "price": 10, "qty": 100, "pnl": 20}
                     for _ in range(11)])  # 2% each
        _seed_trades(db, rows)
        ctx = _ctx(db, atr_multiplier_tp=3.0, use_atr_stops=True)
        from self_tuning import _optimize_atr_multiplier_tp, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        msg = _optimize_atr_multiplier_tp(
                            conn, ctx, 1, 1, overall_wr=55.0, resolved=20)
                        mock_up.assert_called_with(1, atr_multiplier_tp=2.75)
        conn.close()
        assert msg is not None

    def test_respects_lower_bound(self, tmp_path):
        db = _make_db(tmp_path)
        rows = [{"side": "buy", "price": 10, "qty": 100, "pnl": 100}]
        rows.extend([{"side": "buy", "price": 10, "qty": 100, "pnl": 20}
                     for _ in range(11)])
        _seed_trades(db, rows)
        ctx = _ctx(db, atr_multiplier_tp=1.0, use_atr_stops=True)  # at floor
        from self_tuning import _optimize_atr_multiplier_tp, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            with patch("self_tuning._was_adjustment_effective", return_value=None):
                msg = _optimize_atr_multiplier_tp(
                    conn, ctx, 1, 1, overall_wr=55.0, resolved=20)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# trailing_atr_multiplier — placeholder no-op until MFE tracking exists
# ─────────────────────────────────────────────────────────────────────

class TestTrailingAtr:
    def test_noop_placeholder(self, tmp_path):
        db = _make_db(tmp_path)
        ctx = _ctx(db, use_trailing_stops=True)
        from self_tuning import _optimize_trailing_atr_multiplier, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_trailing_atr_multiplier(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# Orchestrator registration
# ─────────────────────────────────────────────────────────────────────

class TestW3OptimizerRegistration:
    def test_all_w3_optimizers_registered(self):
        import self_tuning
        import inspect
        src = inspect.getsource(self_tuning._apply_upward_optimizations)
        for fname in [
            "_optimize_short_take_profit",
            "_optimize_atr_multiplier_sl",
            "_optimize_atr_multiplier_tp",
            "_optimize_trailing_atr_multiplier",
        ]:
            assert fname in src, f"{fname} not registered in orchestrator"
