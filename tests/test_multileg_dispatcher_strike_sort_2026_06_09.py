"""2026-06-09 — vertical-spread dispatcher sorts strikes.

The AI sometimes inverts the `{"short": X, "long": Y}` labels for
bear_call_spread and bear_put_spread. The AI's prompt has only the
bull_put_spread example (short=145, long=140 → short > long); the
AI generalizes that "short is always the higher number," which is
correct for bull_put but WRONG for bear_call (where short < long).

Pre-fix: `_build_multileg_strategy` passed `strikes["short"],
strikes["long"]` to the builder as `(lower, upper)`. When AI gave
inverted labels for a bear_call_spread (e.g., {"short": 18, "long":
17}), the builder received lower=18, upper=17 → raised "upper
strike (17) must be > lower strike (18)" → Multi-leg build/submit
failed → red ERROR badge.

Observed today across pids 41, 44 on RGTI (17/18), POET (9.5/10.0,
9.5/10.0), and others.

Post-fix: dispatcher sorts the two strikes for all 4 vertical
spreads and passes (min, max) to the builder. The builder assigns
short/long based on its strategy's structural rule, so the AI's
label inversion no longer matters.

Contract pinned:

  1. AI-inverted bear_call_spread builds successfully.
  2. AI-inverted bear_put_spread builds successfully.
  3. Correctly-labeled spreads still build identically (no regression).
  4. The short leg gets the structurally-correct strike for each
     strategy after sorting (bear_call → short=lower; bull_call →
     short=upper; bull_put → short=upper; bear_put → short=lower).
  5. Non-vertical spreads (iron_condor, straddle, strangle) are
     untouched — they use named kwargs with no inversion ambiguity.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest


REPO_ROOT = sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)


EXPIRY = date(2099, 1, 16)


# ---------------------------------------------------------------------------
# Layer 1 — inverted-label proposals build successfully
# ---------------------------------------------------------------------------


class TestInvertedLabelsBuildSuccessfully:

    def test_bear_call_spread_with_inverted_labels(self):
        """RGTI reproduction: AI proposes bear_call_spread with
        {"short": 18, "long": 17} (label inversion — bear_call_spread
        is structurally short<long, so short=18, long=17 is wrong).
        Pre-fix: builder raised. Post-fix: sort and proceed."""
        from trade_pipeline import _build_multileg_strategy
        from options_multileg import build_bear_call_spread
        spec = _build_multileg_strategy(
            build_bear_call_spread, "bear_call_spread",
            "RGTI", EXPIRY,
            {"short": 18, "long": 17},  # inverted
            contracts=1,
        )
        assert spec is not None
        assert spec.name == "bear_call_spread"
        # Bear call → short leg is at LOWER strike (17), long leg at
        # upper (18). The dispatcher sorts; the builder's structural
        # rules assign short=lower.
        short_legs = [lg for lg in spec.legs if lg.side == "sell"]
        long_legs = [lg for lg in spec.legs if lg.side == "buy"]
        assert len(short_legs) == 1 and len(long_legs) == 1
        assert short_legs[0].strike == 17.0
        assert long_legs[0].strike == 18.0

    def test_bear_put_spread_with_inverted_labels(self):
        """POET reproduction: bear_put_spread with {"short": 10,
        "long": 9.5}. bear_put is structurally short<long so
        short=10, long=9.5 is inverted. Sort + build."""
        from trade_pipeline import _build_multileg_strategy
        from options_multileg import build_bear_put_spread
        spec = _build_multileg_strategy(
            build_bear_put_spread, "bear_put_spread",
            "POET", EXPIRY,
            {"short": 10.0, "long": 9.5},  # inverted
            contracts=1,
        )
        assert spec is not None
        assert spec.name == "bear_put_spread"
        # Bear put: short=lower (9.5), long=upper (10.0)
        short_legs = [lg for lg in spec.legs if lg.side == "sell"]
        long_legs = [lg for lg in spec.legs if lg.side == "buy"]
        assert short_legs[0].strike == 9.5
        assert long_legs[0].strike == 10.0


# ---------------------------------------------------------------------------
# Layer 2 — correctly-labeled spreads still build correctly (no regression)
# ---------------------------------------------------------------------------


class TestCorrectLabelsStillBuild:

    def test_bull_call_spread_correct_labels(self):
        """bull_call_spread: long=150, short=160 (correct: long < short).
        Builder should assign short=160 (upper), long=150 (lower)."""
        from trade_pipeline import _build_multileg_strategy
        from options_multileg import build_bull_call_spread
        spec = _build_multileg_strategy(
            build_bull_call_spread, "bull_call_spread",
            "AAPL", EXPIRY,
            {"long": 150, "short": 160},
            contracts=1,
        )
        short_legs = [lg for lg in spec.legs if lg.side == "sell"]
        long_legs = [lg for lg in spec.legs if lg.side == "buy"]
        assert short_legs[0].strike == 160.0  # upper
        assert long_legs[0].strike == 150.0   # lower

    def test_bull_put_spread_correct_labels(self):
        """bull_put_spread: short=145, long=140 (correct: short > long).
        Builder: short=145 (upper), long=140 (lower)."""
        from trade_pipeline import _build_multileg_strategy
        from options_multileg import build_bull_put_spread
        spec = _build_multileg_strategy(
            build_bull_put_spread, "bull_put_spread",
            "AAPL", EXPIRY,
            {"short": 145, "long": 140},
            contracts=1,
        )
        short_legs = [lg for lg in spec.legs if lg.side == "sell"]
        long_legs = [lg for lg in spec.legs if lg.side == "buy"]
        assert short_legs[0].strike == 145.0  # upper
        assert long_legs[0].strike == 140.0   # lower

    def test_bear_call_spread_correct_labels(self):
        """bear_call_spread correctly labeled: short=17, long=18."""
        from trade_pipeline import _build_multileg_strategy
        from options_multileg import build_bear_call_spread
        spec = _build_multileg_strategy(
            build_bear_call_spread, "bear_call_spread",
            "RGTI", EXPIRY,
            {"short": 17, "long": 18},  # correct
            contracts=1,
        )
        short_legs = [lg for lg in spec.legs if lg.side == "sell"]
        long_legs = [lg for lg in spec.legs if lg.side == "buy"]
        assert short_legs[0].strike == 17.0
        assert long_legs[0].strike == 18.0

    def test_bear_put_spread_correct_labels(self):
        from trade_pipeline import _build_multileg_strategy
        from options_multileg import build_bear_put_spread
        spec = _build_multileg_strategy(
            build_bear_put_spread, "bear_put_spread",
            "POET", EXPIRY,
            {"short": 9.5, "long": 10.0},  # correct
            contracts=1,
        )
        short_legs = [lg for lg in spec.legs if lg.side == "sell"]
        long_legs = [lg for lg in spec.legs if lg.side == "buy"]
        assert short_legs[0].strike == 9.5
        assert long_legs[0].strike == 10.0


# ---------------------------------------------------------------------------
# Layer 3 — non-vertical spreads are untouched
# ---------------------------------------------------------------------------


def test_iron_condor_dispatch_unchanged():
    """Iron condor uses named kwargs (put_long_strike, put_short_strike,
    etc.) so there's no `short`/`long` label-inversion risk. Confirm
    the dispatcher still routes it through the named-kwarg path."""
    from trade_pipeline import _build_multileg_strategy
    from options_multileg import build_iron_condor
    spec = _build_multileg_strategy(
        build_iron_condor, "iron_condor",
        "SPY", EXPIRY,
        {
            "put_long": 400, "put_short": 405,
            "call_short": 420, "call_long": 425,
        },
        contracts=1,
    )
    assert spec is not None
    assert spec.name == "iron_condor"


def test_long_straddle_dispatch_unchanged():
    """Straddles use single `strike` key — no inversion risk."""
    from trade_pipeline import _build_multileg_strategy
    from options_multileg import build_long_straddle
    spec = _build_multileg_strategy(
        build_long_straddle, "long_straddle",
        "AAPL", EXPIRY,
        {"strike": 150},
        contracts=1,
    )
    assert spec is not None


# ---------------------------------------------------------------------------
# Layer 4 — structural pin
# ---------------------------------------------------------------------------


def test_dispatcher_sorts_vertical_spread_strikes_in_source():
    """Source pin: `_build_multileg_strategy` must use min()/max()
    to sort the two strikes before passing to vertical-spread
    builders. Without this, a future refactor reverting to direct
    label use would re-introduce the AI-label-inversion ERROR."""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "trade_pipeline.py"
    body = src.read_text()
    # Find the dispatcher function body
    fn_start = body.find("def _build_multileg_strategy")
    assert fn_start > 0
    fn_end = body.find("\ndef ", fn_start + 1)
    fn_body = body[fn_start:fn_end if fn_end > 0 else len(body)]
    # The fix uses min()/max() for vertical spreads
    assert "min(strikes" in fn_body, (
        "Vertical-spread dispatch must use min(strikes[...], strikes[...]) "
        "to derive the lower strike. Without sorting, AI label "
        "inversion produces 'upper strike must be > lower strike' "
        "build errors."
    )
    assert "max(strikes" in fn_body, (
        "Vertical-spread dispatch must use max(strikes[...], strikes[...]) "
        "to derive the upper strike."
    )
