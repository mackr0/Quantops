"""Tests for meta_model.py — Phase 1 of Quant Fund Evolution.

Covers:
  - Feature extraction consistency
  - Training with synthetic data
  - Prediction output bounds
  - Graceful degradation (no model, bad data, empty features)
  - Model persistence round-trip
"""

import json
import os
import random
import sqlite3
import tempfile

import pytest


class TestFeatureExtraction:
    """Feature extraction must produce consistent, numeric vectors."""

    def test_extracts_basic_features(self):
        from meta_model import extract_features
        features = {
            "rsi": 45.5, "adx": 22.0, "mfi": 60.0, "score": 2,
            "signal": "BUY", "_regime": "bull",
        }
        result = extract_features(features)
        assert result is not None
        assert result["rsi"] == 45.5
        assert result["adx"] == 22.0
        assert result["score"] == 2.0
        # One-hot encoding
        assert result["signal_BUY"] == 1.0
        assert result["signal_SELL"] == 0.0
        assert result["_regime_bull"] == 1.0
        assert result["_regime_bear"] == 0.0

    def test_missing_features_default_zero(self):
        from meta_model import extract_features
        result = extract_features({"rsi": 50})
        assert result["adx"] == 0.0
        assert result["mfi"] == 0.0

    def test_none_input_returns_none(self):
        from meta_model import extract_features
        assert extract_features(None) is None
        assert extract_features({}) is None  # empty dict should return None-ish

    def test_consistent_output_shape(self):
        """Two calls with same input produce same feature names."""
        from meta_model import extract_features
        a = extract_features({"rsi": 50, "signal": "BUY"})
        b = extract_features({"rsi": 30, "signal": "SELL"})
        assert sorted(a.keys()) == sorted(b.keys())

    def test_non_numeric_values_handled(self):
        from meta_model import extract_features
        result = extract_features({"rsi": "not_a_number", "adx": None})
        assert result["rsi"] == 0.0
        assert result["adx"] == 0.0


class TestTraining:
    """Meta-model training must produce a working classifier."""

    def _make_synthetic_db(self, path, n_samples=150):
        """Create a mock ai_predictions table with synthetic data."""
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                symbol TEXT,
                predicted_signal TEXT,
                confidence REAL,
                reasoning TEXT,
                price_at_prediction REAL,
                status TEXT,
                actual_outcome TEXT,
                actual_return_pct REAL,
                features_json TEXT
            )
        """)
        random.seed(42)
        for i in range(n_samples):
            # Create a pattern: high RSI + volatile regime -> losses more likely
            rsi = random.uniform(20, 80)
            adx = random.uniform(0, 50)
            regime = random.choice(["bull", "bear", "sideways", "volatile"])
            features = {
                "rsi": rsi, "adx": adx, "mfi": random.uniform(20, 80),
                "score": random.randint(-3, 3), "signal": random.choice(["BUY", "SELL"]),
                "_regime": regime, "volume_ratio": random.uniform(0.5, 3.0),
            }
            # Label: biased by features so model can learn something
            win_prob = 0.3 + (50 - abs(rsi - 50)) * 0.01  # favor mid-RSI
            if regime == "volatile":
                win_prob -= 0.15
            outcome = "win" if random.random() < win_prob else "loss"
            conn.execute(
                "INSERT INTO ai_predictions (timestamp, symbol, predicted_signal, "
                "confidence, reasoning, price_at_prediction, status, actual_outcome, "
                "features_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-01-01", "TEST", "BUY", 60, "test", 100.0,
                 "resolved", outcome, json.dumps(features))
            )
        conn.commit()
        conn.close()

    def test_build_training_set_with_enough_data(self, tmp_path):
        from meta_model import build_training_set
        db = str(tmp_path / "test.db")
        self._make_synthetic_db(db, 150)
        X, y, feature_names = build_training_set(db, min_samples=100)
        assert X is not None
        assert y is not None
        assert feature_names is not None
        assert len(X) >= 100
        assert len(X) == len(y)
        assert all(v in (0, 1) for v in y)

    def test_build_training_set_insufficient_data(self, tmp_path):
        from meta_model import build_training_set
        db = str(tmp_path / "test.db")
        self._make_synthetic_db(db, 20)  # below min
        X, y, feature_names = build_training_set(db, min_samples=100)
        assert X is None
        assert y is None

    def test_train_meta_model(self, tmp_path):
        from meta_model import build_training_set, train_meta_model
        db = str(tmp_path / "test.db")
        self._make_synthetic_db(db, 200)
        X, y, feature_names = build_training_set(db, min_samples=100)
        bundle = train_meta_model(X, y, feature_names)

        assert "model" in bundle
        assert "feature_names" in bundle
        assert "metrics" in bundle
        assert "feature_importance" in bundle

        assert 0 <= bundle["metrics"]["accuracy"] <= 1
        assert 0 <= bundle["metrics"]["auc"] <= 1
        assert bundle["metrics"]["n_samples"] == len(X)

        # Feature importance should be sorted
        importances = [i for _, i in bundle["feature_importance"]]
        assert importances == sorted(importances, reverse=True)


class TestPrediction:
    """Prediction must return probabilities in [0, 1] and handle edge cases."""

    def test_predict_returns_probability(self, tmp_path):
        from meta_model import build_training_set, train_meta_model, predict_probability

        # Build a small valid model
        db = str(tmp_path / "test.db")
        TestTraining()._make_synthetic_db(db, 150)
        X, y, feature_names = build_training_set(db, min_samples=100)
        bundle = train_meta_model(X, y, feature_names)

        # Get a probability
        test_features = {"rsi": 50, "adx": 25, "mfi": 55, "signal": "BUY",
                         "_regime": "bull", "volume_ratio": 1.5}
        prob = predict_probability(bundle, test_features)
        assert 0.0 <= prob <= 1.0

    def test_empty_features_returns_neutral(self):
        from meta_model import predict_probability
        # No model at all -> neutral 0.5
        assert predict_probability(None, {"rsi": 50}) == 0.5

    def test_empty_input_returns_neutral(self, tmp_path):
        from meta_model import build_training_set, train_meta_model, predict_probability
        db = str(tmp_path / "test.db")
        TestTraining()._make_synthetic_db(db, 150)
        X, y, feature_names = build_training_set(db, min_samples=100)
        bundle = train_meta_model(X, y, feature_names)

        assert predict_probability(bundle, None) == 0.5
        assert predict_probability(bundle, {}) == 0.5


class TestPersistence:
    """Model save/load round-trip must preserve behavior."""

    def test_save_and_load(self, tmp_path):
        from meta_model import (build_training_set, train_meta_model,
                                 save_model, load_model, predict_probability)
        db = str(tmp_path / "test.db")
        TestTraining()._make_synthetic_db(db, 150)
        X, y, feature_names = build_training_set(db, min_samples=100)
        bundle = train_meta_model(X, y, feature_names)

        path = str(tmp_path / "model.pkl")
        save_model(bundle, path)
        assert os.path.exists(path)

        loaded = load_model(path)
        assert loaded is not None
        assert loaded["feature_names"] == bundle["feature_names"]

        # Same input -> same prediction
        test_features = {"rsi": 50, "adx": 25, "signal": "BUY", "_regime": "bull"}
        p1 = predict_probability(bundle, test_features)
        p2 = predict_probability(loaded, test_features)
        assert abs(p1 - p2) < 0.0001

    def test_load_missing_file_returns_none(self, tmp_path):
        from meta_model import load_model
        assert load_model(str(tmp_path / "nonexistent.pkl")) is None

    def test_model_path_for_profile(self, tmp_path):
        from meta_model import model_path_for_profile
        p = model_path_for_profile(42, base_dir=str(tmp_path))
        assert "42" in p
        assert p.endswith(".pkl")


class TestConfidenceAdjustment:
    """Confidence blending must be bounded and sensible."""

    def test_adjust_confidence_bounds(self):
        from meta_model import adjust_confidence
        # meta=0 halves confidence
        assert adjust_confidence(80, 0.0) == 40
        # meta=0.5 -> 0.75x
        assert adjust_confidence(80, 0.5) == 60
        # meta=1.0 preserves confidence
        assert adjust_confidence(80, 1.0) == 80

    def test_adjust_confidence_clamps(self):
        from meta_model import adjust_confidence
        # Out-of-range meta doesn't produce out-of-range confidence
        result = adjust_confidence(100, 2.0)
        assert 0 <= result <= 100


class TestOrchestration:
    """train_and_save end-to-end behavior."""

    def test_train_and_save_insufficient_data(self, tmp_path):
        from meta_model import train_and_save
        db = str(tmp_path / "test.db")
        TestTraining()._make_synthetic_db(db, 20)
        result = train_and_save(1, db, base_dir=str(tmp_path))
        assert result is None

    def test_train_and_save_creates_file(self, tmp_path):
        from meta_model import train_and_save, model_path_for_profile
        db = str(tmp_path / "test.db")
        TestTraining()._make_synthetic_db(db, 150)
        result = train_and_save(1, db, base_dir=str(tmp_path))
        assert result is not None
        assert os.path.exists(model_path_for_profile(1, str(tmp_path)))
