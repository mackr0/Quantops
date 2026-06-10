"""2026-06-10 — surface real option-order rejection reasons.

Two bugs the user caught in the first post-reset market open:

  1. `INTC ERROR — Broker did not return order_id for INTC 260717C00115000`.
     The upstream caller had no visibility into the actual broker
     rejection. The error string was generic; the operator couldn't
     tell whether Alpaca rejected for an invalid contract, an
     authentication issue, a market-hours issue, etc.

  2. `OPTIONS INTC BLOCKED (Unsupported option_strategy:
     'bull_put_spread')`. The AI proposed a multileg strategy under
     the single-leg OPTIONS action. The pre-fix message just said
     "Unsupported" without telling the AI (or operator) that
     bull_put_spread requires action='MULTILEG_OPEN'.

Fixes:

  1. `submit_option_order` stashes its rejection reason on a
     module-level `_LAST_OPTION_ORDER_ERROR`. The execute_option_strategy
     caller pulls it via `get_last_option_order_error()` and stamps
     it into the result["reason"] instead of the generic "Broker did
     not return order_id".

  2. `execute_option_strategy` recognises multileg strategy names
     (bull_put_spread, iron_condor, etc.) and refuses with a
     specific reason naming action='MULTILEG_OPEN' as the correct
     action class. Other unsupported strategies also get a clearer
     message listing the single-leg supported set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Layer 1 — real broker rejection surfaces in the drop reason
# ---------------------------------------------------------------------------


def test_submit_option_order_records_exception_reason():
    """When the broker raises, submit_option_order returns None and
    stashes the exception name+message on _LAST_OPTION_ORDER_ERROR."""
    import options_trader
    api = MagicMock()
    # Force the raw-submit helper to raise a typical Alpaca rejection
    with patch(
        "options_multileg._submit_alpaca_order_raw",
        side_effect=RuntimeError(
            'Alpaca order rejected (422): {"code":42210000,'
            '"message":"asset is no longer tradable"}'
        ),
    ):
        result = options_trader.submit_option_order(
            api, "INTC  260717C00115000", side="buy", qty=1,
            order_type="market",
        )
    assert result is None
    err = options_trader.get_last_option_order_error()
    assert err is not None
    assert "RuntimeError" in err, (
        f"Error string should include exception class. Got: {err!r}"
    )
    assert "42210000" in err or "tradable" in err, (
        f"Error string should preserve the broker's actual rejection "
        f"detail. Got: {err!r}"
    )


def test_submit_option_order_records_invalid_args_reason():
    """Invalid args (e.g. empty occ_symbol) also produce a specific
    error reason, not just None with no detail."""
    import options_trader
    api = MagicMock()
    result = options_trader.submit_option_order(
        api, "", side="buy", qty=1, order_type="market",
    )
    assert result is None
    err = options_trader.get_last_option_order_error()
    assert err and "invalid args" in err


def test_execute_option_strategy_surfaces_real_rejection():
    """When submit_option_order fails with a real broker rejection,
    execute_option_strategy's drop reason must include the actual
    rejection text, NOT the generic 'Broker did not return order_id'."""
    src = (REPO_ROOT / "options_trader.py").read_text()
    # Find the upstream caller's failure branch
    anchor = src.find("Option order rejected for")
    assert anchor > 0, (
        "The drop reason must use 'Option order rejected for {occ}: "
        "{last_err}' — surfaces the actual broker rejection. The "
        "fallback to 'Broker did not return order_id' is acceptable "
        "ONLY when no last_err is recorded."
    )
    window = src[max(0, anchor - 600):anchor + 600]
    assert "get_last_option_order_error" in window, (
        "Caller must read the actual rejection via "
        "get_last_option_order_error() before falling back to the "
        "generic message."
    )


# ---------------------------------------------------------------------------
# Layer 2 — multileg strategies on OPTIONS action get specific routing message
# ---------------------------------------------------------------------------


def test_multileg_strategy_on_options_action_names_multileg_open():
    """When the AI proposes bull_put_spread under action='OPTIONS',
    the rejection reason must explicitly direct toward
    action='MULTILEG_OPEN' so the prompt-tuner / AI can correct."""
    from options_trader import execute_option_strategy
    proposal = {
        "symbol": "INTC",
        "option_strategy": "bull_put_spread",
        "strike": 105.0,  # single strike — wrong for spreads
        "expiry": "2099-07-17",
        "contracts": 1,
    }
    result = execute_option_strategy(api=MagicMock(), ctx=None, proposal=proposal)
    assert result["action"] == "SKIP", (
        f"Multileg-on-OPTIONS must SKIP (AI mistake, not system error). "
        f"Got action={result['action']!r}"
    )
    reason = (result.get("reason") or "").lower()
    assert "multileg_open" in reason, (
        f"Rejection must explicitly name 'MULTILEG_OPEN' as the "
        f"correct action class. Got: {result['reason']!r}"
    )
    assert "bull_put_spread" in reason, (
        f"Rejection must name the strategy the AI proposed so the "
        f"prompt tuner can identify the misroute. Got: {result['reason']!r}"
    )


def test_unsupported_strategy_lists_single_leg_options():
    """An unrecognized strategy that's not a known multileg either
    should list the supported single-leg set so the AI / operator
    knows what to choose from."""
    from options_trader import execute_option_strategy
    proposal = {
        "symbol": "INTC",
        "option_strategy": "nonsense_strategy",
        "strike": 105.0,
        "expiry": "2099-07-17",
        "contracts": 1,
    }
    result = execute_option_strategy(api=MagicMock(), ctx=None, proposal=proposal)
    assert result["action"] == "SKIP"
    reason = (result.get("reason") or "").lower()
    for name in ("covered_call", "protective_put", "long_call",
                 "long_put", "cash_secured_put"):
        assert name in reason, (
            f"Rejection must list {name} as a supported single-leg "
            f"strategy. Got: {result['reason']!r}"
        )


def test_legit_single_leg_strategy_proceeds_past_validation():
    """Regression: long_call must still pass the strategy validation
    and proceed to the deeper checks (it'll likely fail on missing
    contract data downstream, but it shouldn't be rejected at the
    strategy-name step)."""
    from options_trader import execute_option_strategy
    proposal = {
        "symbol": "INTC",
        "option_strategy": "long_call",
        "strike": 105.0,
        "expiry": "2099-07-17",
        "contracts": 1,
    }
    result = execute_option_strategy(api=MagicMock(), ctx=None, proposal=proposal)
    # Must NOT be rejected with the strategy-name reason. It can be
    # rejected later for other reasons (missing OCC, sizing, etc.)
    reason = (result.get("reason") or "").lower()
    assert "unsupported option_strategy" not in reason, (
        f"long_call is a supported single-leg strategy and must pass "
        f"the strategy-name gate. Got: {result['reason']!r}"
    )
