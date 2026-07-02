"""Selection engine — AI-override logging (operator decision #4) + REAL per-leg
option transaction cost.

Override logging: the ledger's RAR ranking is the default; the AI is the final
chooser. `tag_overrides` marks each chosen trade that took a LOWER-RAR
expression than the ledger's best for that name, and `override_scorecard`
measures — over resolved predictions — whether overrides actually beat the
number. Metadata rides on the real prediction (features_json), never a model
feature, never shadow data.

Real cost: option RAR is charged the true per-leg half-spread (round-trip) from
live quotes, falling back to a conservative fixed cost only when a two-sided
market isn't quotable. See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import opportunity_ledger as ol
import risk_adjusted as ra


# --- override tagging -------------------------------------------------------

def _opps():
    # Real opps carry action + (for options) strategy, so direction can be
    # derived. AAPL: bullish stock BUY vs a bull_put_spread. NVDA: bull_call.
    return [{"symbol": "AAPL", "expression": "stock", "rar": 0.80,
             "action": "BUY"},
            {"symbol": "AAPL", "expression": "option", "rar": 0.30,
             "action": "MULTILEG_OPEN", "strategy": "bull_put_spread"},
            {"symbol": "MSFT", "expression": "stock", "rar": 0.50,
             "action": "BUY"},
            {"symbol": "NVDA", "expression": "option", "rar": 0.60,
             "action": "MULTILEG_OPEN", "strategy": "bull_call_spread"}]


def test_build_rar_index_picks_best_expression():
    idx = ol.build_rar_index(_opps())
    assert idx["AAPL"]["stock"] == 0.80 and idx["AAPL"]["option"] == 0.30
    assert idx["AAPL"]["best"] == 0.80 and idx["AAPL"]["best_expr"] == "stock"
    assert idx["AAPL"]["direction"] == "long"
    assert idx["NVDA"]["best_expr"] == "option"


def test_tag_overrides_flags_lower_rar_expression():
    trades = [{"symbol": "AAPL", "action": "MULTILEG_OPEN",       # option<stock
               "strategy_name": "bull_put_spread"},
              {"symbol": "MSFT", "action": "BUY"},                # only expr
              {"symbol": "NVDA", "action": "MULTILEG_OPEN",       # option IS best
               "strategy_name": "bull_call_spread"}]
    n = ol.tag_overrides(trades, _opps())
    assert n == 1
    assert trades[0]["_ledger_is_override"] is True
    assert trades[0]["_ledger_rar"] == 0.30
    assert trades[0]["_ledger_best_rar"] == 0.80
    assert trades[0]["_ledger_best_expr"] == "stock"
    assert trades[1]["_ledger_is_override"] is False
    assert trades[2]["_ledger_is_override"] is False   # took the best


def test_tag_overrides_skips_offledger_and_bare_sell_exit():
    # bare SELL is ambiguous (exit vs short) — must NOT be tagged as an
    # entry-expression override (reviewer finding). ZZZZ isn't in the ledger.
    trades = [{"symbol": "ZZZZ", "action": "BUY"},
              {"symbol": "AAPL", "action": "SELL"}]     # exit / ambiguous
    assert ol.tag_overrides(trades, _opps()) == 0
    assert "_ledger_is_override" not in trades[0]
    assert "_ledger_is_override" not in trades[1]


def test_tag_overrides_skips_direction_mismatch():
    # Ledger scored AAPL only as a SHORT (bearish); an AI BUY is a DIFFERENT
    # trade, not an override — must be skipped, not scored vs the short's RAR.
    bearish = [{"symbol": "AAPL", "expression": "stock", "rar": 0.40,
                "action": "SHORT"},
               {"symbol": "AAPL", "expression": "option", "rar": 0.60,
                "action": "MULTILEG_OPEN", "strategy": "bear_call_spread"}]
    trades = [{"symbol": "AAPL", "action": "BUY"}]
    assert ol.tag_overrides(trades, bearish) == 0
    assert "_ledger_is_override" not in trades[0]


def test_option_scored_against_chosen_spread_not_best_spread():
    # AAPL has TWO spreads (bull_put 0.30, bull_call 0.55) + stock 0.80. The AI
    # picks bull_put → _ledger_rar must be 0.30 (the chosen spread), not 0.55.
    opps = [{"symbol": "AAPL", "expression": "stock", "rar": 0.80,
             "action": "BUY"},
            {"symbol": "AAPL", "expression": "option", "rar": 0.30,
             "action": "MULTILEG_OPEN", "strategy": "bull_put_spread"},
            {"symbol": "AAPL", "expression": "option", "rar": 0.55,
             "action": "MULTILEG_OPEN", "strategy": "bull_call_spread"}]
    trades = [{"symbol": "AAPL", "action": "MULTILEG_OPEN",
               "strategy_name": "bull_put_spread"}]
    ol.tag_overrides(trades, opps)
    assert trades[0]["_ledger_rar"] == 0.30       # chosen spread, not 0.55
    assert trades[0]["_ledger_is_override"] is True


def test_post_mortem_never_learns_ledger_metadata():
    # HIGH contamination guard: `_ledger_*` override-audit metadata lives in
    # features_json but must NEVER become a post-mortem "learned pattern" —
    # those are injected back into the AI batch prompt, which would let the
    # ledger's own audit tags steer selection and self-corrupt the experiment.
    import post_mortem
    losing = [{"_ledger_is_override": True, "_ledger_best_expr": "stock",
               "rsi_bucket": "overbought"} for _ in range(6)]
    feats = [d["feature"] for d in post_mortem._detect_dominant_features(losing)]
    assert not any(f.startswith("_ledger") for f in feats), feats
    assert "rsi_bucket" in feats            # a real feature still surfaces


# --- override scorecard (measure if overrides beat the number) --------------

def _pred_db_with(rows):
    """Temp DB with an ai_predictions table seeded with (features_json,
    actual_outcome, actual_return_pct)."""
    path = os.path.join(tempfile.mkdtemp(), "p.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ai_predictions (features_json TEXT, "
                 "status TEXT, actual_outcome TEXT, actual_return_pct REAL)")
    for feats, outcome, ret in rows:
        conn.execute("INSERT INTO ai_predictions VALUES (?, 'resolved', ?, ?)",
                     (json.dumps(feats), outcome, ret))
    conn.commit(); conn.close()
    return path


def test_override_scorecard_compares_override_vs_aligned():
    db = _pred_db_with([
        ({"_ledger_is_override": True}, "win", 5.0),
        ({"_ledger_is_override": True}, "loss", -3.0),
        ({"_ledger_is_override": False}, "win", 4.0),
        ({"_ledger_is_override": False}, "win", 6.0),
        ({}, "win", 9.0),                 # no override metadata → ignored
    ])
    sc = ol.override_scorecard(db)
    assert sc["override"]["n"] == 2 and sc["override"]["win_rate"] == 50.0
    assert sc["aligned"]["n"] == 2 and sc["aligned"]["win_rate"] == 100.0
    assert sc["aligned"]["avg_return_pct"] == 5.0


def test_override_scorecard_failopen_on_bad_db():
    assert ol.override_scorecard("/nonexistent/x.db") == {}


# --- real per-leg option transaction cost -----------------------------------

def test_score_prefers_real_roundtrip_cost():
    rec = {"symbol": "AAPL", "strategy": "bull_call_spread", "priced": True,
           "max_loss_per_contract": 100.0, "max_gain_per_contract": 400.0,
           "strikes": {"short": 155, "long": 150},
           "roundtrip_cost_per_contract": 30.0}   # real live-quote cost
    opp = ra.score_option_opportunity(rec, 100_000.0, 0.6, ref_dollars=1000.0)
    assert opp["qty"] == 10
    assert opp["risk_dollars"] == 1300.0     # 1000 + 30×10 (real, not fixed 200)
    assert opp["reward_dollars"] == 3700.0   # 4000 − 300


def test_score_falls_back_to_fixed_cost_without_quote():
    rec = {"symbol": "AAPL", "strategy": "bull_call_spread", "priced": True,
           "max_loss_per_contract": 100.0, "max_gain_per_contract": 400.0,
           "strikes": {"short": 155, "long": 150}}  # no roundtrip_cost
    opp = ra.score_option_opportunity(rec, 100_000.0, 0.6, ref_dollars=1000.0)
    assert opp["risk_dollars"] == 1200.0     # fixed $5/leg × 2 × 2 × 10 = 200


def test_price_option_rec_stamps_real_cost(monkeypatch):
    import options_strategy_advisor as osa
    # ONE quote per leg feeds BOTH the mid (premium) and the half-spread (cost).
    # short 145P: (1.9, 2.1) → mid 2.0, hs 0.10; long 140P: (0.9, 1.1) → mid 1.0.
    monkeypatch.setattr(osa, "_cached_option_quote",
                        lambda occ: (1.9, 2.1) if "00145000" in occ
                        else (0.9, 1.1))
    rec = {"strategy": "bull_put_spread", "symbol": "AAPL",
           "expiry": "2026-08-21", "strikes": {"short": 145.0, "long": 140.0}}
    osa._price_option_rec(rec)
    assert rec["priced"] is True
    assert rec["max_loss_per_contract"] == 400.0    # (5 − net credit 1) × 100
    # round-trip cost = ($0.10 + $0.10) × 100 × 2 = $40
    assert rec["roundtrip_cost_per_contract"] == 40.0
