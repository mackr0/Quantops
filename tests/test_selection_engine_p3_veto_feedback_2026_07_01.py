"""Selection engine P3 — per-(strategy x sector) veto-rate discount.

The AI kept proposing option spreads its OWN specialists then vetoed (~97% veto
storm → idle cash). P3 records every option proposal's outcome (vetoed/accepted)
keyed by SPREAD strategy + sector in a dedicated `option_proposal_outcomes`
table (PHYSICALLY SEPARATE from ai_predictions → zero contamination of
real-trade stats), and the opportunity ledger discounts a spread's RAR by this
profile's own P(veto) for its (strategy, sector) BEFORE selection.

Pins: the journal round-trip + counts, the discount policy (>=30-sample floor,
0.5 cap, positive-RAR-only), the ledger applying it, the pipeline writer, and
the physical isolation from ai_predictions. See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from journal import (init_db, record_option_proposal_outcome,
                     option_veto_counts)
import veto_feedback as vf
import opportunity_ledger as ol


@pytest.fixture
def _db():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "profile.db")
    init_db(path)
    return path


def _seed(db, strategy, sector, vetoed, accepted):
    for _ in range(vetoed):
        record_option_proposal_outcome(db, symbol="AAPL", strategy=strategy,
                                       sector=sector, vetoed=1, veto_reason="x")
    for _ in range(accepted):
        record_option_proposal_outcome(db, symbol="AAPL", strategy=strategy,
                                       sector=sector, vetoed=0)


# --- journal round-trip -----------------------------------------------------

def test_counts_round_trip(_db):
    _seed(_db, "bull_put_spread", "tech", vetoed=35, accepted=5)
    counts = dict(((s, sec), (v, t))
                  for s, sec, v, t in option_veto_counts(_db))
    assert counts[("bull_put_spread", "tech")] == (35, 40)


def test_writer_failopen_on_bad_db():
    # No db_path / no strategy → None, never raises.
    assert record_option_proposal_outcome(None, symbol="X", strategy="s",
                                          sector="tech", vetoed=1) is None
    assert record_option_proposal_outcome("/x", symbol="X", strategy=None,
                                          sector="tech", vetoed=1) is None


# --- discount policy --------------------------------------------------------

def test_discount_needs_min_samples(_db):
    _seed(_db, "iron_condor", "tech", vetoed=10, accepted=0)   # < 30 total
    disc = vf.load_veto_discounts(_db)
    assert vf.discount_for(disc, "iron_condor", "tech") == 0.0


def test_discount_is_capped_and_is_pveto(_db):
    _seed(_db, "bull_put_spread", "tech", vetoed=35, accepted=5)  # p=0.875
    disc = vf.load_veto_discounts(_db)
    assert vf.discount_for(disc, "bull_put_spread", "tech") == 0.5  # capped

    d2 = os.path.join(tempfile.mkdtemp(), "p.db")
    init_db(d2)
    _seed(d2, "bull_call_spread", "energy", vetoed=12, accepted=28)  # p=0.30
    disc2 = vf.load_veto_discounts(d2)
    assert vf.discount_for(disc2, "bull_call_spread", "energy") == pytest.approx(0.30)


def test_apply_discount_only_lowers_positive_rar():
    assert vf.apply_veto_discount(0.80, 0.5) == pytest.approx(0.40)
    assert vf.apply_veto_discount(-0.30, 0.5) == -0.30   # negative untouched
    assert vf.apply_veto_discount(0.80, 0.0) == pytest.approx(0.80)


# --- ledger applies the discount --------------------------------------------

@pytest.fixture
def _offline_options(monkeypatch):
    monkeypatch.setattr("options_strategy_advisor._cached_option_premium",
                        lambda occ, side: 2.0 if side == "sell" else 1.0)
    monkeypatch.setattr("options_strategy_advisor._own_book_held_underlyings",
                        lambda ctx: set())
    monkeypatch.setattr("options_strategy_advisor._options_budget_exhausted",
                        lambda ctx: False)


def _buy(symbol="AAPL"):
    return {"symbol": symbol, "signal": "BUY", "price": 150.0, "score": 2.0,
            "atr": 3.0, "rsi": 62, "adx": 28, "volume_ratio": 1.4}


def test_ledger_discounts_high_veto_spread(_offline_options, _db):
    # Seed a high-veto (bull_put_spread x AAPL's sector) so the ledger halves
    # its option RAR; compare to a run with no history.
    sector = ol._sector_of("AAPL")
    _seed(_db, "bull_put_spread", sector, vetoed=35, accepted=5)   # cap 0.5

    ctx_hist = SimpleNamespace(db_path=_db)
    ctx_none = SimpleNamespace(db_path=None)
    opts_hist = ol.build_opportunities([_buy()], ctx_hist, 100_000.0,
                                       iv_rank_lookup=lambda s: 70)
    opts_none = ol.build_opportunities([_buy()], ctx_none, 100_000.0,
                                       iv_rank_lookup=lambda s: 70)
    opt_h = next(o for o in opts_hist if o["expression"] == "option")
    opt_n = next(o for o in opts_none if o["expression"] == "option")
    assert opt_h.get("veto_discount") == 0.5
    # positive RAR halved; if RAR was negative it's left unchanged either way
    if opt_n["rar"] > 0:
        assert opt_h["rar"] == pytest.approx(opt_n["rar"] * 0.5, abs=1e-3)
    assert opt_h["rar"] <= opt_n["rar"]


# --- pipeline writer + physical isolation -----------------------------------

def test_pipeline_records_veto_and_accept(_db):
    from pipelines.option import OptionPipeline
    ctx = SimpleNamespace(db_path=_db)
    proposal = {"symbol": "AAPL", "action": "MULTILEG_OPEN",
                "strategy_name": "bull_put_spread", "confidence": 70,
                "expiry": "2026-08-21"}
    OptionPipeline._record_option_outcome(ctx, proposal, "AAPL",
                                          vetoed_flag=1, veto_reason="risk")
    OptionPipeline._record_option_outcome(ctx, proposal, "AAPL", vetoed_flag=0)
    counts = {(s, sec): (v, t) for s, sec, v, t in option_veto_counts(_db)}
    (v, t) = next(iter(counts.values()))
    assert (v, t) == (1, 2)


def test_outcomes_table_is_physically_separate_from_ai_predictions(_db):
    # The whole point: a would-be/veto outcome can never land in ai_predictions
    # (which feeds reputation/meta-model/win-rate). Writing an outcome must
    # leave ai_predictions empty.
    import sqlite3
    _seed(_db, "bull_put_spread", "tech", vetoed=5, accepted=1)
    conn = sqlite3.connect(_db)
    n_pred = conn.execute("SELECT COUNT(*) FROM ai_predictions").fetchone()[0]
    n_out = conn.execute(
        "SELECT COUNT(*) FROM option_proposal_outcomes").fetchone()[0]
    conn.close()
    assert n_pred == 0 and n_out == 6
