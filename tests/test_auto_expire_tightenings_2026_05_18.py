"""Item 4 of docs/17 Phase 1 — auto-expiry on restrictions.

Every tightening event in `tuning_history` carries a 14-day TTL.
After that, if the event hasn't been classified as 'improved' by
`review_past_adjustments`, the `_optimize_auto_expire_old_tightenings`
optimizer walks the parameter one cap-bounded step back toward the
pre-tightening value. The rule re-fires across cycles until each
tightening is fully unwound or marked expired.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def configured_db(tmp_path, monkeypatch):
    """Fresh per-test DB with tuning_history + param_references."""
    import config
    db = str(tmp_path / "exp.db")
    monkeypatch.setattr(config, "DB_PATH", db)
    from models import init_user_db
    init_user_db(db)
    return db


def _ctx(**overrides):
    defaults = dict(
        profile_id=1, user_id=1, db_path=":memory:",
        display_name="Test",
        ai_confidence_threshold=80,
        min_volume=2_000_000,
        max_position_pct=0.05,
        max_total_positions=5,
        stop_loss_pct=0.02,
        drawdown_pause_pct=0.12,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _insert_tightening(db, *, profile_id=1, param_name="ai_confidence_threshold",
                        old_value="60", new_value="80",
                        age_days=20, outcome_after="pending",
                        expired_at=None):
    """Synthesize a tuning_history row directly so we don't have to
    spin up the full self_tuning code path."""
    conn = sqlite3.connect(db)
    ts = (datetime.utcnow() - timedelta(days=age_days)).isoformat()
    cur = conn.execute(
        """INSERT INTO tuning_history
           (profile_id, user_id, timestamp, adjustment_type, parameter_name,
            old_value, new_value, reason, win_rate_at_change,
            predictions_resolved, outcome_after, expired_at)
           VALUES (?, ?, ?, 'test_tighten', ?, ?, ?, 'synthetic', NULL, 30,
                   ?, ?)""",
        (profile_id, 1, ts, param_name,
         str(old_value), str(new_value), outcome_after, expired_at),
    )
    conn.commit()
    eid = cur.lastrowid
    conn.close()
    return eid


# ─────────────────────────────────────────────────────────────────────
# Direction inference + step helper
# ─────────────────────────────────────────────────────────────────────

class TestIsTightening:
    def test_up_direction_increase_is_tightening(self):
        from self_tuning import _is_tightening
        # ai_confidence_threshold "up" restrictive: 60→80 is tighten
        assert _is_tightening("ai_confidence_threshold", 60, 80) is True

    def test_up_direction_decrease_is_loosen(self):
        from self_tuning import _is_tightening
        assert _is_tightening("ai_confidence_threshold", 80, 60) is False

    def test_down_direction_decrease_is_tightening(self):
        from self_tuning import _is_tightening
        # max_position_pct "down" restrictive: 0.10→0.05 is tighten
        assert _is_tightening("max_position_pct", 0.10, 0.05) is True

    def test_down_direction_increase_is_loosen(self):
        from self_tuning import _is_tightening
        assert _is_tightening("max_position_pct", 0.05, 0.10) is False

    def test_unknown_param_returns_false(self):
        from self_tuning import _is_tightening
        # No direction registered → can't decide → must not classify
        # as a tightening (false negatives are safer than false positives)
        assert _is_tightening("never_existed", 1, 2) is False


class TestLoosenOneStepToward:
    def test_up_direction_steps_down_capped_at_target(self):
        from self_tuning import _loosen_one_step_toward
        # ai_confidence_threshold "up" restrictive — loosen = down.
        # current 80, target 60. 80 * 0.75 = 60. Cap at target = 60.
        assert _loosen_one_step_toward("ai_confidence_threshold", 80, 60) == pytest.approx(60.0)

    def test_up_direction_step_capped_when_25pct_overshoots(self):
        from self_tuning import _loosen_one_step_toward
        # current 100, target 90. 100*0.75=75 would overshoot past 90.
        # Cap at target = 90.
        assert _loosen_one_step_toward("ai_confidence_threshold", 100, 90) == pytest.approx(90.0)

    def test_down_direction_steps_up_capped_at_target(self):
        from self_tuning import _loosen_one_step_toward
        # max_position_pct "down" restrictive — loosen = up.
        # current 0.05, target 0.10. 0.05 * 1.25 = 0.0625. Below target.
        assert _loosen_one_step_toward("max_position_pct", 0.05, 0.10) == pytest.approx(0.0625)

    def test_returns_none_when_already_at_target(self):
        from self_tuning import _loosen_one_step_toward
        assert _loosen_one_step_toward("ai_confidence_threshold", 60, 60) is None


# ─────────────────────────────────────────────────────────────────────
# Persistence helpers in models.py
# ─────────────────────────────────────────────────────────────────────

class TestGetExpirableTightenings:
    def test_returns_empty_when_no_events(self, configured_db):
        from models import get_expirable_tightenings
        assert get_expirable_tightenings(1) == []

    def test_skips_recent_events(self, configured_db):
        _insert_tightening(configured_db, age_days=5)
        from models import get_expirable_tightenings
        # Event is only 5 days old < 14-day TTL
        assert get_expirable_tightenings(1, ttl_days=14) == []

    def test_returns_old_events(self, configured_db):
        eid = _insert_tightening(configured_db, age_days=20)
        from models import get_expirable_tightenings
        events = get_expirable_tightenings(1, ttl_days=14)
        assert len(events) == 1
        assert events[0]["id"] == eid

    def test_skips_improved_events(self, configured_db):
        """Tightenings whose `review_past_adjustments` classified as
        'improved' are evidence-backed — don't auto-expire them."""
        _insert_tightening(configured_db, age_days=20, outcome_after="improved")
        from models import get_expirable_tightenings
        assert get_expirable_tightenings(1, ttl_days=14) == []

    def test_includes_pending_and_unchanged(self, configured_db):
        _insert_tightening(configured_db, age_days=20,
                            outcome_after="pending",
                            param_name="min_volume",
                            old_value="500000", new_value="1000000")
        _insert_tightening(configured_db, age_days=20,
                            outcome_after="unchanged",
                            param_name="ai_confidence_threshold",
                            old_value="60", new_value="80")
        from models import get_expirable_tightenings
        events = get_expirable_tightenings(1, ttl_days=14, limit=10)
        assert len(events) == 2

    def test_skips_already_expired_events(self, configured_db):
        _insert_tightening(configured_db, age_days=20,
                            expired_at=datetime.utcnow().isoformat())
        from models import get_expirable_tightenings
        assert get_expirable_tightenings(1, ttl_days=14) == []

    def test_oldest_first(self, configured_db):
        eid_new = _insert_tightening(configured_db, age_days=15,
                                       param_name="min_volume")
        eid_old = _insert_tightening(configured_db, age_days=30,
                                       param_name="ai_confidence_threshold")
        from models import get_expirable_tightenings
        events = get_expirable_tightenings(1, ttl_days=14, limit=5)
        assert events[0]["id"] == eid_old, "Oldest event must be first"
        assert events[1]["id"] == eid_new


class TestMarkExpired:
    def test_marks_a_row(self, configured_db):
        eid = _insert_tightening(configured_db, age_days=20)
        from models import mark_tuning_event_expired
        assert mark_tuning_event_expired(eid) is True
        # Second mark is no-op (already non-NULL expired_at)
        assert mark_tuning_event_expired(eid) is False

    def test_returns_false_for_missing_id(self, configured_db):
        from models import mark_tuning_event_expired
        assert mark_tuning_event_expired(99999) is False


# ─────────────────────────────────────────────────────────────────────
# Optimizer — end-to-end fire path
# ─────────────────────────────────────────────────────────────────────

class TestOptimizerFires:
    def test_no_op_when_no_expirable_events(self, configured_db):
        from self_tuning import (
            _optimize_auto_expire_old_tightenings, _get_conn,
        )
        ctx = _ctx()
        conn = _get_conn(configured_db)
        with patch("self_tuning._get_recent_adjustment", return_value=None):
            msg = _optimize_auto_expire_old_tightenings(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None

    def test_fires_on_expired_pending_tightening(self, configured_db):
        # Old tightening: ai_confidence_threshold 60→80, 20 days ago
        _insert_tightening(configured_db, age_days=20)
        ctx = _ctx(ai_confidence_threshold=80)
        from self_tuning import (
            _optimize_auto_expire_old_tightenings, _get_conn,
        )
        conn = _get_conn(configured_db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change", return_value=2):
            msg = _optimize_auto_expire_old_tightenings(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is not None
        assert "AUTO-EXPIRE" in msg
        # 80 * 0.75 = 60, capped at target 60 → write 60
        utp.assert_called_once_with(1, ai_confidence_threshold=60)

    def test_does_not_fire_on_improved_tightening(self, configured_db):
        _insert_tightening(configured_db, age_days=20,
                            outcome_after="improved")
        ctx = _ctx(ai_confidence_threshold=80)
        from self_tuning import (
            _optimize_auto_expire_old_tightenings, _get_conn,
        )
        conn = _get_conn(configured_db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change", return_value=2):
            msg = _optimize_auto_expire_old_tightenings(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is None
        utp.assert_not_called()

    def test_skips_event_for_param_in_cooldown(self, configured_db):
        """The oldest expirable event is for ai_confidence_threshold
        but that param was tuned in the last 24h — fall through to
        the next event."""
        # min_volume is no longer an auto-expirable tightening (operator-only
        # universe floor, removed from _TIGHTENING_DIRECTION 2026-06-26). Use
        # gap_pct_threshold as the next tunable tightening to fall through to.
        _insert_tightening(configured_db, age_days=30,
                            param_name="ai_confidence_threshold",
                            old_value="60", new_value="80")
        _insert_tightening(configured_db, age_days=20,
                            param_name="gap_pct_threshold",
                            old_value="4.0", new_value="8.0")
        ctx = _ctx(ai_confidence_threshold=80, gap_pct_threshold=8.0)
        from self_tuning import (
            _optimize_auto_expire_old_tightenings, _get_conn,
        )

        def fake_recent(profile_id, param_name, days=3):
            return ({"parameter_name": param_name}
                    if param_name == "ai_confidence_threshold" else None)

        conn = _get_conn(configured_db)
        with patch("self_tuning._get_recent_adjustment", side_effect=fake_recent), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change", return_value=2):
            msg = _optimize_auto_expire_old_tightenings(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        # Should fall through to gap_pct_threshold — one 25% step back toward
        # the pre-tightening 4.0: 8.0 * 0.75 = 6.0.
        assert msg is not None
        utp.assert_called_once_with(1, gap_pct_threshold=6.0)


class TestExpiryMarking:
    def test_marks_expired_when_step_reaches_target(self, configured_db):
        """One-step loosen lands exactly at target → mark row expired."""
        eid = _insert_tightening(
            configured_db, age_days=20,
            param_name="ai_confidence_threshold",
            old_value="60", new_value="80",
        )
        ctx = _ctx(ai_confidence_threshold=80)
        from self_tuning import (
            _optimize_auto_expire_old_tightenings, _get_conn,
        )
        conn = _get_conn(configured_db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile"), \
             patch("models.log_tuning_change", return_value=2):
            _optimize_auto_expire_old_tightenings(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()

        check = sqlite3.connect(configured_db)
        row = check.execute(
            "SELECT expired_at FROM tuning_history WHERE id = ?",
            (eid,),
        ).fetchone()
        check.close()
        assert row[0] is not None, (
            "Tightening should be marked expired once the loosen step "
            "reaches the pre-tightening target."
        )

    def test_does_not_mark_expired_mid_walk(self, configured_db):
        """Multi-step walk: 100→60 needs more than one 25% step.
        First call lands at 75 (not at target 60) → leave row open."""
        eid = _insert_tightening(
            configured_db, age_days=20,
            param_name="ai_confidence_threshold",
            old_value="60", new_value="100",
        )
        ctx = _ctx(ai_confidence_threshold=100)
        from self_tuning import (
            _optimize_auto_expire_old_tightenings, _get_conn,
        )
        conn = _get_conn(configured_db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile"), \
             patch("models.log_tuning_change", return_value=2):
            msg = _optimize_auto_expire_old_tightenings(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        assert msg is not None
        # 100 * 0.75 = 75 (above target 60) — row stays open
        check = sqlite3.connect(configured_db)
        row = check.execute(
            "SELECT expired_at FROM tuning_history WHERE id = ?",
            (eid,),
        ).fetchone()
        check.close()
        assert row[0] is None, (
            "Tightening should remain open while the value is still "
            "above (or below, for down-direction) the target."
        )

    def test_marks_expired_when_current_already_past_target(self, configured_db):
        """The recorded tightening was 60→80, but the current value
        is 50 (somehow already past the pre-tightening value). Mark
        the row expired without taking any action."""
        eid = _insert_tightening(
            configured_db, age_days=20,
            param_name="ai_confidence_threshold",
            old_value="60", new_value="80",
        )
        ctx = _ctx(ai_confidence_threshold=50)
        from self_tuning import (
            _optimize_auto_expire_old_tightenings, _get_conn,
        )
        conn = _get_conn(configured_db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change", return_value=2):
            _optimize_auto_expire_old_tightenings(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()

        utp.assert_not_called()
        check = sqlite3.connect(configured_db)
        row = check.execute(
            "SELECT expired_at FROM tuning_history WHERE id = ?",
            (eid,),
        ).fetchone()
        check.close()
        assert row[0] is not None

    def test_marks_expired_for_event_with_unknown_param(self, configured_db):
        """A historical event for a param no longer in the direction map
        should be marked expired so we don't reconsider it every cycle."""
        eid = _insert_tightening(
            configured_db, age_days=20,
            param_name="never_a_real_param",
            old_value="1", new_value="2",
        )
        ctx = _ctx()
        from self_tuning import (
            _optimize_auto_expire_old_tightenings, _get_conn,
        )
        conn = _get_conn(configured_db)
        with patch("self_tuning._get_recent_adjustment", return_value=None), \
             patch("models.update_trading_profile") as utp, \
             patch("models.log_tuning_change", return_value=2):
            _optimize_auto_expire_old_tightenings(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=20)
        conn.close()
        utp.assert_not_called()
        check = sqlite3.connect(configured_db)
        row = check.execute(
            "SELECT expired_at FROM tuning_history WHERE id = ?",
            (eid,),
        ).fetchone()
        check.close()
        assert row[0] is not None


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_tagged_loosen(self):
        from self_tuning import _OPTIMIZER_DIRECTION
        assert _OPTIMIZER_DIRECTION.get(
            "_optimize_auto_expire_old_tightenings") == "LOOSEN"

    def test_in_upward_registry(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "self_tuning.py").read_text()
        assert "_optimize_auto_expire_old_tightenings," in src
