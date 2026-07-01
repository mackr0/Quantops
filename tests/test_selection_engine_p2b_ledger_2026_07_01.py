"""Selection engine P2b — the unified risk-adjusted opportunity ledger.

`opportunity_ledger` scores each candidate's STOCK expression and each
OPTION-spread expression on one RAR axis and ranks them together, replacing the
two asymmetric prompt blocks that drove the ~18:1 option:stock skew. These pin
the ledger's behavior: interleave + RAR sort, enable_options gating (stock-only
ablation), same capital-at-risk envelope, and the P_win model.
See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import opportunity_ledger as ol


def _buy(symbol="AAPL", score=2.0, price=150.0, atr=3.0):
    return {"symbol": symbol, "signal": "BUY", "price": price,
            "score": score, "atr": atr, "rsi": 62, "adx": 28,
            "volume_ratio": 1.4}


@pytest.fixture
def _offline_options(monkeypatch):
    """Deterministic, offline option pricing + no own-book read, so the option
    stream is reproducible without network/DB."""
    monkeypatch.setattr("options_strategy_advisor._cached_option_premium",
                        lambda occ, side: 2.0 if side == "sell" else 1.0)
    monkeypatch.setattr("options_strategy_advisor._own_book_held_underlyings",
                        lambda ctx: set())
    monkeypatch.setattr("options_strategy_advisor._options_budget_exhausted",
                        lambda ctx: False)


# --- P_win model (pure) -----------------------------------------------------

def test_conviction_prior_bounds():
    assert ol._conviction_p_win(0) == 0.50           # no conviction → coin flip
    assert ol._conviction_p_win(1) == pytest.approx(0.56)
    assert ol._conviction_p_win(10) == 0.68          # capped
    assert ol._conviction_p_win(-3) == pytest.approx(0.68)  # |score|, capped


def test_p_win_uses_realized_same_signal_winrate():
    rep = {"win_rate": 40, "total": 50,
           "by_signal": {"BUY": {"win_rate": 80, "total": 30},
                         "SHORT": {"win_rate": 10, "total": 30}}}
    # full-weight blend (total 30 → w=1.0) toward the BUY bucket
    assert ol.p_win_from_reputation(2.0, "BUY", rep) == pytest.approx(0.80)
    # SHORT signal reads the SHORT bucket, not the aggregate
    assert ol.p_win_from_reputation(2.0, "SHORT", rep) == pytest.approx(0.10)


def test_p_win_sell_reads_its_own_bucket_not_short():
    # SELL and SHORT are DISTINCT reputation buckets; a SELL signal must read
    # the SELL win-rate, not the SHORT one (2026-07-01 review finding).
    rep = {"win_rate": 50, "total": 40,
           "by_signal": {"SELL": {"win_rate": 70, "total": 20},
                         "SHORT": {"win_rate": 20, "total": 20}}}
    prior = ol._conviction_p_win(2.0)
    w = min(1.0, 20 / 30.0)
    expected = w * 0.70 + (1 - w) * prior          # SELL bucket, not SHORT
    assert ol.p_win_from_reputation(2.0, "SELL", rep) == pytest.approx(expected)


def test_p_win_thin_sample_falls_back_to_prior():
    rep = {"win_rate": 90, "total": 4,
           "by_signal": {"BUY": {"win_rate": 90, "total": 4}}}
    # <10 resolved → prior, not the noisy 90%
    assert ol.p_win_from_reputation(2.0, "BUY", rep) == \
        pytest.approx(ol._conviction_p_win(2.0))


def test_p_win_no_reputation_is_prior():
    assert ol.p_win_from_reputation(1.5, "BUY", None) == \
        pytest.approx(ol._conviction_p_win(1.5))


# --- ledger construction ----------------------------------------------------

def test_stock_and_option_interleaved_and_rar_sorted(_offline_options):
    opps = ol.build_opportunities([_buy()], SimpleNamespace(), 100_000.0,
                                  iv_rank_lookup=lambda s: 70)
    exprs = {o["expression"] for o in opps}
    assert "stock" in exprs, "the stock expression must be scored"
    assert "option" in exprs, "the option expression must be scored"
    # ranked by RAR desc (tie-break EV$)
    rars = [o["rar"] for o in opps]
    assert rars == sorted(rars, reverse=True)
    # the stock leg of a high-conviction BUY has positive risk-adjusted return
    stock = next(o for o in opps if o["expression"] == "stock")
    assert stock["rar"] > 0


def test_enable_options_false_is_stock_only(_offline_options):
    opps = ol.build_opportunities([_buy()],
                                  SimpleNamespace(enable_options=False),
                                  100_000.0, iv_rank_lookup=lambda s: 70)
    assert opps, "stock opportunities must still be produced"
    assert all(o["expression"] == "stock" for o in opps), (
        "enable_options=False must yield NO option expressions (ablation)")


def test_option_sized_to_same_envelope_as_stock(_offline_options):
    # 8% default size on $100k = $8000 capital-at-risk envelope; the option
    # qty must be floor(REF$/max_loss), NOT floor(equity/max_loss).
    opps = ol.build_opportunities([_buy(score=2.0)], SimpleNamespace(),
                                  100_000.0, iv_rank_lookup=lambda s: 70)
    opt = next((o for o in opps if o["expression"] == "option"), None)
    assert opt is not None
    # priced credit spread: max_loss = (width-1)*100 per contract; whatever it
    # is, the risk must be bounded by the ~$8k envelope, never the full equity.
    assert 0 < opt["risk_dollars"] <= 8_000 * 1.5


def test_option_pwin_uses_pop_not_directional_conviction():
    # A credit bull-put spread with an OTM short strike must score a HIGH POP
    # (short put finishes OTM) — driven by the spread geometry, not by the
    # underlying's directional conviction.
    credit = {"strategy": "bull_put_spread", "is_credit": True,
              "strikes": {"short": 145.0, "long": 140.0},
              "expiry": "2026-08-21", "breakeven": 144.0}
    pop = ol._option_pwin(credit, spot=150.0, atr=3.0)
    assert 0.5 < pop <= 1.0, "OTM credit put spread → POP above a coin flip"
    # Non-vertical / unrecognizable geometry → neutral 0.5 (no information).
    assert ol._option_pwin({"strategy": "iron_condor"}, 150.0, 3.0) == 0.5
    assert ol._option_pwin(credit, spot=0, atr=3.0) == 0.5  # no spot


def test_render_empty_when_no_candidates():
    block, has_opt = ol.render_opportunity_ledger([], SimpleNamespace(),
                                                  100_000.0)
    assert block == "" and has_opt is False


def test_render_has_rows_and_detail(_offline_options):
    block, has_opt = ol.render_opportunity_ledger(
        [_buy()], SimpleNamespace(), 100_000.0, iv_rank_lookup=lambda s: 70)
    assert has_opt is True
    assert "RISK-ADJUSTED OPPORTUNITY LEDGER" in block
    assert "BUY AAPL" in block            # stock row with inline detail
    assert "bull_put_spread" in block     # option row with strategy
    assert "RAR" in block
