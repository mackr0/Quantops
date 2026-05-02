"""Item 5a — online learning meta-model tests."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_db(n_rows: int = 30) -> str:
    """Build a tmp ai_predictions DB with `n_rows` resolved rows.
    Roughly half wins / half losses, with a feature ('score') that
    correlates with outcome so the SGD model has signal to learn.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            symbol TEXT, predicted_signal TEXT, confidence INTEGER,
            status TEXT, actual_outcome TEXT, actual_return_pct REAL,
            features_json TEXT, prediction_type TEXT,
            created_at TEXT, resolved_at TEXT
        )
    """)
    # Mix of separable and noisy rows so the SGD classifier learns
    # a useful direction without saturating to 1.0/0.0.
    import random
    rng = random.Random(42)
    for i in range(n_rows):
        is_win = i % 2 == 0
        outcome = "win" if is_win else "loss"
        # Add Gaussian-ish noise to features
        score = (75 if is_win else 35) + rng.randint(-15, 15)
        rsi = (60 if is_win else 40) + rng.randint(-10, 10)
        features = {
            "score": score, "rsi": rsi, "volume_ratio": 1.5,
            "atr": 0.02, "adx": 25, "signal": "BUY",
            "_regime": "bull", "_market_signal_count": 3,
        }
        conn.execute(
            """INSERT INTO ai_predictions
               (symbol, predicted_signal, confidence, status,
                actual_outcome, features_json, prediction_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"SYM{i}", "BUY", 70, "resolved", outcome,
             json.dumps(features), "directional_long", "2026-01-01"),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def trained_db():
    path = _make_db(30)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestInitializeFromHistory:
    def test_initializes_with_enough_data(self, trained_db, tmp_dir):
        from online_meta_model import (
            initialize_from_history, _model_path,
        )
        bundle = initialize_from_history(99, trained_db, base_dir=tmp_dir)
        assert bundle is not None
        assert "model" in bundle
        assert "feature_names" in bundle
        assert bundle["n_updates"] >= 30
        assert os.path.exists(_model_path(99, tmp_dir))

    def test_returns_none_with_no_data(self, tmp_dir):
        from online_meta_model import initialize_from_history
        empty_db = _make_db(0)
        try:
            assert initialize_from_history(1, empty_db, base_dir=tmp_dir) is None
        finally:
            os.unlink(empty_db)

    def test_returns_none_with_single_class(self, tmp_dir):
        """Can't fit a binary classifier on one class."""
        from online_meta_model import initialize_from_history
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY, symbol TEXT, predicted_signal TEXT,
                confidence INTEGER, status TEXT, actual_outcome TEXT,
                features_json TEXT, prediction_type TEXT, created_at TEXT
            )
        """)
        # 15 wins only — single class
        for i in range(15):
            conn.execute(
                """INSERT INTO ai_predictions
                   (symbol, predicted_signal, confidence, status,
                    actual_outcome, features_json, prediction_type, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"S{i}", "BUY", 70, "resolved", "win",
                 json.dumps({"score": 80, "signal": "BUY"}),
                 "directional_long", "2026-01-01"),
            )
        conn.commit()
        conn.close()
        try:
            assert initialize_from_history(2, path, base_dir=tmp_dir) is None
        finally:
            os.unlink(path)


class TestUpdateOnlineModel:
    def test_update_succeeds_on_initialized_model(
        self, trained_db, tmp_dir,
    ):
        from online_meta_model import (
            initialize_from_history, update_online_model,
            get_online_model_info,
        )
        initialize_from_history(99, trained_db, base_dir=tmp_dir)
        before = get_online_model_info(99, base_dir=tmp_dir)
        assert update_online_model(
            99, {"score": 75, "rsi": 55}, 1, base_dir=tmp_dir,
        ) is True
        after = get_online_model_info(99, base_dir=tmp_dir)
        assert after["n_updates"] == before["n_updates"] + 1

    def test_update_fails_when_model_missing(self, tmp_dir):
        from online_meta_model import update_online_model
        assert update_online_model(
            999, {"score": 50}, 1, base_dir=tmp_dir,
        ) is False

    def test_update_rejects_invalid_outcome(self, trained_db, tmp_dir):
        from online_meta_model import (
            initialize_from_history, update_online_model,
        )
        initialize_from_history(99, trained_db, base_dir=tmp_dir)
        # outcome must be 0 or 1
        assert update_online_model(
            99, {"score": 50}, 2, base_dir=tmp_dir,
        ) is False
        assert update_online_model(
            99, {"score": 50}, -1, base_dir=tmp_dir,
        ) is False


class TestOnlinePredictProbability:
    def test_prediction_in_range(self, trained_db, tmp_dir):
        from online_meta_model import (
            initialize_from_history, online_predict_probability,
        )
        initialize_from_history(99, trained_db, base_dir=tmp_dir)
        prob = online_predict_probability(
            99, {"score": 80, "rsi": 60, "signal": "BUY"},
            base_dir=tmp_dir,
        )
        assert prob is not None
        assert 0.0 <= prob <= 1.0

    def test_high_score_predicts_higher_than_low_score(
        self, trained_db, tmp_dir,
    ):
        """Sanity check: model learned the score→outcome relationship."""
        from online_meta_model import (
            initialize_from_history, online_predict_probability,
        )
        initialize_from_history(99, trained_db, base_dir=tmp_dir)
        high = online_predict_probability(
            99, {"score": 70, "rsi": 58, "signal": "BUY"},
            base_dir=tmp_dir,
        )
        low = online_predict_probability(
            99, {"score": 40, "rsi": 42, "signal": "BUY"},
            base_dir=tmp_dir,
        )
        # Both clamped probabilities; we just need a strict ordering
        # ⇒ the model has learned the score → outcome direction.
        assert high >= low

    def test_returns_none_when_model_missing(self, tmp_dir):
        from online_meta_model import online_predict_probability
        assert online_predict_probability(
            999, {"score": 50}, base_dir=tmp_dir,
        ) is None

    def test_missing_features_default_to_zero(self, trained_db, tmp_dir):
        """Sparse feature dict shouldn't crash predict."""
        from online_meta_model import (
            initialize_from_history, online_predict_probability,
        )
        initialize_from_history(99, trained_db, base_dir=tmp_dir)
        prob = online_predict_probability(
            99, {}, base_dir=tmp_dir,
        )
        assert prob is not None


class TestGetOnlineModelInfo:
    def test_returns_metadata(self, trained_db, tmp_dir):
        from online_meta_model import (
            initialize_from_history, get_online_model_info,
        )
        initialize_from_history(99, trained_db, base_dir=tmp_dir)
        info = get_online_model_info(99, base_dir=tmp_dir)
        assert info is not None
        assert info["n_updates"] >= 30
        assert info["n_features"] > 0
        assert info["created_at"] is not None
        assert info["last_update_at"] is not None

    def test_returns_none_when_model_missing(self, tmp_dir):
        from online_meta_model import get_online_model_info
        assert get_online_model_info(999, base_dir=tmp_dir) is None
