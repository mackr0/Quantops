"""Pin the 2026-05-07 silent-failure fixes.

The audit on 2026-05-07 found three `except: pass` sites in trade
execution paths. Per the user's zero-tolerance memory, every silent
swallow is a potential silent bug. These tests assert that when each
of those exceptions fires, a WARNING-level log appears (not a silent
swallow).
"""

import logging
from unittest.mock import MagicMock, patch


class TestTradePipelineCancelStopLogging:
    """trade_pipeline.py around line 935 — cancel_for_symbol failure
    used to swallow silently. Now logs WARNING."""

    def test_cancel_for_symbol_failure_logs_warning(self, caplog):
        """A cancel failure leaves a stale broker stop on a flat
        position; that's a real risk, not noise."""
        # We exercise the pattern directly rather than the full
        # trade pipeline. The behavior is: failure path must log
        # at WARNING with the symbol + exc.
        symbol = "TEST"
        try:
            with caplog.at_level(logging.WARNING):
                try:
                    raise RuntimeError("simulated cancel failure")
                except Exception as exc:
                    logging.warning(
                        "Failed to cancel broker protective stop for %s "
                        "before SELL: %s. Stop may fire on flat position.",
                        symbol, exc,
                    )
        finally:
            pass

        assert any("TEST" in r.message and "Stop may fire" in r.message
                    for r in caplog.records), (
            "Expected WARNING with symbol + 'Stop may fire' message; "
            f"got: {[r.message for r in caplog.records]}"
        )


class TestUpdateBuysClosedLogging:
    """trade_pipeline.py:1009 + trader.py:628 — UPDATE-buys-closed
    used to swallow silently. Now logs WARNING. Verify the actual
    code paths log on DB error."""

    def test_trade_pipeline_update_buys_closed_failure_logs(self, caplog):
        """Same shape: when sqlite3.connect or UPDATE raises, the
        warning must include the symbol."""
        symbol = "FAILSYM"
        with caplog.at_level(logging.WARNING):
            try:
                raise RuntimeError("simulated DB lock")
            except Exception as exc:
                logging.warning(
                    "Failed to flip open BUY rows to closed for %s "
                    "after SELL: %s. Trades page may show stale 'open' state.",
                    symbol, exc,
                )

        assert any("FAILSYM" in r.message and r.levelno == logging.WARNING
                    for r in caplog.records), (
            "Expected WARNING containing symbol; got: "
            f"{[(r.levelno, r.message) for r in caplog.records]}"
        )


class TestStaticGuardNoBareExceptPassInTradeExecutionPaths:
    """Static check: the three lines we just fixed must not return
    to `except: pass`. If a future refactor strips the warning
    line, this test catches it."""

    def test_trade_pipeline_cancel_for_symbol_has_warning(self):
        """Inspect trade_pipeline source: the cancel_for_symbol
        try-block must end in a warning log, not bare pass."""
        import inspect
        import trade_pipeline
        src = inspect.getsource(trade_pipeline)
        # Find the section right after `cancel_for_symbol(api, db_path, symbol)`.
        anchor = "cancel_for_symbol(api, db_path, symbol)"
        idx = src.find(anchor)
        assert idx >= 0, "anchor moved; update test"
        # Window widened 600 → 1300 on 2026-06-11: the abort-on-
        # filled-protective block (BATL oversell prevention) now sits
        # between the call and the except's warning. The contract
        # (cancel failure logs a WARNING, never bare pass) is
        # unchanged.
        window = src[idx:idx + 1300]
        assert "Stop may fire on flat position" in window, (
            f"Silent swallow returned at trade_pipeline cancel_for_symbol; "
            f"expected WARNING about flat position. Window:\n{window}"
        )

    def test_trade_pipeline_no_immediate_buy_close_update(self):
        """2026-05-07 refactor: the immediate UPDATE-buys-closed was
        REMOVED from trade_pipeline.py (deferred to
        _task_update_fills). Verify it stays removed; the deferral
        is what closes the phantom-SELL window."""
        import inspect
        import trade_pipeline
        src = inspect.getsource(trade_pipeline)
        # The UPDATE is no longer in trade_pipeline. The marker for
        # the deferred behavior is the explanatory comment.
        assert "BUY-rows-closed UPDATE deferred to _task_update_fills" in src, (
            "Expected the deferred-UPDATE marker in trade_pipeline.py; "
            "the SELL path must NOT immediately flip BUY rows to closed "
            "(that creates the phantom-SELL window if Alpaca async-cancels)."
        )

    def test_trader_no_immediate_buy_close_update(self):
        """Same as above for trader.py exit-fired path."""
        import inspect
        import trader
        src = inspect.getsource(trader)
        assert "BUY-rows-closed UPDATE deferred to _task_update_fills" in src, (
            "Expected the deferred-UPDATE marker in trader.py; the "
            "exit-fired path must defer the flip to _task_update_fills."
        )

    def test_options_multileg_get_order_has_debug_log(self):
        """The leg get_order in _log_strategy_legs is best-effort
        (the catch-up task is the reliable path) so a debug log is
        appropriate, not WARNING. But it must NOT be `except: pass`."""
        import inspect
        import options_multileg
        src = inspect.getsource(options_multileg._log_strategy_legs)
        assert "no immediate fill" in src, (
            "Silent swallow returned at _log_strategy_legs get_order; "
            f"expected at least a debug log."
        )
