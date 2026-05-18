"""Guardrails on parameter changes from the self-tuner.

Closes the over-restriction failure mode documented in
`project_self_tuner_overcorrection_2026_05_14` — a 14-day compounding
tightening cascade that killed stock entries entirely. The user's
auto-memory `feedback_self_tuner_must_drift_toward_trading` is the
operating principle these guardrails encode:

  - Per-cycle delta cap (Phase 1 #1 of docs/17)
  - Reference-window invariant (Phase 1 #3 of docs/17)
  - Trade-count auto-loosen floor + auto-expiry on restrictions +
    anomaly alert are tested separately as they require the
    full data fixture.
"""
from __future__ import annotations

import pytest


class TestClampDeltaPerCycle:
    """Per-cycle delta cap — no single adjustment exceeds 25% of the
    current value in either direction. Stops the 2026-05-14 cascade
    at its source."""

    def test_no_clamp_within_band(self):
        from self_tuning import _clamp_delta
        clamped, was_clamped, reason = _clamp_delta(
            "max_position_pct", 0.08, 0.085,
        )
        assert clamped == 0.085
        assert was_clamped is False
        assert reason == ""

    def test_clamp_tighten_25pct(self):
        from self_tuning import _clamp_delta
        # Proposed 50% cut → clamp to 25% cut
        clamped, was_clamped, reason = _clamp_delta(
            "max_position_pct", 0.08, 0.04,
        )
        assert was_clamped is True
        assert clamped == pytest.approx(0.06, abs=1e-9)  # 0.08 * (1 - 0.25)
        assert "per-cycle delta cap" in reason
        assert "max_position_pct" in reason

    def test_clamp_loosen_25pct(self):
        from self_tuning import _clamp_delta
        # Proposed 50% increase → clamp to 25% increase
        clamped, was_clamped, _ = _clamp_delta(
            "ai_confidence_threshold", 60, 90,
        )
        assert was_clamped is True
        assert clamped == pytest.approx(75.0, abs=1e-9)  # 60 * 1.25

    def test_old_value_zero_returns_proposed(self):
        """Can't compute % change from 0. Pass through unchanged."""
        from self_tuning import _clamp_delta
        clamped, was_clamped, _ = _clamp_delta(
            "some_param", 0, 0.5,
        )
        assert clamped == 0.5
        assert was_clamped is False

    def test_equal_values_returns_unchanged(self):
        from self_tuning import _clamp_delta
        clamped, was_clamped, _ = _clamp_delta(
            "max_position_pct", 0.08, 0.08,
        )
        assert clamped == 0.08
        assert was_clamped is False

    def test_string_inputs_handled_gracefully(self):
        """tuning_history stores values as TEXT — caller may pass
        strings. Convert internally, don't crash."""
        from self_tuning import _clamp_delta
        clamped, was_clamped, _ = _clamp_delta(
            "max_position_pct", "0.08", "0.04",
        )
        assert was_clamped is True
        assert clamped == pytest.approx(0.06, abs=1e-9)

    def test_custom_max_pct(self):
        """Caller can pass a stricter cap for sensitive params."""
        from self_tuning import _clamp_delta
        # 10% cap; proposed 50% cut → clamp to 10% cut
        clamped, was_clamped, _ = _clamp_delta(
            "max_position_pct", 0.08, 0.04, max_pct_change=0.10,
        )
        assert was_clamped is True
        assert clamped == pytest.approx(0.072, abs=1e-9)  # 0.08 * 0.9

    def test_per_cycle_cap_alone_slows_but_does_not_stop_cascade(self):
        """Important calibration test: per-cycle cap alone is
        NECESSARY but NOT SUFFICIENT. 14 cycles × 25% cap compounds to
        0.10 * 0.75^14 ≈ 0.00178 — still catastrophic. The
        reference-window invariant (separately tested) is what
        actually prevents the cascade from going past safety.
        This test documents the per-cycle cap's intentional weakness
        so future maintainers know why the reference-window layer
        was added."""
        from self_tuning import _clamp_delta
        val = 0.10
        for _ in range(14):
            val, _, _ = _clamp_delta(
                "max_position_pct", val, val * 0.5,
            )
        # 0.10 * 0.75 ** 14 ≈ 0.001779. Per-cycle cap alone is
        # roughly an order-of-magnitude help but doesn't bound the
        # cascade to safety.
        assert 0.001 < val < 0.003, (
            f"Per-cycle cap math regression: expected ~0.00178 after "
            f"14 cycles of 0.5x proposed cuts, got {val}. The cap is "
            f"applying differently than designed."
        )


class TestReferenceWindowInvariant:
    """Reference-window invariant — no parameter can drift more than
    ±50% from its day-1 value without operator override. Catches the
    cascade case where per-cycle cap is honored but compounding still
    builds past safe bounds over weeks."""

    def test_no_clamp_within_window(self):
        from self_tuning import _within_reference_window
        clamped, was_clamped, _ = _within_reference_window(
            "max_position_pct", reference_value=0.10, proposed_value=0.08,
        )
        assert clamped == 0.08
        assert was_clamped is False

    def test_clamp_below_floor(self):
        """Reference 0.10, floor at -50% = 0.05. Proposed 0.02 → 0.05."""
        from self_tuning import _within_reference_window
        clamped, was_clamped, reason = _within_reference_window(
            "max_position_pct", 0.10, 0.02,
        )
        assert was_clamped is True
        assert clamped == pytest.approx(0.05, abs=1e-9)
        assert "reference-window" in reason

    def test_clamp_above_ceiling(self):
        """Reference 60, ceiling at +50% = 90. Proposed 100 → 90."""
        from self_tuning import _within_reference_window
        clamped, was_clamped, _ = _within_reference_window(
            "ai_confidence_threshold", 60, 100,
        )
        assert was_clamped is True
        assert clamped == pytest.approx(90.0, abs=1e-9)

    def test_no_reference_passes_through(self):
        """When no day-1 baseline has been recorded yet, the
        invariant can't fire — pass the proposed value through."""
        from self_tuning import _within_reference_window
        clamped, was_clamped, _ = _within_reference_window(
            "param", None, 0.5,
        )
        assert clamped == 0.5
        assert was_clamped is False

    def test_combined_with_per_cycle_cap_caps_cascade(self):
        """The full cascade scenario: 14 cycles. Per-cycle cap (25%)
        runs first, reference-window (50%) is the final safety net.
        Expected behavior: per-cycle cap shrinks ~25% per step, but
        reference-window invariant clamps to 0.05 (50% of 0.10) and
        the value stops there."""
        from self_tuning import _clamp_delta, _within_reference_window
        ref = 0.10
        val = 0.10
        for _ in range(14):
            # Adversary proposes 50% cut each cycle
            proposed = val * 0.5
            # Step 1: per-cycle cap
            after_cap, _, _ = _clamp_delta("p", val, proposed)
            # Step 2: reference window
            after_ref, _, _ = _within_reference_window("p", ref, after_cap)
            val = after_ref
        # With both guardrails the value can't drop below the
        # reference floor (0.05).
        assert val == pytest.approx(0.05, abs=1e-9), (
            f"After 14 cycles with both guardrails, expected ~0.05 "
            f"(the reference floor). Got {val}."
        )
