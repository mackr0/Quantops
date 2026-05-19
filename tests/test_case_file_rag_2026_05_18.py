"""Phase 2 of docs/17 — RAG over the AI's own resolved trades.

Tests the case_file_rag module end-to-end:
  - Text builder produces stable, tokenizable case files
  - Bucketed indicators turn numeric features into discrete tokens
  - Retrieval ranks by cosine similarity and respects top_n / min_sim
  - Wins AND losses are returned (no bias toward warnings)
  - The prompt block renders a compact, AI-readable summary
  - Failure modes (missing DB, empty corpus) are fail-soft
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest


def _mkdb(path):
    """Minimal ai_predictions schema for the retrieval query."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, symbol TEXT,
            predicted_signal TEXT, confidence REAL,
            regime_at_prediction TEXT, strategy_type TEXT,
            features_json TEXT,
            status TEXT, actual_outcome TEXT,
            actual_return_pct REAL, days_held INTEGER,
            resolved_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _seed(path, rows):
    """rows = list of dicts with the relevant fields populated."""
    conn = sqlite3.connect(path)
    for r in rows:
        conn.execute(
            "INSERT INTO ai_predictions "
            "(timestamp, symbol, predicted_signal, confidence, "
            " regime_at_prediction, strategy_type, features_json, "
            " status, actual_outcome, actual_return_pct, days_held, "
            " resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'resolved', ?, ?, ?, ?)",
            (r.get("timestamp", datetime.utcnow().isoformat()),
             r["symbol"], r["signal"], r.get("confidence", 70),
             r.get("regime", "chop_regime"),
             r.get("strategy_type", "mean_reversion"),
             json.dumps(r.get("features", {})),
             r["outcome"], r.get("return_pct", 0.0),
             r.get("days_held", 1),
             r.get("resolved_at", datetime.utcnow().isoformat())),
        )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# build_case_file_text
# ─────────────────────────────────────────────────────────────────────

class TestBuildCaseFileText:
    def test_includes_core_tokens(self):
        from case_file_rag import build_case_file_text
        text = build_case_file_text({
            "symbol": "AAPL", "predicted_signal": "BUY",
            "regime_at_prediction": "chop_regime",
            "strategy_type": "mean_reversion",
            "confidence": 72,
        })
        assert "symbol_AAPL" in text
        assert "signal_BUY" in text
        assert "regime_chop_regime" in text
        assert "strategy_mean_reversion" in text
        assert "confidence_70" in text  # bucketed to nearest 10

    def test_buckets_indicators_from_features(self):
        from case_file_rag import build_case_file_text
        text = build_case_file_text({
            "symbol": "AAPL", "predicted_signal": "BUY",
            "features_json": {"rsi": 72, "volume_ratio": 1.8,
                                "momentum_5d": 4.0, "gap_pct": 0.5},
        })
        assert "rsi_70_80" in text
        assert "volume_ratio_1.5_2.5" in text
        assert "momentum_5d_2_5" in text
        assert "gap_pct_-1_1" in text

    def test_features_json_can_be_string(self):
        """The retrieval path reads features_json from sqlite as a
        TEXT column — accept the string form too."""
        from case_file_rag import build_case_file_text
        text = build_case_file_text({
            "symbol": "AAPL", "predicted_signal": "BUY",
            "features_json": json.dumps({"rsi": 75}),
        })
        assert "rsi_70_80" in text

    def test_include_outcome_false_drops_outcome_tokens(self):
        """At retrieval time the new candidate has no outcome yet —
        omitting those tokens prevents structural mismatch from
        dominating the similarity score."""
        from case_file_rag import build_case_file_text
        with_o = build_case_file_text({
            "symbol": "AAPL", "predicted_signal": "BUY",
            "actual_outcome": "win", "actual_return_pct": 3.5,
        })
        without_o = build_case_file_text({
            "symbol": "AAPL", "predicted_signal": "BUY",
            "actual_outcome": "win", "actual_return_pct": 3.5,
        }, include_outcome=False)
        assert "outcome_win" in with_o
        assert "outcome_win" not in without_o

    def test_handles_missing_fields_gracefully(self):
        from case_file_rag import build_case_file_text
        # Just symbol — no crash, returns a usable string
        assert build_case_file_text({"symbol": "AAPL"}) == "symbol_AAPL"
        # Totally empty — empty string
        assert build_case_file_text({}) == ""

    def test_non_numeric_feature_skipped(self):
        from case_file_rag import build_case_file_text
        text = build_case_file_text({
            "symbol": "X", "predicted_signal": "BUY",
            "features_json": {"rsi": "not a number"},
        })
        assert "rsi_" not in text


# ─────────────────────────────────────────────────────────────────────
# retrieve_similar — ranking + thresholds
# ─────────────────────────────────────────────────────────────────────

class TestRetrieveSimilar:
    def test_returns_empty_on_missing_db(self, tmp_path):
        from case_file_rag import retrieve_similar
        out = retrieve_similar(str(tmp_path / "no.db"),
                                {"symbol": "AAPL", "predicted_signal": "BUY"})
        assert out == []

    def test_returns_empty_on_empty_corpus(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        from case_file_rag import retrieve_similar
        out = retrieve_similar(db,
                                {"symbol": "AAPL", "predicted_signal": "BUY"})
        assert out == []

    def test_ranks_same_symbol_first(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed(db, [
            {"symbol": "AAPL", "signal": "BUY", "regime": "chop_regime",
             "outcome": "loss", "return_pct": -2.0,
             "features": {"rsi": 72, "volume_ratio": 1.8}},
            {"symbol": "NVDA", "signal": "BUY", "regime": "trend_up",
             "outcome": "win", "return_pct": 4.0,
             "features": {"rsi": 60, "volume_ratio": 2.5}},
        ])
        from case_file_rag import retrieve_similar
        out = retrieve_similar(db, {
            "symbol": "AAPL", "predicted_signal": "BUY",
            "regime_at_prediction": "chop_regime",
            "features_json": {"rsi": 73, "volume_ratio": 1.7},
        })
        assert len(out) >= 1
        assert out[0]["symbol"] == "AAPL"

    def test_returns_both_wins_and_losses(self, tmp_path):
        """Per feedback_self_tuner_must_drift_toward_trading the RAG
        must surface BOTH outcomes — filtering to only warnings would
        bias the AI away from action."""
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed(db, [
            {"symbol": f"S{i}", "signal": "BUY", "regime": "chop_regime",
             "outcome": "win" if i % 2 == 0 else "loss",
             "return_pct": 2.0 if i % 2 == 0 else -2.0,
             "features": {"rsi": 70 + (i % 3), "volume_ratio": 1.8}}
            for i in range(6)
        ])
        from case_file_rag import retrieve_similar
        out = retrieve_similar(db, {
            "symbol": "S0", "predicted_signal": "BUY",
            "regime_at_prediction": "chop_regime",
            "features_json": {"rsi": 71, "volume_ratio": 1.8},
        }, top_n=6, min_similarity=0.0)
        outcomes = {c["actual_outcome"] for c in out}
        assert outcomes == {"win", "loss"}, (
            "Retrieval must return both wins and losses so the AI "
            f"sees both base rates. Got: {outcomes}"
        )

    def test_respects_top_n(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed(db, [
            {"symbol": "S", "signal": "BUY", "regime": "chop_regime",
             "outcome": "win", "return_pct": 2.0,
             "features": {"rsi": 70}}
            for _ in range(10)
        ])
        from case_file_rag import retrieve_similar
        out = retrieve_similar(db, {
            "symbol": "S", "predicted_signal": "BUY",
            "regime_at_prediction": "chop_regime",
            "features_json": {"rsi": 70},
        }, top_n=3)
        assert len(out) == 3

    def test_respects_min_similarity(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed(db, [
            {"symbol": "ZZZ", "signal": "SHORT", "regime": "trend_up",
             "outcome": "loss", "return_pct": -10.0,
             "features": {"rsi": 20}},
        ])
        from case_file_rag import retrieve_similar
        # Candidate is totally different — high min_sim should filter out
        out = retrieve_similar(db, {
            "symbol": "AAA", "predicted_signal": "BUY",
            "regime_at_prediction": "chop_regime",
            "features_json": {"rsi": 80},
        }, top_n=5, min_similarity=0.9)
        assert out == []

    def test_each_case_annotated_with_similarity(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed(db, [
            {"symbol": "S", "signal": "BUY", "regime": "chop_regime",
             "outcome": "win", "return_pct": 2.0,
             "features": {"rsi": 70}},
        ])
        from case_file_rag import retrieve_similar
        out = retrieve_similar(db, {
            "symbol": "S", "predicted_signal": "BUY",
            "regime_at_prediction": "chop_regime",
            "features_json": {"rsi": 70},
        })
        assert out and 0.0 <= out[0]["similarity"] <= 1.0

    def test_pending_predictions_excluded(self, tmp_path):
        """Only resolved cases enter the corpus — a pending row has
        no outcome to learn from yet."""
        db = str(tmp_path / "p.db")
        _mkdb(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, status, actual_outcome) "
            "VALUES ('AAPL', 'BUY', 'pending', NULL)"
        )
        conn.commit()
        conn.close()
        from case_file_rag import retrieve_similar
        out = retrieve_similar(db, {"symbol": "AAPL", "predicted_signal": "BUY"})
        assert out == []

    def test_neutral_outcomes_excluded(self, tmp_path):
        """Neutrals are timeouts where the directional thesis didn't
        play out — they're noise for RAG. Mirror the existing self-
        tuning convention that only counts decisive wins/losses."""
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed(db, [
            {"symbol": "S", "signal": "BUY", "outcome": "neutral",
             "return_pct": 0.1, "features": {"rsi": 70}},
        ])
        from case_file_rag import retrieve_similar
        out = retrieve_similar(db, {"symbol": "S", "predicted_signal": "BUY",
                                     "features_json": {"rsi": 70}})
        assert out == []


# ─────────────────────────────────────────────────────────────────────
# format_cases_for_prompt
# ─────────────────────────────────────────────────────────────────────

class TestFormatForPrompt:
    def test_empty_input_returns_empty_string(self):
        from case_file_rag import format_cases_for_prompt
        assert format_cases_for_prompt([]) == ""

    def test_renders_outcome_and_indicators(self):
        from case_file_rag import format_cases_for_prompt
        out = format_cases_for_prompt([{
            "resolved_at": "2026-05-10T00:00:00",
            "symbol": "AAPL", "predicted_signal": "BUY",
            "regime_at_prediction": "chop_regime",
            "actual_outcome": "loss", "actual_return_pct": -2.3,
            "days_held": 3, "similarity": 0.85,
            "features_json": {"rsi": 72, "momentum_5d": 4.2},
        }])
        assert "AAPL" in out
        assert "LOSS" in out
        assert "-2.3%" in out
        assert "sim=0.85" in out
        assert "rsi=72" in out

    def test_handles_missing_indicator_block(self):
        from case_file_rag import format_cases_for_prompt
        out = format_cases_for_prompt([{
            "symbol": "AAPL", "predicted_signal": "BUY",
            "actual_outcome": "win", "actual_return_pct": 1.5,
        }])
        # First line still renders even without features
        assert "AAPL" in out
        assert "WIN" in out


# ─────────────────────────────────────────────────────────────────────
# build_prompt_block — end to end
# ─────────────────────────────────────────────────────────────────────

class TestBuildPromptBlock:
    def test_empty_corpus_returns_empty_string(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        from case_file_rag import build_prompt_block
        assert build_prompt_block(db, {"symbol": "AAPL",
                                         "predicted_signal": "BUY"}) == ""

    def test_includes_header_with_symbol(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed(db, [
            {"symbol": "AAPL", "signal": "BUY", "regime": "chop_regime",
             "outcome": "win", "return_pct": 2.0,
             "features": {"rsi": 70}},
        ])
        from case_file_rag import build_prompt_block
        block = build_prompt_block(db, {
            "symbol": "AAPL", "predicted_signal": "BUY",
            "regime_at_prediction": "chop_regime",
            "features_json": {"rsi": 70},
        })
        assert "SIMILAR PAST CASES" in block
        assert "FOR AAPL" in block

    def test_cap_at_top_n_by_default(self, tmp_path):
        db = str(tmp_path / "p.db")
        _mkdb(db)
        _seed(db, [
            {"symbol": "S", "signal": "BUY", "regime": "chop_regime",
             "outcome": "win", "return_pct": 2.0,
             "features": {"rsi": 70}}
            for _ in range(20)
        ])
        from case_file_rag import build_prompt_block
        block = build_prompt_block(db, {
            "symbol": "S", "predicted_signal": "BUY",
            "regime_at_prediction": "chop_regime",
            "features_json": {"rsi": 70},
        })
        # Default top_n=3 → block has at most 3 enumerated lines
        numbered = [ln for ln in block.split("\n")
                     if ln.strip().startswith(("1.", "2.", "3.", "4."))]
        assert len(numbered) <= 3


# ─────────────────────────────────────────────────────────────────────
# Prompt integration smoke test
# ─────────────────────────────────────────────────────────────────────

class TestPromptIntegration:
    def test_module_wired_into_ai_analyst(self):
        """The prompt builder must import case_file_rag.build_prompt_block.
        Without this, all the retrieval work is dead code."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "ai_analyst.py").read_text()
        assert "from case_file_rag import build_prompt_block" in src or \
               "case_file_rag" in src, (
            "ai_analyst.py must import case_file_rag — without it the "
            "retrieved cases never reach the prompt."
        )
