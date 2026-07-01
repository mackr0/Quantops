"""Selection engine P2b — risk-adjusted shortlist ranking in _rank_candidates.

The shortlist the AI sees is now ordered by the candidate's stock-expression
RAR (reputation-aware P_win × the profile's reward/risk ratio) instead of raw
|score|. For a symbol with no track record this is monotonic in |score| (order
unchanged); a chronically-losing symbol drops even at equal conviction. Fail-
safe to the old abs(score) key. See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from trade_pipeline import _rank_candidates


def _sig(symbol, score=2.0, signal="BUY", rsi=50):
    return {"symbol": symbol, "signal": signal, "score": score,
            "rsi": rsi, "votes": {}, "price": 100.0}


def test_bad_reputation_drops_below_good_at_equal_conviction():
    # Same conviction (|score|=2) for both; only realized win-rate differs.
    rep = {
        "GOOD": {"win_rate": 75, "total": 30,
                 "by_signal": {"BUY": {"win_rate": 75, "total": 30}}},
        "BAD": {"win_rate": 10, "total": 30,
                "by_signal": {"BUY": {"win_rate": 10, "total": 30}}},
    }
    shortlist = _rank_candidates(
        [_sig("BAD"), _sig("GOOD")], held_symbols=set(), enable_shorts=False,
        ctx=SimpleNamespace(), symbol_reputation=rep)
    order = [s["symbol"] for s in shortlist]
    assert order.index("GOOD") < order.index("BAD"), (
        "a symbol with a strong realized win-rate must outrank an equally-"
        "convicted symbol with a poor win-rate")


def test_no_reputation_preserves_conviction_order():
    # Without reputation, RAR is monotonic in |score| → higher score first.
    shortlist = _rank_candidates(
        [_sig("LO", score=1.0), _sig("HI", score=3.0)],
        held_symbols=set(), enable_shorts=False,
        ctx=SimpleNamespace(), symbol_reputation=None)
    order = [s["symbol"] for s in shortlist]
    assert order == ["HI", "LO"]


def test_high_conviction_order_preserved_under_prior_saturation():
    # Both |score|>=3 → the conviction prior saturates at 0.68 → RAR ties. The
    # |score| tie-break (NOT RSI extremity) must keep the stronger signal first,
    # matching the old abs(score) order (2026-07-01 review finding).
    shortlist = _rank_candidates(
        [_sig("WEAKER", score=4.0, rsi=90), _sig("STRONGER", score=8.0, rsi=50)],
        held_symbols=set(), enable_shorts=False,
        ctx=SimpleNamespace(), symbol_reputation=None)
    assert [s["symbol"] for s in shortlist] == ["STRONGER", "WEAKER"]


def test_backward_compatible_without_ctx_or_reputation():
    # Old call shape (no ctx/reputation) must still rank by conviction.
    shortlist = _rank_candidates(
        [_sig("A", score=1.0), _sig("B", score=2.5)],
        held_symbols=set(), enable_shorts=False)
    assert [s["symbol"] for s in shortlist] == ["B", "A"]
