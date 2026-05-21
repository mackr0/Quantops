"""#195 follow-up — live position-count decrement during dispatch
(2026-05-21).

#195 Phase 1 made the position cap a SOFT bound: the pre-filter no
longer drops at-max candidates, the AI prompt carries a swap
directive, and the STEP 5 dispatch sorts SELL/STRONG_SELL before
BUY. But a gap remained: `execute_trade.check_portfolio_constraints`
counts open positions from a `positions_list` SNAPSHOT taken at
cycle start. When the AI emits a cap-aware "SELL X + BUY Y" pair,
the SELL executes first (good) but the snapshot still counts X, so
the BUY hits "Already at max positions (N)" even though the slot
just opened.

Fix: `_decrement_closed_stock_position` removes the fully-closed
stock symbol from the live positions_list as closes execute, so the
later BUY's cap check sees the freed slot.

Pipeline separation (operator rule — stocks and options are
distinct pipelines):
  - Only STOCK closes decrement (action='SELL', the stock-pipeline
    close result). Option closes go through OptionPipeline and don't
    touch this stock cap.
  - The removal matches NON-option rows only, so an option leg on
    the same underlying is preserved.

Tests pin:
  1. Full stock close (position_closed=True) removes the stock row.
  2. Partial close (position_closed=False) removes nothing.
  3. Non-SELL actions remove nothing.
  4. An option leg on the same underlying is NOT removed.
  5. Decrement makes a previously-at-cap BUY pass
     check_portfolio_constraints (the end-to-end property).
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class _Pos(dict):
    """Position stand-in: dict access for ['symbol'] + attribute
    access for .is_option (mirrors the real Position dict-shim)."""
    def __init__(self, symbol, is_option=False, **kw):
        super().__init__(symbol=symbol, **kw)
        self.is_option = is_option


# ---------------------------------------------------------------------------
# 1-4. _decrement_closed_stock_position behavior
# ---------------------------------------------------------------------------

class TestDecrementHelper:
    def test_full_close_removes_stock_row(self):
        from trade_pipeline import _decrement_closed_stock_position
        positions = [
            _Pos("AAPL"), _Pos("MSFT"), _Pos("NVDA"),
        ]
        removed = _decrement_closed_stock_position(
            positions,
            {"action": "SELL", "symbol": "MSFT", "position_closed": True},
        )
        assert removed == 1
        assert [p["symbol"] for p in positions] == ["AAPL", "NVDA"]

    def test_partial_close_removes_nothing(self):
        from trade_pipeline import _decrement_closed_stock_position
        positions = [_Pos("AAPL"), _Pos("MSFT")]
        removed = _decrement_closed_stock_position(
            positions,
            # 75% partial SELL → position still open
            {"action": "SELL", "symbol": "MSFT", "position_closed": False},
        )
        assert removed == 0
        assert len(positions) == 2

    def test_non_sell_action_removes_nothing(self):
        from trade_pipeline import _decrement_closed_stock_position
        positions = [_Pos("AAPL"), _Pos("MSFT")]
        for result in (
            {"action": "BUY", "symbol": "MSFT", "position_closed": True},
            {"action": "SKIP", "symbol": "MSFT"},
            {"action": "BLOCKED", "symbol": "MSFT"},
            {},
            None,
        ):
            removed = _decrement_closed_stock_position(positions, result)
            assert removed == 0
        assert len(positions) == 2

    def test_option_leg_on_same_underlying_preserved(self):
        """A stock close of AAPL must NOT remove an AAPL option leg —
        the options pipeline tracks that separately and gates on
        Greek caps, not this position-count cap."""
        from trade_pipeline import _decrement_closed_stock_position
        positions = [
            _Pos("AAPL", is_option=False),           # the stock
            _Pos("AAPL", is_option=True,             # an option leg
                 occ_symbol="AAPL260101C00200000"),
            _Pos("MSFT", is_option=False),
        ]
        removed = _decrement_closed_stock_position(
            positions,
            {"action": "SELL", "symbol": "AAPL", "position_closed": True},
        )
        assert removed == 1
        remaining = [(p["symbol"], p.is_option) for p in positions]
        assert ("AAPL", True) in remaining, (
            "The AAPL option leg must survive a stock-side AAPL close."
        )
        assert ("AAPL", False) not in remaining
        assert ("MSFT", False) in remaining


# ---------------------------------------------------------------------------
# 5. End-to-end: decrement unblocks a previously-at-cap BUY
# ---------------------------------------------------------------------------

class TestDecrementUnblocksBuy:
    def test_at_cap_buy_passes_after_close(self):
        """The whole point: at cap=3 with 3 stock positions, a BUY of
        a new symbol is blocked. After a full close decrements the
        list to 2, the same BUY passes."""
        from trade_pipeline import _decrement_closed_stock_position
        from portfolio_manager import check_portfolio_constraints

        positions_list = [_Pos("AAPL"), _Pos("MSFT"), _Pos("NVDA")]
        account = {"equity": 100000.0, "cash": 50000.0}
        # positions dict the constraint check consumes (stock-only)
        def _as_dict(plist):
            return {p["symbol"]: p for p in plist
                    if not getattr(p, "is_option", False)}

        proposed = {"side": "buy", "qty": 10, "price": 100.0}

        # Before close: at cap (3/3) → new symbol blocked
        allowed, reason = check_portfolio_constraints(
            "TSLA", proposed, _as_dict(positions_list), account,
            max_position_pct=0.5, max_total_positions=3,
        )
        assert allowed is False
        assert "max positions" in reason.lower()

        # AI's cap-aware swap: STRONG_SELL MSFT (full close)
        _decrement_closed_stock_position(
            positions_list,
            {"action": "SELL", "symbol": "MSFT", "position_closed": True},
        )

        # After close: 2/3 → the same new-symbol BUY now passes
        allowed2, reason2 = check_portfolio_constraints(
            "TSLA", proposed, _as_dict(positions_list), account,
            max_position_pct=0.5, max_total_positions=3,
        )
        assert allowed2 is True, (
            f"BUY still blocked after a full close freed a slot: "
            f"{reason2}. The live-decrement is what makes the AI's "
            "cap-aware SELL+BUY swap actually work."
        )
