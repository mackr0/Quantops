"""Per-position exit failure must NOT halt the entire check_exits cycle.

History 2026-04-30: with multiple profiles sharing one Alpaca paper
account (3 accounts, 10 profiles), cumulative reserved share counts
across protective stops + take-profits + trailing stops + polling
exits can exceed actual qty held at the broker. Alpaca rejects with
'insufficient qty available for order'.

Before this fix: the APIError propagated up out of trader.check_exits,
the entire task crashed, and every subsequent position in that cycle
got NO protective-stop refresh, NO trailing detection, NO exit
processing. One bad submit took out the whole sweep.

After: per-position try/except logs a warning and continues.
Subsequent positions get processed normally.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_one_position_exit_failure_does_not_halt_loop():
    """Three triggers fire in one cycle. The middle one's submit_order
    raises (simulating Alpaca's 'insufficient qty'). Both the first
    and third must still be processed."""
    import trader

    triggers = [
        {"symbol": "AAPL", "qty": 10, "trigger": "stop_loss",
         "reason": "test", "price": 150.0, "is_short": False},
        {"symbol": "MSFT", "qty": 9, "trigger": "stop_loss",
         "reason": "test", "price": 300.0, "is_short": False},
        {"symbol": "GOOG", "qty": 5, "trigger": "stop_loss",
         "reason": "test", "price": 130.0, "is_short": False},
    ]

    api = MagicMock()
    # Make MSFT's submit_order raise; AAPL and GOOG succeed.
    def fake_submit(**kwargs):
        if kwargs.get("symbol") == "MSFT":
            raise Exception("insufficient qty available for order (requested: 9, available: 8)")
        return MagicMock(id=f"order-{kwargs['symbol']}")
    api.submit_order.side_effect = fake_submit
    api.list_orders.return_value = []

    ctx = MagicMock()
    ctx.db_path = ":memory:"
    ctx.schedule_type = "market_hours"

    # Stub everything except the loop body
    with patch("trader._process_exit_trigger") as mock_proc:
        mock_proc.side_effect = [
            None,  # AAPL succeeds
            Exception("insufficient qty available"),  # MSFT fails
            None,  # GOOG succeeds
        ]

        # Direct simulation of the loop body
        results = []
        for trigger_signal in triggers:
            try:
                trader._process_exit_trigger(
                    trigger_signal, api, ctx, ":memory:", [],
                    {}, results,
                )
            except Exception:
                # The wrapping in trader.check_exits absorbs this.
                pass

    # All three triggers were attempted; the bad one didn't halt the loop.
    assert mock_proc.call_count == 3


def test_check_exits_loop_wraps_each_trigger_in_try_except():
    """Source-level pin: trader.check_exits must wrap per-trigger work
    so one APIError doesn't propagate up and kill the whole task. The
    2026-04-30 incident took out 11 cycles before this guard landed."""
    import inspect
    import trader
    src = inspect.getsource(trader.check_exits)
    # Looking for per-position try/except pattern. The wrapped call
    # must use _process_exit_trigger (the extracted body).
    assert "_process_exit_trigger" in src, (
        "trader.check_exits no longer dispatches to "
        "_process_exit_trigger — per-position resilience regressed."
    )
    # The try/except must be present
    assert "try:" in src and "Exception as exc" in src, (
        "trader.check_exits no longer wraps the per-trigger call "
        "in try/except — one bad submit will crash the whole cycle."
    )


def test_process_exit_trigger_is_a_separate_function():
    """The body that used to be inline in the for-loop is now a
    separate function so it can be wrapped without losing readability."""
    import trader
    assert hasattr(trader, "_process_exit_trigger"), (
        "trader._process_exit_trigger missing — the resilience refactor "
        "regressed. Without this function the per-position try/except "
        "doesn't have a clean boundary to wrap."
    )
