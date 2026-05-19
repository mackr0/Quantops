"""Phase 4c (shadow mode) — regime ML classifier.

Tests cover:
  - Feature extractor: returns None on too-short series, returns
    the documented 9 features on a healthy series
  - Label generator: bull / bear / sideways / volatile rules
  - Training dataset builder: skips bars without enough history or
    forward labels
  - RegimeClassifier: train + predict roundtrip; save / load
  - Shadow logging: writes the comparison row without ever raising
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import List

import pytest


def _synthetic_history(n: int = 300, base: float = 100.0,
                        drift: float = 0.001, vol: float = 0.01,
                        vix_base: float = 18.0):
    """Generate deterministic synthetic SPY OHLC + VIX series for
    tests. Drift + vol shape the distribution so labels exist
    across regimes."""
    import random
    random.seed(42)
    closes = []
    highs = []
    lows = []
    vix = []
    price = base
    for i in range(n):
        ret = drift + vol * (random.random() - 0.5)
        price *= 1 + ret
        # Crude OHLC: high/low ±0.5% around close
        closes.append(price)
        highs.append(price * 1.005)
        lows.append(price * 0.995)
        vix.append(vix_base + 5 * (random.random() - 0.5))
    return closes, highs, lows, vix


# ─────────────────────────────────────────────────────────────────────
# compute_features
# ─────────────────────────────────────────────────────────────────────

class TestComputeFeatures:
    def test_returns_none_on_short_history(self):
        from regime_classifier_ml import compute_features
        short_close = [100.0] * 50
        short_high = [101.0] * 50
        short_low = [99.0] * 50
        vix = [18.0] * 25
        assert compute_features(short_close, short_high, short_low, vix) is None

    def test_returns_expected_keys(self):
        from regime_classifier_ml import compute_features
        c, h, l, v = _synthetic_history(n=300)
        feats = compute_features(c, h, l, v)
        assert feats is not None
        expected = {
            "vix_level", "vix_change_20d",
            "spy_vs_sma50_pct", "spy_vs_sma200_pct",
            "spy_5d_return_pct", "spy_20d_return_pct",
            "breadth_pct", "atr_pct", "sma50_slope_pct",
        }
        assert set(feats.keys()) == expected
        # All values must be finite floats
        for k, val in feats.items():
            assert isinstance(val, float), f"{k} should be float"

    def test_handles_zero_division_safely(self):
        from regime_classifier_ml import compute_features
        # All zeros — should fall into the guard and return None or a
        # safe value, never raise
        zero_close = [0.0] * 300
        zero_high = [0.0] * 300
        zero_low = [0.0] * 300
        vix = [18.0] * 25
        # Either None or a defined dict — never an exception
        try:
            out = compute_features(zero_close, zero_high, zero_low, vix)
            assert out is None or isinstance(out, dict)
        except ZeroDivisionError:
            pytest.fail("compute_features must not raise on zero prices")


# ─────────────────────────────────────────────────────────────────────
# label_from_forward
# ─────────────────────────────────────────────────────────────────────

class TestLabelFromForward:
    def test_returns_none_when_no_forward_horizon(self):
        from regime_classifier_ml import label_from_forward
        closes = [100.0] * 10
        assert label_from_forward(closes, 5, horizon=20) is None

    def test_labels_bull_on_strong_forward_return(self):
        from regime_classifier_ml import label_from_forward
        # Day 0: 100, day 20: 105 = +5% forward, low vol
        closes = [100.0 + i * 0.25 for i in range(40)]
        label = label_from_forward(closes, 5, horizon=20)
        assert label == "bull"

    def test_labels_bear_on_strong_forward_drop(self):
        from regime_classifier_ml import label_from_forward
        closes = [100.0 - i * 0.25 for i in range(40)]
        label = label_from_forward(closes, 5, horizon=20)
        assert label == "bear"

    def test_labels_sideways_on_flat_low_vol(self):
        from regime_classifier_ml import label_from_forward
        closes = [100.0 + 0.05 * (i % 2) for i in range(40)]
        label = label_from_forward(closes, 5, horizon=20)
        assert label == "sideways"

    def test_labels_volatile_on_high_realized_vol(self):
        from regime_classifier_ml import label_from_forward
        # Alternating big swings → very high realized vol
        closes = [100.0]
        for i in range(40):
            closes.append(closes[-1] * (1.05 if i % 2 == 0 else 0.96))
        label = label_from_forward(closes, 5, horizon=20)
        assert label == "volatile"


# ─────────────────────────────────────────────────────────────────────
# build_training_dataset
# ─────────────────────────────────────────────────────────────────────

class TestTrainingDataset:
    def test_produces_aligned_X_y(self):
        from regime_classifier_ml import build_training_dataset
        c, h, l, v = _synthetic_history(n=400)
        X, y, feats = build_training_dataset(c, h, l, v)
        assert len(X) == len(y) == len(feats)
        # Each X row matches the sorted-feature-name length
        if X:
            assert len(X[0]) == len(feats[0])

    def test_skips_too_early_bars(self):
        """Must require 200 bars of history before emitting the first
        sample."""
        from regime_classifier_ml import build_training_dataset
        c, h, l, v = _synthetic_history(n=400)
        X, y, _ = build_training_dataset(c, h, l, v)
        # n=400, need >=200 history + >=20 forward → max valid index
        # range is [200, 380). So at most 180 samples.
        assert len(X) <= 180


# ─────────────────────────────────────────────────────────────────────
# RegimeClassifier — train / predict / save / load
# ─────────────────────────────────────────────────────────────────────

class TestRegimeClassifier:
    def _make_trained_clf(self, tmp_path):
        from regime_classifier_ml import (
            RegimeClassifier, build_training_dataset,
        )
        c, h, l, v = _synthetic_history(n=600)
        X, y, feats = build_training_dataset(c, h, l, v)
        # Need at least 2 classes for the classifier to fit
        if len(set(y)) < 2:
            # Force diversity by appending a synthetic bear sample
            X.append(X[0])
            y.append("bear" if y[0] != "bear" else "bull")
            feats.append(feats[0])
        clf = RegimeClassifier()
        feature_names = sorted(feats[0].keys())
        clf.train(X, y, feature_names)
        return clf, feats[0]

    def test_train_predict_roundtrip(self, tmp_path):
        clf, sample_feats = self._make_trained_clf(tmp_path)
        pred = clf.predict(sample_feats)
        assert pred is not None
        assert pred["regime"] in ("bull", "bear", "sideways", "volatile")
        assert 0.0 <= pred["confidence"] <= 1.0
        # probas dict sums to ~1
        assert abs(sum(pred["probas"].values()) - 1.0) < 1e-6

    def test_predict_returns_none_when_features_missing(self, tmp_path):
        clf, _ = self._make_trained_clf(tmp_path)
        # Drop a feature
        pred = clf.predict({"vix_level": 18.0})
        assert pred is None

    def test_save_load_roundtrip(self, tmp_path):
        from regime_classifier_ml import RegimeClassifier
        clf, sample_feats = self._make_trained_clf(tmp_path)
        path = str(tmp_path / "regime.pkl")
        clf.save(path)
        loaded = RegimeClassifier.load(path)
        assert loaded is not None
        assert loaded.feature_names == clf.feature_names
        pred1 = clf.predict(sample_feats)
        pred2 = loaded.predict(sample_feats)
        assert pred1["regime"] == pred2["regime"]

    def test_load_missing_path_returns_none(self, tmp_path):
        from regime_classifier_ml import RegimeClassifier
        assert RegimeClassifier.load(str(tmp_path / "nope.pkl")) is None


# ─────────────────────────────────────────────────────────────────────
# shadow_predict_and_log — writes the comparison row safely
# ─────────────────────────────────────────────────────────────────────

class TestShadowLog:
    def test_writes_row_when_model_present(self, tmp_path):
        from regime_classifier_ml import (
            RegimeClassifier, build_training_dataset,
            shadow_predict_and_log,
        )
        c, h, l, v = _synthetic_history(n=600)
        X, y, feats = build_training_dataset(c, h, l, v)
        if len(set(y)) < 2:
            X.append(X[0]); y.append("bear" if y[0] != "bear" else "bull"); feats.append(feats[0])
        clf = RegimeClassifier()
        clf.train(X, y, sorted(feats[0].keys()))
        model_path = str(tmp_path / "rc.pkl")
        clf.save(model_path)
        # Reset cache so the test sees the new model
        import regime_classifier_ml as rc
        rc._LOADED["clf"] = None

        db = str(tmp_path / "main.db")
        result = shadow_predict_and_log(
            model_path=model_path, db_path=db,
            rule_regime="bull",
            spy_close=c, spy_high=h, spy_low=l, vix_series=v,
            spy_price_now=c[-1], vix_now=v[-1],
        )
        assert result is not None
        # Row landed in regime_shadow_calls
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT rule_regime, ml_regime FROM regime_shadow_calls"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "bull"
        assert rows[0][1] in ("bull", "bear", "sideways", "volatile")

    def test_returns_none_when_model_missing(self, tmp_path):
        from regime_classifier_ml import shadow_predict_and_log
        import regime_classifier_ml as rc
        rc._LOADED["clf"] = None
        result = shadow_predict_and_log(
            model_path=str(tmp_path / "doesnt_exist.pkl"),
            db_path=str(tmp_path / "m.db"),
            rule_regime="bull",
            spy_close=[100.0] * 300,
            spy_high=[101.0] * 300, spy_low=[99.0] * 300,
            vix_series=[18.0] * 25,
            spy_price_now=100.0, vix_now=18.0,
        )
        assert result is None

    def test_does_not_raise_on_bad_features(self, tmp_path):
        """Shadow logging must never break the production regime
        detection — even if features are bogus."""
        from regime_classifier_ml import shadow_predict_and_log
        import regime_classifier_ml as rc
        rc._LOADED["clf"] = None
        # Short series — compute_features returns None → result None,
        # no exception
        result = shadow_predict_and_log(
            model_path=str(tmp_path / "x.pkl"),
            db_path=str(tmp_path / "m.db"),
            rule_regime="bull",
            spy_close=[100.0] * 10,
            spy_high=[101.0] * 10, spy_low=[99.0] * 10,
            vix_series=[18.0] * 5,
            spy_price_now=100.0, vix_now=18.0,
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────────
# Structural — module is importable + no production-mutation pattern
# ─────────────────────────────────────────────────────────────────────

class TestNoProductionMutation:
    """The shadow module MUST NOT alter the rule-based regime
    decision. Pinned with a source scan — promotion logic must be
    a separate explicit change."""

    def test_module_does_not_mutate_market_regime_result(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "regime_classifier_ml.py").read_text()
        # No place in this module should be writing back to the
        # rule path's result dict or returning a regime label that
        # would replace the rule output.
        forbidden = [
            'result["regime"] =',
            "result['regime'] =",
            'rule_regime =',  # would suggest a mutation of caller state
        ]
        for f in forbidden:
            assert f not in src, (
                f"regime_classifier_ml.py must not contain {f!r} — shadow "
                "module must NEVER mutate production regime decision."
            )
