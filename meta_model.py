"""Meta-model: a second-layer classifier that learns our AI's systematic errors.

Phase 1 of the Quant Fund Evolution roadmap (see ROADMAP.md).

The model takes the features the AI saw when making each prediction — the full
technical indicator suite, alternative data, sector context, track record — and
predicts the probability that the AI's call was correct. This probability
re-weights the AI's confidence at execution time.

The core insight: the AI is a generalist reasoning about markets. It has
systematic blind spots. Our resolved prediction database captures those blind
spots in labeled form. A gradient-boosted tree learns patterns like:
"AI overconfident on low-volume mid-caps in sideways markets, RSI 45-55."

The training data is our proprietary AI predictions — literally impossible
for competitors to replicate.

Key functions:
    extract_features(features_json)  -> dict of numeric features
    build_training_set(db_path)       -> (X, y) for ML training
    train_meta_model(X, y)            -> trained classifier + metrics
    predict_probability(model, features_json) -> P(AI correct) in [0, 1]
    save_model(model, path) / load_model(path)
"""

import json
import logging
import os
import pickle
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Minimum resolved predictions required before training
MIN_TRAINING_SAMPLES = 100

# Model filename pattern
MODEL_FILE_TEMPLATE = "meta_model_{profile_id}.pkl"


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

# Numeric features we expect in the features_json payload. Missing fields
# default to 0. Non-numeric fields are encoded separately.
NUMERIC_FEATURES = [
    "score", "rsi", "volume_ratio", "atr", "adx", "stoch_rsi", "roc_10",
    "pct_from_52w_high", "mfi", "cmf", "squeeze", "pct_from_vwap",
    "nearest_fib_dist", "gap_pct", "rel_strength_vs_sector", "short_pct_float",
    "put_call_ratio", "pe_trailing", "reddit_mentions", "reddit_sentiment",
    "_market_signal_count",
    # New per-symbol features
    "finra_short_vol_ratio", "insider_cluster", "eps_revision_magnitude",
    # New macro features
    "_yield_spread_10y2y", "_cboe_skew", "_unemployment_rate", "_cpi_yoy",
    # Wave 2
    "dark_pool_pct", "earnings_surprise_streak",
]

# Categorical features — one-hot encoded
CATEGORICAL_FEATURES = {
    "signal": ["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL", "SHORT", "STRONG_SHORT"],
    # P1.12 of LONG_SHORT_PLAN.md — prediction_type as a feature so
    # the meta-model can learn that some technical patterns predict
    # well for longs but not shorts (or vice versa). Backfilled rows
    # have prediction_type populated; new rows store it at write
    # time. At inference time the caller derives a candidate's
    # likely prediction_type from its strategy signal.
    "prediction_type": ["directional_long", "directional_short",
                          "exit_long", "exit_short"],
    "insider_direction": ["buying", "selling", "neutral"],
    "options_signal": ["bullish_flow", "bearish_flow", "neutral"],
    "vwap_position": ["above", "at", "below"],
    "sector_trend": ["inflow", "outflow", "flat"],
    "_regime": ["bull", "bear", "sideways", "volatile", "unknown"],
    # New categorical features
    "congress_direction": ["buying", "selling", "neutral"],
    "eps_revision_direction": ["up", "down", "flat"],
    "_curve_status": ["normal", "flat", "inverted"],
    # Wave 2
    "insider_near_earnings": ["bullish", "bearish", "neutral"],
    "_rotation_phase": ["risk_on", "risk_off", "mixed"],
    "earnings_surprise_direction": ["beats", "misses", "mixed"],
    "_market_gex_regime": ["pinning", "expansion", "balanced"],
}


def extract_features(features_dict: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Flatten a prediction's feature payload into a numeric feature vector.

    Returns None if input is empty/invalid. Returns a flat dict of
    {feature_name: numeric_value} on success.
    """
    if not features_dict:
        return None

    result = {}

    # Numeric features
    for name in NUMERIC_FEATURES:
        val = features_dict.get(name, 0)
        try:
            result[name] = float(val) if val is not None else 0.0
        except (TypeError, ValueError):
            result[name] = 0.0

    # Categorical (one-hot)
    for cat_name, values in CATEGORICAL_FEATURES.items():
        raw = features_dict.get(cat_name, "")
        raw_str = str(raw) if raw is not None else ""
        for v in values:
            result[f"{cat_name}_{v}"] = 1.0 if raw_str == v else 0.0

    # Vote pattern signals (bool -> int per known strategy names)
    # Strategy names vary by market type; we capture the total count in
    # _market_signal_count (already numeric) rather than enumerating all
    # possible strategy names.

    return result


def _parse_features_json(row_features_json: Optional[str]) -> Optional[Dict[str, Any]]:
    """Safely parse a features_json column value."""
    if not row_features_json:
        return None
    try:
        return json.loads(row_features_json)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Training data assembly
# ---------------------------------------------------------------------------

def build_training_set(db_path: str,
                        min_samples: int = MIN_TRAINING_SAMPLES
                        ) -> Tuple[Optional[List[List[float]]],
                                    Optional[List[int]],
                                    Optional[List[str]]]:
    """Assemble resolved predictions into ML training data.

    Returns (X, y, feature_names) where:
        X is a list of feature vectors
        y is a list of 0/1 labels (1 = correct prediction)
        feature_names is the ordered list of feature names corresponding to X columns

    Returns (None, None, None) if insufficient resolved data.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # P1.12 of LONG_SHORT_PLAN.md — pull prediction_type when
        # available. Legacy DBs may not have the column yet (fresh test
        # fixtures, pre-migration prod). Probe schema first; fall back
        # to query without prediction_type when missing — _resolve_one's
        # legacy fallback in extract_features still works on those rows.
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(ai_predictions)"
        )}
        has_ptype = "prediction_type" in cols
        # ORDER BY id ASC is REQUIRED for time-ordered train/test split.
        if has_ptype:
            rows = conn.execute(
                "SELECT features_json, actual_outcome, prediction_type "
                "FROM ai_predictions "
                "WHERE status = 'resolved' "
                "AND actual_outcome IN ('win', 'loss') "
                "AND features_json IS NOT NULL "
                "ORDER BY id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT features_json, actual_outcome, "
                "NULL AS prediction_type "
                "FROM ai_predictions "
                "WHERE status = 'resolved' "
                "AND actual_outcome IN ('win', 'loss') "
                "AND features_json IS NOT NULL "
                "ORDER BY id ASC"
            ).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("Failed to load training data from %s: %s", db_path, exc)
        return None, None, None

    if len(rows) < min_samples:
        logger.info("Only %d resolved predictions with features; need %d to train.",
                    len(rows), min_samples)
        return None, None, None

    X: List[List[float]] = []
    y: List[int] = []
    feature_names: Optional[List[str]] = None

    for row in rows:
        features = _parse_features_json(row["features_json"])
        if not features:
            continue
        # P1.12 — inject prediction_type from the column into the
        # feature payload so extract_features one-hots it.
        ptype = row["prediction_type"]
        if ptype:
            features = dict(features)
            features["prediction_type"] = ptype
        extracted = extract_features(features)
        if not extracted:
            continue

        if feature_names is None:
            feature_names = sorted(extracted.keys())

        vector = [extracted.get(name, 0.0) for name in feature_names]
        X.append(vector)
        y.append(1 if row["actual_outcome"] == "win" else 0)

    if len(X) < min_samples:
        logger.info("Only %d valid training samples after extraction; need %d.",
                    len(X), min_samples)
        return None, None, None

    return X, y, feature_names


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train_meta_model(X: List[List[float]], y: List[int],
                      feature_names: List[str]) -> Dict[str, Any]:
    """Train a gradient-boosted classifier.

    Returns a dict containing:
        model: trained scikit-learn classifier
        feature_names: list of feature names (column order)
        metrics: dict of {accuracy, auc, n_samples, positive_rate}
        feature_importance: list of (name, importance) sorted desc
    """
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import accuracy_score, roc_auc_score

    # Time-ordered split (NOT random). Inputs are required to arrive in
    # ascending time order from build_training_set's `ORDER BY id ASC`.
    # We use the most recent 20% as the held-out test set so the model
    # is evaluated only on predictions made AFTER its training horizon.
    #
    # Why this matters: financial features are highly autocorrelated
    # day-to-day (RSI today ≈ RSI tomorrow, regime today ≈ regime
    # tomorrow), so a random split lets the model memorize "this regime
    # ≈ this outcome" rather than learn predictive patterns. The 0.95
    # AUCs we saw with random splitting were a known data-leakage
    # artifact, not real edge. See CHANGELOG 2026-04-27.
    n = len(X)
    n_test = max(1, int(round(n * 0.2)))
    n_train = n - n_test
    X_train, X_test = X[:n_train], X[n_train:]
    y_train, y_test = y[:n_train], y[n_train:]

    model = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=3,
        learning_rate=0.05,
        random_state=42,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    try:
        y_proba = model.predict_proba(X_test)[:, 1]
        auc = float(roc_auc_score(y_test, y_proba))
    except (ValueError, IndexError):
        auc = 0.5  # fallback when only one class in test set

    acc = float(accuracy_score(y_test, y_pred))

    # Feature importance
    importances = list(zip(feature_names, model.feature_importances_.tolist()))
    importances.sort(key=lambda x: x[1], reverse=True)

    # Per-direction sample counts. The pregate uses these to decide
    # whether the model can reliably score a candidate of that direction.
    # Without this, profiles with 0 short training samples would still
    # score SHORT candidates with a long-trained model — extrapolation
    # the model has no business doing.
    n_train_short = 0
    n_train_long = 0
    if "prediction_type_directional_short" in feature_names:
        idx_short = feature_names.index("prediction_type_directional_short")
        n_train_short = sum(1 for row in X_train if row[idx_short] >= 0.5)
    if "prediction_type_directional_long" in feature_names:
        idx_long = feature_names.index("prediction_type_directional_long")
        n_train_long = sum(1 for row in X_train if row[idx_long] >= 0.5)

    return {
        "model": model,
        "feature_names": feature_names,
        "metrics": {
            "accuracy": round(acc, 4),
            "auc": round(auc, 4),
            "n_samples": len(X),
            "n_train": len(X_train),
            "n_test": len(X_test),
            "n_train_short": n_train_short,
            "n_train_long": n_train_long,
            "positive_rate": round(sum(y) / len(y), 4),
        },
        "feature_importance": [(n, round(i, 4)) for n, i in importances],
    }


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def predict_probability(model_bundle: Dict[str, Any],
                         features_dict: Dict[str, Any]) -> float:
    """Given a trained model bundle and raw features, return P(AI correct).

    Returns 0.5 (no information) if features cannot be extracted.
    """
    if not model_bundle:
        return 0.5

    extracted = extract_features(features_dict)
    if not extracted:
        return 0.5

    feature_names = model_bundle["feature_names"]
    vector = [[extracted.get(name, 0.0) for name in feature_names]]

    try:
        proba = model_bundle["model"].predict_proba(vector)[0][1]
        return float(proba)
    except Exception as exc:
        logger.warning("Meta-model inference failed: %s", exc)
        return 0.5


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def model_path_for_profile(profile_id: int, base_dir: str = ".") -> str:
    """Return the filesystem path for a profile's meta-model pickle."""
    return os.path.join(base_dir, MODEL_FILE_TEMPLATE.format(profile_id=profile_id))


def save_model(bundle: Dict[str, Any], path: str) -> None:
    """Persist a model bundle to disk."""
    with open(path, "wb") as f:
        pickle.dump(bundle, f)


def load_model(path: str) -> Optional[Dict[str, Any]]:
    """Load a model bundle, or return None if not found or corrupt."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as exc:
        logger.warning("Failed to load meta-model from %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------

def train_and_save(profile_id: int, db_path: str,
                   base_dir: str = ".") -> Optional[Dict[str, Any]]:
    """Train a model for a profile and save to disk.

    Returns the model bundle on success, None if insufficient data.
    """
    X, y, feature_names = build_training_set(db_path)
    if X is None:
        return None

    bundle = train_meta_model(X, y, feature_names)
    path = model_path_for_profile(profile_id, base_dir)
    save_model(bundle, path)

    logger.info("Trained meta-model for profile %d: AUC=%.4f, acc=%.4f (n=%d)",
                profile_id,
                bundle["metrics"]["auc"],
                bundle["metrics"]["accuracy"],
                bundle["metrics"]["n_samples"])
    return bundle


def adjust_confidence(ai_confidence: int, meta_prob: float) -> int:
    """Blend AI confidence with meta-model probability.

    Formula: ai_confidence * (0.5 + meta_prob * 0.5)
    This produces a confidence multiplier in [0.5, 1.0]:
        meta_prob=0.0 -> 0.5x AI confidence
        meta_prob=0.5 -> 0.75x AI confidence
        meta_prob=1.0 -> 1.0x AI confidence

    Returns integer in [0, 100].
    """
    multiplier = 0.5 + meta_prob * 0.5
    return max(0, min(100, int(round(ai_confidence * multiplier))))


# Threshold below which a trade is suppressed even if AI selected it
SUPPRESSION_THRESHOLD = 0.3
