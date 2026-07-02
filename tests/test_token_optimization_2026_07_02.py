"""Token/latency optimization pass (2026-07-02) — everything except the
ensemble topology (pinned separately in test_ensemble_topology_2026_07_02).

Pins: cycle-level prompt/raw-response dedupe + dataset-builder join, the
features_json stash-blob exclusions, cached-token cost telemetry (capture →
ledger column → discounted pricing), the transcript boilerplate filter, the
rule-panel CONFIRM compression, and the 13-worker pool.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# --- cycle-level prompt dedupe ------------------------------------------------

def test_dataset_builder_joins_cycle_prompt():
    from journal import init_db, _get_conn
    from ai_tracker import record_prediction, build_training_dataset
    db = os.path.join(tempfile.mkdtemp(), "p.db")
    init_db(db)
    # cycle row holds the prompt ONCE
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ai_cycles (cycle_id, profile_id, prompt_text, "
        "raw_response_json) VALUES (?, ?, ?, ?)",
        ("cyc1", 1, "THE CYCLE PROMPT", json.dumps({"trades": []})))
    conn.commit(); conn.close()
    # prediction row carries NO prompt (post-dedupe shape), only cycle_id
    record_prediction("AAPL", "BUY", 70, "r", 100.0, db_path=db,
                      cycle_id="cyc1", prompt_text=None, raw_response=None)
    rows = build_training_dataset(db_path=db, include_unresolved=True)
    real = [r for r in rows if r.get("is_real")]
    assert real and real[0]["prompt_text"] == "THE CYCLE PROMPT"
    assert json.loads(real[0]["raw_response_json"]) == {"trades": []}


def test_dataset_builder_prefers_row_copy_for_legacy_rows():
    from journal import init_db
    from ai_tracker import record_prediction, build_training_dataset
    db = os.path.join(tempfile.mkdtemp(), "p.db")
    init_db(db)
    record_prediction("AAPL", "BUY", 70, "r", 100.0, db_path=db,
                      cycle_id="cyc9", prompt_text="ROW PROMPT",
                      raw_response={"legacy": True})
    rows = build_training_dataset(db_path=db, include_unresolved=True)
    real = [r for r in rows if r.get("is_real")]
    assert real[0]["prompt_text"] == "ROW PROMPT"     # pre-dedupe rows intact


def test_features_payload_excludes_stash_blobs():
    # Structural pin: the prompt-render stash blobs (81% of features_json)
    # must be excluded from the per-row snapshot. _panel_verdicts is
    # separately persisted as rule_votes_json; the cycle context lives on
    # ai_cycles.
    src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                            "trade_pipeline.py")).read()
    for key in ("_market_context", "_portfolio", "_panel_verdicts"):
        assert f'"{key}"' in src.split("features_payload = {")[1][:900], (
            f"{key} must be excluded from features_payload")


def test_migration_adds_new_columns_to_legacy_db():
    # Review H2: the universal migration uses an EXPLICIT per-table column
    # list — the new columns must be in it, or every pre-change prod DB
    # silently lacks them (cycle-mint insert fails every cycle; prompt data
    # permanently lost; cached telemetry never records).
    db = os.path.join(tempfile.mkdtemp(), "legacy.db")
    conn = sqlite3.connect(db)
    # Pre-change table shapes (no prompt_text / raw_response_json /
    # cached_tokens).
    conn.execute("CREATE TABLE ai_cycles (cycle_id TEXT PRIMARY KEY, "
                 "timestamp TEXT, profile_id INTEGER, regime TEXT, vix REAL, "
                 "ai_reasoning TEXT, shortlist_json TEXT, "
                 "market_context_json TEXT, sector_rotation_json TEXT, "
                 "learned_patterns_json TEXT, meta_model_stats_json TEXT, "
                 "ensemble_summary_json TEXT, n_trades_selected INTEGER, "
                 "n_candidates_in_shortlist INTEGER)")
    conn.execute("CREATE TABLE ai_cost_ledger (id INTEGER PRIMARY KEY, "
                 "timestamp TEXT, provider TEXT, model TEXT, "
                 "input_tokens INTEGER, output_tokens INTEGER, purpose TEXT, "
                 "estimated_cost_usd REAL)")
    conn.commit(); conn.close()
    from journal import init_db
    init_db(db)
    conn = sqlite3.connect(db)
    cyc_cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_cycles)")}
    led_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_cost_ledger)")}
    conn.close()
    assert {"prompt_text", "raw_response_json"} <= cyc_cols, cyc_cols
    assert "cached_tokens" in led_cols, led_cols


def test_save_cycle_data_roundtrips_prompt_with_full_stats(tmp_path,
                                                           monkeypatch):
    # Review H1: the full-stats cycle write must carry the prompt (the
    # REPLACE overwrites the whole row) — the values are THREADED IN as
    # parameters; referencing the caller's locals raised NameError every
    # cycle and silently dropped the entire full row.
    import os as _os
    monkeypatch.chdir(tmp_path)          # the JSON side-file lands here
    from types import SimpleNamespace
    from journal import init_db
    from trade_pipeline import _save_cycle_data
    db = str(tmp_path / "p.db")
    init_db(db)
    ctx = SimpleNamespace(db_path=db, profile_id=7, display_name="T")
    _save_cycle_data(
        ctx, candidates_data=[], shortlist=[], ai_trades=[],
        portfolio_reasoning="the reasoning", market_ctx={},
        regime_info={"regime": "neutral", "vix": 15.0},
        cycle_id="cyc42", cycle_prompt="THE PROMPT",
        cycle_raw_response_json='{"trades": []}',
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT ai_reasoning, prompt_text, raw_response_json, regime "
        "FROM ai_cycles WHERE cycle_id='cyc42'").fetchone()
    conn.close()
    assert row is not None, "full ai_cycles row must be written"
    assert row[0] == "the reasoning"          # full stats present
    assert row[1] == "THE PROMPT"             # prompt carried by the REPLACE
    assert row[2] == '{"trades": []}'
    assert row[3] == "neutral"


# --- cached-token cost telemetry ----------------------------------------------

def test_estimate_cost_discounts_cached_tokens():
    from ai_pricing import estimate_cost_usd
    full = estimate_cost_usd("gemini-3.1-flash-lite", 1_000_000, 0)
    half_cached = estimate_cost_usd("gemini-3.1-flash-lite", 1_000_000, 0,
                                    cached_tokens=500_000)
    # 500K at $0.25/M + 500K at $0.025/M = 0.125 + 0.0125
    assert full == pytest.approx(0.25)
    assert half_cached == pytest.approx(0.1375)
    # cached can never exceed input (clamped), never negative pricing
    clamped = estimate_cost_usd("gemini-3.1-flash-lite", 100_000, 0,
                                cached_tokens=10_000_000)
    assert clamped == pytest.approx(100_000 * 0.25 * 0.10 / 1_000_000)


def test_ledger_stores_cached_tokens_with_discounted_cost():
    from journal import init_db
    from ai_cost_ledger import log_ai_call
    db = os.path.join(tempfile.mkdtemp(), "p.db")
    init_db(db)
    log_ai_call(db, "google", "gemini-3.1-flash-lite", 10_000, 500,
                purpose="batch_select", cached_tokens=4_000)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT input_tokens, cached_tokens, estimated_cost_usd "
        "FROM ai_cost_ledger").fetchone()
    conn.close()
    assert row[0] == 10_000 and row[1] == 4_000
    expected = (6_000 * 0.25 + 4_000 * 0.025 + 500 * 1.50) / 1_000_000
    assert row[2] == pytest.approx(expected, rel=1e-4)


def test_ledger_legacy_table_fallback():
    # A hand-built table WITHOUT cached_tokens must still get the row
    # (legacy INSERT shape), not drop it.
    db = os.path.join(tempfile.mkdtemp(), "p.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ai_cost_ledger (id INTEGER PRIMARY KEY, "
                 "timestamp TEXT DEFAULT (datetime('now')), provider TEXT, "
                 "model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
                 "purpose TEXT, estimated_cost_usd REAL, call_id TEXT)")
    conn.commit(); conn.close()
    from ai_cost_ledger import log_ai_call
    log_ai_call(db, "google", "gemini-3.1-flash-lite", 1000, 100,
                cached_tokens=500)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM ai_cost_ledger").fetchone()[0]
    conn.close()
    assert n == 1


def test_provider_dispatch_normalizes_to_four_tuple(monkeypatch):
    import ai_providers as ap
    monkeypatch.setattr(ap, "_call_anthropic",
                        lambda *a: ("txt", 10, 5))          # legacy 3-tuple
    monkeypatch.setattr(ap, "_call_google",
                        lambda *a: ("txt", 10, 5, 7))       # new 4-tuple
    assert ap._call_provider("anthropic", "p", "m", "k", 10) == \
        ("txt", 10, 5, 0)
    assert ap._call_provider("google", "p", "m", "k", 10) == \
        ("txt", 10, 5, 7)


# --- transcript boilerplate filter ---------------------------------------------

def test_sentiment_phrase_filter_drops_boilerplate():
    from sec_filings import _filter_sentiment_phrases
    junk = ("announced its results for the quarter ended April 26, 2026, "
            "financial information and commentary by the CFO")
    real = "record data center revenue with strong sequential growth"
    out = _filter_sentiment_phrases([junk, real, ""])
    assert out == [real]
    # length hard-cap
    long_real = "x" * 500
    assert len(_filter_sentiment_phrases([long_real])[0]) == 140


# --- rule-panel CONFIRM compression ---------------------------------------------

def test_panel_confirms_render_as_name_list():
    from deterministic_specialists import format_panel_for_prompt
    verdicts = [
        {"name": "rsi_overbought", "severity": "CAUTION",
         "reasoning": "RSI 81 — stretched"},
        {"name": "dark_pool_accumulation", "severity": "CONFIRM",
         "reasoning": "Dark pool: 208,763,350 shares across 50 ATS venues"},
        {"name": "earnings_surprise_streak", "severity": "CONFIRM",
         "reasoning": "Earnings: beats (4/4, avg +4.6%)"},
        {"name": "gap_risk", "severity": "VETO",
         "reasoning": "binary event tomorrow"},
    ]
    out = format_panel_for_prompt(verdicts)
    # VETO/CAUTION keep verbatim reasoning (the risk detail IS the signal)
    assert "[VETO] gap_risk: binary event tomorrow" in out
    assert "[CAUTION] rsi_overbought: RSI 81" in out
    # CONFIRMs compress to a name list — prose duplication gone
    assert "[CONFIRM x2] dark_pool_accumulation, earnings_surprise_streak" in out
    assert "208,763,350" not in out
    assert "4/4" not in out


def test_cycle_workers_cover_full_fleet():
    from multi_scheduler import _CYCLE_MAX_WORKERS
    assert _CYCLE_MAX_WORKERS == 13
