"""A name already held SHORT never reaches the AI's menu — 2026-08-07.

Operator: "those names shouldn't reach the shortlist."

`_rank_candidates` had filtered LONG signals on held symbols since it
was written:

    if _is_long_action(action) and symbol in held_symbols:
        continue

but there was NO short-side counterpart. A SHORT/SELL signal on a name
the book was already short passed straight through to the AI menu, the
AI picked it, and `_execute_signal` then discarded it with
"Already short {symbol}".

Measured over the 7 days to 2026-08-07 across profiles 210-219: **236 of
288 SKIP drops were exactly this**, concentrated on five names
re-nominated cycle after cycle — GOOGL 74, KO 56, SO 52, EQIX 30,
WMT 28. Each one consumed a slot on a ~15-name menu that the operator
had already flagged as under-trading (70.3% of cycles selected nothing),
so the waste compounded the very problem it sat inside.

The load-bearing subtlety, and the reason this is pinned rather than
just fixed: the filter keys on `short_held_symbols`, which contains ONLY
negative-qty positions. A SELL on a LONG holding is an EXIT and must
keep flowing. Filtering on `held_symbols` instead would silently strand
every long position the AI wanted to close — a far worse bug than the
one being fixed.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trade_pipeline import (  # noqa: E402
    _rank_candidates, short_held_stock_symbols,
)


class _Pos(dict):
    """Minimal stand-in for a broker Position: dict access plus the
    `is_option` attribute the real class exposes."""

    def __init__(self, symbol, qty, is_option=False):
        super().__init__(symbol=symbol, qty=qty)
        self.is_option = is_option


def _sig(symbol, signal, score, price=100.0):
    return {"symbol": symbol, "signal": signal, "score": score,
            "price": price}


def _syms(out):
    return sorted((c["symbol"], c["signal"]) for c in out)


class TestAlreadyShortNeverReachesTheMenu:
    def test_short_on_an_already_short_name_is_filtered(self):
        out = _rank_candidates(
            [_sig("GOOGL", "SELL", -80), _sig("KO", "STRONG_SELL", -90)],
            held_symbols={"GOOGL", "KO"}, enable_shorts=True,
            short_held_symbols={"GOOGL", "KO"},
        )
        assert _syms(out) == [], (
            "a second short on a name already held short cannot become a "
            "trade — it must not occupy a menu slot"
        )

    def test_every_short_action_verb_is_covered(self):
        """The executor rejects all of these identically, so the
        shortlist must filter all of them identically."""
        for verb in ("SELL", "STRONG_SELL", "SHORT", "STRONG_SHORT"):
            out = _rank_candidates(
                [_sig("KO", verb, -85)],
                held_symbols={"KO"}, enable_shorts=True,
                short_held_symbols={"KO"},
            )
            assert _syms(out) == [], f"{verb} on an already-short name leaked"


class TestExitsMustStillFlow:
    """The dangerous failure mode of this fix."""

    def test_sell_on_a_LONG_holding_is_not_filtered(self):
        """AAPL is held LONG; a SELL is the AI closing it. Keying the
        filter on held_symbols instead of short_held_symbols would
        strand every long the AI wanted to exit."""
        out = _rank_candidates(
            [_sig("AAPL", "SELL", -70)],
            held_symbols={"AAPL"}, enable_shorts=True,
            short_held_symbols=set(),   # long, not short
        )
        assert ("AAPL", "SELL") in _syms(out), (
            "EXIT on a long position was filtered — this would trap "
            "positions in the book"
        )

    def test_exit_flows_even_when_shorts_are_disabled(self):
        out = _rank_candidates(
            [_sig("AAPL", "SELL", -70)],
            held_symbols={"AAPL"}, enable_shorts=False,
            short_held_symbols=set(),
        )
        assert ("AAPL", "SELL") in _syms(out)

    def test_buy_on_a_held_name_is_still_filtered(self):
        """The pre-existing long-side rule must be untouched."""
        out = _rank_candidates(
            [_sig("AAPL", "BUY", 85)],
            held_symbols={"AAPL"}, enable_shorts=True,
            short_held_symbols=set(),
        )
        assert ("AAPL", "BUY") not in _syms(out)


class TestOptionLegsDoNotPoisonTheSet:
    """Caught in review of this very change, against the live p210 book.

    Option legs share the UNDERLYING's symbol — p210 really holds
    EQIX stock -8 alongside EQIX option legs -1 and +1. A first cut of
    `_is_short_stock_position` counted any negative qty, so a symbol
    held LONG in stock with a SHORT option leg would have been marked
    "already short" and its stock EXIT suppressed. `positions_dict` a
    few lines below already filters `is_option` for the same collision.
    """

    def test_short_option_leg_on_a_long_stock_does_not_mark_it_short(self):
        assert short_held_stock_symbols([
            _Pos("EQIX", 8.0, is_option=False),
            _Pos("EQIX", -1.0, is_option=True),
        ]) == set(), (
            "a short OPTION leg must not mark the underlying as "
            "already-short — that would suppress the stock's exit"
        )

    def test_short_stock_is_still_detected_alongside_option_legs(self):
        """The real p210 shape."""
        assert short_held_stock_symbols([
            _Pos("EQIX", -8.0, is_option=False),
            _Pos("EQIX", -1.0, is_option=True),
            _Pos("EQIX", 1.0, is_option=True),
        ]) == {"EQIX"}

    def test_long_stock_alone_is_never_short(self):
        assert short_held_stock_symbols(
            [_Pos("AAPL", 100.0, is_option=False)]) == set()

    def test_unparseable_qty_fails_open(self):
        assert short_held_stock_symbols(
            [_Pos("KO", "n/a", is_option=False)]) == set()

    def test_empty_and_none_are_safe(self):
        assert short_held_stock_symbols([]) == set()
        assert short_held_stock_symbols(None) == set()


class TestNoCollateralChange:
    def test_omitting_the_argument_changes_nothing(self):
        """Back-compat: callers that don't pass short_held_symbols (and
        the many tests that construct _rank_candidates directly) must
        behave exactly as before."""
        sigs = [_sig("NVDA", "SHORT", -75), _sig("MSFT", "BUY", 80)]
        a = _rank_candidates(sigs, set(), True, short_held_symbols=set())
        b = _rank_candidates(sigs, set(), True)
        assert _syms(a) == _syms(b)

    def test_unheld_long_candidate_is_unaffected(self):
        out = _rank_candidates(
            [_sig("MSFT", "BUY", 80)],
            held_symbols={"GOOGL"}, enable_shorts=True,
            short_held_symbols={"GOOGL"},
        )
        assert ("MSFT", "BUY") in _syms(out)


class TestPipelineWiring:
    def test_short_held_set_is_built_from_negative_qty_only(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "trade_pipeline.py")).read()
        assert "short_held_symbols" in src
        assert "short_held_symbols=short_held_symbols" in src, (
            "the set must actually be handed to _rank_candidates"
        )
        # Derived from position sign via the shared helper, not from
        # held_symbols (which would filter exits).
        assert "short_held_stock_symbols(positions_list)" in src

    def test_filter_is_counted_for_observability(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "trade_pipeline.py")).read()
        assert '"already_short"' in src or "'already_short'" in src, (
            "a silent filter is how this went unnoticed for months — it "
            "must appear in the short-candidate filter log line"
        )
