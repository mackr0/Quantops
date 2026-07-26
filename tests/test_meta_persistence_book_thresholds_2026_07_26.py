"""Meta-score persistence + book-scaled tuner thresholds (2026-07-26).

Two gaps found in the learning-review session:
1. `meta_model_score` / `online_meta_score` were NULL on all 21,000
   predictions ever journaled — the scores were computed in the
   pregate/suppression passes and DISCARDED (record_prediction accepted
   the kwargs all along; no caller carried them). Without persistence,
   "is the meta-model predictive" (WR by score quartile) is
   uncomputable and the model's veto power is unfalsifiable.
2. The tuner's dollar triggers were sized for $200K books (avg_loss <
   −$200 ≈ 0.1% of book). On $25K books that demanded 0.8%-of-book
   losses — 8× stricter — so the tuner sat idle on exactly the worst
   profiles (A3-25K-Candidate, 13–18% WR).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────────────
# Meta-score persistence
# ─────────────────────────────────────────────────────────────────────

class TestPregateStampsScores:
    def test_kept_and_spared_candidates_carry_meta_prob(self):
        from trade_pipeline import _meta_pregate_candidates
        cands = [{"symbol": "AAA", "price": 10, "signal": "BUY"},
                 {"symbol": "BBB", "price": 10, "signal": "BUY"},
                 {"symbol": "CCC", "price": 10, "signal": "BUY"}]
        probs = {"AAA": 0.9, "BBB": 0.2, "CCC": 0.6}
        ctx = SimpleNamespace(db_path=None, profile_id=1,
                              meta_pregate_threshold=0.5)

        def fake_predict(bundle, features):
            # features carries the candidate's fields; symbol survives
            return probs.get(features.get("symbol"), 0.5)

        import meta_model as mm
        with patch.object(mm, "load_meta_bundle",
                          return_value=object(), create=True), \
             patch.object(mm, "predict_probability",
                          side_effect=fake_predict):
            out = _meta_pregate_candidates(cands, ctx)
        stamped = {c["symbol"]: c.get("_meta_prob") for c in cands}
        # every scored candidate carries its stamp — including any the
        # trimmer spared/dropped (the dicts are shared objects)
        assert stamped["AAA"] == 0.9
        assert stamped["CCC"] == 0.6
        assert stamped["BBB"] == 0.2
        assert out  # pregate returned a list (fail-open guarantees)

    def test_record_site_reads_the_stamp(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        assert '_meta_score = c.get(\n                    "_meta_prob"' \
            in src or '_meta_score = c.get("_meta_prob"' in src, (
            "record_prediction's meta_model_score no longer sources "
            "the candidate's pregate stamp — the column goes NULL "
            "again."
        )

    def test_writeback_call_present(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        assert "update_prediction_meta_scores(" in src, (
            "the STEP-4.5 score write-back is gone — selected trades' "
            "rows lose their highest-fidelity scores."
        )


class TestScoreWriteback:
    def test_round_trip(self, tmp_path):
        from journal import init_db
        from ai_tracker import (init_tracker_db, record_prediction,
                                update_prediction_meta_scores)
        db = str(tmp_path / "p.db")
        init_db(db)          # full current schema (incl. migrations)
        init_tracker_db(db)
        pid = record_prediction(
            symbol="NVDA", predicted_signal="BUY", confidence=60,
            reasoning="t", price_at_prediction=100.0, db_path=db,
            meta_model_score=0.41,
        )
        assert pid and pid > 0
        assert update_prediction_meta_scores(pid, 0.62, 0.55, db_path=db)
        conn = sqlite3.connect(db)
        m, o = conn.execute(
            "SELECT meta_model_score, online_meta_score "
            "FROM ai_predictions WHERE id=?", (pid,)).fetchone()
        assert (m, o) == (0.62, 0.55)

    def test_none_scores_noop(self, tmp_path):
        from ai_tracker import update_prediction_meta_scores
        assert update_prediction_meta_scores(1, None, None) is False
        assert update_prediction_meta_scores(None, 0.5, 0.5) is False


# ─────────────────────────────────────────────────────────────────────
# Book-scaled thresholds
# ─────────────────────────────────────────────────────────────────────

class TestBookScaledDollars:
    def test_scaling(self):
        from self_tuning import _book_scaled_dollars
        assert _book_scaled_dollars(
            SimpleNamespace(initial_capital=200_000), 200) == 200
        assert _book_scaled_dollars(
            SimpleNamespace(initial_capital=25_000), 200) == 25
        assert _book_scaled_dollars(
            SimpleNamespace(initial_capital=700_000), 200) == 700

    def test_floor_and_missing_capital(self):
        from self_tuning import _book_scaled_dollars
        # 1K book: raw scale 0.005 → floored at 10% of base
        assert _book_scaled_dollars(
            SimpleNamespace(initial_capital=1_000), 200) == 20
        # missing/zero capital → base unchanged (safe default)
        assert _book_scaled_dollars(SimpleNamespace(), 200) == 200
        assert _book_scaled_dollars(
            SimpleNamespace(initial_capital=0), 200) == 200


class TestOptimizerFiresOnSmallBooks:
    def _db_with_losses(self, tmp_path, avg_loss):
        db = str(tmp_path / "t.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, "
                     "pnl REAL, data_quality TEXT)")
        for _ in range(10):
            conn.execute("INSERT INTO trades (pnl) VALUES (?)",
                         (avg_loss,))
        conn.commit()
        conn.row_factory = sqlite3.Row
        return conn

    def test_25k_book_small_losses_now_trigger(self, tmp_path):
        """The A3-25K shape: 30% WR, avg loss −$40. Pre-fix the −$200
        constant silenced the reducer; book-scaled (−$25 on 25K) it
        fires."""
        import self_tuning as st
        conn = self._db_with_losses(tmp_path, -40.0)
        ctx = SimpleNamespace(initial_capital=25_000,
                              max_total_positions=999)
        calls = []
        with patch.object(st, "_safe_change_guarded",
                          return_value=True), \
             patch.object(st, "_apply_param_change",
                          side_effect=lambda *a, **k:
                          (calls.append(a), (749, True, " [g]"))[1]):
            out = st._optimize_max_total_positions(
                conn, ctx, 216, 1, overall_wr=30.0, resolved=300)
        assert calls, (
            "the concentration reducer did not fire on the 25K book "
            "with −$40 avg losses — the small-book threshold gap is "
            "back."
        )
        assert out is not None

    def test_200k_book_behavior_unchanged(self, tmp_path):
        """Backward compat: −$150 avg loss on a 200K book must NOT
        fire (threshold −$200, exactly as before the change)."""
        import self_tuning as st
        conn = self._db_with_losses(tmp_path, -150.0)
        ctx = SimpleNamespace(initial_capital=200_000,
                              max_total_positions=999)
        calls = []
        with patch.object(st, "_safe_change_guarded",
                          return_value=True), \
             patch.object(st, "_apply_param_change",
                          side_effect=lambda *a, **k:
                          (calls.append(a), (749, True, " [g]"))[1]):
            st._optimize_max_total_positions(
                conn, ctx, 218, 1, overall_wr=30.0, resolved=300)
        assert not calls, (
            "a −$150 avg loss fired the reducer on a 200K book — the "
            "200K threshold must remain exactly −$200."
        )
