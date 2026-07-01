"""Selection engine P4 — would-be-P&L resolver + veto-QUALITY calibration.

Every VETOED spread is priced at veto time (entry legs + max-loss/gain + both
strikes), so its TRUE would-be P&L can later be resolved from the underlying's
expiry close (intrinsic — no illiquid near-expiry option marks). The ledger's
veto discount is then refined: discount = P(veto) × veto_quality, where
veto_quality = fraction of RESOLVED would-be vetoes that would actually have
LOST — so we only down-rank a (strategy × sector) whose specialists were RIGHT.
All in `option_proposal_outcomes` (physically separate from ai_predictions →
zero contamination). See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from journal import (init_db, record_option_proposal_outcome,
                     option_veto_quality_counts, pending_veto_outcomes)
import veto_feedback as vf


@pytest.fixture
def _db():
    path = os.path.join(tempfile.mkdtemp(), "profile.db")
    init_db(path)
    return path


def _seed_priced_veto(db, strategy="bull_put_spread", sector="tech",
                      lo=140.0, hi=145.0, ml=400.0, mg=100.0,
                      expiry="2026-01-01"):
    return record_option_proposal_outcome(
        db, symbol="AAPL", strategy=strategy, sector=sector, vetoed=1,
        veto_reason="risk", max_loss_per_contract=ml, max_gain_per_contract=mg,
        lo_strike=lo, hi_strike=hi, expiry=expiry)


# --- intrinsic P&L ----------------------------------------------------------

def test_intrinsic_pnl_all_verticals():
    f = vf._intrinsic_expiry_pnl
    assert f("bull_put_spread", 140, 145, 400, 100, 150) == 100.0   # max gain
    assert f("bull_put_spread", 140, 145, 400, 100, 138) == -400.0  # max loss
    assert f("bull_call_spread", 150, 155, 400, 100, 160) == 100.0
    assert f("bear_call_spread", 150, 155, 400, 100, 145) == 100.0
    assert f("bear_put_spread", 140, 145, 400, 100, 138) == 100.0
    assert f("iron_condor", 140, 160, 400, 100, 150) is None        # non-dir
    assert f("bull_put_spread", 145, 145, 400, 100, 150) is None    # lo==hi


# --- resolver ---------------------------------------------------------------

def test_resolver_marks_win_and_loss(_db):
    _seed_priced_veto(_db)                                # bull_put 140/145
    _seed_priced_veto(_db)
    # first row settles at 150 (win, +max_gain), second at 138 (loss)
    spots = {0: 150.0, 1: 138.0}
    calls = {"i": 0}

    def _spot(sym, expiry):
        v = spots[calls["i"]]
        calls["i"] += 1
        return v

    n = vf.resolve_option_proposal_outcomes(_db, _spot, today="2026-07-01")
    assert n == 2
    q = {(s, sec): (r, l) for s, sec, r, l in option_veto_quality_counts(_db)}
    resolved, losses = q[("bull_put_spread", "tech")]
    assert resolved == 2 and losses == 1


def test_resolver_leaves_recent_pending_when_spot_missing(_db):
    # Recent expiry (within the stale window) + no spot yet → stays pending,
    # resolvable next cadence (NOT retired).
    _seed_priced_veto(_db, expiry="2026-06-28")     # 3 days before "today"
    n = vf.resolve_option_proposal_outcomes(_db, lambda s, e: None,
                                            today="2026-07-01")
    assert n == 0
    assert len(pending_veto_outcomes(_db, "2026-07-01")) == 1


def test_stale_pending_retired_unresolvable(_db):
    # Far past expiry + no priceable close (delisted / non-trading expiry) →
    # retired 'unresolvable', so it stops re-querying AND never pollutes the
    # veto-quality signal.
    _seed_priced_veto(_db, expiry="2026-01-01")     # ~180 days stale
    vf.resolve_option_proposal_outcomes(_db, lambda s, e: None,
                                        today="2026-07-01")
    assert pending_veto_outcomes(_db, "2026-07-01") == []       # gone
    assert option_veto_quality_counts(_db) == []               # not counted


def test_expiry_day_is_not_resolved(_db):
    # Strict `expiry < today`: a row expiring TODAY is not eligible (the daily
    # bar is still the in-progress partial print, not the settlement close).
    _seed_priced_veto(_db, expiry="2026-07-01")
    assert pending_veto_outcomes(_db, "2026-07-01") == []
    n = vf.resolve_option_proposal_outcomes(_db, lambda s, e: 150.0,
                                            today="2026-07-01")
    assert n == 0


def test_resolver_skips_future_expiry(_db):
    _seed_priced_veto(_db, expiry="2099-01-01")
    n = vf.resolve_option_proposal_outcomes(_db, lambda s, e: 150.0,
                                            today="2026-07-01")
    assert n == 0                                # expiry not reached


def test_mark_resolved_returns_false_when_not_pending(_db):
    from journal import mark_veto_outcome_resolved
    rid = _seed_priced_veto(_db, expiry="2026-06-28")
    assert mark_veto_outcome_resolved(_db, rid, outcome="win", pnl=100.0) is True
    # second call: row no longer pending → False (rowcount gate)
    assert mark_veto_outcome_resolved(_db, rid, outcome="loss", pnl=-100.0) is False


def test_unpriced_veto_is_not_resolvable(_db):
    # No max_gain / strikes → cannot resolve accurately; excluded from pending.
    record_option_proposal_outcome(_db, symbol="AAPL",
                                   strategy="iron_condor", sector="tech",
                                   vetoed=1, expiry="2026-01-01")
    assert pending_veto_outcomes(_db, "2026-07-01") == []


# --- veto-quality calibration -----------------------------------------------

def _seed_pveto(db, strategy, sector, vetoed, accepted, **priced):
    for _ in range(vetoed):
        record_option_proposal_outcome(db, symbol="AAPL", strategy=strategy,
                                       sector=sector, vetoed=1, **priced)
    for _ in range(accepted):
        record_option_proposal_outcome(db, symbol="AAPL", strategy=strategy,
                                       sector=sector, vetoed=0)


def test_smart_vetoes_keep_discount(_db):
    # 30 vetoed + 10 accepted (P(veto)=0.75 → cap 0.5); 12 resolved as LOSSES
    # (vetoes avoided losses) → quality=1.0 → discount stays 0.5.
    _seed_pveto(_db, "bull_put_spread", "tech", vetoed=30, accepted=10,
                max_loss_per_contract=400.0, max_gain_per_contract=100.0,
                lo_strike=140.0, hi_strike=145.0, expiry="2026-01-01")
    # settle all pending at 138 → all would-be LOSSES
    vf.resolve_option_proposal_outcomes(_db, lambda s, e: 138.0,
                                        today="2026-07-01")
    disc = vf.load_veto_discounts(_db)
    assert vf.discount_for(disc, "bull_put_spread", "tech") == 0.5


def test_dumb_vetoes_shrink_discount_to_zero(_db):
    # Same veto rate, but the vetoes blocked would-be WINNERS → quality=0 →
    # discount collapses to 0 (stop suppressing an expression the specialists
    # were WRONG about).
    _seed_pveto(_db, "bull_put_spread", "tech", vetoed=30, accepted=10,
                max_loss_per_contract=400.0, max_gain_per_contract=100.0,
                lo_strike=140.0, hi_strike=145.0, expiry="2026-01-01")
    vf.resolve_option_proposal_outcomes(_db, lambda s, e: 150.0,   # all WINS
                                        today="2026-07-01")
    disc = vf.load_veto_discounts(_db)
    assert vf.discount_for(disc, "bull_put_spread", "tech") == 0.0


def test_quality_ignored_below_min_resolved(_db):
    # 30 vetoed + 10 accepted, but only 3 resolved (< _MIN_RESOLVED) → quality
    # not trusted yet → raw P(veto) discount (0.5 cap) still applies.
    _seed_pveto(_db, "bull_put_spread", "tech", vetoed=30, accepted=10)
    # 3 priced vetoes resolvable as wins — too few to trust quality
    for _ in range(3):
        _seed_priced_veto(_db)
    vf.resolve_option_proposal_outcomes(_db, lambda s, e: 150.0,
                                        today="2026-07-01")
    disc = vf.load_veto_discounts(_db)
    assert vf.discount_for(disc, "bull_put_spread", "tech") == 0.5


# --- capture (pipeline prices the vetoed spread) ----------------------------

def test_option_open_is_a_distinct_meta_model_feature():
    # P4 per-expression calibration: the meta-model must one-hot option_open so
    # a stock and a spread of the same name can get a different P(correct).
    from meta_model import CATEGORICAL_FEATURES, extract_features
    assert "option_open" in CATEGORICAL_FEATURES["prediction_type"]
    f = extract_features({"prediction_type": "option_open", "rsi": 55})
    assert f.get("prediction_type_option_open") == 1.0
    assert f.get("prediction_type_directional_long") == 0.0


def test_pipeline_prices_vetoed_spread(monkeypatch, _db):
    monkeypatch.setattr("options_strategy_advisor._cached_option_premium",
                        lambda occ, side: 2.0 if side == "sell" else 1.0)
    from pipelines.option import OptionPipeline
    ctx = SimpleNamespace(db_path=_db)
    proposal = {"symbol": "AAPL", "action": "MULTILEG_OPEN",
                "strategy_name": "bull_put_spread", "confidence": 70,
                "expiry": "2026-08-21", "strikes": {"short": 145, "long": 140}}
    OptionPipeline._record_option_outcome(ctx, proposal, "AAPL",
                                          vetoed_flag=1, veto_reason="risk")
    rows = pending_veto_outcomes(_db, "2026-12-31")
    assert len(rows) == 1
    r = rows[0]
    # priced: real max-loss/gain + both strikes captured for resolution
    assert r["max_loss_per_contract"] == 400.0    # (5 − 1) × 100
    assert r["max_gain_per_contract"] == 100.0
    assert {r["lo_strike"], r["hi_strike"]} == {140.0, 145.0}
