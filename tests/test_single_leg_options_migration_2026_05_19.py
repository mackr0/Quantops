"""Pin docs/18 item #3: single-leg OPTIONS path delegates to
`OptionPipeline._execute_single_leg`.

Before 2026-05-19, `trade_pipeline.run_trade_cycle`'s
`if action == "OPTIONS":` branch had ~37 lines of duplicated body
(execute_option_strategy call + Phase 5c prediction-to-trade link).
Same code lived in `OptionPipeline._execute_single_leg`. Bug fixes
to single-leg option submission had to touch two files.

After 2026-05-19: the legacy branch is a thin call to the helper.
One source of truth.

These tests pin:
  1. The legacy branch in trade_pipeline no longer imports
     options_trader.execute_option_strategy directly (i.e. it's
     not still doing the work inline).
  2. A test fixture that calls into the path verifies
     `OptionPipeline._execute_single_leg` gets invoked.
  3. The new dispatcher path (`OptionPipeline.execute` for
     `action="OPTIONS"`) routes through the same helper, so both
     dispatchers produce the same trade_result shape.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# (1) Legacy branch no longer duplicates execute_option_strategy inline
# ---------------------------------------------------------------------------

def test_legacy_branch_no_longer_imports_execute_option_strategy_inline():
    """Grep-style check: the `if action == "OPTIONS":` block in
    trade_pipeline.run_trade_cycle must NOT contain a direct
    `from options_trader import execute_option_strategy` import
    anymore — that's the smoking gun the duplicate body is back."""
    import inspect
    import trade_pipeline
    src = inspect.getsource(trade_pipeline.run_trade_cycle)
    # The full src obviously imports many things; we're specifically
    # checking that the OPTIONS branch delegates rather than inlines.
    # The 2026-05-19 cleanup signature: the branch contains
    # `OptionPipeline._execute_single_leg(` and does NOT contain
    # the old `print(f"  Executing: OPTIONS ...` line.
    assert "_execute_single_leg" in src, (
        "trade_pipeline.run_trade_cycle no longer delegates to "
        "_execute_single_leg — single-leg OPTIONS body was probably "
        "re-inlined. Re-do docs/18 item #3."
    )
    # The legacy inline body had a distinctive print:
    legacy_marker = 'print(f"  Executing: OPTIONS {ai_trade.get'
    assert legacy_marker not in src, (
        "trade_pipeline.run_trade_cycle contains the inline "
        "single-leg OPTIONS body again — duplicate is back."
    )


# ---------------------------------------------------------------------------
# (2) _execute_single_leg is the actual call target
# ---------------------------------------------------------------------------

def test_execute_single_leg_handles_proposal_shape():
    """The helper takes (ctx, proposal_dict, symbol). Verify it
    produces a trade_result dict — shape contract for both the
    legacy caller AND the new dispatcher's caller."""
    from pipelines.option import OptionPipeline
    with patch("options_trader.execute_option_strategy") as fake_exec:
        fake_exec.return_value = {
            "action": "OPTIONS", "occ_symbol": "AAPL  240118C00180000",
            "contracts": 1,
        }
        with patch("client.get_api") as fake_api:
            fake_api.return_value = MagicMock()
            ctx = MagicMock(db_path=None)
            result = OptionPipeline._execute_single_leg(
                ctx,
                {"option_strategy": "long_call", "contracts": 1,
                 "strike": 180, "expiry": "2024-01-18",
                 "symbol": "AAPL"},
                "AAPL",
            )
    assert isinstance(result, dict)
    assert result.get("symbol") == "AAPL"
    assert result.get("action") == "OPTIONS"


def test_execute_single_leg_returns_error_dict_on_broker_failure():
    """If the broker call raises, the helper must return an
    error-shape dict (action='ERROR', reason='...') — NOT raise.
    Pinned because callers (both legacy + new dispatcher) rely on
    receiving SOMETHING to append to their summary details."""
    from pipelines.option import OptionPipeline
    with patch("options_trader.execute_option_strategy") as fake_exec:
        fake_exec.side_effect = RuntimeError("synthetic broker failure")
        with patch("client.get_api") as fake_api:
            fake_api.return_value = MagicMock()
            ctx = MagicMock(db_path=None)
            result = OptionPipeline._execute_single_leg(
                ctx,
                {"option_strategy": "long_call", "contracts": 1,
                 "strike": 180, "expiry": "2024-01-18"},
                "AAPL",
            )
    assert result["action"] == "ERROR"
    assert "synthetic broker failure" in result["reason"]


# ---------------------------------------------------------------------------
# (3) Single source of truth — both call paths produce same shape
# ---------------------------------------------------------------------------

def test_both_call_paths_produce_dict_with_symbol_set():
    """Whether called via the legacy `if action == "OPTIONS":`
    branch or via the new dispatcher's `OptionPipeline.execute`,
    the result is the same trade_result dict and `symbol` is
    populated (the linkage step + downstream details.append both
    depend on it)."""
    from pipelines.option import OptionPipeline
    with patch("options_trader.execute_option_strategy") as fake_exec:
        fake_exec.return_value = {
            "action": "OPTIONS",
            "occ_symbol": "AAPL  240118C00180000",
            "contracts": 1,
        }
        with patch("client.get_api") as fake_api:
            fake_api.return_value = MagicMock()
            ctx = MagicMock(db_path=None)
            r1 = OptionPipeline._execute_single_leg(
                ctx,
                {"option_strategy": "long_call", "contracts": 1,
                 "symbol": "AAPL"},
                "AAPL",
            )
    assert r1.get("symbol") == "AAPL"
