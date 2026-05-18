"""Item 3 of docs/17 Phase 1 — reference-window invariant persistence.

The per-cycle delta cap (Item 1) slows but doesn't stop a 14-day
compounding cascade. The reference-window invariant clamps proposed
values that drift more than ±50% from a day-1 reference. This file
covers the persistence layer (`param_references` table + helpers)
and the wrapper integration that uses them.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture
def configured_db(tmp_path, monkeypatch):
    """Point models.DB_PATH at a fresh per-test database with the
    tuning_history + param_references tables initialized via the
    real init_user_db() path."""
    import config
    db = str(tmp_path / "refs.db")
    monkeypatch.setattr(config, "DB_PATH", db)
    from models import init_user_db
    init_user_db(db)
    yield db


# ─────────────────────────────────────────────────────────────────────
# Persistence helpers in models.py
# ─────────────────────────────────────────────────────────────────────

class TestRecordReference:
    def test_first_insert_returns_true(self, configured_db):
        from models import record_param_reference_if_absent
        ok = record_param_reference_if_absent(1, "ai_confidence_threshold", 60)
        assert ok is True

    def test_second_insert_returns_false(self, configured_db):
        """Idempotent — calling twice on the same (profile, param) is
        a no-op the second time. Behavior the wrapper depends on so
        it can safely call this every cycle."""
        from models import record_param_reference_if_absent
        assert record_param_reference_if_absent(1, "ai_confidence_threshold", 60) is True
        assert record_param_reference_if_absent(1, "ai_confidence_threshold", 70) is False

    def test_different_param_inserts_independently(self, configured_db):
        from models import record_param_reference_if_absent
        assert record_param_reference_if_absent(1, "ai_confidence_threshold", 60) is True
        assert record_param_reference_if_absent(1, "max_position_pct", 0.10) is True

    def test_different_profile_inserts_independently(self, configured_db):
        from models import record_param_reference_if_absent
        assert record_param_reference_if_absent(1, "ai_confidence_threshold", 60) is True
        assert record_param_reference_if_absent(2, "ai_confidence_threshold", 80) is True


class TestGetReference:
    def test_returns_none_when_missing(self, configured_db):
        from models import get_param_reference
        assert get_param_reference(1, "ai_confidence_threshold") is None

    def test_round_trip(self, configured_db):
        from models import (
            record_param_reference_if_absent, get_param_reference,
        )
        record_param_reference_if_absent(1, "ai_confidence_threshold", 60)
        assert get_param_reference(1, "ai_confidence_threshold") == 60.0

    def test_first_record_wins_on_subsequent_calls(self, configured_db):
        """If the wrapper sees old_value=60 first, then sees old_value=70
        on the next call, the reference must STILL be 60 — that's the
        invariant. Without it the reference would just track the
        cascading current value, defeating the guardrail."""
        from models import (
            record_param_reference_if_absent, get_param_reference,
        )
        record_param_reference_if_absent(1, "ai_confidence_threshold", 60)
        record_param_reference_if_absent(1, "ai_confidence_threshold", 70)
        record_param_reference_if_absent(1, "ai_confidence_threshold", 80)
        assert get_param_reference(1, "ai_confidence_threshold") == 60.0

    def test_returns_float_for_int_value(self, configured_db):
        """Stored as TEXT to keep the column type-agnostic; getter
        coerces to float for the clamp math."""
        from models import (
            record_param_reference_if_absent, get_param_reference,
        )
        record_param_reference_if_absent(1, "max_total_positions", 10)
        assert get_param_reference(1, "max_total_positions") == 10.0

    def test_returns_float_for_float_value(self, configured_db):
        from models import (
            record_param_reference_if_absent, get_param_reference,
        )
        record_param_reference_if_absent(1, "max_position_pct", 0.10)
        assert get_param_reference(1, "max_position_pct") == pytest.approx(0.10)


class TestClearReferences:
    def test_deletes_all_rows_for_profile(self, configured_db):
        """Reset script must wipe references — otherwise post-reset
        profile would be locked to pre-reset values."""
        from models import (
            record_param_reference_if_absent,
            get_param_reference,
            clear_param_references,
        )
        record_param_reference_if_absent(1, "ai_confidence_threshold", 60)
        record_param_reference_if_absent(1, "max_position_pct", 0.10)
        record_param_reference_if_absent(2, "ai_confidence_threshold", 50)

        deleted = clear_param_references(1)
        assert deleted == 2
        assert get_param_reference(1, "ai_confidence_threshold") is None
        assert get_param_reference(1, "max_position_pct") is None
        # Other profile untouched
        assert get_param_reference(2, "ai_confidence_threshold") == 50.0

    def test_returns_zero_for_profile_with_no_references(self, configured_db):
        from models import clear_param_references
        assert clear_param_references(99) == 0


# ─────────────────────────────────────────────────────────────────────
# Wrapper integration — _apply_param_change consults references
# ─────────────────────────────────────────────────────────────────────

class TestWrapperUsesReferences:
    def test_first_call_records_old_value_as_reference(self, configured_db, monkeypatch):
        from unittest.mock import MagicMock
        utp = MagicMock()
        ltc = MagicMock(return_value=1)
        monkeypatch.setattr("models.update_trading_profile", utp)
        monkeypatch.setattr("models.log_tuning_change", ltc)
        from self_tuning import _apply_param_change
        from models import get_param_reference

        _apply_param_change(
            profile_id=1, user_id=1,
            adjustment_type="test", param_name="ai_confidence_threshold",
            old_value=60, proposed_new_value=66,
            reason="testing",
        )
        # Reference snapshot was taken from old_value
        assert get_param_reference(1, "ai_confidence_threshold") == 60.0

    def test_reference_window_clamps_excessive_drift(self, configured_db, monkeypatch):
        """Reference 60, proposed 95 (+58%) → reference-window clamps
        to 90 (+50% ceiling)."""
        from unittest.mock import MagicMock
        utp = MagicMock()
        ltc = MagicMock(return_value=1)
        monkeypatch.setattr("models.update_trading_profile", utp)
        monkeypatch.setattr("models.log_tuning_change", ltc)
        from self_tuning import _apply_param_change
        from models import record_param_reference_if_absent

        # Pre-seed the reference at 60 (simulating prior tuning runs)
        record_param_reference_if_absent(1, "ai_confidence_threshold", 60)

        # current = 80, propose 95. Per-cycle cap: 80*1.25 = 100 → no
        # clamp from Item 1 since 95 < 100. Reference-window: 60 + 50%
        # = 90, so 95 → 90.
        applied, was_clamped, suffix = _apply_param_change(
            profile_id=1, user_id=1,
            adjustment_type="test", param_name="ai_confidence_threshold",
            old_value=80, proposed_new_value=95,
            reason="testing",
        )
        assert was_clamped is True
        assert applied == pytest.approx(90.0)
        assert "guardrail" in suffix.lower()
        utp.assert_called_once_with(1, ai_confidence_threshold=90)

    def test_no_reference_passes_through(self, configured_db, monkeypatch):
        """When no prior reference exists, only the per-cycle cap
        applies. The first call records old_value as the new
        reference for SUBSEQUENT calls — it doesn't constrain
        itself."""
        from unittest.mock import MagicMock
        utp = MagicMock()
        ltc = MagicMock(return_value=1)
        monkeypatch.setattr("models.update_trading_profile", utp)
        monkeypatch.setattr("models.log_tuning_change", ltc)
        from self_tuning import _apply_param_change

        # First call: no prior reference. Proposal within per-cycle cap.
        applied, was_clamped, _ = _apply_param_change(
            profile_id=1, user_id=1,
            adjustment_type="test", param_name="ai_confidence_threshold",
            old_value=60, proposed_new_value=70,
            reason="testing",
        )
        # 70 is within 60*1.25=75, so per-cycle cap allows. No prior
        # reference, so reference-window can't fire.
        assert was_clamped is False
        assert applied == 70.0

    def test_per_cycle_cap_and_reference_window_compose(self, configured_db, monkeypatch):
        """Adversarial proposer asks for a 200% jump; per-cycle cap
        clamps to +25%, then reference-window further clamps to +50%
        from reference. Verifies both layers run in sequence."""
        from unittest.mock import MagicMock
        utp = MagicMock()
        ltc = MagicMock(return_value=1)
        monkeypatch.setattr("models.update_trading_profile", utp)
        monkeypatch.setattr("models.log_tuning_change", ltc)
        from self_tuning import _apply_param_change
        from models import record_param_reference_if_absent

        # Reference 60, current already at 70 (snuck up before
        # references existed). Adversary proposes 200.
        record_param_reference_if_absent(1, "ai_confidence_threshold", 60)
        applied, was_clamped, _ = _apply_param_change(
            profile_id=1, user_id=1,
            adjustment_type="test", param_name="ai_confidence_threshold",
            old_value=70, proposed_new_value=200,
            reason="adversarial",
        )
        # Per-cycle cap: 70 * 1.25 = 87.5
        # Reference window: 60 * 1.5 = 90 (ceiling)
        # 87.5 ≤ 90, so per-cycle cap is the binding constraint.
        assert was_clamped is True
        assert applied == pytest.approx(87.5)

    def test_reference_clamp_logged_in_reason(self, configured_db, monkeypatch):
        """When the reference-window guardrail fires, the tuning_history
        reason must explain it — operator audit trail."""
        captured = []

        def fake_log(profile_id, user_id, atype, pname, old, new, reason,
                     **kwargs):
            captured.append(reason)
            return 1

        monkeypatch.setattr("models.update_trading_profile", lambda *a, **kw: None)
        monkeypatch.setattr("models.log_tuning_change", fake_log)

        from self_tuning import _apply_param_change
        from models import record_param_reference_if_absent

        record_param_reference_if_absent(1, "ai_confidence_threshold", 60)
        _apply_param_change(
            profile_id=1, user_id=1,
            adjustment_type="test", param_name="ai_confidence_threshold",
            old_value=80, proposed_new_value=95,
            reason="some reason",
        )
        assert captured, "log_tuning_change must have been called"
        reason_text = captured[0].lower()
        assert "reference-window" in reason_text
        assert "guardrail" in reason_text


# ─────────────────────────────────────────────────────────────────────
# Cascade scenario — the original 14-day failure mode
# ─────────────────────────────────────────────────────────────────────

class TestCascadeStopsAtReference:
    def test_14_cycles_held_to_reference_floor(self, configured_db, monkeypatch):
        """The full original cascade: 14 cycles, each proposing a
        50% cut. With BOTH guardrails wired together via the wrapper,
        the value should stop at the reference floor (0.05) rather
        than spiraling toward zero.

        Per-cycle cap alone (from the calibration test in Item 1)
        leaves the value at ~0.00178 after 14 cycles — catastrophic.
        Adding the reference-window stops it at exactly 0.05.
        """
        from unittest.mock import MagicMock
        utp = MagicMock()
        ltc = MagicMock(return_value=1)
        monkeypatch.setattr("models.update_trading_profile", utp)
        monkeypatch.setattr("models.log_tuning_change", ltc)
        from self_tuning import _apply_param_change

        # The wrapper records the FIRST observed old_value as the
        # reference. Simulate that by calling once with old_value=0.10
        # — that snapshot becomes the day-1 reference for all
        # subsequent cycles.
        current = 0.10
        for cycle in range(14):
            applied, _, _ = _apply_param_change(
                profile_id=1, user_id=1,
                adjustment_type="cascade_sim",
                param_name="max_position_pct",
                old_value=current, proposed_new_value=current * 0.5,
                reason=f"cycle {cycle}",
            )
            current = applied

        # With both guardrails the final value must be the reference
        # floor (0.05). Without the reference-window invariant the
        # value would be ~0.00178 (the per-cycle-cap-only outcome).
        assert current == pytest.approx(0.05, abs=1e-9), (
            f"Reference-window failed to bound the cascade. Got "
            f"current={current}, expected reference-floor ~0.05."
        )
