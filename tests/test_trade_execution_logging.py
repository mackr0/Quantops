"""Guardrail: trade execution failures must be logged loudly, not
silently swallowed.

History: on 2026-04-28 the user reported "the SHORT was listed but
never went through." The ticker showed `Executing: SHORT VALE` but
no order_id, no submitted log, no error trace — the order was
silently rejected. Root cause: `run_trade_cycle`'s try/except
around `execute_trade` swallowed all exceptions into the `errors[]`
list with no log emission. The pipeline summary said `errors=0`
because the count was per-symbol-error in a different counter.

Fix:
- Exception path: `logging.error(..., exc_info=True)` so the full
  traceback hits the journal.
- Non-exception SKIP path (action=SKIP / EXCLUDED / EARNINGS_SKIP /
  etc.): `logging.warning(...)` so the user sees WHY a trade
  printed "Executing:" but didn't actually submit.

These tests source-walk `trade_pipeline.run_trade_cycle` to ensure
both log calls are present.
"""

from __future__ import annotations

import inspect

import pytest


def _exec_block_source():
    """Pull the source of the for-loop block that calls execute_trade
    inside run_trade_cycle. We don't have a clean function boundary
    so we slice on the print/Executing marker.

    Window history:
      - 2500 originally
      - 4000 when wash-trade + insufficient-qty classifiers landed
      - 5500 when the OPTIONS dispatch branch landed in front (Item 1a)
      - 7500 when MULTILEG_OPEN dispatch + helper landed (Phase B4)
        — each new action branch pushes the genuine logging.error call
        further down. Future branches will need similar bumps.
    """
    import trade_pipeline as tp
    src = inspect.getsource(tp)
    start = src.find('print(f"  Executing: ')
    assert start > 0, "Could not locate the Executing: print site"
    return src[start:start + 7500]


def test_exception_path_logs_full_traceback():
    """When execute_trade raises, the exception MUST be logged
    with `exc_info=True` so the journal captures the traceback.
    Otherwise an Alpaca rejection (e.g., 'not shortable') is
    invisible to operators."""
    blob = _exec_block_source()
    # Look for logging.error with exc_info=True somewhere after the
    # 'except Exception' block.
    assert "logging.error" in blob, (
        "REGRESSION: run_trade_cycle's except Exception clause no "
        "longer calls logging.error. Trade-submit failures will be "
        "silently swallowed again. See 2026-04-28 VALE SHORT incident."
    )
    assert "exc_info=True" in blob, (
        "REGRESSION: the exception logger doesn't include exc_info=True "
        "— without the traceback, diagnosing a rejected order requires "
        "reproducing it manually. See 2026-04-28 incident."
    )


def test_skip_path_logs_warning_with_reason():
    """When execute_trade returns a non-trade action (SKIP / EXCLUDED
    / EARNINGS_SKIP / etc.) the caller MUST log a warning so
    operators can see WHY the printed 'Executing:' didn't produce a
    submitted order."""
    blob = _exec_block_source()
    # Look for the conditional check that SKIP-type actions trigger
    # a warning log. The exact phrasing isn't fixed but the words
    # "Trade NOT submitted" or similar should appear.
    has_skip_warning = (
        "Trade NOT submitted" in blob
        or 'logging.warning' in blob
    )
    assert has_skip_warning, (
        "REGRESSION: when execute_trade returns SKIP / EXCLUDED, "
        "the caller no longer logs a warning. The user sees "
        "'Executing: SHORT X' on the dashboard but no follow-up "
        "trace of why the order didn't submit. See 2026-04-28 "
        "VALE SHORT incident."
    )
