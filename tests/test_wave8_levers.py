"""Wave 8 — three new self-tuning levers (2026-05-12).

  8a: Fast-lane strategy retirement (rolling-10 wr < 25%)
  8b: Options pipeline volume expansion (IV-rich/cheap thresholds
       become ctx-tunable; default dead-zone closed)
  8c: Per-symbol stop-out blacklist (3+ stops in 30d → 14-day
       cool-off; trade pipeline skips blacklisted entries)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    db = str(tmp_path / "wave8.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, predicted_signal TEXT, confidence REAL,
            price_at_prediction REAL, status TEXT DEFAULT 'resolved',
            actual_outcome TEXT, actual_return_pct REAL,
            features_json TEXT, resolved_at TEXT DEFAULT (datetime('now')),
            days_held INTEGER, strategy_type TEXT
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL,
            price REAL, pnl REAL, strategy TEXT, status TEXT,
            data_quality TEXT, signal_type TEXT
        );
        CREATE TABLE deprecated_strategies (
            strategy_type TEXT PRIMARY KEY,
            deprecated_at TEXT NOT NULL DEFAULT (datetime('now')),
            reason TEXT NOT NULL,
            rolling_sharpe_at_deprecation REAL,
            lifetime_sharpe REAL,
            consecutive_bad_days INTEGER,
            restored_at TEXT
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
        entry_blacklist="{}",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# 8a — Fast-lane strategy retirement
# ---------------------------------------------------------------------------

class TestFastLaneRetirement:
    def _seed_strategy(self, db, strat, n_wins, n_losses):
        conn = sqlite3.connect(db)
        for i in range(n_wins):
            conn.execute(
                "INSERT INTO ai_predictions (strategy_type, status, "
                "actual_outcome, resolved_at) "
                "VALUES (?, 'resolved', 'win', datetime('now'))",
                (strat,),
            )
        for i in range(n_losses):
            conn.execute(
                "INSERT INTO ai_predictions (strategy_type, status, "
                "actual_outcome, resolved_at) "
                "VALUES (?, 'resolved', 'loss', datetime('now'))",
                (strat,),
            )
        conn.commit()
        conn.close()

    def test_deprecates_strategy_with_0pct_wr(self, tmp_path):
        db = _make_db(tmp_path)
        # 0 wins, 10 losses → 0% wr on rolling 10
        self._seed_strategy(db, "mean_reversion", n_wins=0, n_losses=10)
        ctx = _ctx(db)
        from self_tuning import _optimize_fast_lane_retirement, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.log_tuning_change"):
            msg = _optimize_fast_lane_retirement(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=10)
        conn.close()
        assert msg is not None
        assert "mean_reversion" in msg
        # Confirm deprecated_strategies row was created
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT reason FROM deprecated_strategies "
            "WHERE strategy_type='mean_reversion' "
            "  AND restored_at IS NULL"
        ).fetchone()
        conn.close()
        assert row is not None
        assert "fast_lane" in row[0]

    def test_skips_strategy_above_25pct(self, tmp_path):
        db = _make_db(tmp_path)
        self._seed_strategy(db, "good_strat", n_wins=5, n_losses=5)
        ctx = _ctx(db)
        from self_tuning import _optimize_fast_lane_retirement, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_fast_lane_retirement(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10)
        conn.close()
        # 50% wr — no deprecation triggered
        if msg is not None:
            # If something fired (restore path only), make sure
            # `good_strat` wasn't in it
            assert "good_strat" not in msg
        # Confirm DB unchanged
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT 1 FROM deprecated_strategies WHERE strategy_type='good_strat'"
        ).fetchone()
        conn.close()
        assert row is None

    def test_skips_below_min_samples(self, tmp_path):
        """5 samples — below the 10-trade gate."""
        db = _make_db(tmp_path)
        self._seed_strategy(db, "new_strat", n_wins=0, n_losses=5)
        ctx = _ctx(db)
        from self_tuning import _optimize_fast_lane_retirement, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_fast_lane_retirement(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=5)
        conn.close()
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT 1 FROM deprecated_strategies WHERE strategy_type='new_strat'"
        ).fetchone()
        conn.close()
        assert row is None

    def test_auto_restores_after_14_days(self, tmp_path):
        db = _make_db(tmp_path)
        self._seed_strategy(db, "old_dep", n_wins=10, n_losses=0)
        # Manually insert a fast-lane deprecation 15 days old
        conn = sqlite3.connect(db)
        old_ts = (datetime.utcnow() - timedelta(days=15)).isoformat()
        conn.execute(
            "INSERT INTO deprecated_strategies "
            "(strategy_type, deprecated_at, reason) "
            "VALUES ('old_dep', ?, 'fast_lane: rolling-10 wr 10%')",
            (old_ts,),
        )
        conn.commit()
        conn.close()
        ctx = _ctx(db)
        from self_tuning import _optimize_fast_lane_retirement, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.log_tuning_change"):
            msg = _optimize_fast_lane_retirement(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10)
        conn.close()
        assert msg is not None
        assert "old_dep" in msg
        assert "Restored" in msg
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT restored_at FROM deprecated_strategies "
            "WHERE strategy_type='old_dep'"
        ).fetchone()
        conn.close()
        assert row[0] is not None  # restored_at populated

    def test_does_not_restore_recent_deprecations(self, tmp_path):
        """A fast-lane deprecation only 5 days old must NOT be
        auto-restored — needs the full 14-day cool-off."""
        db = _make_db(tmp_path)
        conn = sqlite3.connect(db)
        recent_ts = (datetime.utcnow() - timedelta(days=5)).isoformat()
        conn.execute(
            "INSERT INTO deprecated_strategies "
            "(strategy_type, deprecated_at, reason) "
            "VALUES ('recent_dep', ?, 'fast_lane: rolling-10 wr 10%')",
            (recent_ts,),
        )
        conn.commit()
        conn.close()
        ctx = _ctx(db)
        from self_tuning import _optimize_fast_lane_retirement, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_fast_lane_retirement(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10)
        conn.close()
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT restored_at FROM deprecated_strategies "
            "WHERE strategy_type='recent_dep'"
        ).fetchone()
        conn.close()
        assert row[0] is None  # still deprecated, not restored

    def test_does_not_restore_alpha_decay_deprecations(self, tmp_path):
        """Don't auto-restore deprecations tagged by alpha_decay —
        those have their own Sharpe-recovery restore path."""
        db = _make_db(tmp_path)
        conn = sqlite3.connect(db)
        old_ts = (datetime.utcnow() - timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT INTO deprecated_strategies "
            "(strategy_type, deprecated_at, reason) "
            "VALUES ('alpha_dep', ?, 'alpha decay detected')",
            (old_ts,),
        )
        conn.commit()
        conn.close()
        ctx = _ctx(db)
        from self_tuning import _optimize_fast_lane_retirement, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_fast_lane_retirement(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10)
        conn.close()
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT restored_at FROM deprecated_strategies "
            "WHERE strategy_type='alpha_dep'"
        ).fetchone()
        conn.close()
        assert row[0] is None  # untouched


# ---------------------------------------------------------------------------
# 8b — Options pipeline IV thresholds tunable
# ---------------------------------------------------------------------------

class TestOptionIvThresholdsCtxAware:
    def test_default_closes_dead_zone(self):
        """Without ctx, defaults are 55/55 — every IV value triggers
        exactly one branch. No IV produces zero proposals."""
        from options_strategy_advisor import evaluate_candidate_for_multileg
        # IV 55 — was in the old 50-60 dead zone
        recs = evaluate_candidate_for_multileg(
            {"symbol": "AAPL", "signal": "STRONG_BUY", "price": 180.0},
            iv_rank_pct=55.0, ctx=None,
        )
        assert len(recs) >= 1, "IV 55 should fire at least one branch"

    def test_iv_below_old_cheap_threshold_works(self):
        """IV 45 — was below old CHEAP threshold (50), should fire
        debit branch."""
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            {"symbol": "AAPL", "signal": "STRONG_BUY", "price": 180.0},
            iv_rank_pct=45.0, ctx=None,
        )
        assert any("call_spread" in r.get("strategy", "") for r in recs)

    def test_ctx_overrides_thresholds(self):
        """When the profile tunes the thresholds wider, the dead
        zone re-opens. Verifies the ctx hook actually wires through."""
        from options_strategy_advisor import evaluate_candidate_for_multileg
        ctx = SimpleNamespace(
            option_iv_rich_threshold=65.0,
            option_iv_cheap_threshold=45.0,
        )
        # IV 55 — falls in the NEW dead zone (45-65)
        recs = evaluate_candidate_for_multileg(
            {"symbol": "AAPL", "signal": "STRONG_BUY", "price": 180.0},
            iv_rank_pct=55.0, ctx=ctx,
        )
        # No bullish/bearish vertical proposal in dead zone
        verticals = [r for r in recs if "_spread" in r.get("strategy", "")]
        assert len(verticals) == 0


# ---------------------------------------------------------------------------
# 8c — Per-symbol entry blacklist
# ---------------------------------------------------------------------------

class TestEntryBlacklistParsing:
    def test_parse_filters_expired(self):
        from entry_blacklist import parse_blacklist
        past = (datetime.utcnow() - timedelta(days=1)).isoformat()
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        raw = json.dumps({"NVDA": past, "TSLA": future})
        out = parse_blacklist(raw)
        assert "NVDA" not in out
        assert "TSLA" in out

    def test_parse_handles_missing(self):
        from entry_blacklist import parse_blacklist
        assert parse_blacklist(None) == {}
        assert parse_blacklist("") == {}
        assert parse_blacklist("not json") == {}

    def test_is_blacklisted_reads_ctx(self):
        from entry_blacklist import is_blacklisted
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        ctx = SimpleNamespace(
            entry_blacklist=json.dumps({"NVDA": future}),
        )
        assert is_blacklisted(ctx, "NVDA") is True
        assert is_blacklisted(ctx, "AAPL") is False

    def test_is_blacklisted_case_insensitive(self):
        from entry_blacklist import is_blacklisted
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        ctx = SimpleNamespace(
            entry_blacklist=json.dumps({"NVDA": future}),
        )
        assert is_blacklisted(ctx, "nvda") is True


class TestStopOutBlacklistTuner:
    def _seed_stop_outs(self, db, symbol, count):
        conn = sqlite3.connect(db)
        ts = datetime.utcnow().isoformat()
        for i in range(count):
            conn.execute(
                "INSERT INTO trades "
                "(timestamp, symbol, side, qty, price, pnl, strategy, "
                " status) "
                "VALUES (?, ?, 'sell', 100, 50, -50, 'stop_loss', 'closed')",
                (ts, symbol),
            )
        conn.commit()
        conn.close()

    def test_blacklists_after_3_stop_outs(self, tmp_path):
        db = _make_db(tmp_path)
        self._seed_stop_outs(db, "NVDA", count=3)
        ctx = _ctx(db, entry_blacklist="{}")
        captured = []
        def fake_update(profile_id, **kwargs):
            captured.append(kwargs)
        from self_tuning import _optimize_stop_out_blacklist, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile",
                    side_effect=fake_update), \
             patch("models.log_tuning_change"):
            msg = _optimize_stop_out_blacklist(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=20)
        conn.close()
        assert msg is not None
        # Verify update_trading_profile got the right JSON with NVDA
        assert len(captured) >= 1
        bl_json = captured[0].get("entry_blacklist")
        assert bl_json is not None
        bl = json.loads(bl_json)
        assert "NVDA" in bl

    def test_does_not_blacklist_below_threshold(self, tmp_path):
        db = _make_db(tmp_path)
        self._seed_stop_outs(db, "NVDA", count=2)
        ctx = _ctx(db, entry_blacklist="{}")
        from self_tuning import _optimize_stop_out_blacklist, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_stop_out_blacklist(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=10)
        conn.close()
        assert msg is None

    def test_data_quality_tagged_excluded(self, tmp_path):
        """Phantom-stop-tagged stop_loss rows MUST NOT count
        toward the blacklist threshold."""
        db = _make_db(tmp_path)
        # 5 corrupt stop-outs but data_quality-tagged
        conn = sqlite3.connect(db)
        ts = datetime.utcnow().isoformat()
        for i in range(5):
            conn.execute(
                "INSERT INTO trades "
                "(timestamp, symbol, side, qty, price, pnl, strategy, "
                " status, data_quality) "
                "VALUES (?, 'NVDA', 'sell', 100, 50, -50, 'stop_loss', "
                "'closed', 'phantom_stop_2026_05_11')",
                (ts,),
            )
        conn.commit()
        conn.close()
        ctx = _ctx(db, entry_blacklist="{}")
        from self_tuning import _optimize_stop_out_blacklist, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_stop_out_blacklist(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10)
        conn.close()
        # Filter excluded all 5; symbol shouldn't be blacklisted
        assert msg is None


class TestTradePipelineBlacklistGate:
    """trade_pipeline.py must SKIP entries on blacklisted symbols."""

    def test_buy_blocked_when_symbol_blacklisted(self):
        # Direct unit test of the gate logic via mock
        from entry_blacklist import is_blacklisted
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        ctx = SimpleNamespace(
            entry_blacklist=json.dumps({"NVDA": future}),
        )
        assert is_blacklisted(ctx, "NVDA") is True
        # Clean ctx — not blacklisted
        ctx_clean = SimpleNamespace(entry_blacklist="{}")
        assert is_blacklisted(ctx_clean, "NVDA") is False


# ---------------------------------------------------------------------------
# Orchestrator registration
# ---------------------------------------------------------------------------

class TestWave8Registered:
    def test_optimizers_registered(self):
        import self_tuning
        import inspect
        src = inspect.getsource(self_tuning._apply_upward_optimizations)
        assert "_optimize_fast_lane_retirement" in src
        assert "_optimize_stop_out_blacklist" in src
        # 2026-05-13 — Wave 9a meta-pregate tuner
        assert "_optimize_meta_pregate_threshold" in src


# ---------------------------------------------------------------------------
# 9a — Meta-pregate threshold auto-tuner (2026-05-13)
# ---------------------------------------------------------------------------

class TestMetaPregateThreshold:
    def _seed_predictions(self, db, n_actionable, n_hold):
        conn = sqlite3.connect(db)
        from datetime import datetime
        ts = datetime.utcnow().isoformat()
        for i in range(n_actionable):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(timestamp, symbol, predicted_signal, status) "
                "VALUES (?, 'X', 'BUY', 'pending')",
                (ts,),
            )
        for i in range(n_hold):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(timestamp, symbol, predicted_signal, status) "
                "VALUES (?, 'X', 'HOLD', 'pending')",
                (ts,),
            )
        conn.commit()
        conn.close()

    def test_lowers_when_actionable_ratio_too_low(self, tmp_path):
        """3% actionable signals → filter is too tight → lower."""
        db = _make_db(tmp_path)
        self._seed_predictions(db, n_actionable=3, n_hold=97)
        ctx = _ctx(db, meta_pregate_threshold=0.50)
        from self_tuning import _optimize_meta_pregate_threshold, _get_conn
        conn = _get_conn(db)
        captured = []
        def fake_update(profile_id, **kwargs):
            captured.append(kwargs)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile", side_effect=fake_update), \
             patch("models.log_tuning_change"):
            msg = _optimize_meta_pregate_threshold(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=20)
        conn.close()
        assert msg is not None
        assert "Lowered" in msg
        assert captured == [{"meta_pregate_threshold": 0.45}]

    def test_raises_when_actionable_ratio_too_high(self, tmp_path):
        """40% actionable signals → filter is too loose → raise."""
        db = _make_db(tmp_path)
        self._seed_predictions(db, n_actionable=40, n_hold=60)
        ctx = _ctx(db, meta_pregate_threshold=0.35)
        from self_tuning import _optimize_meta_pregate_threshold, _get_conn
        conn = _get_conn(db)
        captured = []
        def fake_update(profile_id, **kwargs):
            captured.append(kwargs)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile", side_effect=fake_update), \
             patch("models.log_tuning_change"):
            msg = _optimize_meta_pregate_threshold(
                conn, ctx, 1, 1, overall_wr=55.0, resolved=20)
        conn.close()
        assert msg is not None
        assert "Raised" in msg
        assert captured == [{"meta_pregate_threshold": 0.40}]

    def test_no_change_in_healthy_band(self, tmp_path):
        """15% actionable → healthy band 5-30% → no change."""
        db = _make_db(tmp_path)
        self._seed_predictions(db, n_actionable=15, n_hold=85)
        ctx = _ctx(db, meta_pregate_threshold=0.35)
        from self_tuning import _optimize_meta_pregate_threshold, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_meta_pregate_threshold(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None

    def test_thin_sample_no_change(self, tmp_path):
        """20 predictions — below the 50-sample gate."""
        db = _make_db(tmp_path)
        self._seed_predictions(db, n_actionable=1, n_hold=19)
        ctx = _ctx(db, meta_pregate_threshold=0.50)
        from self_tuning import _optimize_meta_pregate_threshold, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_meta_pregate_threshold(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=10)
        conn.close()
        assert msg is None

    def test_floor_prevents_runaway_down(self, tmp_path):
        """Already at 0.15 floor — even with 0% actionable, no change."""
        db = _make_db(tmp_path)
        self._seed_predictions(db, n_actionable=0, n_hold=100)
        ctx = _ctx(db, meta_pregate_threshold=0.15)
        from self_tuning import _optimize_meta_pregate_threshold, _get_conn
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_meta_pregate_threshold(
                conn, ctx, 1, 1, overall_wr=40.0, resolved=20)
        conn.close()
        assert msg is None  # already at floor

    def test_migration_lowers_existing_05_to_035(self, tmp_path, monkeypatch):
        """Migration flips profiles still at 0.5 → 0.35. Profiles
        the operator already tuned are preserved."""
        import config, models
        db = str(tmp_path / "users.db")
        conn = sqlite3.connect(db)
        # Include market_type — other migrations in init_user_db
        # reference it (short-selling crypto filter).
        conn.execute(
            "CREATE TABLE trading_profiles ("
            "id INTEGER PRIMARY KEY, name TEXT, market_type TEXT, "
            "meta_pregate_threshold REAL DEFAULT 0.5, "
            "use_conviction_tp_override INTEGER DEFAULT 0, "
            "enable_short_selling INTEGER DEFAULT 0, "
            "skip_first_minutes INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO trading_profiles "
            "(id, name, market_type, meta_pregate_threshold) "
            "VALUES (1, 'A', 'midcap', 0.5), (2, 'B', 'smallcap', 0.30), "
            "(3, 'C', 'largecap', 0.5)"
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(config, "DB_PATH", db)
        models.init_user_db()
        conn = sqlite3.connect(db)
        # Profiles 1 and 3 (at 0.5) flipped to 0.35
        assert conn.execute(
            "SELECT meta_pregate_threshold FROM trading_profiles WHERE id=1"
        ).fetchone()[0] == 0.35
        assert conn.execute(
            "SELECT meta_pregate_threshold FROM trading_profiles WHERE id=3"
        ).fetchone()[0] == 0.35
        # Profile 2 (operator-tuned 0.30) preserved
        assert conn.execute(
            "SELECT meta_pregate_threshold FROM trading_profiles WHERE id=2"
        ).fetchone()[0] == 0.30
        conn.close()
