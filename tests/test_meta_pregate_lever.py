"""Guardrails for Lever 2 of COST_AND_QUALITY_LEVERS_PLAN.md —
meta-model pre-gate before the ensemble.

The pre-gate runs the meta-model on each shortlisted candidate
BEFORE the ensemble fires. Candidates with meta_prob < threshold
are dropped, saving specialist calls AND sharpening the cohort
the specialists analyze.

Tests:
1. No meta-model loaded → gate falls open (preserves cold-start).
2. Threshold 0.0 → gate disabled (returns all candidates).
3. Threshold 0.5 → candidates with meta_prob < 0.5 dropped,
   ≥0.5 kept.
4. predict_probability raising → fail-open at the per-candidate
   level (keeps the candidate).
5. Source-level: pipeline calls _meta_pregate_candidates BEFORE
   _get_shared_ensemble.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _ctx(threshold=0.5, profile_id=1):
    ctx = MagicMock()
    ctx.meta_pregate_threshold = threshold
    ctx.profile_id = profile_id
    return ctx


def _candidate(symbol, **kwargs):
    base = {
        "symbol": symbol, "signal": "BUY", "score": 3, "rsi": 50,
        "price": 100.0,
    }
    base.update(kwargs)
    return base


def test_no_model_loaded_falls_open():
    """Cold start: meta_model.load_model returns None.
    Pre-gate must return all candidates unchanged."""
    import trade_pipeline as tp
    cands = [_candidate("AAA"), _candidate("BBB"), _candidate("CCC")]

    with patch("meta_model.load_model", return_value=None):
        with patch("meta_model.model_path_for_profile", return_value="/tmp/x"):
            result = tp._meta_pregate_candidates(cands, _ctx())

    assert result == cands, (
        "With no meta-model trained, the pre-gate must return all "
        "candidates unchanged. Otherwise cold-start profiles can't "
        "trade."
    )


def test_threshold_zero_disables_gate():
    import trade_pipeline as tp
    cands = [_candidate("AAA"), _candidate("BBB")]

    # Threshold 0 → return all without even trying to load the model
    with patch("meta_model.load_model") as mock_load:
        result = tp._meta_pregate_candidates(cands, _ctx(threshold=0.0))

    assert result == cands
    assert mock_load.call_count == 0, (
        "Threshold 0 means 'gate disabled' — must short-circuit "
        "before even loading the meta-model."
    )


def test_drops_candidates_below_threshold():
    """Mock predict_probability per candidate. Candidates returning
    < 0.5 should be dropped; ≥ 0.5 should survive."""
    import trade_pipeline as tp
    cands = [
        _candidate("KEEP1"),  # will get prob 0.7
        _candidate("DROP1"),  # will get prob 0.3
        _candidate("KEEP2"),  # will get prob 0.9
        _candidate("DROP2"),  # will get prob 0.1
    ]
    probs = {"KEEP1": 0.7, "DROP1": 0.3, "KEEP2": 0.9, "DROP2": 0.1}

    def _fake_predict(bundle, features):
        return probs.get(features.get("symbol", ""), 0.5)

    with patch("meta_model.load_model", return_value={"model": "fake"}):
        with patch("meta_model.model_path_for_profile", return_value="/tmp/x"):
            with patch("meta_model.predict_probability",
                       side_effect=_fake_predict):
                result = tp._meta_pregate_candidates(cands, _ctx(threshold=0.5))

    surviving_syms = [c["symbol"] for c in result]
    assert surviving_syms == ["KEEP1", "KEEP2"], (
        f"Expected only KEEP1, KEEP2 to survive 0.5 threshold; got "
        f"{surviving_syms}"
    )


def test_predict_failure_falls_open_per_candidate():
    """If predict_probability raises for a specific candidate,
    we keep that candidate rather than dropping it on noise."""
    import trade_pipeline as tp
    cands = [_candidate("OK"), _candidate("ERR"), _candidate("ALSO_OK")]

    def _fake_predict(bundle, features):
        if features.get("symbol") == "ERR":
            raise RuntimeError("synthetic")
        return 0.8

    with patch("meta_model.load_model", return_value={"model": "fake"}):
        with patch("meta_model.model_path_for_profile", return_value="/tmp/x"):
            with patch("meta_model.predict_probability",
                       side_effect=_fake_predict):
                result = tp._meta_pregate_candidates(cands, _ctx(threshold=0.5))

    assert {c["symbol"] for c in result} == {"OK", "ERR", "ALSO_OK"}, (
        f"All 3 should survive — OK and ALSO_OK pass the threshold, "
        f"ERR fails open. Got: {[c['symbol'] for c in result]}"
    )


def test_short_candidate_bypassed_when_model_has_no_short_training_data():
    """Critical for long/short cold-start: when meta-model has < 30
    short training samples (n_train_short), the pregate must not
    filter SHORT candidates — the model can't reliably score them.

    Without this bypass, a long-trained model scores every SHORT as
    low-prob (extrapolation) and the pregate drops them all, defeating
    the long/short capability before it gets a chance to accumulate
    its own training data."""
    import trade_pipeline as tp
    cands = [
        _candidate("BUY1", signal="BUY"),       # long
        _candidate("BUY2", signal="BUY"),       # long
        _candidate("SHORT1", signal="SHORT"),   # short — should be bypassed
        _candidate("SHORT2", signal="SHORT"),   # short — should be bypassed
    ]
    bundle = {
        "model": "fake",
        "metrics": {"n_train_short": 0, "n_train_long": 800},
    }
    # Predict: longs get 0.7 (kept), shorts get 0.1 (would be DROPPED
    # under uniform threshold). With bypass, shorts must survive.
    probs = {"BUY1": 0.7, "BUY2": 0.7, "SHORT1": 0.1, "SHORT2": 0.1}

    def _fake_predict(bundle, features):
        return probs.get(features.get("symbol", ""), 0.5)

    with patch("meta_model.load_model", return_value=bundle):
        with patch("meta_model.model_path_for_profile", return_value="/tmp/x"):
            with patch("meta_model.predict_probability", side_effect=_fake_predict):
                result = tp._meta_pregate_candidates(cands, _ctx(threshold=0.5))

    surviving = {c["symbol"] for c in result}
    assert "SHORT1" in surviving and "SHORT2" in surviving, (
        f"SHORT candidates must bypass the pregate when n_train_short=0. "
        f"Got: {sorted(surviving)}"
    )
    assert "BUY1" in surviving and "BUY2" in surviving, (
        "Longs should still pass via the normal threshold path"
    )


def test_short_candidate_filtered_when_model_has_enough_short_training_data():
    """Once the model has >= 30 short samples, the bypass turns off
    and the normal threshold applies to shorts."""
    import trade_pipeline as tp
    cands = [
        _candidate("SHORT_DROP", signal="SHORT"),
        _candidate("SHORT_KEEP", signal="SHORT"),
    ]
    bundle = {
        "model": "fake",
        "metrics": {"n_train_short": 50, "n_train_long": 800},
    }
    probs = {"SHORT_DROP": 0.2, "SHORT_KEEP": 0.7}

    def _fake_predict(bundle, features):
        return probs.get(features.get("symbol", ""), 0.5)

    with patch("meta_model.load_model", return_value=bundle):
        with patch("meta_model.model_path_for_profile", return_value="/tmp/x"):
            with patch("meta_model.predict_probability", side_effect=_fake_predict):
                result = tp._meta_pregate_candidates(cands, _ctx(threshold=0.5))

    surviving = {c["symbol"] for c in result}
    assert "SHORT_DROP" not in surviving, (
        "With 50 short samples, the bypass is OFF and SHORT_DROP at "
        "0.2 prob should be filtered."
    )
    assert "SHORT_KEEP" in surviving


def test_long_candidate_bypassed_when_model_has_no_long_training_data():
    """Symmetric: a model with no LONG training data shouldn't filter
    long candidates either. (Edge case: a hypothetical short-only
    profile that has only built short training data.)"""
    import trade_pipeline as tp
    cands = [
        _candidate("BUY_KEEP", signal="BUY"),  # would normally be dropped
    ]
    bundle = {
        "model": "fake",
        "metrics": {"n_train_short": 200, "n_train_long": 5},
    }

    def _fake_predict(bundle, features):
        return 0.05  # very low

    with patch("meta_model.load_model", return_value=bundle):
        with patch("meta_model.model_path_for_profile", return_value="/tmp/x"):
            with patch("meta_model.predict_probability", side_effect=_fake_predict):
                result = tp._meta_pregate_candidates(cands, _ctx(threshold=0.5))

    assert "BUY_KEEP" in [c["symbol"] for c in result], (
        "Long should bypass when n_train_long < 30 — model is short-trained."
    )


def test_train_meta_model_records_per_direction_sample_counts():
    """train_meta_model must populate n_train_short and n_train_long
    in the metrics dict so the pregate can decide whether to bypass."""
    import meta_model
    # Minimal feature set + synthetic data
    feature_names = [
        "score", "rsi",
        "prediction_type_directional_long",
        "prediction_type_directional_short",
        "prediction_type_exit_long",
        "prediction_type_exit_short",
    ]
    # 60 long, 30 short
    X = (
        [[3, 50, 1.0, 0.0, 0.0, 0.0]] * 60 +
        [[3, 50, 0.0, 1.0, 0.0, 0.0]] * 30
    )
    y = [1, 0] * 45  # alternating, exactly 90 long
    bundle = meta_model.train_meta_model(X, y, feature_names)
    metrics = bundle["metrics"]
    # 80% train split → 72 train rows
    # 60 long * 0.8 = 48; 30 short * 0.8 = 24 (approximately)
    assert metrics["n_train_short"] >= 1
    assert metrics["n_train_long"] >= 1
    assert metrics["n_train_short"] + metrics["n_train_long"] <= metrics["n_train"]


def test_pipeline_calls_pregate_before_ensemble():
    """Source-level guard: removing the pre-gate from the trade
    pipeline silently re-enables full-cost behavior. Test verifies
    the call is present and ordered before _get_shared_ensemble."""
    import trade_pipeline as tp
    src = inspect.getsource(tp)
    pregate_idx = src.find("_meta_pregate_candidates(candidates_data")
    ensemble_idx = src.find("_get_shared_ensemble(\n                candidates_data")
    assert pregate_idx > 0, (
        "REGRESSION: trade_pipeline no longer calls "
        "_meta_pregate_candidates. Lever 2 cost + quality benefits "
        "regressed. See COST_AND_QUALITY_LEVERS_PLAN.md."
    )
    assert ensemble_idx > 0, (
        "Couldn't find _get_shared_ensemble call site to verify ordering."
    )
    assert pregate_idx < ensemble_idx, (
        "REGRESSION: meta-pregate must run BEFORE _get_shared_ensemble. "
        "Reordering breaks the cost-saving and quality-improvement "
        "contracts. See COST_AND_QUALITY_LEVERS_PLAN.md."
    )


def test_zero_candidates_returns_empty():
    """Defensive: empty input list returns empty list, not crash."""
    import trade_pipeline as tp
    assert tp._meta_pregate_candidates([], _ctx()) == []


def test_no_profile_id_falls_open():
    """ctx without profile_id can't load a per-profile meta-model;
    must fail-open."""
    import trade_pipeline as tp
    cands = [_candidate("AAA")]
    ctx = MagicMock()
    ctx.meta_pregate_threshold = 0.5
    ctx.profile_id = 0
    assert tp._meta_pregate_candidates(cands, ctx) == cands
