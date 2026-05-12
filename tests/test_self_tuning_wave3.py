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
            # 2026-05-12 — stop-to-TP ratio rebalancer.
            "_optimize_stop_to_tp_ratio",
        ]:
            assert fname in src, f"{fname} not registered in orchestrator"


# ─────────────────────────────────────────────────────────────────────
# Stop-to-TP ratio rebalancer (2026-05-12) — closes the 4.5:1 gap
# observed across 11 profiles. When stops fire much more than TPs,
# the AI auto-widens the ATR-SL multiplier and tightens the
# ATR-TP multiplier in one pass.
# ─────────────────────────────────────────────────────────────────────

def _make_db_with_strategy_and_dq(tmp_path):
    """Wave 3's _make_db skipped `strategy` + `data_quality` columns.
    The stop-to-TP rule reads both, so this fixture adds them."""
    db = str(tmp_path / "stop_tp.db")
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
            stop_loss REAL, take_profit REAL,
            strategy TEXT, status TEXT, data_quality TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _seed_exits(db, n_stops, n_tps, n_trailing=0, tagged_stops=0):
    """Insert closed sell rows with the right `strategy` attribution.
    `tagged_stops` are data_quality-tagged rows that should be EXCLUDED
    by the rule's data_quality filter (defensive against phantom-stop
    pollution feeding back into stop/TP tuning)."""
    conn = sqlite3.connect(db)
    for i in range(n_stops):
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, strategy, status) "
            "VALUES ('AAA', 'sell', 100, 10.0, -50.0, 'stop_loss', 'closed')")
    for i in range(n_trailing):
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, strategy, status) "
            "VALUES ('BBB', 'sell', 100, 10.0, -30.0, 'trailing_stop', 'closed')")
    for i in range(n_tps):
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, strategy, status) "
            "VALUES ('CCC', 'sell', 100, 10.0, 80.0, 'take_profit', 'closed')")
    for i in range(tagged_stops):
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, price, pnl, strategy, status, data_quality) "
            "VALUES ('DDD', 'sell', 100, 10.0, -200.0, 'stop_loss', 'closed', "
            "'phantom_stop_2026_05_11')")
    conn.commit()
    conn.close()


class TestStopToTpRatio:
    def test_widens_sl_tightens_tp_when_ratio_above_2_5(self, tmp_path):
        db = _make_db_with_strategy_and_dq(tmp_path)
        # 35 stops + trailing vs 10 TPs → ratio 3.5
        _seed_exits(db, n_stops=20, n_tps=10, n_trailing=15)
        ctx = _ctx(db, atr_multiplier_sl=2.0, atr_multiplier_tp=3.0)
        from self_tuning import _optimize_stop_to_tp_ratio, _get_conn
        conn = _get_conn(db)
        captured = []
        def fake_update(profile_id, **kwargs):
            captured.append(kwargs)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile", side_effect=fake_update), \
             patch("models.log_tuning_change"):
            msg = _optimize_stop_to_tp_ratio(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=20)
        conn.close()
        assert msg is not None
        # Both multipliers got updated
        sl_change = [c for c in captured if "atr_multiplier_sl" in c]
        tp_change = [c for c in captured if "atr_multiplier_tp" in c]
        assert sl_change and tp_change
        # SL widened: 2.0 → ~2.3
        assert sl_change[0]["atr_multiplier_sl"] > 2.0
        # TP tightened: 3.0 → ~2.7
        assert tp_change[0]["atr_multiplier_tp"] < 3.0

    def test_no_change_in_acceptable_band(self, tmp_path):
        db = _make_db_with_strategy_and_dq(tmp_path)
        # 18 stops vs 12 TPs → ratio 1.5 (acceptable)
        _seed_exits(db, n_stops=18, n_tps=12)
        ctx = _ctx(db)
        from self_tuning import _optimize_stop_to_tp_ratio, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_stop_to_tp_ratio(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None

    def test_tightens_sl_loosens_tp_when_ratio_too_low(self, tmp_path):
        db = _make_db_with_strategy_and_dq(tmp_path)
        # 5 stops vs 30 TPs → ratio 0.17 (TPs firing too easily —
        # stops might be too wide or TPs too close)
        _seed_exits(db, n_stops=5, n_tps=30)
        ctx = _ctx(db, atr_multiplier_sl=2.5, atr_multiplier_tp=2.5)
        from self_tuning import _optimize_stop_to_tp_ratio, _get_conn
        conn = _get_conn(db)
        captured = []
        def fake_update(profile_id, **kwargs):
            captured.append(kwargs)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile", side_effect=fake_update), \
             patch("models.log_tuning_change"):
            msg = _optimize_stop_to_tp_ratio(
                conn, ctx, 1, 1, overall_wr=70.0, resolved=20)
        conn.close()
        assert msg is not None
        sl_change = [c for c in captured if "atr_multiplier_sl" in c]
        tp_change = [c for c in captured if "atr_multiplier_tp" in c]
        # SL tightens, TP loosens
        assert sl_change[0]["atr_multiplier_sl"] < 2.5
        assert tp_change[0]["atr_multiplier_tp"] > 2.5

    def test_insufficient_samples_no_change(self, tmp_path):
        db = _make_db_with_strategy_and_dq(tmp_path)
        # Only 15 total exits — below threshold of 30
        _seed_exits(db, n_stops=12, n_tps=3)
        ctx = _ctx(db)
        from self_tuning import _optimize_stop_to_tp_ratio, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_stop_to_tp_ratio(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=20)
        conn.close()
        assert msg is None

    def test_data_quality_tagged_rows_excluded(self, tmp_path):
        """Phantom-stop-tagged stop_loss rows MUST be excluded from
        the ratio calc; otherwise corrupt SELL rows pollute the
        very tuner that's supposed to react to clean signal."""
        db = _make_db_with_strategy_and_dq(tmp_path)
        # 15 clean stops + 15 TPs = ratio 1.0 (acceptable).
        # Plus 30 TAGGED stop_loss rows that would push ratio to 3.0.
        # With filter: rule should NOT fire.
        _seed_exits(db, n_stops=15, n_tps=15, tagged_stops=30)
        ctx = _ctx(db)
        from self_tuning import _optimize_stop_to_tp_ratio, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_stop_to_tp_ratio(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        # If the filter works, total clean exits = 30, ratio = 1.0,
        # no change. If the filter is broken, ratio = 3.0 and the
        # rule would fire.
        assert msg is None

    def test_atr_off_skips(self, tmp_path):
        db = _make_db_with_strategy_and_dq(tmp_path)
        _seed_exits(db, n_stops=30, n_tps=5)
        ctx = _ctx(db, use_atr_stops=False)
        from self_tuning import _optimize_stop_to_tp_ratio, _get_conn
        conn = _get_conn(db)
        msg = _optimize_stop_to_tp_ratio(
            conn, ctx, 1, 1, overall_wr=40.0, resolved=20)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# Conviction-TP-override self-tuner (2026-05-12) — auto flip per
# profile based on MFE capture + stop-to-TP ratio. Replaces the
# operator-set toggle with an AI-driven decision.
# ─────────────────────────────────────────────────────────────────────

class TestConvictionTpOverrideTuner:
    def test_enable_when_capping_winners(self, tmp_path):
        """Low capture (winners running but exits cutting) +
        unbalanced stop-to-TP → flip ON."""
        db = _make_db_with_strategy_and_dq(tmp_path)
        ctx = _ctx(db, use_conviction_tp_override=False)
        ctx.db_path = db
        from self_tuning import _optimize_conviction_tp_override, _get_conn
        conn = _get_conn(db)
        captured = []
        def fake_update(profile_id, **kwargs):
            captured.append(kwargs)
        # Low capture: 35%, stop-to-TP: 2.5 (imbalanced)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("mfe_capture.compute_capture_ratio", return_value={
                 "avg_capture_ratio": 0.35, "n_trades": 30,
                 "median_capture_ratio": 0.30, "n_negative_capture": 5,
             }), \
             patch("mfe_capture.compute_stop_to_tp_ratio", return_value={
                 "ratio": 2.5, "ratio_label": "2.5",
                 "n_stops": 50, "n_tps": 20, "window_days": 30,
             }), \
             patch("models.update_trading_profile", side_effect=fake_update), \
             patch("models.log_tuning_change"):
            msg = _optimize_conviction_tp_override(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=30)
        conn.close()
        assert msg is not None
        assert "Enabled" in msg
        assert captured == [{"use_conviction_tp_override": 1}]

    def test_disable_when_already_capturing_well(self, tmp_path):
        """High capture (winners locking in) + balanced stop-to-TP →
        flip OFF, disciplined fixed TP wins."""
        db = _make_db_with_strategy_and_dq(tmp_path)
        ctx = _ctx(db, use_conviction_tp_override=True)
        ctx.db_path = db
        from self_tuning import _optimize_conviction_tp_override, _get_conn
        conn = _get_conn(db)
        captured = []
        def fake_update(profile_id, **kwargs):
            captured.append(kwargs)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("mfe_capture.compute_capture_ratio", return_value={
                 "avg_capture_ratio": 0.75, "n_trades": 30,
                 "median_capture_ratio": 0.70, "n_negative_capture": 2,
             }), \
             patch("mfe_capture.compute_stop_to_tp_ratio", return_value={
                 "ratio": 1.2, "ratio_label": "1.2",
                 "n_stops": 24, "n_tps": 20, "window_days": 30,
             }), \
             patch("models.update_trading_profile", side_effect=fake_update), \
             patch("models.log_tuning_change"):
            msg = _optimize_conviction_tp_override(
                conn, ctx, 1, 1, overall_wr=55.0, resolved=30)
        conn.close()
        assert msg is not None
        assert "Disabled" in msg
        assert captured == [{"use_conviction_tp_override": 0}]

    def test_no_change_when_data_thin(self, tmp_path):
        """Below 20 MFE-tracked trades — don't flip on noise."""
        db = _make_db_with_strategy_and_dq(tmp_path)
        ctx = _ctx(db, use_conviction_tp_override=False)
        ctx.db_path = db
        from self_tuning import _optimize_conviction_tp_override, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("mfe_capture.compute_capture_ratio", return_value={
                 "avg_capture_ratio": 0.30, "n_trades": 10,
                 "median_capture_ratio": 0.25, "n_negative_capture": 3,
             }), \
             patch("mfe_capture.compute_stop_to_tp_ratio", return_value={
                 "ratio": 3.0, "ratio_label": "3.0",
                 "n_stops": 30, "n_tps": 10, "window_days": 30,
             }):
            msg = _optimize_conviction_tp_override(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=10)
        conn.close()
        assert msg is None

    def test_neutral_band_no_change(self, tmp_path):
        """Capture 55% + ratio 1.7 — in neutral territory. No flip."""
        db = _make_db_with_strategy_and_dq(tmp_path)
        ctx = _ctx(db, use_conviction_tp_override=False)
        ctx.db_path = db
        from self_tuning import _optimize_conviction_tp_override, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("mfe_capture.compute_capture_ratio", return_value={
                 "avg_capture_ratio": 0.55, "n_trades": 30,
                 "median_capture_ratio": 0.50, "n_negative_capture": 3,
             }), \
             patch("mfe_capture.compute_stop_to_tp_ratio", return_value={
                 "ratio": 1.7, "ratio_label": "1.7",
                 "n_stops": 34, "n_tps": 20, "window_days": 30,
             }):
            msg = _optimize_conviction_tp_override(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=30)
        conn.close()
        assert msg is None


# ─────────────────────────────────────────────────────────────────────
# Schema migration: existing profiles flipped 0→1 idempotently
# ─────────────────────────────────────────────────────────────────────

class TestConvictionTpDefaultFlipMigration:
    def test_existing_profile_flipped_on_init(self, tmp_path, monkeypatch):
        """A profile that was 0 before init_user_db gets flipped to
        1 on the first init call. The marker prevents re-fire."""
        import sqlite3
        import models
        db = str(tmp_path / "users.db")
        # Pre-create a trading_profiles table with a profile at 0
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trading_profiles ("
                      "id INTEGER PRIMARY KEY, "
                      "use_conviction_tp_override INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO trading_profiles (id, "
                      "use_conviction_tp_override) VALUES (1, 0)")
        conn.commit()
        conn.close()
        import config
        monkeypatch.setattr(config, "DB_PATH", db)
        models.init_user_db()
        # After init: profile flipped to 1, marker present
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT use_conviction_tp_override FROM trading_profiles "
            "WHERE id = 1"
        ).fetchone()
        assert row[0] == 1
        marker = conn.execute(
            "SELECT 1 FROM migration_markers WHERE key = ?",
            ("conviction_tp_default_on_2026_05_12",),
        ).fetchone()
        assert marker is not None
        conn.close()

    def test_migration_idempotent_doesnt_reflip(self, tmp_path, monkeypatch):
        """If the operator flips a profile back to 0 AFTER the
        migration ran, a subsequent init does NOT re-flip it."""
        import sqlite3
        import models
        db = str(tmp_path / "users.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trading_profiles ("
                      "id INTEGER PRIMARY KEY, "
                      "use_conviction_tp_override INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO trading_profiles (id, "
                      "use_conviction_tp_override) VALUES (1, 0)")
        conn.commit()
        conn.close()
        import config
        monkeypatch.setattr(config, "DB_PATH", db)
        # First init: flips to 1, sets marker
        models.init_user_db()
        # Operator turns it back off
        conn = sqlite3.connect(db)
        conn.execute("UPDATE trading_profiles SET "
                      "use_conviction_tp_override = 0 WHERE id = 1")
        conn.commit()
        conn.close()
        # Second init: marker exists, migration skipped
        models.init_user_db()
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT use_conviction_tp_override FROM trading_profiles "
            "WHERE id = 1"
        ).fetchone()
        assert row[0] == 0  # operator's choice preserved
        conn.close()


class TestConvictionTpRegistered:
    def test_optimizer_registered_in_orchestrator(self):
        import self_tuning
        import inspect
        src = inspect.getsource(self_tuning._apply_upward_optimizations)
        assert "_optimize_conviction_tp_override" in src
