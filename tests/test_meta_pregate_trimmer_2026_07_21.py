"""2026-07-21 — the meta-pregate is a TRIMMER, not a gate.

Prod (EXP-A3, first cycle after the truth layer): "6 shortlisted →
meta-pregate dropped 6 → 0 candidates left for the AI". A meta-model
trained on one bad week scored everything below threshold and emptied
live cycles — the gate-as-allocator disease. Structural rule: the
pregate may drop at most the worst half (len//2); it can never zero
the flow.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _cands(n):
    return [{"symbol": f"S{i}", "signal": "BUY", "score": 3,
             "rsi": 50, "volume_ratio": 1.0} for i in range(n)]


def _ctx():
    ctx = MagicMock()
    ctx.enable_meta_model = True
    ctx.meta_pregate_threshold = 0.5
    ctx.profile_id = 219
    return ctx


def _run(cands, probs):
    """probs: symbol -> meta_prob returned by the mocked model."""
    import meta_model
    from trade_pipeline import _meta_pregate_candidates
    bundle = {"metrics": {"n_train_short": 100, "n_train_long": 100}}
    with patch.object(meta_model, "model_path_for_profile",
                      return_value="x"), \
         patch.object(meta_model, "load_model", return_value=bundle), \
         patch.object(meta_model, "predict_probability",
                      side_effect=lambda b, f: probs[f["symbol"]]):
        return _meta_pregate_candidates(cands, _ctx())


def test_model_hating_everything_cannot_empty_the_cycle():
    """The prod shape: all 6 below threshold → at least half survive,
    and the survivors are the HIGHEST meta_prob ones."""
    cands = _cands(6)
    probs = {"S0": 0.10, "S1": 0.40, "S2": 0.20, "S3": 0.45,
             "S4": 0.05, "S5": 0.30}
    kept = _run(cands, probs)
    assert len(kept) == 3, "6//2 = 3 max drops → 3 must survive"
    assert {c["symbol"] for c in kept} == {"S1", "S3", "S5"}, (
        "the spared survivors must be the least-bad by meta_prob")
    assert [c["symbol"] for c in kept] == ["S1", "S3", "S5"], (
        "original order preserved")


def test_single_candidate_always_survives():
    kept = _run(_cands(1), {"S0": 0.01})
    assert len(kept) == 1


def test_normal_trimming_within_cap_unchanged():
    """2 of 6 below threshold — ordinary behavior: both drop."""
    cands = _cands(6)
    probs = {"S0": 0.9, "S1": 0.1, "S2": 0.8, "S3": 0.2,
             "S4": 0.7, "S5": 0.6}
    kept = _run(cands, probs)
    assert {c["symbol"] for c in kept} == {"S0", "S2", "S4", "S5"}


def test_exact_cap_boundary_drops_all_below():
    """3 of 6 below threshold == cap → all three drop, three survive."""
    cands = _cands(6)
    probs = {"S0": 0.9, "S1": 0.1, "S2": 0.8, "S3": 0.2,
             "S4": 0.7, "S5": 0.3}
    kept = _run(cands, probs)
    assert {c["symbol"] for c in kept} == {"S0", "S2", "S4"}
