"""Selection engine P1 — option recs carry real max-loss/gain/breakeven.

`evaluate_candidate_for_multileg` used to return strikes + prose only; the
risk-adjusted scorer (P2) needs real dollar bounds. `_price_option_rec` fetches
the two legs' premiums and computes them via `_vertical_pl_bounds`, failing open
to a CONSERVATIVE width×$100 max-loss (never $0). See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import options_strategy_advisor as osa


def _rec(strategy="bull_put_spread", **strikes):
    return {"strategy": strategy, "symbol": "AAPL", "expiry": "2026-08-21",
            "strikes": strikes or {"short": 145.0, "long": 140.0}}


def test_credit_spread_priced(monkeypatch):
    # short leg premium $2.00, long leg $1.00 → net credit $1.00, width $5
    monkeypatch.setattr(osa, "_cached_option_premium",
                        lambda occ, side: 2.0 if side == "sell" else 1.0)
    rec = _rec("bull_put_spread", short=145.0, long=140.0)
    osa._price_option_rec(rec)
    assert rec["priced"] is True
    assert rec["is_credit"] is True
    assert rec["max_loss_per_contract"] == 400.0   # (5 − 1) × 100
    assert rec["max_gain_per_contract"] == 100.0   # 1 × 100
    assert rec["breakeven"] == 144.0               # short 145 − credit 1


def test_debit_spread_priced(monkeypatch):
    # long leg $2.00, short leg $1.00 → net debit $1.00
    monkeypatch.setattr(osa, "_cached_option_premium",
                        lambda occ, side: 1.0 if side == "sell" else 2.0)
    rec = _rec("bull_call_spread", long=150.0, short=155.0)
    osa._price_option_rec(rec)
    assert rec["priced"] is True
    assert rec["is_credit"] is False
    assert rec["max_loss_per_contract"] == 100.0   # debit 1 × 100
    assert rec["max_gain_per_contract"] == 400.0   # (5 − 1) × 100
    assert rec["breakeven"] == 151.0               # long 150 + debit 1


def test_failopen_to_conservative_width(monkeypatch):
    # untrusted marks (0) → keep the conservative width×$100 fallback, not $0
    monkeypatch.setattr(osa, "_cached_option_premium", lambda occ, side: 0.0)
    rec = _rec("bull_put_spread", short=145.0, long=140.0)
    osa._price_option_rec(rec)
    assert rec["priced"] is False
    assert rec["max_loss_per_contract"] == 500.0   # width 5 × 100 (>= true)
    assert rec["max_gain_per_contract"] is None


def test_nonvertical_gets_conservative_fallback():
    rec = {"strategy": "iron_condor", "symbol": "AAPL", "expiry": "2026-08-21",
           "strikes": {"put_long": 130.0, "put_short": 135.0,
                       "call_short": 165.0, "call_long": 170.0}}
    osa._price_option_rec(rec)
    assert rec["priced"] is False
    # conservative: never leaves risk unquantified for the scorer
    assert rec["max_loss_per_contract"] and rec["max_loss_per_contract"] > 0
