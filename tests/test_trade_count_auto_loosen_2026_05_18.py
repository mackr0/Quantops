"""Item 2 of docs/17 Phase 1 — trade-count floor with auto-loosen.

Encodes `feedback_self_tuner_must_drift_toward_trading` as a hard
rule: when weekly stock entries fall below the floor, the most-
restrictive entry-filter parameter is FORCED to loosen by ~25%
(within the Item 1 per-cycle delta cap). Stops the 2026-05-14
cascade from re-occurring: even if every other loosener self-
skips for lack of data, this rule still acts.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_db(tmp_path):
    db = str(tmp_path / "tcl.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, side TEXT, qty REAL, price REAL,
            pnl REAL, signal_type TEXT, occ_symbol TEXT,
            status TEXT DEFAULT 'closed', data_quality TEXT
        );
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT DEFAULT (datetime('now')),
            status TEXT, actual_outcome TEXT, confidence REAL
        );
        CREATE TABLE tuning_history (
            id INTEGER PRIMARY KEY,
            profile_id INTEGER, user_id INTEGER,
            timestamp TEXT, adjustment_type TEXT,
            parameter_name TEXT, old_value TEXT, new_value TEXT,
            reason TEXT, win_rate_at_change REAL,
            predictions_resolved INTEGER,
            outcome_after TEXT DEFAULT 'pending'
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _ctx(db, **overrides):
    """Default ctx with every entry-filter parameter set near the
    MIDDLE of its bounds. Tests that need a specific param to be the
    most-restrictive should override that one toward the tight end."""
    defaults = dict(
        profile_id=1, user_id=1, db_path=db, enable_self_tuning=True,
        display_name="Test",
        # entry-filter params
        ai_confidence_threshold=50,
        min_volume=1_000_000,
        volume_surge_multiplier=2.0,
        breakout_volume_threshold=1.5,
        gap_pct_threshold=3.0,
        momentum_5d_gain=5.0,
        momentum_20d_gain=5.0,
        avoid_earnings_days=2,
        skip_first_minutes=5,
        meta_pregate_threshold=0.35,
        max_total_positions=10,
        max_sector_positions=5,
        max_correlation=0.70,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _seed_entries(db, n_entries, days_ago=1):
    """Insert n entry rows (side IN ('buy','short') with a signal_type
    that the floor query counts). All within the last 7 days."""
    conn = sqlite3.connect(db)
    when = (datetime.utcnow() - timedelta(days=days_ago)).isoformat()
    for i in range(n_entries):
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "signal_type, status) "
            "VALUES (?, ?, 'buy', 100, 50, 'BUY', 'open')",
            (when, f"S{i}"),
        )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# Helpers — _restriction_score / _loosen_target
# ─────────────────────────────────────────────────────────────────────

class TestRestrictionScore:
    def test_loose_end_scores_zero(self):
        from self_tuning import _restriction_score
        # ai_confidence_threshold bounds (10, 90), "up" = restrictive.
        # At 10 it is fully loose.
        assert _restriction_score("ai_confidence_threshold", 10) == pytest.approx(0.0)

    def test_tight_end_scores_one(self):
        from self_tuning import _restriction_score
        assert _restriction_score("ai_confidence_threshold", 90) == pytest.approx(1.0)

    def test_midpoint_scores_half(self):
        from self_tuning import _restriction_score
        # (50-10)/(90-10) = 0.5
        assert _restriction_score("ai_confidence_threshold", 50) == pytest.approx(0.5)

    def test_down_direction_max_total_positions(self):
        from self_tuning import _restriction_score
        # max_total_positions bounds (3, 25), "down" = restrictive.
        # At 25 (loose end) score is 0; at 3 (tight end) score is 1.
        assert _restriction_score("max_total_positions", 25) == pytest.approx(0.0)
        assert _restriction_score("max_total_positions", 3) == pytest.approx(1.0)

    def test_unknown_param_returns_zero(self):
        from self_tuning import _restriction_score
        assert _restriction_score("not_a_real_param", 0.5) == 0.0


class TestLoosenTarget:
    def test_loosens_up_direction_param_downward(self):
        from self_tuning import _loosen_target
        # ai_confidence_threshold "up" restrictive → loosen = downward.
        # int param, 60 * 0.75 = 45.0 → int 45.
        assert _loosen_target("ai_confidence_threshold", 60) == 45

    def test_loosens_down_direction_param_upward(self):
        from self_tuning import _loosen_target
        # max_total_positions "down" restrictive → loosen = upward.
        # int param, 8 * 1.25 = 10.0 → int 10.
        assert _loosen_target("max_total_positions", 8) == 10

    def test_no_op_when_cast_collapses_to_current(self):
        from self_tuning import _loosen_target
        # int param at 4: 4 * 1.25 = 5.0 → int 5 → no, wait, 5 != 4.
        # Try 3 → 3*1.25=3.75 → int 3 → equals current → None
        assert _loosen_target("max_total_positions", 3) is None

    def test_returns_none_for_unknown_param(self):
        from self_tuning import _loosen_target
        assert _loosen_target("not_a_real_param", 0.5) is None

    def test_respects_param_bounds(self):
        from self_tuning import _loosen_target
        # ai_confidence_threshold floor 10. From 11, 11*0.75 = 8.25
        # which is below floor → bound clamps to 10 → cast int → 10
        assert _loosen_target("ai_confidence_threshold", 11) == 10

    def test_float_param_preserves_precision(self):
        from self_tuning import _loosen_target
        # volume_surge_multiplier float, 2.0 * 0.75 = 1.5
        assert _loosen_target("volume_surge_multiplier", 2.0) == pytest.approx(1.5)


# ─────────────────────────────────────────────────────────────────────
# Trigger — only fires below the floor
# ─────────────────────────────────────────────────────────────────────

class TestTriggerFloor:
    def test_no_op_when_entries_at_floor(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=3)  # at the floor exactly
        ctx = _ctx(db)
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None

    def test_no_op_when_entries_above_floor(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=10)
        ctx = _ctx(db)
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None

    def test_fires_when_entries_below_floor(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=1)  # below floor
        ctx = _ctx(db, ai_confidence_threshold=80)  # very restrictive
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change"):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is not None
        assert "AUTO-LOOSEN" in msg
        # 80 * 0.75 = 60 (int)
        utp.assert_called_once_with(1, ai_confidence_threshold=60)

    def test_fires_when_no_entries_at_all(self, tmp_path):
        db = _make_db(tmp_path)
        # zero entries — definitely below floor
        ctx = _ctx(db, ai_confidence_threshold=80)
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change"):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is not None
        assert "AUTO-LOOSEN" in msg
        utp.assert_called_once()


# ─────────────────────────────────────────────────────────────────────
# Selection — picks the most-restrictive eligible parameter
# ─────────────────────────────────────────────────────────────────────

class TestSelection:
    def test_picks_highest_restriction_score(self, tmp_path):
        """ai_confidence_threshold at 88 (score 0.975) should beat
        max_total_positions at 10 (score 0.68)."""
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=0)
        ctx = _ctx(db,
                    ai_confidence_threshold=88,  # very restrictive
                    max_total_positions=10)       # moderate
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change"):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is not None
        # 88 * 0.75 = 66.0 → int 66
        utp.assert_called_once_with(1, ai_confidence_threshold=66)

    def test_alphabetical_tie_break(self, tmp_path):
        """When two params are equally restrictive, tie-break on
        parameter name (alphabetical). Use two params we can pin to
        identical 50% restriction scores."""
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=0)
        # avoid_earnings_days mid = (0+7)/2 = 3.5 → use 4 = score
        # (4-0)/7 = 0.571. min_volume mid for same score: lo=100K,
        # hi=5M, score = (v-100K)/4.9M = 0.571 → v = 2_898_571.
        # Just put both at deterministic high scores; alphabet wins.
        ctx = _ctx(db,
                    avoid_earnings_days=4,   # score 0.571
                    momentum_5d_gain=9.0,    # score (9-1)/14 = 0.571
                    # zero everything else's restriction by setting to loose end
                    ai_confidence_threshold=10,
                    min_volume=100_000,
                    volume_surge_multiplier=1.0,
                    breakout_volume_threshold=0.5,
                    gap_pct_threshold=1.0,
                    momentum_20d_gain=1.0,
                    skip_first_minutes=0,
                    meta_pregate_threshold=0.15,
                    max_total_positions=25,
                    max_sector_positions=10,
                    max_correlation=0.95,
                    )
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change"):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        # avoid_earnings_days < momentum_5d_gain alphabetically — wins tie
        utp.assert_called_once()
        kw = utp.call_args.kwargs
        assert "avoid_earnings_days" in kw

    def test_no_op_when_all_params_at_loose_end(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=0)
        # Every entry filter set to its loose-end value
        ctx = _ctx(db,
                    ai_confidence_threshold=10,
                    min_volume=100_000,
                    volume_surge_multiplier=1.0,
                    breakout_volume_threshold=0.5,
                    gap_pct_threshold=1.0,
                    momentum_5d_gain=1.0,
                    momentum_20d_gain=1.0,
                    avoid_earnings_days=0,
                    skip_first_minutes=0,
                    meta_pregate_threshold=0.15,
                    max_total_positions=25,
                    max_sector_positions=10,
                    max_correlation=0.95,
                    )
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change"):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None
        utp.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Cooldown — skip params adjusted in the last 24 hours
# ─────────────────────────────────────────────────────────────────────

class TestCooldown:
    def test_skips_param_adjusted_in_last_24h(self, tmp_path):
        """The single restrictive param has a recent adjustment — rule
        should fall through to the next candidate, not the one in
        cooldown."""
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=0)
        ctx = _ctx(db,
                    ai_confidence_threshold=88,   # most restrictive
                    min_volume=4_000_000)          # next-most
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )

        def fake_recent(profile_id, param_name, days=3):
            # Pretend ai_confidence_threshold was just adjusted
            if param_name == "ai_confidence_threshold":
                return {"timestamp": "2026-05-18T12:00:00",
                        "parameter_name": param_name}
            return None

        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", side_effect=fake_recent), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change"):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        # Should have fallen through to min_volume — 4M * 0.75 = 3M
        utp.assert_called_once_with(1, min_volume=3_000_000)
        assert msg is not None
        assert "min_volume" in msg.lower() or "Min Volume" in msg

    def test_no_op_when_only_candidate_is_in_cooldown(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=0)
        # Only one param is restrictive; others at loose end
        ctx = _ctx(db,
                    ai_confidence_threshold=80,
                    min_volume=100_000,
                    volume_surge_multiplier=1.0,
                    breakout_volume_threshold=0.5,
                    gap_pct_threshold=1.0,
                    momentum_5d_gain=1.0,
                    momentum_20d_gain=1.0,
                    avoid_earnings_days=0,
                    skip_first_minutes=0,
                    meta_pregate_threshold=0.15,
                    max_total_positions=25,
                    max_sector_positions=10,
                    max_correlation=0.95,
                    )
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        conn = _get_conn(db)
        # ai_confidence_threshold is in cooldown — and is the only candidate
        with patch("self_tuning._get_recent_adjustment",
                    return_value={"parameter_name": "ai_confidence_threshold"}), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change"):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None
        utp.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Integration — wrapper records the change and respects per-cycle cap
# ─────────────────────────────────────────────────────────────────────

class TestWrapperIntegration:
    def test_change_logged_to_tuning_history(self, tmp_path):
        """The rule must route through _apply_param_change so the
        change appears in tuning_history. Without it, downstream
        cooldown checks (and the docs/17 Item 4 auto-expiry helper)
        wouldn't see this loosen ever happened."""
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=0)
        ctx = _ctx(db, ai_confidence_threshold=80)
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        captured_log = []

        def fake_log(profile_id, user_id, atype, pname, old, new, reason,
                     **kwargs):
            captured_log.append({
                "adjustment_type": atype,
                "parameter_name": pname,
                "old_value": old,
                "new_value": new,
                "reason": reason,
            })
            return 1

        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile"), \
             patch("models.log_tuning_change", side_effect=fake_log):
            _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert len(captured_log) == 1
        entry = captured_log[0]
        assert entry["adjustment_type"] == "trade_count_auto_loosen"
        assert entry["parameter_name"] == "ai_confidence_threshold"
        assert "trade-count floor breach" in entry["reason"].lower()

    def test_per_cycle_cap_does_not_clamp_normal_loosen(self, tmp_path):
        """A 25% loosen is exactly at the per-cycle cap (Item 1).
        The 1e-9 boundary tolerance in `_clamp_delta` lets it through
        without firing the clamp. Verifies Items 1 and 2 compose
        cleanly — otherwise every auto-loosen would land with a
        '(clamped by guardrail)' suffix."""
        db = _make_db(tmp_path)
        _seed_entries(db, n_entries=0)
        ctx = _ctx(db, ai_confidence_threshold=80)
        from self_tuning import (
            _optimize_trade_count_auto_loosen, _get_conn,
        )
        conn = _get_conn(db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile"), \
             patch("models.log_tuning_change"):
            msg = _optimize_trade_count_auto_loosen(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is not None
        # If the clamp fired, msg would carry the suffix
        assert "clamped by guardrail" not in msg


# ─────────────────────────────────────────────────────────────────────
# Registry — the optimizer is tagged LOOSEN so it fires FIRST
# ─────────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_optimizer_tagged_loosen(self):
        from self_tuning import _OPTIMIZER_DIRECTION
        assert _OPTIMIZER_DIRECTION.get("_optimize_trade_count_auto_loosen") == "LOOSEN"

    def test_optimizer_in_upward_registry(self):
        """Walk the source for the registration. Without this the
        rule would never fire from `_apply_upward_optimizations`."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "self_tuning.py").read_text()
        assert "_optimize_trade_count_auto_loosen," in src, (
            "Auto-loosen rule must appear in the all_optimizers "
            "list inside _apply_upward_optimizations"
        )
