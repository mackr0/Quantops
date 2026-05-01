"""Phase B1 of OPTIONS_PROGRAM_PLAN.md — multi-leg vertical spreads.

Each builder is verified against hand-computed P&L bounds:
  - debit spreads: max_loss = net_debit * 100,
                   max_gain = (width - net_debit) * 100,
                   breakeven = entry_strike ± net_debit
  - credit spreads: max_gain = net_credit * 100,
                    max_loss = (width - net_credit) * 100,
                    breakeven = strike ∓ net_credit
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


EXPIRY = date(2099, 1, 16)


class TestOptionLeg:
    def test_signed_qty_long(self):
        from options_multileg import OptionLeg
        leg = OptionLeg(
            occ_symbol="AAPL  990116C00150000",
            underlying="AAPL", expiry="2099-01-16",
            strike=150.0, right="C", side="buy", qty=2,
        )
        assert leg.signed_qty() == 2

    def test_signed_qty_short(self):
        from options_multileg import OptionLeg
        leg = OptionLeg(
            occ_symbol="AAPL  990116C00160000",
            underlying="AAPL", expiry="2099-01-16",
            strike=160.0, right="C", side="sell", qty=2,
        )
        assert leg.signed_qty() == -2


class TestBullCallSpread:
    def test_basic_structure(self):
        """Long 150C + short 160C with width $10. 2 legs, 'C' rights,
        long lower / short upper convention."""
        from options_multileg import build_bull_call_spread
        spec = build_bull_call_spread("AAPL", EXPIRY, 150, 160, qty=1)
        assert spec.name == "bull_call_spread"
        assert spec.underlying == "AAPL"
        assert spec.spread_width_points == 10.0
        assert spec.is_credit is False
        assert len(spec.legs) == 2
        assert spec.legs[0].side == "buy"  # long lower
        assert spec.legs[0].strike == 150
        assert spec.legs[0].right == "C"
        assert spec.legs[1].side == "sell"  # short upper
        assert spec.legs[1].strike == 160

    def test_invalid_strikes_raise(self):
        from options_multileg import build_bull_call_spread
        with pytest.raises(ValueError, match="upper.*must be"):
            build_bull_call_spread("AAPL", EXPIRY, 160, 150)
        with pytest.raises(ValueError):
            build_bull_call_spread("AAPL", EXPIRY, -1, 10)
        with pytest.raises(ValueError):
            build_bull_call_spread("AAPL", EXPIRY, 100, 110, qty=0)

    def test_pl_bounds_with_premiums(self):
        """Long premium $5, short premium $2 → net debit $3.
        Max loss = $300; max gain = (10 - 3) * 100 = $700;
        breakeven = 150 + 3 = 153."""
        from options_multileg import build_bull_call_spread
        spec = build_bull_call_spread(
            "AAPL", EXPIRY, 150, 160, qty=1,
            long_premium=5.00, short_premium=2.00,
        )
        assert spec.net_premium_per_contract == 3.00
        assert spec.max_loss_per_contract == 300.0
        assert spec.max_gain_per_contract == 700.0
        assert spec.breakeven_at_expiry == 153.0

    def test_no_premiums_leaves_pl_none(self):
        from options_multileg import build_bull_call_spread
        spec = build_bull_call_spread("AAPL", EXPIRY, 150, 160)
        assert spec.net_premium_per_contract is None
        assert spec.max_loss_per_contract is None
        assert spec.breakeven_at_expiry is None
        # Width is still known
        assert spec.spread_width_points == 10.0


class TestBearPutSpread:
    def test_basic_structure(self):
        """Long 160P + short 150P. Long upper / short lower for puts."""
        from options_multileg import build_bear_put_spread
        spec = build_bear_put_spread("AAPL", EXPIRY, 150, 160, qty=1)
        assert spec.name == "bear_put_spread"
        assert spec.is_credit is False
        # Long upper, short lower
        assert spec.legs[0].side == "buy"
        assert spec.legs[0].strike == 160
        assert spec.legs[0].right == "P"
        assert spec.legs[1].side == "sell"
        assert spec.legs[1].strike == 150

    def test_pl_bounds_with_premiums(self):
        """Long $4 put, short $2 put → debit $2.
        Max loss = $200, max gain = $800, breakeven = 160 - 2 = 158."""
        from options_multileg import build_bear_put_spread
        spec = build_bear_put_spread(
            "AAPL", EXPIRY, 150, 160, qty=1,
            long_premium=4.00, short_premium=2.00,
        )
        assert spec.net_premium_per_contract == 2.00
        assert spec.max_loss_per_contract == 200.0
        assert spec.max_gain_per_contract == 800.0
        assert spec.breakeven_at_expiry == 158.0


class TestBullPutSpread:
    def test_basic_structure(self):
        """Short 160P + long 150P. Credit spread; short leg listed first."""
        from options_multileg import build_bull_put_spread
        spec = build_bull_put_spread("AAPL", EXPIRY, 150, 160, qty=1)
        assert spec.name == "bull_put_spread"
        assert spec.is_credit is True
        # Short upper, long lower (short first in legs)
        assert spec.legs[0].side == "sell"
        assert spec.legs[0].strike == 160
        assert spec.legs[1].side == "buy"
        assert spec.legs[1].strike == 150

    def test_pl_bounds_with_premiums(self):
        """Short $5 put (collect), long $2 put (pay) → net credit $3.
        Max gain = $300, max loss = (10 - 3) * 100 = $700,
        breakeven = 160 - 3 = 157."""
        from options_multileg import build_bull_put_spread
        spec = build_bull_put_spread(
            "AAPL", EXPIRY, 150, 160, qty=1,
            short_premium=5.00, long_premium=2.00,
        )
        # Signed: credits stored negative
        assert spec.net_premium_per_contract == -3.0
        assert spec.max_gain_per_contract == 300.0
        assert spec.max_loss_per_contract == 700.0
        assert spec.breakeven_at_expiry == 157.0


class TestBearCallSpread:
    def test_basic_structure(self):
        """Short 150C + long 160C. Credit spread."""
        from options_multileg import build_bear_call_spread
        spec = build_bear_call_spread("AAPL", EXPIRY, 150, 160, qty=1)
        assert spec.name == "bear_call_spread"
        assert spec.is_credit is True
        assert spec.legs[0].side == "sell"
        assert spec.legs[0].strike == 150
        assert spec.legs[1].side == "buy"
        assert spec.legs[1].strike == 160

    def test_pl_bounds_with_premiums(self):
        """Short $5 call, long $2 call → net credit $3.
        Max gain = $300, max loss = $700, breakeven = 150 + 3 = 153."""
        from options_multileg import build_bear_call_spread
        spec = build_bear_call_spread(
            "AAPL", EXPIRY, 150, 160, qty=1,
            short_premium=5.00, long_premium=2.00,
        )
        assert spec.net_premium_per_contract == -3.0
        assert spec.max_gain_per_contract == 300.0
        assert spec.max_loss_per_contract == 700.0
        assert spec.breakeven_at_expiry == 153.0


class TestQuantityScaling:
    def test_qty_5_scales_legs(self):
        from options_multileg import build_bull_call_spread
        spec = build_bull_call_spread("AAPL", EXPIRY, 150, 160, qty=5)
        assert spec.qty == 5
        assert spec.legs[0].qty == 5
        assert spec.legs[1].qty == 5

    def test_qty_5_scales_pl_correctly(self):
        """P&L bounds in dataclass are PER CONTRACT (per spread); the
        executor multiplies by qty for total exposure."""
        from options_multileg import build_bull_call_spread
        spec = build_bull_call_spread(
            "AAPL", EXPIRY, 150, 160, qty=5,
            long_premium=5.00, short_premium=2.00,
        )
        # Per contract figures unchanged regardless of qty
        assert spec.max_loss_per_contract == 300.0
        assert spec.max_gain_per_contract == 700.0


class TestSerialization:
    def test_as_dict_round_trip(self):
        from options_multileg import build_bull_call_spread
        spec = build_bull_call_spread(
            "AAPL", EXPIRY, 150, 160, qty=2,
            long_premium=5.00, short_premium=2.00,
        )
        d = spec.as_dict()
        assert d["name"] == "bull_call_spread"
        assert d["qty"] == 2
        assert d["max_gain_per_contract"] == 700.0
        assert len(d["legs"]) == 2
        assert d["legs"][0]["side"] == "buy"
        assert d["legs"][1]["side"] == "sell"


class TestRegistry:
    def test_all_four_verticals_in_registry(self):
        from options_multileg import VERTICAL_SPREAD_BUILDERS
        assert set(VERTICAL_SPREAD_BUILDERS.keys()) == {
            "bull_call_spread", "bear_put_spread",
            "bull_put_spread", "bear_call_spread",
        }

    def test_registry_lookup_callable(self):
        from options_multileg import VERTICAL_SPREAD_BUILDERS
        builder = VERTICAL_SPREAD_BUILDERS["bull_call_spread"]
        spec = builder("AAPL", EXPIRY, 150, 160, qty=1)
        assert spec.name == "bull_call_spread"
