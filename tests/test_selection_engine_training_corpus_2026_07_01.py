"""Selection engine — veto counterfactuals reach the fine-tune corpus (labeled),
and survive resets.

The AI's proposed-then-vetoed spreads (with their TRUE would-be P&L) are
high-value decision-quality data. They live in `option_proposal_outcomes`
(physically separate from `ai_predictions` → zero contamination of real-trade
stats), so `build_training_dataset` must pull them in EXPLICITLY LABELED
(source="veto_counterfactual", is_real=False) alongside the real predictions
(is_real=True), and `predictions_archive` must dump the table before a reset so
the data is never lost. Operator-approved labeled-counterfactual approach.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from journal import (init_db, record_option_proposal_outcome,
                     mark_veto_outcome_resolved, resolved_veto_counterfactuals)
from ai_tracker import build_training_dataset, record_prediction
import predictions_archive


def _make_db():
    path = os.path.join(tempfile.mkdtemp(), "profile.db")
    init_db(path)
    return path


def _seed_resolved_veto(db, outcome="loss", pnl=-400.0):
    rid = record_option_proposal_outcome(
        db, symbol="AAPL", strategy="bull_put_spread", sector="tech", vetoed=1,
        veto_reason="risk_assessor: too rich", confidence=72,
        max_loss_per_contract=400.0, max_gain_per_contract=100.0,
        lo_strike=140.0, hi_strike=145.0, expiry="2026-01-01",
        entry_net_premium=100.0, breakeven=144.0)
    mark_veto_outcome_resolved(db, rid, outcome=outcome, pnl=pnl)
    return rid


# --- journal helper ---------------------------------------------------------

def test_resolved_veto_counterfactuals_returns_resolved_only():
    db = _make_db()
    _seed_resolved_veto(db, outcome="loss", pnl=-400.0)
    # an unresolved vetoed row must NOT appear
    record_option_proposal_outcome(db, symbol="MSFT",
                                   strategy="bull_call_spread", sector="tech",
                                   vetoed=1, max_loss_per_contract=200.0,
                                   max_gain_per_contract=300.0, lo_strike=150.0,
                                   hi_strike=155.0, expiry="2099-01-01")
    rows = resolved_veto_counterfactuals(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "AAPL" and r["strategy"] == "bull_put_spread"
    assert r["wouldbe_outcome"] == "loss" and r["wouldbe_pnl"] == -400.0


# --- training dataset -------------------------------------------------------

def test_training_dataset_tags_real_and_counterfactual():
    db = _make_db()
    record_prediction("NVDA", "BUY", 70, "bullish", 120.0, db_path=db)
    _seed_resolved_veto(db, outcome="win", pnl=100.0)

    rows = build_training_dataset(db, include_unresolved=True)
    by_source = {}
    for r in rows:
        by_source.setdefault(r.get("source"), []).append(r)

    assert len(by_source.get("real", [])) == 1
    assert by_source["real"][0]["is_real"] is True
    assert by_source["real"][0]["symbol"] == "NVDA"

    cf = by_source.get("veto_counterfactual", [])
    assert len(cf) == 1
    assert cf[0]["is_real"] is False
    assert cf[0]["prediction_type"] == "option_open"
    assert cf[0]["strategy_type"] == "bull_put_spread"
    assert cf[0]["outcomes"]["wouldbe"]["outcome_class"] == "win"
    assert cf[0]["outcomes"]["wouldbe"]["wouldbe_pnl"] == 100.0


def test_training_dataset_can_exclude_counterfactuals():
    db = _make_db()
    _seed_resolved_veto(db)
    rows = build_training_dataset(db, include_unresolved=True,
                                  include_veto_counterfactuals=False)
    assert all(r.get("source") != "veto_counterfactual" for r in rows)


def test_counterfactual_included_even_with_no_real_predictions():
    # A profile with only veto data (no resolved real predictions) must still
    # contribute its counterfactuals — the early-return guard was removed.
    db = _make_db()
    _seed_resolved_veto(db)
    rows = build_training_dataset(db, include_unresolved=True)
    assert any(r.get("source") == "veto_counterfactual" for r in rows)


# --- archive (survives resets) ----------------------------------------------

def test_archive_dumps_option_proposal_outcomes():
    db = _make_db()
    _seed_resolved_veto(db)
    root = tempfile.mkdtemp()
    counts = predictions_archive.archive_predictions(db, profile_id=999,
                                                     archive_root=root,
                                                     reset_timestamp="ts")
    assert counts.get("option_proposal_outcomes") == 1
    path = os.path.join(root, "999", "ts", "option_proposal_outcomes.jsonl")
    assert os.path.exists(path)
    with open(path) as f:
        row = json.loads(f.readline())
    assert row["symbol"] == "AAPL" and row["wouldbe_outcome"] == "loss"
