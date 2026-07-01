"""Selection engine P2 (scorer) — one risk-adjusted axis for stock vs option.

Pure `risk_adjusted` scorer: RAR = P·(reward/risk) − (1−P), sized to a common
capital-at-risk envelope so a stock and a spread rank apples-to-apples.
See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import risk_adjusted as ra


def test_rar_formula():
    import pytest
    # P=0.6, reward:risk = 2:1 → 0.6·2 − 0.4 = 0.8
    assert ra.rar(0.6, 200.0, 100.0) == pytest.approx(0.8)
    # break-even coin flip on 1:1 → 0
    assert ra.rar(0.5, 100.0, 100.0) == pytest.approx(0.0)
    # non-positive risk → strongly disfavored (unsizeable)
    assert ra.rar(0.9, 100.0, 0.0) == -1.0
    assert ra.rar(0.9, 100.0, -5.0) == -1.0


def test_stock_opportunity_scored():
    rec = {"symbol": "NVDA", "action": "BUY", "size_pct": 8.0,
           "stop_loss_pct": 4.0, "take_profit_pct": 8.0}
    opp = ra.score_stock_opportunity(rec, equity=100_000.0, p_win=0.6,
                                     cost_pct=0.1)
    # ref = 8000; risk = 320 + 8 cost = 328; reward = 640 − 8 = 632
    assert opp["risk_dollars"] == 328.0
    assert opp["reward_dollars"] == 632.0
    assert opp["expression"] == "stock"
    # rar = 0.6·(632/328) − 0.4 ≈ 0.7561
    assert abs(opp["rar"] - 0.7561) < 0.001


def test_option_opportunity_scored_and_sized_to_envelope():
    # credit spread: max-loss $400/contract, max-gain $100 (1:4 reward:risk)
    rec = {"symbol": "AAPL", "strategy": "bull_put_spread", "priced": True,
           "max_loss_per_contract": 400.0, "max_gain_per_contract": 100.0,
           "expiry": "2026-08-21", "strikes": {"short": 145, "long": 140}}
    opp = ra.score_option_opportunity(rec, equity=100_000.0, p_win=0.6,
                                      ref_dollars=8000.0)
    assert opp["qty"] == 20                    # floor(8000/400)
    # transaction cost = $5/leg × 2 legs × 2 (round trip) × 20 = $400, netted
    # into risk (+) and reward (−) so the option ranks apples-to-apples with the
    # cost-charged stock expression.
    assert opp["risk_dollars"] == 8400.0       # 8000 + 400 cost
    assert opp["reward_dollars"] == 1600.0     # 2000 − 400 cost
    # rar = 0.6·(1600/8400) − 0.4 ≈ −0.286 → a low-POP credit spread is correctly
    # UNattractive vs a stock at the same P_win (needs POP>0.8 to be positive)
    assert opp["rar"] == pytest.approx(-0.2857, abs=1e-3)


def test_option_charges_round_trip_transaction_cost():
    # A spread must be charged a per-leg half-spread (round trip), so its
    # reported risk exceeds the gross max-loss and its reward is haircut —
    # otherwise a zero-cost option out-ranks the cost-charged stock and the
    # ledger re-tilts toward options (2026-07-01 verification finding).
    rec = {"symbol": "AAPL", "strategy": "bull_call_spread", "priced": True,
           "max_loss_per_contract": 100.0, "max_gain_per_contract": 400.0,
           "strikes": {"short": 155, "long": 150}, "expiry": "2026-08-21"}
    opp = ra.score_option_opportunity(rec, 100_000.0, 0.6, ref_dollars=1000.0)
    assert opp["qty"] == 10                     # floor(1000/100)
    # cost = $5/leg × 2 legs × 2 (round trip) × 10 = $200
    assert opp["risk_dollars"] == 1200.0        # 1000 gross + 200 cost
    assert opp["reward_dollars"] == 3800.0      # 4000 gross − 200 cost


def test_unsizeable_option_returns_none():
    rec = {"symbol": "AAPL", "strategy": "long_straddle",
           "max_loss_per_contract": None}
    assert ra.score_option_opportunity(rec, 100_000.0, 0.6) is None


def test_option_pop_is_conservative_and_bounded():
    # ATM-ish short strike, modest IV → POP in (0,1); min-of-two is conservative
    p = ra.option_pop(spot=100.0, short_strike=95.0, dte_days=30, iv=0.30,
                      right="P", is_credit=True, breakeven=94.0,
                      implied_move_pct=5.0)
    assert 0.0 <= p <= 1.0
    # no inputs → 0.5 (no information)
    p2 = ra.option_pop(spot=0, short_strike=0, dte_days=0, iv=0,
                       right="P", is_credit=True)
    assert p2 == 0.5
