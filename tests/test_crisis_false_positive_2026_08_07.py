"""A calm market is not a crisis — 2026-08-07.

Operator: "yes for fucks sake fix the problem of more than 70% being
useless that is the whole point of the session."

**The measurement.** Over the 7 days to 2026-08-07, 1,217 of 1,738
cycles (70.0%) selected ZERO trades. That was not a starved menu: only
2 cycles had an empty shortlist. In 1,217 cycles the AI was shown 3-10
real candidates and took none. **38.4% of those cycles cite "Crisis
State" in the AI's own stated reasoning** — "I am electing to pass on
all candidates this cycle. The portfolio is currently operating in a
'Crisis State' with high market uncertainty…"

**What the crisis actually was.** All ten profiles sat at level
ELEVATED (0.5x position sizes) on ONE medium-severity signal:

    gold_rally: "GLD +3.1% over 5 days (safe-haven demand)"

against this tape:

    spy_5d_pct             +3.65   equities rallying just as hard
    vix                     15.41  calm
    price_shock_count_30m       0
    yield_spread_10y2y      +0.43  not inverted

Three defects stacked to turn a benign gold rally into a fleet-wide
stand-down, and all three are pinned here:

1. **The gold signal asserted its conclusion without its premise.**
   "Safe-haven demand" means gold up *and equities down*. The sibling
   detector `_check_bond_stock_divergence` — the other flight-to-safety
   check in the same file — already required `spy_5d < 0`. The gold
   check never got that condition.

2. **One medium signal forced ELEVATED.** `_classify_level` returned
   ELEVATED at `total >= 1`, directly contradicting its own docstring
   ("VIX normal → ELEVATED with ≥ 2 signals").

3. **ELEVATED told the AI to stop trading.** Every non-normal level
   carried "prefer exits over entries". At ELEVATED the designed
   response is HALF SIZE, not no trades — standing down entirely is the
   CRISIS-level response.

Same family as the sizing anchor fixed earlier the same day: a
protective mechanism, calibrated without a floor, quietly converting
into "don't trade".
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crisis_detector import THRESHOLDS, _check_gold_rally  # noqa: E402


class TestGoldRallyNeedsEquityWeakness:
    """The exact 2026-08-06 reading set that halved the fleet."""

    def test_gold_up_with_equities_up_is_not_a_crisis(self, monkeypatch):
        monkeypatch.setattr("crisis_detector._fetch_close_series",
                            lambda sym, days=30: _series(100.0, 103.12))
        readings = {"spy_5d_pct": 3.65}   # equities rallying too
        assert _check_gold_rally(readings) is None, (
            "gold and stocks rising together is a liquidity/inflation "
            "regime, not a flight to safety"
        )

    def test_gold_up_with_equities_DOWN_still_fires(self, monkeypatch):
        """The signal must keep working for the case it was written
        for — this fix must not disarm a real detector."""
        monkeypatch.setattr("crisis_detector._fetch_close_series",
                            lambda sym, days=30: _series(100.0, 103.12))
        readings = {"spy_5d_pct": -4.0}
        sig = _check_gold_rally(readings)
        assert sig is not None
        assert sig["name"] == "gold_rally"
        assert "SPY -4.0%" in sig["detail"], (
            "the detail line must show the equity leg, so a future "
            "reader can see the premise was actually checked"
        )

    def test_below_threshold_gold_never_fires(self, monkeypatch):
        monkeypatch.setattr("crisis_detector._fetch_close_series",
                            lambda sym, days=30: _series(100.0, 101.0))
        assert _check_gold_rally({"spy_5d_pct": -4.0}) is None

    def test_missing_equity_reading_does_not_fire(self, monkeypatch):
        """If spy_5d_pct is unavailable the premise cannot be
        confirmed. Asserting a crisis flag without its defining
        condition is the whole bug — stay silent instead."""
        monkeypatch.setattr("crisis_detector._fetch_close_series",
                            lambda sym, days=30: _series(100.0, 103.12))
        assert _check_gold_rally({}) is None

    def test_gold_reading_is_still_recorded_for_analysis(self, monkeypatch):
        """Not firing must not mean not measuring."""
        monkeypatch.setattr("crisis_detector._fetch_close_series",
                            lambda sym, days=30: _series(100.0, 103.12))
        readings = {"spy_5d_pct": 3.65}
        _check_gold_rally(readings)
        assert readings["gld_5d_pct"] == 3.12

    def test_threshold_is_configurable_and_defaults_to_flat(self):
        assert THRESHOLDS["gold_rally_spy_max_pct"] == 0.0


class TestElevatedIsASizeCutNotAStandDown:
    def test_elevated_guidance_tells_the_ai_to_keep_trading_smaller(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "trade_pipeline.py")).read()
        idx = src.index("CRISIS STATE:")
        block = src[idx - 1500:idx + 900]
        assert "SIZE DOWN by the multiplier" in block
        assert "NOT a reason to stand aside" in block

    def test_crisis_and_severe_still_say_prefer_exits(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "trade_pipeline.py")).read()
        assert "prefer exits over" in src, (
            "standing down must remain the CRISIS/SEVERE response — the "
            "fix narrows where it applies, it does not delete it"
        )

    def test_guidance_branches_on_level(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "trade_pipeline.py")).read()
        assert '_lvl == "elevated"' in src


def _series(base, last):
    """6-point close series: index -6 is `base`, index -1 is `last`."""
    import pandas as pd
    return pd.Series([base, base, base, base, base, last])
