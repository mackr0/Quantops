"""Phase B2: deterministic specialist coverage parity for options
(2026-05-19).

Before today, the 179 deterministic specialists in
`deterministic_specialists/` were gated to stock-side actions
(`APPLIES_TO_SIGNALS = ("BUY", ...)` or `(... "SHORT")`). An
OPTIONS or MULTILEG_OPEN candidate matched zero rules and skipped
the whole panel — option proposals saw only the 3 LLM-narrative
option specialists (`option_spread_risk`, `gamma_pin_specialist`,
`iv_skew_specialist`) plus 5 underlying-shaped LLM specialists.

After: the router computes the candidate's direction from
(action, option_strategy) and routes to the same-direction stock
rules. No per-rule edits — the 179-rule library now covers options
of matching direction by construction.

These tests pin:
  1. `signal_direction` returns the correct label for stock,
     options (long_call, bull_call_spread, etc.), and multileg
     candidates; returns None on unknown/missing strategies so
     directional rules don't mis-fire.
  2. `run_panel` fires a bullish LONG-only rule on an OPTIONS
     candidate with bullish strategy.
  3. `run_panel` fires a bearish SHORT-only rule on an OPTIONS
     candidate with bearish strategy.
  4. `run_panel` does NOT fire a bullish rule on a bearish
     options candidate (and vice versa) — the directional gate
     is bidirectional-correct, not just additive.
  5. `run_panel` does NOT fire directional rules on neutral
     option strategies (iron_condor) — those have their own
     LLM specialists.
  6. Legacy stock-side routing is unchanged — a BUY candidate
     still fires the same long-only rules.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


from deterministic_specialists import (  # noqa: E402
    signal_direction,
    run_panel,
    BULLISH_OPTION_STRATEGIES,
    BEARISH_OPTION_STRATEGIES,
)


# ---------------------------------------------------------------------------
# (1) signal_direction labels
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signal,expected", [
    ("BUY", "bullish"),
    ("STRONG_BUY", "bullish"),
    ("WEAK_BUY", "bullish"),
    ("SELL", "bearish"),
    ("STRONG_SELL", "bearish"),
    ("WEAK_SELL", "bearish"),
    ("SHORT", "bearish"),
])
def test_stock_actions_translate_to_direction(signal, expected):
    assert signal_direction({"signal": signal}) == expected


@pytest.mark.parametrize("strategy", sorted(BULLISH_OPTION_STRATEGIES))
def test_bullish_option_strategies_translate_to_bullish(strategy):
    assert signal_direction({
        "signal": "OPTIONS", "option_strategy": strategy,
    }) == "bullish"
    assert signal_direction({
        "signal": "MULTILEG_OPEN", "option_strategy": strategy,
    }) == "bullish"


@pytest.mark.parametrize("strategy", sorted(BEARISH_OPTION_STRATEGIES))
def test_bearish_option_strategies_translate_to_bearish(strategy):
    assert signal_direction({
        "signal": "OPTIONS", "option_strategy": strategy,
    }) == "bearish"


def test_neutral_option_strategies_translate_to_neutral():
    for strat in ("iron_condor", "iron_butterfly", "straddle", "strangle",
                   "calendar_spread"):
        assert signal_direction({
            "signal": "OPTIONS", "option_strategy": strat,
        }) == "neutral"


def test_unknown_option_strategy_returns_none():
    """Unknown option_strategy on an OPTIONS/MULTILEG_OPEN candidate
    returns None — directional rules don't fire (avoid mis-attribution)."""
    assert signal_direction({
        "signal": "OPTIONS", "option_strategy": "made_up_strategy",
    }) is None
    assert signal_direction({
        "signal": "MULTILEG_OPEN", "option_strategy": None,
    }) is None


def test_unknown_signal_returns_none():
    assert signal_direction({"signal": "WTF"}) is None
    assert signal_direction({}) is None


def test_case_insensitive_signal_and_strategy():
    """Real production candidates sometimes lowercase the signal
    (and option_strategy is consistently lowercased upstream).
    Pin that the translation is robust to case."""
    assert signal_direction({"signal": "buy"}) == "bullish"
    assert signal_direction({
        "signal": "OPTIONS", "option_strategy": "Long_Call",
    }) == "bullish"


# ---------------------------------------------------------------------------
# (2-6) run_panel directional dispatch
# ---------------------------------------------------------------------------

class _StubRule:
    """A fake deterministic rule. Records whether evaluate() was
    invoked + returns a configurable verdict."""
    def __init__(self, name, applies_to_signals, returns_verdict=True):
        self.NAME = name
        self.__name__ = name
        self.APPLIES_TO_SIGNALS = applies_to_signals
        self._returns = returns_verdict
        self.called = False

    def evaluate(self, candidate, ctx=None):
        self.called = True
        if not self._returns:
            return None
        return {"severity": "CAUTION", "reasoning": "stub fired"}


def _run_with_rules(rules, candidate):
    """Patch discover_rules() to return the supplied stubs."""
    with patch("deterministic_specialists.discover_rules",
                return_value=rules):
        return run_panel(candidate)


def test_long_only_rule_fires_on_bullish_options_candidate():
    bullish_rule = _StubRule("bullish_check",
                              ("BUY", "STRONG_BUY", "WEAK_BUY"))
    fired = _run_with_rules(
        [bullish_rule],
        {"signal": "OPTIONS", "option_strategy": "long_call"},
    )
    assert bullish_rule.called
    assert len(fired) == 1
    assert fired[0]["name"] == "bullish_check"


def test_long_only_rule_does_not_fire_on_bearish_options_candidate():
    """The directional gate is exclusive — bullish rules must NOT
    fire on bearish options (and vice versa)."""
    bullish_rule = _StubRule("bullish_check",
                              ("BUY", "STRONG_BUY", "WEAK_BUY"))
    fired = _run_with_rules(
        [bullish_rule],
        {"signal": "OPTIONS", "option_strategy": "long_put"},
    )
    assert not bullish_rule.called
    assert fired == []


def test_short_only_rule_fires_on_bearish_multileg_candidate():
    bearish_rule = _StubRule("bearish_check",
                              ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT"))
    fired = _run_with_rules(
        [bearish_rule],
        {"signal": "MULTILEG_OPEN", "option_strategy": "bear_put_spread"},
    )
    assert bearish_rule.called
    assert len(fired) == 1


def test_short_only_rule_does_not_fire_on_bullish_options():
    bearish_rule = _StubRule("bearish_check",
                              ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT"))
    fired = _run_with_rules(
        [bearish_rule],
        {"signal": "OPTIONS", "option_strategy": "long_call"},
    )
    assert not bearish_rule.called
    assert fired == []


def test_neutral_options_strategy_skips_directional_rules():
    """An iron_condor is non-directional. The 179-rule library is
    directional; the operator's option-specific LLM specialists
    handle non-directional strategies. So neither bullish nor
    bearish rules should fire on neutral."""
    bullish_rule = _StubRule("bullish_check",
                              ("BUY", "STRONG_BUY", "WEAK_BUY"))
    bearish_rule = _StubRule("bearish_check",
                              ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT"))
    fired = _run_with_rules(
        [bullish_rule, bearish_rule],
        {"signal": "OPTIONS", "option_strategy": "iron_condor"},
    )
    assert not bullish_rule.called
    assert not bearish_rule.called
    assert fired == []


def test_bidirectional_rule_fires_on_options_of_either_direction():
    """A bidirectional rule lists both stock-action sets. It
    should fire on bullish AND bearish options."""
    bidir_rule = _StubRule(
        "bidir_check",
        ("BUY", "STRONG_BUY", "WEAK_BUY",
         "SELL", "STRONG_SELL", "WEAK_SELL", "SHORT"),
    )
    fired_bull = _run_with_rules(
        [bidir_rule],
        {"signal": "OPTIONS", "option_strategy": "long_call"},
    )
    assert bidir_rule.called
    bidir_rule.called = False
    fired_bear = _run_with_rules(
        [bidir_rule],
        {"signal": "OPTIONS", "option_strategy": "long_put"},
    )
    assert bidir_rule.called
    assert len(fired_bull) == 1
    assert len(fired_bear) == 1


def test_legacy_stock_routing_is_unchanged():
    """Pin that the existing stock-side path still works exactly
    as before — no regression in the 99% of cycles where the
    candidate is a stock action."""
    bullish_rule = _StubRule("bullish_check",
                              ("BUY", "STRONG_BUY", "WEAK_BUY"))
    bearish_rule = _StubRule("bearish_check",
                              ("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT"))

    fired_buy = _run_with_rules(
        [bullish_rule, bearish_rule],
        {"signal": "BUY"},
    )
    assert bullish_rule.called
    assert not bearish_rule.called
    assert len(fired_buy) == 1

    bullish_rule.called = False
    bearish_rule.called = False
    fired_short = _run_with_rules(
        [bullish_rule, bearish_rule],
        {"signal": "SHORT"},
    )
    assert not bullish_rule.called
    assert bearish_rule.called
    assert len(fired_short) == 1


def test_empty_applies_to_signals_means_fire_always():
    """A rule with no APPLIES_TO_SIGNALS gate fires on every
    candidate (current behavior — pin it.)"""
    universal = _StubRule("universal", ())  # empty tuple = no gate
    fired_buy = _run_with_rules(
        [universal],
        {"signal": "BUY"},
    )
    assert universal.called
    universal.called = False
    fired_opt = _run_with_rules(
        [universal],
        {"signal": "OPTIONS", "option_strategy": "iron_condor"},
    )
    assert universal.called


# ---------------------------------------------------------------------------
# Integration: realistic candidate against a sample of LIVE rules
# ---------------------------------------------------------------------------

def test_real_panel_now_fires_some_rules_on_bullish_options():
    """Run the LIVE deterministic panel on a synthetic bullish
    options candidate that carries the same indicator/alt_data
    fields as a stock candidate would. At least ONE LONG-only
    rule should fire (zero would mean the directional routing
    isn't working in production)."""
    candidate = {
        "signal": "OPTIONS",
        "option_strategy": "long_call",
        "symbol": "AAPL",
        "price": 180.0,
        # Indicators a typical long-only rule reads
        "rsi": 85.0,         # overbought → multiple bullish-veto rules
        "52_week_high": 180.0,  # at 52w high → late-stage rules
        "volume_ratio": 0.6,  # dry volume on a "breakout"
        "alt_data": {
            "options": {"iv_rank": 85.0},  # IV extreme → options_iv_extreme_high
        },
    }
    fired = run_panel(candidate)
    # Before today's change this would have been 0 (no rule
    # matches an OPTIONS signal in any stock-action tuple).
    # After: at least one rule should fire — typically several.
    assert len(fired) >= 1, (
        "No deterministic rule fired on a clearly-overbought "
        "bullish options candidate. Directional routing isn't "
        "translating OPTIONS → bullish stock rules. Phase B2 "
        "regression."
    )
