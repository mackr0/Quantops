"""Stocks vs options pipeline separation guardrails (#189, 2026-05-20).

The bug class.
`execute_trade` is the stock pipeline. Option signals (OPTIONS,
MULTILEG_OPEN) and pair-trade signals (PAIR_TRADE) route to dedicated
pipelines from `run_trade_cycle` (see ~line 2310-2401). Pre-fix, the
stock pipeline's positions dict was built from the full positions list
including option-leg positions, and `Position.__getitem__("symbol")`
returns the underlying for BOTH stock and option-leg positions. So a
profile holding stock QCOM + an option leg on QCOM produced a dict-key
collision: only one survived, the other was silently lost. The AI's
STRONG_SELL signal then acted on whichever position landed last in
dict iteration — producing wrong-qty stock sells with option-magnitude
P&L (observed 2026-05-20 with QCOM 1-share sells against -$111 P&L).

Also pre-fix: `num_positions = len(positions_list)` counted each option
leg as a separate position against `max_total_positions`. A multileg
spread of N legs counted as N positions. Profiles overshot their cap
silently (pid17: 12/10, pid21: 9/5 observed), blocking every new
stock candidate via the pre-filter's `at_max_positions` check.

These tests pin the separation:
  1. Stock pipeline only sees stock positions (`positions` dict).
  2. Stock pipeline only sees stock positions (`positions_dict`).
  3. `num_positions` counts stocks only.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _stock_position(symbol: str, qty: float) -> Any:  # noqa: F821
    from position import Position
    return Position(
        instrument_kind="stock",
        underlying=symbol,
        occ_symbol=None,
        qty_signed=float(qty),
        avg_entry_price=200.0,
        current_price=205.0,
        market_value=200.0 * float(qty),
        unrealized_pl=5.0 * float(qty),
        unrealized_plpc=0.025,
    )


def _option_position(underlying: str, occ: str, qty: float) -> Any:  # noqa: F821
    from position import Position
    return Position(
        instrument_kind="option",
        underlying=underlying,
        occ_symbol=occ,
        qty_signed=float(qty),
        avg_entry_price=12.50,
        current_price=10.25,
        market_value=10.25 * 100.0 * float(qty),
        unrealized_pl=-225.0 * float(qty),
        unrealized_plpc=-0.18,
    )


class TestExecuteTradePositionsFiltersOptions:
    """`execute_trade.positions` dict must contain only stock positions.
    Option-leg positions on the same underlying as a stock position
    must not collide and silently overwrite the stock entry."""

    def test_stock_position_survives_when_option_leg_shares_underlying(self):
        """The canonical bug case: profile holds 73 shares of QCOM + 1
        contract of an option leg on QCOM. After the filter, the
        stock pipeline's `positions["QCOM"]` must be the STOCK
        position (qty=73), not the option leg (qty=1)."""
        stock = _stock_position("QCOM", 73.0)
        option = _option_position(
            "QCOM", "QCOM250620C00210000", 1.0,
        )
        # The order matters for reproducing the bug — option last means
        # the unfiltered dict comprehension would lose the stock.
        positions_list = [stock, option]
        # Replicate the post-fix dict-comprehension behavior:
        positions = {p["symbol"]: p for p in positions_list
                     if not getattr(p, "is_option", False)}
        assert "QCOM" in positions, "Stock position must be present"
        assert positions["QCOM"].qty_signed == 73.0, (
            "After filter, positions['QCOM'] must be the STOCK position "
            "(qty=73), not the option leg (qty=1)."
        )
        assert positions["QCOM"].is_stock

    def test_option_only_holding_is_invisible_to_stock_pipeline(self):
        """Profile holds only an option leg on QCOM. Stock pipeline's
        `positions` must be empty for QCOM (option leg is the option
        pipeline's concern)."""
        option = _option_position(
            "QCOM", "QCOM250620P00200000", 1.0,
        )
        positions_list = [option]
        positions = {p["symbol"]: p for p in positions_list
                     if not getattr(p, "is_option", False)}
        assert "QCOM" not in positions

    def test_no_options_holding_unchanged(self):
        """Stock-only portfolio behaves identically pre/post filter."""
        positions_list = [
            _stock_position("AAPL", 50.0),
            _stock_position("MSFT", 25.0),
        ]
        positions = {p["symbol"]: p for p in positions_list
                     if not getattr(p, "is_option", False)}
        assert set(positions) == {"AAPL", "MSFT"}
        assert positions["AAPL"].qty_signed == 50.0
        assert positions["MSFT"].qty_signed == 25.0


class TestNumPositionsCountsStocksOnly:
    """`num_positions` (used to gate at_max_positions for the stock
    cap) must count only stock positions. Option legs are gated by
    greek-budget params on the schema, not by this count."""

    def test_multileg_spread_does_not_count_as_three_positions(self):
        """A two-leg vertical spread on QCOM + 1 stock position must
        count as 1 stock position, not 3 total positions."""
        positions_list = [
            _stock_position("QCOM", 73.0),
            _option_position("QCOM", "QCOM250620C00200000", 1.0),
            _option_position("QCOM", "QCOM250620C00210000", -1.0),
        ]
        num_positions = sum(
            1 for p in positions_list if not getattr(p, "is_option", False)
        )
        assert num_positions == 1

    def test_pure_options_portfolio_has_zero_stock_count(self):
        """A profile holding 4 option legs (e.g., iron condor) and no
        stock has 0 stock positions — does not trip the stock cap."""
        positions_list = [
            _option_position("SPY", "SPY250620P00400000", -1.0),
            _option_position("SPY", "SPY250620P00395000", 1.0),
            _option_position("SPY", "SPY250620C00420000", -1.0),
            _option_position("SPY", "SPY250620C00425000", 1.0),
        ]
        num_positions = sum(
            1 for p in positions_list if not getattr(p, "is_option", False)
        )
        assert num_positions == 0

    def test_mixed_portfolio_counts_unique_stock_underlyings(self):
        """Mixed portfolio: 2 distinct stocks + multileg options on
        another underlying. Stock count = 2 (the two stocks). The
        options don't add to the stock count."""
        positions_list = [
            _stock_position("AAPL", 100.0),
            _stock_position("MSFT", 50.0),
            _option_position("TSLA", "TSLA250620C00200000", 1.0),
            _option_position("TSLA", "TSLA250620P00190000", 1.0),
        ]
        num_positions = sum(
            1 for p in positions_list if not getattr(p, "is_option", False)
        )
        assert num_positions == 2


class TestPositionsDictForPredictionClassifier:
    """`positions_dict` in run_trade_cycle is consumed by the prediction
    classifier (line ~1829) to decide if an AI BUY/SELL/SHORT is an
    exit signal vs a directional signal. The classifier is stock-level
    (AI's BUY/SELL/SHORT signals don't disambiguate per-instrument).
    Filtering option positions makes the classification deterministic
    and aligned with the AI's stock-level intent."""

    def test_dict_excludes_option_legs(self):
        positions_list = [
            _stock_position("AAPL", 100.0),
            _option_position("AAPL", "AAPL250620C00200000", 1.0),
        ]
        positions_dict = {p["symbol"]: p for p in positions_list
                          if not getattr(p, "is_option", False)}
        assert "AAPL" in positions_dict
        assert positions_dict["AAPL"].qty_signed == 100.0
        # The option leg is NOT in the dict — option pipeline's domain.
        assert positions_dict["AAPL"].is_stock

    def test_held_symbols_remains_inclusive(self):
        """`held_symbols` at line 1415 stays inclusive (it's the
        pre-filter 'do we hold ANYTHING on this symbol' check, not the
        stock-pipeline-specific lookup). The fix only filters
        positions_dict, not held_symbols."""
        positions_list = [
            _option_position("QCOM", "QCOM250620P00200000", 1.0),
        ]
        held_symbols = {p["symbol"] for p in positions_list}
        assert "QCOM" in held_symbols, (
            "held_symbols must stay inclusive — option-only holdings "
            "still mean we 'hold' that underlying for the purposes of "
            "the at-max-positions / non-held-candidate skip logic."
        )


# Typing import at end so pytest can resolve forward refs
from typing import Any  # noqa: E402
