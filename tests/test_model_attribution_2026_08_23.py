"""Model attribution + promotion without a reset (docs/25 step 5.4).

Production shape: one primary trades, challengers shadow; promotion
switches the decision-maker without halting or wiping the profile.
For that to be clean, every prediction must carry the model that made
it, and the learned state must scope to the profile's CURRENT model:
  - record_prediction stamps ai_provider/ai_model;
  - the track-record block, meta-model training set and scoreboard
    scope to the current model and state (not blend) other history;
  - promote() swaps primary/shadow config and keys atomically and
    never touches positions.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _pred_db(tmp_path, rows):
    """ai_predictions with attribution; rows = (signal, conf, ret, outcome, model)."""
    path = str(tmp_path / "quantopsai_profile_5.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE ai_predictions (
        id INTEGER PRIMARY KEY, timestamp TEXT, predicted_signal TEXT,
        confidence REAL, actual_return_pct REAL, actual_outcome TEXT,
        status TEXT, data_quality TEXT, features_json TEXT,
        prediction_type TEXT, strategy_type TEXT, regime_at_prediction TEXT,
        resolved_at TEXT, ai_provider TEXT, ai_model TEXT)""")
    conn.executemany(
        "INSERT INTO ai_predictions (timestamp, predicted_signal, confidence, "
        "actual_return_pct, actual_outcome, status, features_json, "
        "prediction_type, resolved_at, ai_provider, ai_model) "
        "VALUES ('2026-08-24 10:00', ?, ?, ?, ?, 'resolved', "
        "'{\"rsi\": 50, \"adx\": 20}', 'directional_long', '2026-08-25 10:00', "
        "'x', ?)", rows)
    conn.commit(); conn.close()
    return path


class TestStamp:
    def test_record_prediction_stamps_the_model(self, tmp_path):
        from ai_tracker import init_tracker_db, record_prediction
        db = str(tmp_path / "quantopsai_profile_9.db")
        init_tracker_db(db)
        from journal import init_db
        init_db(db)   # applies the migration list incl. ai_model
        pid = record_prediction("AAPL", "BUY", 70, "r", 100.0, db_path=db,
                                ai_provider="openai", ai_model="gpt-4.1-nano")
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT ai_provider, ai_model FROM ai_predictions "
                            "WHERE id=?", (pid,)).fetchone() == ("openai", "gpt-4.1-nano")

    def test_pipeline_passes_ctx_model(self):
        import inspect
        import trade_pipeline
        src = inspect.getsource(trade_pipeline)
        idx = src.index("pred_id = record_prediction(")
        call = src[idx:idx + 2500]
        assert 'ai_model=getattr(ctx, "ai_model", None)' in call


class TestScoping:
    def test_track_record_scopes_to_current_model(self, tmp_path):
        import calibration_block as cb
        rows = ([("BUY", 80, 1.0, "win", "gpt-4.1-nano")] * 25
                + [("BUY", 80, -1.0, "loss", "gemini-3.1-flash-lite")] * 40)
        db = _pred_db(tmp_path, rows)
        rec = cb.compute_track_record(db, ai_model="gpt-4.1-nano")
        assert rec["overall"][:2] == (25, 25)
        assert rec["other_models"] == 40
        text = cb.render_track_record(db, ai_model="gpt-4.1-nano")
        assert "All-time: 100% win rate on 25" in text
        assert "40 resolved directional predictions made by a previous model" in text
        # Without a model, everything counts (legacy behaviour).
        assert cb.compute_track_record(db)["overall"][0] == 65

    def test_meta_model_training_set_filters_by_model(self, tmp_path):
        from meta_model import build_training_set
        rows = ([("BUY", 80, 1.0, "win", "gpt-4.1-nano")] * 6
                + [("BUY", 80, -1.0, "loss", "other")] * 6)
        db = _pred_db(tmp_path, rows)
        X, y, _ = build_training_set(db, min_samples=1, ai_model="gpt-4.1-nano")
        assert len(y) == 6 and all(v == 1 for v in y)
        X2, y2, _ = build_training_set(db, min_samples=1)
        assert len(y2) == 12

    def test_scoreboard_scopes_weekly_rows_to_the_arm(self, tmp_path):
        import learning_scoreboard as ls
        rows = ([("BUY", 80, 1.0, "win", "gpt-4.1-nano")] * 10
                + [("BUY", 80, -1.0, "loss", "old-model")] * 10)
        db = _pred_db(tmp_path, rows)
        w = ls.profile_weekly_predictions(db, ai_model="gpt-4.1-nano")["2026-W35"]
        assert (w["n"], w["hits"]) == (10, 10)
        w_all = ls.profile_weekly_predictions(db)["2026-W35"]
        assert w_all["n"] == 20


class TestPromotion:
    def _profile(self, **over):
        p = {"id": 1, "ai_provider": "google", "ai_model": "gemini-3.5-flash-lite",
             "ai_api_key_enc": "ENC-GOOGLE",
             "shadow_models": json.dumps(["openai:gpt-4.1-nano",
                                          "google:gemini-3.7-flash"]),
             "shadow_api_keys_enc": json.dumps({"openai": "ENC-OPENAI"})}
        p.update(over)
        return p

    def test_plan_swaps_primary_and_shadow_and_keys(self):
        from model_promotion import plan_promotion
        plan = plan_promotion(self._profile(), "openai", "gpt-4.1-nano")
        assert (plan["ai_provider"], plan["ai_model"]) == ("openai", "gpt-4.1-nano")
        assert plan["ai_api_key_enc"] == "ENC-OPENAI"
        shadows = json.loads(plan["shadow_models"])
        assert "openai:gpt-4.1-nano" not in shadows          # never shadows itself
        assert "google:gemini-3.5-flash-lite" in shadows      # old primary demoted
        assert "google:gemini-3.7-flash" in shadows
        keys = json.loads(plan["shadow_api_keys_enc"])
        assert keys["google"] == "ENC-GOOGLE"                 # old key kept for the shadow
        assert plan["enable_shadow_eval"] == 1

    def test_same_provider_switch_keeps_the_primary_key(self):
        from model_promotion import plan_promotion
        plan = plan_promotion(self._profile(), "google", "gemini-3.7-flash")
        assert plan["ai_api_key_enc"] == "ENC-GOOGLE"

    def test_refuses_unregistered_already_primary_or_keyless(self):
        from model_promotion import plan_promotion, PromotionError
        with pytest.raises(PromotionError):
            plan_promotion(self._profile(), "openai", "gpt-99")
        with pytest.raises(PromotionError):
            plan_promotion(self._profile(), "google", "gemini-3.5-flash-lite")
        with pytest.raises(PromotionError):
            plan_promotion(self._profile(shadow_api_keys_enc="{}"),
                           "openai", "gpt-4.1-nano")

    def test_promote_writes_config_and_logs_without_touching_trades(
            self, tmp_main_db, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import config
        config.DB_PATH = str(tmp_main_db)
        from models import (create_user, create_trading_profile,
                            update_trading_profile, get_user_profiles)
        uid = create_user("p@t.com", "password123", "P")
        pid = create_trading_profile(uid, "EXP-A1-G35LITE-1", "stocks")
        update_trading_profile(
            pid, ai_provider="google", ai_model="gemini-3.5-flash-lite",
            ai_api_key_enc="ENC-GOOGLE", enable_shadow_eval=1,
            shadow_models=json.dumps(["openai:gpt-4.1-nano"]),
            shadow_api_keys_enc=json.dumps({"openai": "ENC-OPENAI"}))
        from model_promotion import promote
        out = promote(pid, "openai", "gpt-4.1-nano", reason="beat the primary")
        assert out["_to"] == "openai:gpt-4.1-nano"
        p = next(x for x in get_user_profiles(uid) if x["id"] == pid)
        assert (p["ai_provider"], p["ai_model"]) == ("openai", "gpt-4.1-nano")
        assert "google:gemini-3.5-flash-lite" in json.loads(p["shadow_models"])
        conn = sqlite3.connect(str(tmp_main_db))
        assert conn.execute("SELECT COUNT(*) FROM activity_log WHERE "
                            "activity_type='model_promoted'").fetchone()[0] == 1
        # No broker / journal path is touched by promotion.
        import inspect
        import model_promotion
        src = inspect.getsource(model_promotion)
        assert "submit_order" not in src and "get_alpaca_api" not in src
