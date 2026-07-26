"""Meta-pregate AUC floor (P1.6, 2026-07-26).

The pregate had no quality bar on the model wielding it: p216's
meta-model sat at holdout AUC 0.397 — ANTI-predictive — and still
trimmed its candidate shortlist every cycle. Below the floor
(META_PREGATE_MIN_AUC) the pregate is now AUDIT-ONLY: it still
scores every candidate (meta_model_score persistence keeps the
WR-by-quartile audit computable — the very evidence a retrain
needs) but drops nothing. Legacy bundles without an auc metric
keep the old trimming behavior.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _cands(n):
    return [{"symbol": f"S{i}", "signal": "BUY", "score": 3,
             "rsi": 50, "volume_ratio": 1.0} for i in range(n)]


def _ctx():
    ctx = MagicMock()
    ctx.enable_meta_model = True
    ctx.meta_pregate_threshold = 0.5
    ctx.profile_id = 216
    return ctx


def _run(cands, probs, auc):
    import meta_model
    from trade_pipeline import _meta_pregate_candidates
    metrics = {"n_train_short": 100, "n_train_long": 100}
    if auc is not None:
        metrics["auc"] = auc
    bundle = {"metrics": metrics}
    with patch.object(meta_model, "model_path_for_profile",
                      return_value="x"), \
         patch.object(meta_model, "load_model", return_value=bundle), \
         patch.object(meta_model, "predict_probability",
                      side_effect=lambda b, f: probs[f["symbol"]]):
        return _meta_pregate_candidates(cands, _ctx())


LOW_PROBS = {"S0": 0.10, "S1": 0.40, "S2": 0.20, "S3": 0.45,
             "S4": 0.05, "S5": 0.30}


def test_anti_predictive_model_drops_nothing():
    """The p216 shape: AUC 0.397 wants to drop everything — audit-only
    mode keeps the full cohort."""
    kept = _run(_cands(6), LOW_PROBS, auc=0.397)
    assert len(kept) == 6, (
        "a model below the AUC floor trimmed candidates — the "
        "anti-predictive-model-with-a-knife bug is back."
    )


def test_audit_only_still_stamps_scores():
    """Falling open must NOT stop score persistence — the quartile
    audit is how a below-floor model earns (or loses) a retrain."""
    kept = _run(_cands(6), LOW_PROBS, auc=0.397)
    assert all(c.get("_meta_prob") is not None for c in kept)


def test_predictive_model_keeps_the_knife():
    kept = _run(_cands(6), LOW_PROBS, auc=0.70)
    assert len(kept) == 3  # half-cap still applies


def test_floor_boundary_trims():
    """Exactly at the floor = allowed to trim."""
    from trade_pipeline import META_PREGATE_MIN_AUC
    kept = _run(_cands(6), LOW_PROBS, auc=META_PREGATE_MIN_AUC)
    assert len(kept) == 3


def test_legacy_bundle_without_auc_keeps_old_behavior():
    kept = _run(_cands(6), LOW_PROBS, auc=None)
    assert len(kept) == 3
