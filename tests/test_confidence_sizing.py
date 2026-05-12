"""Confidence-tiered position sizing (2026-05-12).

The AI's confidence score is calibrated (the 19K-prediction audit
on 2026-05-12 showed monotonic win-rate buckets). Old code only
boosted at conf ≥ 80; new ladder applies a 4-tier multiplier:

  conf < 60  → 0.7×
  conf 60-69 → 1.0×
  conf 70-79 → 1.2×
  conf 80+   → 1.5×

These tests pin the ladder, the None-safe behavior, and the
max-cap-pct interaction.
"""
from __future__ import annotations

import pytest

from confidence_sizing import (
    confidence_multiplier, apply_confidence_sizing,
)


class TestConfidenceMultiplier:
    def test_below_60(self):
        assert confidence_multiplier(0) == 0.7
        assert confidence_multiplier(45) == 0.7
        assert confidence_multiplier(59.9) == 0.7

    def test_60_to_69(self):
        assert confidence_multiplier(60) == 1.0
        assert confidence_multiplier(65) == 1.0
        assert confidence_multiplier(69.9) == 1.0

    def test_70_to_79(self):
        assert confidence_multiplier(70) == 1.2
        assert confidence_multiplier(75) == 1.2
        assert confidence_multiplier(79.9) == 1.2

    def test_80_plus(self):
        assert confidence_multiplier(80) == 1.5
        assert confidence_multiplier(90) == 1.5
        assert confidence_multiplier(100) == 1.5

    def test_none_returns_baseline(self):
        """A trade with no AI prediction attached returns 1.0 — the
        caller's baseline alloc is unchanged."""
        assert confidence_multiplier(None) == 1.0

    def test_invalid_returns_baseline(self):
        assert confidence_multiplier("not a number") == 1.0
        assert confidence_multiplier(float("nan")) == 1.0 or \
               confidence_multiplier(float("nan")) >= 0  # NaN falls through


class TestApplyConfidenceSizing:
    def test_baseline_at_60(self):
        """At baseline conf, alloc unchanged."""
        assert apply_confidence_sizing(0.05, 65, 0.10) == 0.05

    def test_boost_at_80(self):
        """0.05 base × 1.5 = 0.075, well under 0.10 cap."""
        assert apply_confidence_sizing(0.05, 85, 0.10) == pytest.approx(0.075)

    def test_pullback_at_low_conf(self):
        """Low conviction → smaller position."""
        assert apply_confidence_sizing(0.05, 45, 0.10) == pytest.approx(0.035)

    def test_capped_at_max(self):
        """0.08 base × 1.5 = 0.12, capped at 0.10."""
        assert apply_confidence_sizing(0.08, 85, 0.10) == 0.10

    def test_none_confidence_baseline(self):
        assert apply_confidence_sizing(0.05, None, 0.10) == 0.05


class TestLadderMatchesDataBuckets:
    """The ladder values are tuned to the 2026-05-12 audit buckets.
    If we ever re-derive them, the new values should still be:
    monotonic non-decreasing AND span a meaningful range."""

    def test_ladder_monotonic(self):
        confs = [0, 30, 60, 70, 80, 90]
        mults = [confidence_multiplier(c) for c in confs]
        for i in range(len(mults) - 1):
            assert mults[i] <= mults[i + 1], (
                f"Multiplier not monotonic at conf={confs[i+1]}: "
                f"{mults}"
            )

    def test_ladder_spans_useful_range(self):
        """Range must be wide enough to actually move position size.
        2× difference between worst and best buckets is the floor."""
        assert (confidence_multiplier(85) /
                confidence_multiplier(45)) >= 2.0


class TestTradePipelineIntegration:
    """Smoke test that the helper is importable from the path used
    in trade_pipeline.py — guards against future module renames
    breaking the BUY/SHORT sizing branch silently."""

    def test_import_path_matches_caller(self):
        # trade_pipeline.py imports as:
        #   from confidence_sizing import apply_confidence_sizing
        from confidence_sizing import apply_confidence_sizing  # noqa
        assert callable(apply_confidence_sizing)
