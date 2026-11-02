"""Confidence-tiered position sizing — flat since 2026-11-02.

The 2026-05-12 ladder (0.7× < 60, 1.2× 70s, 1.5× 80+) assumed the AI's
confidence was calibrated. On the 10,532 directional predictions
resolved since the 2026-08-24 reset it is inverted (80–89 the worst
bucket). These tests pin the retirement: every multiplier is 1.0, the
API and the None/invalid handling are unchanged, and re-introducing a
ladder is a deliberate decision (CONFIDENCE_SIZING_FLAT flips).
"""
from __future__ import annotations

import pytest

from confidence_sizing import (
    confidence_multiplier, apply_confidence_sizing, CONFIDENCE_SIZING_FLAT,
    CONFIDENCE_TIERS,
)


class TestFlatLadder:
    @pytest.mark.parametrize("conf", [0, 30, 45, 59.9, 60, 65, 69.9, 70, 75, 79.9, 80, 90, 100])
    def test_every_bucket_is_one(self, conf):
        assert confidence_multiplier(conf) == 1.0

    def test_flag_and_table_agree(self):
        assert CONFIDENCE_SIZING_FLAT is True
        assert all(m == 1.0 for _lo, m in CONFIDENCE_TIERS)

    def test_none_and_invalid_return_baseline(self):
        assert confidence_multiplier(None) == 1.0
        assert confidence_multiplier("not a number") == 1.0


class TestApplyConfidenceSizing:
    def test_alloc_unchanged_at_any_confidence(self):
        assert apply_confidence_sizing(0.05, 65, 0.10) == 0.05
        assert apply_confidence_sizing(0.05, 85, 0.10) == 0.05
        assert apply_confidence_sizing(0.05, 45, 0.10) == 0.05
        assert apply_confidence_sizing(0.05, None, 0.10) == 0.05

    def test_cap_still_applies(self):
        assert apply_confidence_sizing(0.12, 85, 0.10) == 0.10


class TestTradePipelineIntegration:
    def test_import_path_matches_caller(self):
        from confidence_sizing import apply_confidence_sizing  # noqa
        assert callable(apply_confidence_sizing)
