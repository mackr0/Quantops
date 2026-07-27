"""Sizing telemetry (P4.2, 2026-07-27 — operator decision: telemetry-first).

Kelly/vol numbers were computed and rendered as prompt English while
the AI's own size_pct proposal went unrecorded next to them — so
"does the AI size sensibly with honest inputs?" was unanswerable.
Decision (consistent with the P3.1 blind-era ruling): NO sizing
behavior change; record the raw divergence inputs on every SELECTED
trade's prediction row and judge at the ~2026-08-14 post-fix review.

Pinned here: the stamp exists in the selected-trade record path, the
Kelly reference is computed once per cycle, and the telemetry keys
can never leak into the meta-model's feature extractor.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _record_block(src):
    i = src.find("# Record a prediction for EVERY candidate")
    assert i != -1
    return src[i:i + 12000]


class TestTelemetryStamp:
    def test_selected_trades_record_sizing_inputs(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        block = _record_block(src)
        for key in ('_size_pct_ai', '_size_kelly_frac',
                    '_atr_pct_at_decision'):
            assert key in block, (
                f"{key} no longer stamped on selected trades — the "
                "P4.2 sizing-divergence review loses its data."
            )

    def test_kelly_computed_once_per_cycle_not_per_candidate(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        block = _record_block(src)
        head, loop = block.split("for c in candidates_data:", 1)
        assert "compute_kelly_recommendation" in head, (
            "the Kelly reference must be computed once before the "
            "candidate loop (it's a per-direction DB aggregate)."
        )
        assert "compute_kelly_recommendation" not in loop


class TestMetaModelStaysBlind:
    def test_telemetry_keys_not_trainable(self):
        """Telemetry-first means the meta model must NOT train on
        these keys — they describe the decision, not the setup."""
        from meta_model import NUMERIC_FEATURES, CATEGORICAL_FEATURES
        for key in ('_size_pct_ai', '_size_kelly_frac',
                    '_atr_pct_at_decision'):
            assert key not in NUMERIC_FEATURES
            assert key not in CATEGORICAL_FEATURES
