"""Regression test for the INTC stuck-exit bug, 2026-05-05.

Bug: profile_11 (Large Cap Limit Orders) had INTC long with +33%
unrealized gain and a take-profit set BELOW the current price. Every
exit cycle deferred with reason "entry order has not filled at the
broker yet" — even though the position was real.

Cause: `_entry_order_filled_at_broker` looked up the journal's entry
order_id and queried Alpaca for THAT order's status. For the limit-
order profile, the original limit order from 11 days earlier had
been cancelled/expired/replaced. Alpaca returned status='canceled'.
Gate saw not-filled → deferred forever, even though shares existed
at the broker because a SUBSEQUENT limit order filled.

Fix: gate checks `api.list_positions()` for actual shares, not a
specific order_id's status. This regression test pins both halves —
the position-based check that catches the prod scenario AND the
backwards-compat behavior that no-broker-shares correctly defers.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _api_with_positions(positions_list):
    api = MagicMock()
    api.list_positions.return_value = positions_list
    return api


def _pos(symbol, qty):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    return p


def test_long_position_at_broker_allows_exit():
    """The exact prod scenario: long INTC at broker, journal has
    stale order_id from cancelled limit order, gate must say 'allow
    exit' so the SELL goes through."""
    from trader import _entry_order_filled_at_broker
    api = _api_with_positions([_pos("INTC", 28)])
    assert _entry_order_filled_at_broker(api, "any_db", "INTC", is_short=False) is True


def test_no_position_blocks_exit():
    """No shares at broker → can't sell → defer."""
    from trader import _entry_order_filled_at_broker
    api = _api_with_positions([])
    assert _entry_order_filled_at_broker(api, "any_db", "INTC", is_short=False) is False


def test_short_position_allows_short_exit():
    """Short qty < 0 at broker → BUY-to-cover should be allowed."""
    from trader import _entry_order_filled_at_broker
    api = _api_with_positions([_pos("TSLA", -10)])
    assert _entry_order_filled_at_broker(api, "any_db", "TSLA", is_short=True) is True


def test_long_held_when_short_exit_attempted_blocks():
    """If we hold a LONG but the caller asks 'can I close my short?',
    the answer is no — wrong side."""
    from trader import _entry_order_filled_at_broker
    api = _api_with_positions([_pos("INTC", 28)])
    assert _entry_order_filled_at_broker(api, "any_db", "INTC", is_short=True) is False


def test_short_held_when_long_exit_attempted_blocks():
    from trader import _entry_order_filled_at_broker
    api = _api_with_positions([_pos("TSLA", -10)])
    assert _entry_order_filled_at_broker(api, "any_db", "TSLA", is_short=False) is False


def test_broker_call_failure_is_permissive():
    """If list_positions raises (broker outage), don't block exits —
    the submit step has its own error handling."""
    from trader import _entry_order_filled_at_broker
    api = MagicMock()
    api.list_positions.side_effect = RuntimeError("broker down")
    assert _entry_order_filled_at_broker(api, "any_db", "INTC", is_short=False) is True


def test_case_insensitive_symbol_match():
    from trader import _entry_order_filled_at_broker
    api = _api_with_positions([_pos("intc", 28)])
    assert _entry_order_filled_at_broker(api, "any_db", "INTC", is_short=False) is True


def test_other_symbols_dont_satisfy_check():
    from trader import _entry_order_filled_at_broker
    api = _api_with_positions([_pos("MSFT", 10), _pos("AAPL", 5)])
    assert _entry_order_filled_at_broker(api, "any_db", "INTC", is_short=False) is False
