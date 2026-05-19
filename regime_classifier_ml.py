"""ML regime classifier — Phase 4c (shadow mode, 2026-05-18 PM).

A learned alternative to the rule-based `market_regime.detect_regime`.
SHADOW ONLY for now: every call to `detect_regime()` also runs this
classifier and logs both predictions to `regime_shadow_calls`. The
production decision continues to use the rule-based path; the ML
output is observed-only. Promotion requires accumulated comparison
data showing measured outperformance.

Architecture:
- `GradientBoostingClassifier` over 9 features derived from SPY +
  VIX history.
- Trained on labels = forward-20d regime classification (bull / bear
  / sideways / volatile). Label rules are documented inline.
- Bootstrap dataset: 10+ years of yfinance ^GSPC + ^VIX. Once the
  system has accumulated production regime+outcome data the model
  can retrain on its own history; for now we bootstrap on public.
- Model persisted as a pickle to `regime_classifier_ml_v{N}.pkl`.

Feature set (compact — gradient boosting handles interactions
internally so we don't need polynomials):
1. vix_level — current VIX
2. vix_change_20d — VIX 20d change in points
3. spy_vs_sma50_pct — (spy_price - sma50) / sma50 * 100
4. spy_vs_sma200_pct — (spy_price - sma200) / sma200 * 100
5. spy_5d_return_pct — last 5 trading days
6. spy_20d_return_pct — last 20 trading days
7. breadth_pct — % of last 20 days closing above 20d SMA
8. atr_pct — ATR(14) as % of price (realized vol proxy)
9. sma50_slope_pct — (sma50 - sma50_10d_ago) / sma50_10d_ago * 100

Honest limits:
- The label rules are heuristic. "Bull = forward +2% AND realized
  vol < 25%" is one defensible labeling; alternatives exist (e.g.,
  Hidden Markov Model labels, expert-annotated regimes). We use
  the simple version because it's reproducible and matches what
  downstream consumers care about ("can I be long here?").
- 9 features is enough for a baseline; a production-grade model
  would add yield-curve, credit spreads, sector dispersion, etc.
  Out of scope tonight.
- Bootstrap on public data is a starting point; the model gets
  re-trained on the system's own outcomes once enough accumulate.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Regime labels — must match the rule-based output values so
# comparison-tracking is symbol-aligned.
REGIMES = ("bull", "bear", "sideways", "volatile")

# Knobs — exposed for tests / future tuning.
FORWARD_HORIZON_DAYS = 20
LABEL_BULL_RETURN_PCT = 2.0
LABEL_BEAR_RETURN_PCT = -2.0
LABEL_VOLATILE_REALIZED_VOL_PCT = 25.0  # annualized


def compute_features(spy_close: List[float], spy_high: List[float],
                     spy_low: List[float], vix_series: List[float]
                     ) -> Optional[Dict[str, float]]:
    """Extract the feature vector from rolling SPY OHLC + VIX series.

    Inputs are lists in ascending date order (oldest first). The
    function reads the LAST value as "today" and computes features
    that look backward from it.

    Returns None when there isn't enough history (need 200+ bars).
    """
    if len(spy_close) < 200 or len(spy_high) < 200 or len(spy_low) < 200:
        return None
    if len(vix_series) < 20:
        return None
    try:
        spy_today = float(spy_close[-1])
        sma50 = sum(spy_close[-50:]) / 50.0
        sma200 = sum(spy_close[-200:]) / 200.0
        sma20 = sum(spy_close[-20:]) / 20.0
        spy_5d_ago = float(spy_close[-6])
        spy_20d_ago = float(spy_close[-21])

        # ATR(14) as a % of today's price
        tr = []
        for i in range(-15, -1):
            h = float(spy_high[i]); l = float(spy_low[i])
            cp = float(spy_close[i - 1])
            tr.append(max(h - l, abs(h - cp), abs(l - cp)))
        atr = sum(tr) / max(len(tr), 1)
        atr_pct = (atr / spy_today * 100) if spy_today > 0 else 0.0

        # Breadth proxy: % of last 20 closes above 20d SMA
        above = sum(1 for c in spy_close[-20:] if float(c) > sma20)
        breadth_pct = above / 20.0 * 100.0

        # SMA50 slope: today's SMA50 vs SMA50 from 10 days ago
        sma50_10d_ago = sum(spy_close[-60:-10]) / 50.0
        slope_pct = ((sma50 - sma50_10d_ago) / sma50_10d_ago * 100
                     if sma50_10d_ago else 0.0)

        vix_today = float(vix_series[-1])
        vix_20d_ago = float(vix_series[-21]) if len(vix_series) >= 21 else vix_today
        vix_change_20d = vix_today - vix_20d_ago

        return {
            "vix_level": vix_today,
            "vix_change_20d": vix_change_20d,
            "spy_vs_sma50_pct": (spy_today - sma50) / sma50 * 100,
            "spy_vs_sma200_pct": (spy_today - sma200) / sma200 * 100,
            "spy_5d_return_pct": (spy_today - spy_5d_ago) / spy_5d_ago * 100,
            "spy_20d_return_pct": (spy_today - spy_20d_ago) / spy_20d_ago * 100,
            "breadth_pct": breadth_pct,
            "atr_pct": atr_pct,
            "sma50_slope_pct": slope_pct,
        }
    except (IndexError, ZeroDivisionError, TypeError, ValueError) as exc:
        logger.warning("compute_features failed: %s: %s",
                       type(exc).__name__, exc)
        return None


def label_from_forward(spy_close: List[float], i: int,
                       horizon: int = FORWARD_HORIZON_DAYS
                       ) -> Optional[str]:
    """Generate the forward-looking regime label for index `i` in the
    series. Looks at the next `horizon` bars; needs them to exist."""
    if i + horizon >= len(spy_close):
        return None
    try:
        start = float(spy_close[i])
        end = float(spy_close[i + horizon])
        forward_return_pct = (end - start) / start * 100

        # Realized vol over the forward window (daily-stdev × √252)
        window = [float(spy_close[i + k]) for k in range(1, horizon + 1)]
        prev = [float(spy_close[i + k - 1]) for k in range(1, horizon + 1)]
        daily_rets = [(w - p) / p for w, p in zip(window, prev) if p > 0]
        if not daily_rets:
            return None
        n = len(daily_rets)
        mean = sum(daily_rets) / n
        var = sum((r - mean) ** 2 for r in daily_rets) / max(n - 1, 1)
        realized_vol_annualized_pct = (var ** 0.5) * (252 ** 0.5) * 100

        if realized_vol_annualized_pct >= LABEL_VOLATILE_REALIZED_VOL_PCT:
            return "volatile"
        if forward_return_pct >= LABEL_BULL_RETURN_PCT:
            return "bull"
        if forward_return_pct <= LABEL_BEAR_RETURN_PCT:
            return "bear"
        return "sideways"
    except (IndexError, ZeroDivisionError, TypeError, ValueError):
        return None


def build_training_dataset(spy_close: List[float], spy_high: List[float],
                            spy_low: List[float], vix_series: List[float]
                            ) -> Tuple[List[List[float]], List[str], List[Dict]]:
    """Slide over the SPY history and emit (X, y, feature_dicts) for
    every day that has both a valid feature vector and a valid
    forward label."""
    X: List[List[float]] = []
    y: List[str] = []
    feature_dicts: List[Dict] = []
    # Need 200 bars of history AND 20 bars of forward — so valid i is
    # 200 <= i < len - 20
    for i in range(200, len(spy_close) - FORWARD_HORIZON_DAYS):
        feats = compute_features(
            spy_close[:i + 1], spy_high[:i + 1], spy_low[:i + 1],
            vix_series[:i + 1],
        )
        if feats is None:
            continue
        label = label_from_forward(spy_close, i)
        if label is None:
            continue
        X.append([feats[k] for k in sorted(feats)])
        y.append(label)
        feature_dicts.append(feats)
    return X, y, feature_dicts


class RegimeClassifier:
    """Thin wrapper around sklearn's GradientBoostingClassifier with
    train / predict / save / load. The class memorizes the feature-
    name ordering at train time so inference can't accidentally
    permute features."""

    def __init__(self, feature_names: Optional[List[str]] = None,
                  model=None, trained_at: Optional[str] = None,
                  trained_samples: int = 0):
        self.feature_names = feature_names or []
        self.model = model
        self.trained_at = trained_at
        self.trained_samples = trained_samples

    def train(self, X: List[List[float]], y: List[str],
              feature_names: List[str]) -> Dict[str, Any]:
        """Fit a GradientBoostingClassifier on the (X, y) data."""
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score
        # Stratified train/test split — preserves regime distribution
        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y,
        )
        self.model = GradientBoostingClassifier(
            n_estimators=100, max_depth=3, random_state=42,
        )
        self.model.fit(X_tr, y_tr)
        self.feature_names = feature_names
        self.trained_at = datetime.utcnow().isoformat()
        self.trained_samples = len(X)
        train_acc = accuracy_score(y_tr, self.model.predict(X_tr))
        test_acc = accuracy_score(y_te, self.model.predict(X_te))
        # Per-class accuracy on test set
        from collections import Counter
        test_preds = self.model.predict(X_te)
        per_class = {}
        for actual in REGIMES:
            mask = [i for i, t in enumerate(y_te) if t == actual]
            if not mask:
                per_class[actual] = None
                continue
            correct = sum(1 for i in mask if test_preds[i] == actual)
            per_class[actual] = correct / len(mask)
        return {
            "trained_at": self.trained_at,
            "trained_samples": self.trained_samples,
            "train_acc": round(train_acc, 4),
            "test_acc": round(test_acc, 4),
            "per_class_test_acc": per_class,
            "class_distribution": dict(Counter(y)),
        }

    def predict(self, feats: Dict[str, float]) -> Optional[Dict[str, Any]]:
        """Predict the regime for a single feature dict. Returns
        `{regime, confidence, probas}` or None if the model isn't
        loaded or features can't be aligned."""
        if self.model is None or not self.feature_names:
            return None
        try:
            x = [float(feats[k]) for k in self.feature_names]
        except (KeyError, TypeError, ValueError):
            return None
        try:
            probas = self.model.predict_proba([x])[0]
            classes = list(self.model.classes_)
            idx = int(probas.argmax())
            return {
                "regime": classes[idx],
                "confidence": float(probas[idx]),
                "probas": {c: float(p) for c, p in zip(classes, probas)},
            }
        except Exception as exc:
            logger.warning("predict failed: %s: %s", type(exc).__name__, exc)
            return None

    def save(self, path: str) -> None:
        payload = {
            "model": self.model,
            "feature_names": self.feature_names,
            "trained_at": self.trained_at,
            "trained_samples": self.trained_samples,
        }
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(payload, f)

    @classmethod
    def load(cls, path: str) -> Optional["RegimeClassifier"]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
            return cls(
                feature_names=payload.get("feature_names", []),
                model=payload.get("model"),
                trained_at=payload.get("trained_at"),
                trained_samples=payload.get("trained_samples", 0),
            )
        except (pickle.UnpicklingError, EOFError, OSError) as exc:
            logger.warning("RegimeClassifier.load failed: %s: %s",
                           type(exc).__name__, exc)
            return None


# Singleton cache — load once per process.
_LOADED: Dict[str, Any] = {"clf": None, "path": None, "loaded_at": None}


def get_active_classifier(model_path: str) -> Optional[RegimeClassifier]:
    """Lazy-load + memoize the classifier so concurrent callers don't
    re-read the pickle each time."""
    if _LOADED["clf"] is not None and _LOADED["path"] == model_path:
        return _LOADED["clf"]
    clf = RegimeClassifier.load(model_path)
    if clf is not None:
        _LOADED["clf"] = clf
        _LOADED["path"] = model_path
        _LOADED["loaded_at"] = datetime.utcnow().isoformat()
    return clf


def shadow_predict_and_log(model_path: str, db_path: str,
                            rule_regime: str,
                            spy_close: List[float],
                            spy_high: List[float], spy_low: List[float],
                            vix_series: List[float],
                            spy_price_now: float,
                            vix_now: Optional[float]) -> Optional[Dict[str, Any]]:
    """End-to-end shadow path: compute features → predict via the
    loaded classifier → log both predictions to `regime_shadow_calls`.
    Returns the ML prediction dict for the caller, or None if the
    classifier or features aren't available.

    Fail-soft on every step — shadow path must never break the
    production regime detection.
    """
    try:
        clf = get_active_classifier(model_path)
        if clf is None:
            return None
        feats = compute_features(spy_close, spy_high, spy_low, vix_series)
        if feats is None:
            return None
        pred = clf.predict(feats)
        if pred is None:
            return None
        # Persist
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS regime_shadow_calls ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " timestamp TEXT NOT NULL DEFAULT (datetime('now')),"
                " rule_regime TEXT,"
                " ml_regime TEXT,"
                " ml_confidence REAL,"
                " ml_probas_json TEXT,"
                " features_json TEXT,"
                " spy_price REAL,"
                " vix REAL,"
                " model_trained_at TEXT,"
                " model_trained_samples INTEGER)"
            )
            conn.execute(
                "INSERT INTO regime_shadow_calls "
                "(rule_regime, ml_regime, ml_confidence, ml_probas_json, "
                " features_json, spy_price, vix, model_trained_at, "
                " model_trained_samples) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rule_regime, pred["regime"], pred["confidence"],
                 json.dumps(pred["probas"]),
                 json.dumps(feats),
                 spy_price_now, vix_now,
                 clf.trained_at, clf.trained_samples),
            )
            conn.commit()
        return pred
    except Exception as exc:
        logger.debug("shadow_predict_and_log failed: %s: %s",
                     type(exc).__name__, exc)
        return None
