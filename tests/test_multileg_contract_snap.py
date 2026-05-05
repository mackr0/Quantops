"""Snap-to-listed-contract for multi-leg strategies.

Bug 2026-05-05: AI proposed a bear_put_spread on VALE at June 12,
2026 / $15.50 + $15.00 strikes. Alpaca rejected with
"asset 'VALE  260612P00015500' not found" — those exact contracts
weren't listed (real expiry is the 3rd-Friday June 19, real strike
intervals don't include 15.50 at that DTE). Every multi-leg with
mismatched strikes/expiries failed.

The fix: before submission, look up listed contracts from Alpaca and
snap each leg's strike + expiry to the closest listed contract within
tolerance (5% strike, 30 days expiry). Refuse the whole strategy if
any leg can't snap within tolerance.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


SAMPLE_CONTRACTS = [
    # VALE June 19 2026 puts
    {"symbol": "VALE260619P00014000", "expiration_date": "2026-06-19",
     "type": "put", "strike": 14.0},
    {"symbol": "VALE260619P00015000", "expiration_date": "2026-06-19",
     "type": "put", "strike": 15.0},
    {"symbol": "VALE260619P00016000", "expiration_date": "2026-06-19",
     "type": "put", "strike": 16.0},
    # VALE July 17 2026 puts
    {"symbol": "VALE260717P00014000", "expiration_date": "2026-07-17",
     "type": "put", "strike": 14.0},
    {"symbol": "VALE260717P00015000", "expiration_date": "2026-07-17",
     "type": "put", "strike": 15.0},
    # VALE June 19 2026 calls
    {"symbol": "VALE260619C00018000", "expiration_date": "2026-06-19",
     "type": "call", "strike": 18.0},
]


def test_exact_match_snaps_unchanged():
    from options_chain_alpaca import snap_to_listed_contract
    snapped = snap_to_listed_contract(
        "VALE", "2026-06-19", 15.0, "P", contracts=SAMPLE_CONTRACTS,
    )
    assert snapped is not None
    assert snapped["symbol"] == "VALE260619P00015000"
    assert snapped["strike"] == 15.0


def test_snaps_strike_to_nearest():
    """AI asks for $15.40 — closest listed strike is $15.0."""
    from options_chain_alpaca import snap_to_listed_contract
    snapped = snap_to_listed_contract(
        "VALE", "2026-06-19", 15.40, "P", contracts=SAMPLE_CONTRACTS,
    )
    assert snapped is not None
    # 15.40 is 0.40 from 15, 0.60 from 16 → snaps to 15
    assert snapped["strike"] == 15.0
    assert snapped["expiration_date"] == "2026-06-19"


def test_snaps_expiry_to_nearest():
    """AI asks for June 12 — closest listed is June 19 (7 days off, well within 30)."""
    from options_chain_alpaca import snap_to_listed_contract
    snapped = snap_to_listed_contract(
        "VALE", "2026-06-12", 15.0, "P", contracts=SAMPLE_CONTRACTS,
    )
    assert snapped is not None
    assert snapped["expiration_date"] == "2026-06-19"
    assert snapped["strike"] == 15.0


def test_refuses_when_expiry_too_far():
    """AI asks for an expiry > 30 days from any listed — refuse."""
    from options_chain_alpaca import snap_to_listed_contract
    snapped = snap_to_listed_contract(
        "VALE", "2027-12-01", 15.0, "P", contracts=SAMPLE_CONTRACTS,
    )
    assert snapped is None


def test_refuses_when_strike_too_far():
    """AI asks for a strike > 5% off any listed strike at the closest expiry."""
    from options_chain_alpaca import snap_to_listed_contract
    # Closest expiry June 19 has strikes 14, 15, 16 only.
    # AI asks for $50 (way above any listed strike).
    snapped = snap_to_listed_contract(
        "VALE", "2026-06-19", 50.0, "P", contracts=SAMPLE_CONTRACTS,
    )
    assert snapped is None


def test_filters_by_option_type():
    """A put request shouldn't match a call contract even at same strike/expiry."""
    from options_chain_alpaca import snap_to_listed_contract
    # Calls at June 19: only $18 available
    snapped_call = snap_to_listed_contract(
        "VALE", "2026-06-19", 18.0, "C", contracts=SAMPLE_CONTRACTS,
    )
    assert snapped_call is not None
    assert snapped_call["type"] == "call"
    # Same strike requested as put → matches the listed put, NOT call
    snapped_put = snap_to_listed_contract(
        "VALE", "2026-06-19", 15.0, "P", contracts=SAMPLE_CONTRACTS,
    )
    assert snapped_put["type"] == "put"


def test_empty_contract_list_returns_none():
    from options_chain_alpaca import snap_to_listed_contract
    assert snap_to_listed_contract(
        "VALE", "2026-06-19", 15.0, "P", contracts=[],
    ) is None


def test_invalid_target_expiry_returns_none():
    from options_chain_alpaca import snap_to_listed_contract
    assert snap_to_listed_contract(
        "VALE", "not-a-date", 15.0, "P", contracts=SAMPLE_CONTRACTS,
    ) is None


def test_invalid_option_type_returns_none():
    from options_chain_alpaca import snap_to_listed_contract
    assert snap_to_listed_contract(
        "VALE", "2026-06-19", 15.0, "X", contracts=SAMPLE_CONTRACTS,
    ) is None


# ---------------------------------------------------------------------------
# Integration with execute_multileg_strategy
# ---------------------------------------------------------------------------

def test_execute_multileg_snaps_strikes_before_submit():
    """End-to-end: caller provides AI-picked strikes that don't exist
    as listed contracts; execute_multileg_strategy should snap them
    to the closest listed contracts and submit with the corrected
    OCC symbols."""
    from unittest.mock import MagicMock, patch
    from options_multileg import (
        build_bear_put_spread, execute_multileg_strategy,
    )
    from datetime import date as _date

    # AI builds a spread at strikes that don't exist (15.50/14.50)
    # and an expiry off by a week.
    strategy = build_bear_put_spread(
        underlying="VALE",
        expiry=_date(2026, 6, 12),
        upper_strike=15.50,  # listed: 15.0 or 16.0
        lower_strike=14.50,  # listed: 14.0 or 15.0
        qty=2,
    )

    fake_api = MagicMock()
    fake_order = MagicMock()
    fake_order.id = "order-123"
    fake_api.submit_order.return_value = fake_order

    fake_ctx = MagicMock()
    fake_ctx.db_path = None

    with patch(
        "options_chain_alpaca.list_available_contracts",
        return_value=SAMPLE_CONTRACTS,
    ):
        result = execute_multileg_strategy(
            fake_api, strategy, fake_ctx, log=False, use_combo=True,
        )

    assert result["action"] == "MULTILEG_OPEN", result
    submitted = fake_api.submit_order.call_args.kwargs
    legs = submitted["legs"]
    # Both leg OCC symbols should match snapped (June 19) contracts
    leg_symbols = [leg["symbol"] for leg in legs]
    for sym in leg_symbols:
        assert sym in {c["symbol"] for c in SAMPLE_CONTRACTS}, (
            f"submitted symbol {sym} not in listed contracts"
        )


def test_execute_multileg_refuses_when_no_close_match():
    """If AI's chosen strikes are >5% off any listed strike, refuse the
    whole strategy — better than half-baked partial fill."""
    from unittest.mock import MagicMock, patch
    from options_multileg import (
        build_bear_put_spread, execute_multileg_strategy,
    )
    from datetime import date as _date

    # Strikes way out of range — listed only has 14/15/16
    strategy = build_bear_put_spread(
        underlying="VALE",
        expiry=_date(2026, 6, 12),
        upper_strike=200.0,
        lower_strike=190.0,
        qty=1,
    )

    fake_api = MagicMock()
    fake_ctx = MagicMock()
    fake_ctx.db_path = None

    with patch(
        "options_chain_alpaca.list_available_contracts",
        return_value=SAMPLE_CONTRACTS,
    ):
        result = execute_multileg_strategy(
            fake_api, strategy, fake_ctx, log=False, use_combo=True,
        )

    assert result["action"] == "ERROR"
    assert "No listed Alpaca contract" in result["reason"]
    fake_api.submit_order.assert_not_called()


def test_execute_multileg_falls_through_when_chain_unavailable():
    """If the contracts API is down, snap path returns []; we should
    submit the strategy as-is (graceful degradation, same as before
    this feature existed)."""
    from unittest.mock import MagicMock, patch
    from options_multileg import (
        build_bear_put_spread, execute_multileg_strategy,
    )
    from datetime import date as _date

    strategy = build_bear_put_spread(
        underlying="VALE",
        expiry=_date(2026, 6, 12),
        upper_strike=15.0,
        lower_strike=14.0,
        qty=1,
    )

    fake_api = MagicMock()
    fake_order = MagicMock()
    fake_order.id = "order-456"
    fake_api.submit_order.return_value = fake_order

    fake_ctx = MagicMock()
    fake_ctx.db_path = None

    # Contracts API returns empty (failure)
    with patch(
        "options_chain_alpaca.list_available_contracts",
        return_value=[],
    ):
        result = execute_multileg_strategy(
            fake_api, strategy, fake_ctx, log=False, use_combo=True,
        )

    # Submitted — let Alpaca reject if needed
    assert result["action"] == "MULTILEG_OPEN"
    fake_api.submit_order.assert_called_once()
