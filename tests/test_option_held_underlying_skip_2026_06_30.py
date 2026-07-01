"""Options candidate generator skips underlyings the profile already holds
(2026-06-30).

The stock candidate path became concentration-aware earlier (book_fit + a
sector haircut on the LONG sort key), but the OPTIONS menu never did: it
proposed a multi-leg spread for EVERY screener candidate, including names the
profile already holds as stock or option. The adversarial_reviewer then
vetoed those redundant proposals ~every cycle ("redundant long" / "net-zero
synthetic wash"), burning an LLM round-trip each time and flooding the UI with
vetoes while diversifying names never surfaced.

`evaluate_candidate_for_multileg` now takes the profile's OWN held-underlying
set and returns no recs for an already-held name — suppressing the proposal
BEFORE the prompt and the veto. The held set is own-book only (read via
client.get_positions(ctx=ctx)); isolation is preserved.

This file pins:
- SKIP: a candidate whose underlying is in `held` yields no recs.
- CONTROL/BACK-COMPAT: the same candidate yields recs when held is None.
- RENDER: the prompt block drops held names, keeps diversifiers.
- HELPER: own-book read, uppercased, and gated by ENABLE_CONCENTRATION_AWARE.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from options_strategy_advisor import (
    evaluate_candidate_for_multileg,
    _own_book_held_underlyings,
)
from opportunity_ledger import build_opportunities


def _bullish_candidate(sym="NVDA"):
    # Bullish + IV-rich → at least one spread rec in the control case.
    return {"symbol": sym, "signal": "BUY", "price": 120.0, "score": 2.0,
            "volatility_view": None}


def test_held_underlying_is_suppressed_but_renders_without_filter():
    """The held filter is the ONLY thing that changes the outcome: the
    candidate yields recs normally, and none once it's on the book."""
    cand = _bullish_candidate("NVDA")
    control = evaluate_candidate_for_multileg(
        cand, iv_rank_pct=90, regime="trending", held=None)
    blocked = evaluate_candidate_for_multileg(
        cand, iv_rank_pct=90, regime="trending", held={"NVDA"})

    assert control, ("control: a bullish IV-rich candidate must yield recs "
                     "without the held filter (else the test input is wrong)")
    assert blocked == [], "an already-held underlying must yield no option recs"


def test_non_held_candidate_survives():
    cand = _bullish_candidate("AMD")
    recs = evaluate_candidate_for_multileg(
        cand, iv_rank_pct=90, regime="trending", held={"NVDA", "PLTR"})
    assert recs, "a diversifying (non-held) name must still be proposed"


def test_held_match_is_case_insensitive():
    cand = _bullish_candidate("nvda")
    recs = evaluate_candidate_for_multileg(
        cand, iv_rank_pct=90, regime="trending", held={"NVDA"})
    assert recs == []


def test_ledger_drops_held_option_keeps_diversifier(monkeypatch):
    """In the unified opportunity ledger, an already-held underlying yields
    NO option expression (the held-skip), while a diversifying name does.
    The held name can still appear as a STOCK expression — the skip is
    option-only (you may still buy the stock), which is exactly the intent."""
    monkeypatch.setattr(
        "options_strategy_advisor._own_book_held_underlyings",
        lambda ctx: {"NVDA"},
    )
    cands = [_bullish_candidate("NVDA"), _bullish_candidate("AMD")]
    opps = build_opportunities(
        cands, ctx=SimpleNamespace(), equity=100_000.0,
        iv_rank_lookup=lambda s: 90, regime="trending")
    option_syms = {o.get("symbol") for o in opps
                   if o.get("expression") == "option"}
    assert "NVDA" not in option_syms, (
        "a held underlying must yield no OPTION expression in the ledger")
    assert "AMD" in option_syms, (
        "a diversifying name must still yield an option expression")


def test_helper_respects_flag(monkeypatch):
    import config
    monkeypatch.setattr(config, "ENABLE_CONCENTRATION_AWARE", False,
                        raising=False)
    # Even with positions present, an off flag yields the empty set.
    monkeypatch.setattr("client.get_positions",
                        lambda api=None, ctx=None: [{"symbol": "NVDA"}])
    assert _own_book_held_underlyings(SimpleNamespace()) == set()


def test_helper_reads_own_book_uppercased(monkeypatch):
    import config
    monkeypatch.setattr(config, "ENABLE_CONCENTRATION_AWARE", True,
                        raising=False)
    monkeypatch.setattr(
        "client.get_positions",
        lambda api=None, ctx=None: [{"symbol": "nvda"}, {"symbol": "PLTR"}],
    )
    assert _own_book_held_underlyings(SimpleNamespace()) == {"NVDA", "PLTR"}


def test_helper_failopen(monkeypatch):
    import config
    monkeypatch.setattr(config, "ENABLE_CONCENTRATION_AWARE", True,
                        raising=False)

    def boom(api=None, ctx=None):
        raise RuntimeError("book unavailable")

    monkeypatch.setattr("client.get_positions", boom)
    assert _own_book_held_underlyings(SimpleNamespace()) == set()
