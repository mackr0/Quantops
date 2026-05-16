"""Tests for tuning_auto_expiry — the 4th permanent guardrail from
the 2026-05-14 over-restriction collapse.

The contract being pinned:

  Revert a gate-tightening change ONLY when:
    1. category(adjustment_type) == 'gate_tighten' — refinements
       (ATR multipliers, RSI thresholds, Layer-2 signal-weight
       intensity) MUST NOT be auto-expired
    2. timestamp >= 7 days old
    3. no subsequent change has touched the same parameter
    4. >= 20 predictions resolved since the change (evidence gate)
    5. outcome_after != 'improved'
  → revert + log 'auto_expiry_revert' row + mark original as
    outcome_after='auto_expired'

The evidence gate (#4) is the key design choice: time alone is not
sufficient. A tightening that hasn't accumulated samples doesn't
HAVE evidence yet and must defer the decision. Otherwise auto-
expiry becomes its own mechanical-over-time restriction — the
exact pattern this guardrail is meant to prevent.

Architectural maxim per Mack: "It shouldn't make it harder to
trade — it should make it harder to make BAD trades." A gate-
tightening with no measurable improvement in win rate is making
it harder to trade WITHOUT making it harder to make bad trades.
That's the case auto-expiry catches.
"""
from __future__ import annotations

import os
import sys
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ── Helpers ────────────────────────────────────────────────────────

def _utc_iso_days_ago(n):
    return (datetime.utcnow() - timedelta(days=n)).isoformat()


@pytest.fixture
def tmp_profile_db(tmp_path):
    """A minimal per-profile DB with ai_predictions + deprecated_strategies."""
    db = str(tmp_path / "quantopsai_profile_999.db")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, status TEXT,
                resolved_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE deprecated_strategies (
                strategy_type TEXT PRIMARY KEY,
                deprecated_at TEXT,
                reason TEXT,
                rolling_sharpe_at_deprecation REAL,
                lifetime_sharpe REAL,
                consecutive_bad_days INTEGER,
                restored_at TEXT
            )
        """)
        conn.commit()
    return db


def _seed_predictions(db, n_resolved, since_days_ago):
    """Insert N resolved predictions, each resolved between now and
    `since_days_ago` days ago — so they fall after a timestamp
    that's `since_days_ago + 1` days ago."""
    with closing(sqlite3.connect(db)) as conn:
        for i in range(n_resolved):
            resolved_at = _utc_iso_days_ago(max(0, since_days_ago - i))
            conn.execute(
                "INSERT INTO ai_predictions (symbol, status, resolved_at) "
                "VALUES (?, 'resolved', ?)",
                (f"SYM{i}", resolved_at),
            )
        conn.commit()


# ── Tests ──────────────────────────────────────────────────────────

class TestEligibilityRules:
    def test_refinement_is_NOT_eligible(self):
        """Pinning the most important boundary: refinements
        (ATR / RSI / signal-weight intensity) must never auto-expire.
        Reverting them would undo legitimate learning."""
        from tuning_auto_expiry import _is_eligible_for_revert
        row = {
            "adjustment_type": "atr_tp_tighten",  # REFINEMENT
            "timestamp": _utc_iso_days_ago(30),
            "outcome_after": "unchanged",
        }
        eligible, why = _is_eligible_for_revert(row, 100, 7, 20)
        assert not eligible
        assert "not a gate_tighten" in why

    def test_signal_weight_down_is_NOT_eligible(self):
        """signal_weight_down is a Layer-2 intensity change, not a
        gate. Must not auto-expire."""
        from tuning_auto_expiry import _is_eligible_for_revert
        row = {
            "adjustment_type": "signal_weight_down",
            "timestamp": _utc_iso_days_ago(30),
            "outcome_after": "unchanged",
        }
        eligible, why = _is_eligible_for_revert(row, 100, 7, 20)
        assert not eligible

    def test_gate_tighten_with_insufficient_samples_DEFERRED(self):
        """The key evidence gate: a tightening with <20 samples
        hasn't had a chance to prove itself. Must be SKIPPED, not
        reverted. Auto-expiry without evidence would itself become
        a mechanical-over-time restriction."""
        from tuning_auto_expiry import _is_eligible_for_revert
        row = {
            "adjustment_type": "correlation_tighten",
            "timestamp": _utc_iso_days_ago(30),
            "outcome_after": "unchanged",
        }
        eligible, why = _is_eligible_for_revert(row, 5, 7, 20)
        assert not eligible
        assert "insufficient samples" in why

    def test_gate_tighten_marked_improved_is_NOT_eligible(self):
        """Tightening worked. Keep it."""
        from tuning_auto_expiry import _is_eligible_for_revert
        row = {
            "adjustment_type": "correlation_tighten",
            "timestamp": _utc_iso_days_ago(30),
            "outcome_after": "improved",
        }
        eligible, why = _is_eligible_for_revert(row, 100, 7, 20)
        assert not eligible
        assert "improved" in why

    def test_gate_tighten_too_recent_is_NOT_eligible(self):
        from tuning_auto_expiry import _is_eligible_for_revert
        row = {
            "adjustment_type": "correlation_tighten",
            "timestamp": _utc_iso_days_ago(2),
            "outcome_after": "unchanged",
        }
        eligible, why = _is_eligible_for_revert(row, 100, 7, 20)
        assert not eligible
        assert "too recent" in why

    def test_already_auto_expired_is_NOT_eligible(self):
        """Idempotency. An auto-expired row should never be
        re-processed (would log an infinite loop of revert rows)."""
        from tuning_auto_expiry import _is_eligible_for_revert
        row = {
            "adjustment_type": "correlation_tighten",
            "timestamp": _utc_iso_days_ago(30),
            "outcome_after": "auto_expired",
        }
        eligible, why = _is_eligible_for_revert(row, 100, 7, 20)
        assert not eligible
        assert "auto-expired" in why

    def test_gate_tighten_with_enough_samples_AND_not_improved_IS_eligible(self):
        """The positive path. Tightening is old enough, has real
        evidence, didn't help → revert."""
        from tuning_auto_expiry import _is_eligible_for_revert
        for outcome in ("worsened", "unchanged", "pending", None):
            row = {
                "adjustment_type": "correlation_tighten",
                "timestamp": _utc_iso_days_ago(30),
                "outcome_after": outcome,
            }
            eligible, why = _is_eligible_for_revert(row, 100, 7, 20)
            assert eligible, (
                f"Should be eligible for outcome={outcome!r}; why={why}"
            )


class TestNewerChangeDetection:
    def test_skip_when_param_already_moved_later(self):
        """If a subsequent tuning row touched the same parameter,
        auto-expiry has nothing to do — the system has already
        decided. Prevents double-reverts."""
        from tuning_auto_expiry import _newer_change_exists_for_param
        history = [
            {"profile_id": 1, "parameter_name": "max_correlation",
             "timestamp": _utc_iso_days_ago(20)},
            {"profile_id": 1, "parameter_name": "max_correlation",
             "timestamp": _utc_iso_days_ago(5)},  # newer
            {"profile_id": 1, "parameter_name": "drawdown_pause_pct",
             "timestamp": _utc_iso_days_ago(2)},
        ]
        assert _newer_change_exists_for_param(
            history, 1, "max_correlation", _utc_iso_days_ago(20),
        ) is True

    def test_no_newer_change_returns_false(self):
        from tuning_auto_expiry import _newer_change_exists_for_param
        history = [
            {"profile_id": 1, "parameter_name": "max_correlation",
             "timestamp": _utc_iso_days_ago(20)},
        ]
        assert _newer_change_exists_for_param(
            history, 1, "max_correlation", _utc_iso_days_ago(20),
        ) is False

    def test_newer_change_on_different_param_does_NOT_count(self):
        from tuning_auto_expiry import _newer_change_exists_for_param
        history = [
            {"profile_id": 1, "parameter_name": "max_correlation",
             "timestamp": _utc_iso_days_ago(20)},
            {"profile_id": 1, "parameter_name": "drawdown_pause_pct",
             "timestamp": _utc_iso_days_ago(5)},  # different param
        ]
        assert _newer_change_exists_for_param(
            history, 1, "max_correlation", _utc_iso_days_ago(20),
        ) is False


class TestCastOldValue:
    def test_int(self):
        from tuning_auto_expiry import _cast_old_value
        assert _cast_old_value("80") == 80
        assert isinstance(_cast_old_value("80"), int)

    def test_float(self):
        from tuning_auto_expiry import _cast_old_value
        assert _cast_old_value("0.7") == 0.7

    def test_bool_normalization(self):
        from tuning_auto_expiry import _cast_old_value
        assert _cast_old_value("true") == 1
        assert _cast_old_value("false") == 0

    def test_text_fallback(self):
        from tuning_auto_expiry import _cast_old_value
        assert _cast_old_value("largecap") == "largecap"

    def test_empty_returns_none(self):
        from tuning_auto_expiry import _cast_old_value
        assert _cast_old_value("") is None
        assert _cast_old_value(None) is None


class TestSamplesCount:
    def test_counts_resolved_after_timestamp(self, tmp_profile_db):
        """Sanity check the sample-count primitive — the evidence
        gate that prevents premature auto-expiry."""
        from tuning_auto_expiry import _count_resolved_predictions_since
        _seed_predictions(tmp_profile_db, n_resolved=50, since_days_ago=10)
        # The gate-tightening happened 14 days ago; samples in the
        # last 10 days should ALL count.
        n = _count_resolved_predictions_since(
            tmp_profile_db, _utc_iso_days_ago(14),
        )
        assert n == 50

    def test_excludes_resolved_before_change(self, tmp_profile_db):
        """Predictions resolved BEFORE the tightening are not
        evidence of its effect."""
        from tuning_auto_expiry import _count_resolved_predictions_since
        _seed_predictions(tmp_profile_db, n_resolved=10, since_days_ago=30)
        # Tightening 5 days ago — none of these 10 (all 25-30d old)
        # count toward post-change evidence.
        n = _count_resolved_predictions_since(
            tmp_profile_db, _utc_iso_days_ago(5),
        )
        assert n == 0
