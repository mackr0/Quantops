"""Item 5a of COMPETITIVE_GAP_PLAN.md — online learning meta-model.

The primary `meta_model` is a GradientBoostingClassifier — accurate
but requires full retrains (slow, data-hungry). It can't update from
a single new resolved prediction.

This module adds an SGDClassifier-based "freshness layer" that
updates incrementally per resolved prediction. Trade-offs:

  GBM (existing):
    - Better calibration on stable data
    - Full retrain weekly via _task_retrain_meta_model
    - Slow to adapt to regime shifts (data needs to enter the
      training pool, then a retrain)

  SGD (this module):
    - Lower per-prediction accuracy
    - Updates per resolved prediction (partial_fit)
    - Adapts to regime shifts in real time
    - Higher variance — single noisy resolution can shift the model

The two are complementary. The AI prompt sees BOTH scores; the
divergence between them is itself signal:
  - GBM 0.65, SGD 0.55 → 10pp divergence = recent regime drift
  - GBM 0.50, SGD 0.50 → models agree, signal is stable

This commit:
  - Initialize an SGD classifier from existing training data
  - Update incrementally on each resolved prediction
  - Persisted as pickle alongside the GBM model
  - Inference function returns probability + freshness metadata

Wired into ai_tracker.resolve_predictions: each resolved prediction
calls update_online_model with that row's features + outcome.
"""
from __future__ import annotations

import logging
import os
import pickle
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Persisted model file lives next to the profile DB
ONLINE_MODEL_FILENAME_TEMPLATE = "online_meta_model_p{profile_id}.pkl"

# SGDClassifier hyperparameters — chosen for stability over speed
# of adaptation. eta0 (learning rate start) is small so single
# noisy outcomes can't shift predictions wildly. learning_rate
# 'optimal' decays automatically.
SGD_PARAMS = {
    "loss": "log_loss",       # logistic regression
    "alpha": 0.0001,           # L2 regularization
    "learning_rate": "optimal",
    "random_state": 42,
    "max_iter": 1,              # single-pass partial_fit calls
    "tol": None,                # disable early stopping
    "warm_start": True,
}


def _model_path(profile_id: int, base_dir: str = ".") -> str:
    return os.path.join(
        base_dir, ONLINE_MODEL_FILENAME_TEMPLATE.format(profile_id=profile_id),
    )


def _load(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            return pickle.load(fh)
    except Exception as exc:
        logger.warning("online model load failed at %s: %s", path, exc)
        return None


def _save(bundle: Dict[str, Any], path: str) -> None:
    try:
        with open(path, "wb") as fh:
            pickle.dump(bundle, fh)
    except Exception as exc:
        logger.warning("online model save failed at %s: %s", path, exc)


def initialize_from_history(
    profile_id: int,
    db_path: str,
    base_dir: str = ".",
) -> Optional[Dict[str, Any]]:
    """Bootstrap the SGD model from all historical resolved
    predictions for this profile. Run once per profile (or after
    feature schema changes).

    Returns the trained bundle dict, or None if not enough data.
    """
    from sklearn.linear_model import SGDClassifier
    from meta_model import build_training_set

    # Use a small min_samples (10) — online models can bootstrap from
    # fewer rows than GBM's 100 because they keep learning incrementally.
    X, y, feature_names = build_training_set(db_path, min_samples=10)
    if X is None or y is None or feature_names is None:
        logger.info(
            "online_meta_model: not enough training data for profile %s",
            profile_id,
        )
        return None

    model = SGDClassifier(**SGD_PARAMS)
    # First fit must specify all classes for partial_fit to work later.
    classes = sorted(set(y))
    if len(classes) < 2:
        # Only one class in training — can't fit binary classifier yet
        return None
    # Single fit on the full historical set
    model.fit(X, y)

    bundle = {
        "model": model,
        "feature_names": feature_names,
        "classes": classes,
        "n_updates": len(X),
        "created_at": _now_iso(),
        "last_update_at": _now_iso(),
    }
    _save(bundle, _model_path(profile_id, base_dir))
    logger.info("online_meta_model initialized for profile %s on %d rows",
                profile_id, len(X))
    return bundle


def update_online_model(
    profile_id: int,
    features_dict: Dict[str, float],
    outcome_label: int,
    base_dir: str = ".",
) -> bool:
    """Single-prediction incremental update.

    Args:
        profile_id: which profile's model to update.
        features_dict: feature values keyed by feature name.
        outcome_label: 0 (loss) or 1 (win).

    Returns True on success, False if the model isn't initialized
    yet or the update fails. Initialization should happen via
    `initialize_from_history` separately (the daily training task can
    call it for new profiles).
    """
    if outcome_label not in (0, 1):
        return False
    path = _model_path(profile_id, base_dir)
    bundle = _load(path)
    if bundle is None:
        return False

    try:
        # Project feature dict into the model's expected feature_names
        # order. Missing features → 0.0.
        feat_names = bundle["feature_names"]
        x_row = [float(features_dict.get(name, 0.0))
                 for name in feat_names]
        bundle["model"].partial_fit(
            [x_row], [outcome_label], classes=bundle.get("classes", [0, 1]),
        )
        bundle["n_updates"] = bundle.get("n_updates", 0) + 1
        bundle["last_update_at"] = _now_iso()
        _save(bundle, path)
        return True
    except Exception as exc:
        logger.warning("online_meta_model update failed for profile %s: %s",
                       profile_id, exc)
        return False


def online_predict_probability(
    profile_id: int,
    features_dict: Dict[str, Any],
    base_dir: str = ".",
) -> Optional[float]:
    """Return P(win) under the online model. None if no model exists."""
    path = _model_path(profile_id, base_dir)
    bundle = _load(path)
    if bundle is None:
        return None
    try:
        feat_names = bundle["feature_names"]
        x_row = [float(features_dict.get(name, 0.0))
                 for name in feat_names]
        proba = bundle["model"].predict_proba([x_row])
        # Find index of class=1 (win)
        classes = list(bundle["model"].classes_)
        if 1 in classes:
            return float(proba[0][classes.index(1)])
        return None
    except Exception as exc:
        logger.debug("online_predict_probability failed: %s", exc)
        return None


def get_online_model_info(profile_id: int,
                              base_dir: str = ".") -> Optional[Dict[str, Any]]:
    """Return metadata about the online model: n_updates, last_update,
    feature count. None if no model exists."""
    path = _model_path(profile_id, base_dir)
    bundle = _load(path)
    if bundle is None:
        return None
    return {
        "n_updates": bundle.get("n_updates", 0),
        "n_features": len(bundle.get("feature_names", [])),
        "created_at": bundle.get("created_at"),
        "last_update_at": bundle.get("last_update_at"),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
