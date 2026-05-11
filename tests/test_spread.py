"""Pin the Spread class — multileg-aware grouping + per-spread P&L
display capped at structural max loss. Phase 4 of Position class
refactor (2026-05-11).

These tests pin:
1. Bull call spread (debit) max loss = debit paid.
2. Bull put spread (credit) max loss = (strike_width - net_credit).
3. group_into_spreads pairs legs by (option_strategy, underlying,
   timestamp window).
4. Per-leg P&L sum capped at structural max loss when broker marks
   produce fictitious losses (the PCG -10100% incident).
5. Single legs (orphans) end up in `ungrouped`.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from position import Position
from spread import Spread, group_into_spreads


def _opt(occ, qty, entry, current=None):
    """Build an option Position. Negative qty for short."""
    cur = current if current is not None else entry
    ap = SimpleNamespace(
        symbol=occ, qty=str(qty),
        avg_entry_price=str(entry),
        current_price=str(cur),
        market_value=str(qty * cur * 100),
        unrealized_pl=str(qty * (cur - entry) * 100),
        unrealized_plpc=str((cur - entry) / entry if entry else 0),
    )
    return Position.from_alpaca(ap)


class TestStructuralMaxLoss:
    def test_bull_call_spread_max_loss_is_debit(self):
        """Long lower-strike call + short higher-strike call. Paid
        the difference in premiums (debit)."""
        legs = [
            _opt("AAPL260612C00150000", qty=2, entry=3.00),  # long 150C
            _opt("AAPL260612C00155000", qty=-2, entry=1.50),  # short 155C
        ]
        spread = Spread(
            strategy_name="bull_call_spread",
            underlying="AAPL", legs=legs,
        )
        # Net debit = (3.00 - 1.50) * 2 contracts * 100 = $300
        assert spread.structural_max_loss == pytest.approx(300.0)

    def test_bull_put_spread_max_loss_is_width_minus_credit(self):
        """Short higher-strike put + long lower-strike put. Receive
        net credit; max loss = (strike_width - credit) per spread."""
        legs = [
            _opt("RTX260618P00170000", qty=-1, entry=3.15),  # short 170P
            _opt("RTX260618P00160000", qty=1, entry=1.74),  # long 160P
        ]
        spread = Spread(
            strategy_name="bull_put_spread",
            underlying="RTX", legs=legs,
        )
        # Width = 170 - 160 = 10; credit = 3.15 - 1.74 = 1.41
        # Max loss = (10 - 1.41) * 1 contract * 100 = $859
        assert spread.structural_max_loss == pytest.approx(859.0)

    def test_unknown_strategy_returns_none(self):
        legs = [_opt("AAPL260612C00150000", qty=1, entry=1.0)]
        spread = Spread(
            strategy_name="not_a_real_strategy",
            underlying="AAPL", legs=legs,
        )
        assert spread.structural_max_loss is None


class TestDisplayPnLCapping:
    def test_per_leg_loss_exceeding_max_is_capped(self):
        """The PCG-style scenario: short leg's broker mark went
        from $0.01 entry to $1.02 current (fictitious due to wide
        bid-ask on illiquid OTM option). Per-leg P&L = -$505 on a
        bull_call_spread with $300 max loss. Display should show
        the structural cap, not the broker fiction."""
        legs = [
            _opt("PCG260612C00017000", qty=5, entry=0.47, current=0.39),
            _opt("PCG260612C00018000", qty=-5, entry=0.01, current=1.02),
        ]
        spread = Spread(
            strategy_name="bull_call_spread",
            underlying="PCG", legs=legs,
        )
        # Per-leg sum:
        #   long: 5 * (0.39 - 0.47) * 100 = -$40
        #   short: -5 * (1.02 - 0.01) * 100 = -$505
        #   sum = -$545
        assert spread.per_leg_unrealized_pl_sum == pytest.approx(-545.0)
        # Max loss = (0.47 - 0.01) * 5 * 100 = $230
        assert spread.structural_max_loss == pytest.approx(230.0)
        # Display capped at -max_loss
        assert spread.display_unrealized_pl == pytest.approx(-230.0)

    def test_real_loss_within_cap_shown_as_is(self):
        """Spread legitimately down $50 on a $300 max-loss spread
        — display the real number, not the cap."""
        legs = [
            _opt("AAPL260612C00150000", qty=2, entry=3.00, current=2.75),
            _opt("AAPL260612C00155000", qty=-2, entry=1.50, current=1.40),
        ]
        spread = Spread(
            strategy_name="bull_call_spread",
            underlying="AAPL", legs=legs,
        )
        # long: 2*(2.75-3.00)*100 = -50
        # short: -2*(1.40-1.50)*100 = +20
        # sum = -30
        assert spread.per_leg_unrealized_pl_sum == pytest.approx(-30.0)
        # Within cap → display unchanged
        assert spread.display_unrealized_pl == pytest.approx(-30.0)

    def test_profit_uncapped(self):
        """Capping is loss-side only; profitable spreads display
        their full P&L (capped above by the spread's max gain at
        expiry, but for live mark-to-market it can exceed)."""
        legs = [
            _opt("AAPL260612C00150000", qty=2, entry=3.00, current=4.00),
            _opt("AAPL260612C00155000", qty=-2, entry=1.50, current=2.00),
        ]
        spread = Spread(
            strategy_name="bull_call_spread",
            underlying="AAPL", legs=legs,
        )
        # long: 2*1.00*100 = +200; short: -2*0.50*100 = -100; sum = +100
        assert spread.display_unrealized_pl == pytest.approx(100.0)


class TestGrouping:
    def test_two_legs_same_combo_grouped(self):
        legs = [
            _opt("RTX260618P00170000", qty=-1, entry=3.15),
            _opt("RTX260618P00160000", qty=1, entry=1.74),
        ]
        rows = [
            {"occ_symbol": "RTX260618P00170000",
             "option_strategy": "bull_put_spread",
             "timestamp": "2026-05-11T13:44:20"},
            {"occ_symbol": "RTX260618P00160000",
             "option_strategy": "bull_put_spread",
             "timestamp": "2026-05-11T13:44:20"},
        ]
        spreads, ungrouped = group_into_spreads(legs, rows)
        assert len(spreads) == 1
        assert spreads[0].strategy_name == "bull_put_spread"
        assert spreads[0].underlying == "RTX"
        assert len(spreads[0].legs) == 2
        assert ungrouped == []

    def test_different_strategies_not_grouped(self):
        legs = [
            _opt("RTX260618P00170000", qty=-1, entry=3.15),
            _opt("RTX260618C00190000", qty=1, entry=2.00),
        ]
        rows = [
            {"occ_symbol": "RTX260618P00170000",
             "option_strategy": "bull_put_spread",
             "timestamp": "2026-05-11T13:44:20"},
            {"occ_symbol": "RTX260618C00190000",
             "option_strategy": "iron_condor",
             "timestamp": "2026-05-11T13:44:20"},
        ]
        spreads, ungrouped = group_into_spreads(legs, rows)
        # Different strategy → each is single leg → ungrouped
        assert spreads == []
        assert len(ungrouped) == 2

    def test_orphan_single_leg_returned_in_ungrouped(self):
        """Multileg with partner leg expired — only one leg remains."""
        legs = [_opt("CWAN260612C00026000", qty=3, entry=4.80)]
        rows = [
            {"occ_symbol": "CWAN260612C00026000",
             "option_strategy": "bull_call_spread",
             "timestamp": "2026-05-08T18:54:05"},
        ]
        spreads, ungrouped = group_into_spreads(legs, rows)
        assert spreads == []
        assert len(ungrouped) == 1

    def test_stock_position_passes_through_ungrouped(self):
        from types import SimpleNamespace
        ap = SimpleNamespace(
            symbol="AAPL", qty="100", avg_entry_price="150",
            current_price="155", market_value="15500",
            unrealized_pl="500", unrealized_plpc="0.033",
        )
        stk = Position.from_alpaca(ap)
        spreads, ungrouped = group_into_spreads([stk], [])
        assert spreads == []
        assert ungrouped == [stk]

    def test_outside_timestamp_window_not_grouped(self):
        """Legs >60s apart belong to different submission batches."""
        legs = [
            _opt("RTX260618P00170000", qty=-1, entry=3.15),
            _opt("RTX260618P00160000", qty=1, entry=1.74),
        ]
        rows = [
            {"occ_symbol": "RTX260618P00170000",
             "option_strategy": "bull_put_spread",
             "timestamp": "2026-05-11T13:00:00"},
            {"occ_symbol": "RTX260618P00160000",
             "option_strategy": "bull_put_spread",
             "timestamp": "2026-05-11T15:00:00"},  # 2h later
        ]
        spreads, ungrouped = group_into_spreads(legs, rows)
        assert spreads == []
        assert len(ungrouped) == 2
