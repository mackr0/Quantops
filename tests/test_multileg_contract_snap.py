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


def test_duplicate_position_guard_blocks_re_open():
    """Caught 2026-05-06: profile_10 fired the same ARCC bull_call_spread
    every cycle for 4 hours. Long leg filled, short leg didn't, strategy
    never noticed it had an open position. Resulted in 13 phantom long
    calls accumulating at the broker, no offsetting shorts.

    Fix: before submitting, check journal for ANY open row referencing
    the snapped OCC symbols. If found, refuse with action='SKIP'."""
    import sqlite3
    import tempfile
    from unittest.mock import MagicMock, patch
    from options_multileg import (
        build_bull_call_spread, execute_multileg_strategy,
    )
    from datetime import date as _date

    # Build a fake journal that already has an open ARCC long leg
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT DEFAULT 'open', pnl REAL, fill_price REAL,
            occ_symbol TEXT, option_strategy TEXT, expiry TEXT, strike REAL
        )
    """)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, occ_symbol, option_strategy, status) "
        "VALUES ('ARCC', 'buy', 1, 'ARCC260618C00020000', 'bull_call_spread', 'open')",
    )
    conn.commit()
    conn.close()

    strategy = build_bull_call_spread(
        underlying="ARCC",
        expiry=_date(2026, 6, 18),
        lower_strike=20.0,
        upper_strike=21.0,
        qty=1,
    )

    fake_api = MagicMock()
    fake_ctx = MagicMock()
    fake_ctx.db_path = f.name

    contracts = [
        {"symbol": "ARCC260618C00020000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 20.0},
        {"symbol": "ARCC260618C00021000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 21.0},
    ]
    with patch(
        "options_chain_alpaca.list_available_contracts",
        return_value=contracts,
    ):
        result = execute_multileg_strategy(
            fake_api, strategy, fake_ctx, log=False, use_combo=True,
        )

    assert result["action"] == "SKIP", result
    assert "Duplicate-position guard" in result["reason"]
    fake_api.submit_order.assert_not_called()


def test_duplicate_guard_allows_when_no_open_position():
    """Sanity: when journal has no open row for the OCC symbols, the
    guard doesn't block — strategy submits normally."""
    import sqlite3
    import tempfile
    from unittest.mock import MagicMock, patch
    from options_multileg import (
        build_bull_call_spread, execute_multileg_strategy,
    )
    from datetime import date as _date

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT DEFAULT 'open', pnl REAL, fill_price REAL,
            occ_symbol TEXT, option_strategy TEXT, expiry TEXT, strike REAL
        )
    """)
    conn.commit()
    conn.close()

    strategy = build_bull_call_spread(
        underlying="ARCC",
        expiry=_date(2026, 6, 18),
        lower_strike=20.0,
        upper_strike=21.0,
        qty=1,
    )

    fake_api = MagicMock()
    fake_order = MagicMock()
    fake_order.id = "test-order"
    fake_api.submit_order.return_value = fake_order
    fake_ctx = MagicMock()
    fake_ctx.db_path = f.name

    contracts = [
        {"symbol": "ARCC260618C00020000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 20.0},
        {"symbol": "ARCC260618C00021000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 21.0},
    ]
    with patch(
        "options_chain_alpaca.list_available_contracts",
        return_value=contracts,
    ):
        result = execute_multileg_strategy(
            fake_api, strategy, fake_ctx, log=False, use_combo=True,
        )

    assert result["action"] == "MULTILEG_OPEN", result
    fake_api.submit_order.assert_called_once()


def test_multileg_log_captures_fill_price():
    """Caught 2026-05-06: WMT/MSFT multileg legs displayed as $-- on
    dashboard because log_trade was called without `price`. Now the
    log path queries each leg order's filled_avg_price after submit
    and stores it as both `price` and `fill_price`."""
    import sqlite3
    import tempfile
    from unittest.mock import MagicMock, patch
    from options_multileg import (
        build_bull_call_spread, execute_multileg_strategy,
    )
    from datetime import date as _date

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            ai_reasoning TEXT, ai_confidence REAL,
            stop_loss REAL, take_profit REAL,
            status TEXT DEFAULT 'open', pnl REAL,
            decision_price REAL, fill_price REAL, slippage_pct REAL,
            occ_symbol TEXT, option_strategy TEXT, expiry TEXT, strike REAL,
            predicted_slippage_bps REAL, adv_at_decision REAL
        )
    """)
    conn.commit()
    conn.close()

    strategy = build_bull_call_spread(
        underlying="ARCC",
        expiry=_date(2026, 6, 18),
        lower_strike=20.0,
        upper_strike=21.0,
        qty=1,
    )

    fake_api = MagicMock()
    # Combo order returns one id, but each leg is queried separately
    combo_order = MagicMock()
    combo_order.id = "combo-id"
    fake_api.submit_order.return_value = combo_order
    # When leg orders are queried, return filled_avg_price
    leg_order_responses = {
        "combo-id": MagicMock(filled_avg_price=0.45),
    }
    fake_api.get_order.side_effect = lambda oid: leg_order_responses.get(
        oid, MagicMock(filled_avg_price=0.45),
    )

    fake_ctx = MagicMock()
    fake_ctx.db_path = f.name

    contracts = [
        {"symbol": "ARCC260618C00020000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 20.0},
        {"symbol": "ARCC260618C00021000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 21.0},
    ]
    with patch(
        "options_chain_alpaca.list_available_contracts",
        return_value=contracts,
    ):
        result = execute_multileg_strategy(
            fake_api, strategy, fake_ctx, log=True, use_combo=True,
        )

    assert result["action"] == "MULTILEG_OPEN", result
    # Verify journal rows have non-NULL price + fill_price
    conn = sqlite3.connect(f.name)
    rows = conn.execute(
        "SELECT side, qty, price, fill_price FROM trades"
    ).fetchall()
    conn.close()
    assert len(rows) == 2  # both legs logged
    for side, qty, price, fill_price in rows:
        assert price is not None and price > 0, (
            f"leg {side} qty={qty} has NULL/zero price"
        )
        assert fill_price is not None and fill_price > 0


def test_friendly_time_handles_nanosecond_precision():
    """Broker timestamps with 9-digit (nanosecond) precision broke
    Python's strptime %f (max 6 digits). Caught 2026-05-06: a
    backfilled SELL row showed as raw ISO '2026-05-06T19:59' on the
    dashboard instead of 'May 6, 3:59 PM ET'."""
    from display_names import friendly_time
    nanosecond_iso = "2026-05-06T19:59:07.765154638+00:00"
    result = friendly_time(nanosecond_iso)
    assert "May" in result and "ET" in result, (
        f"expected 'May ... ET', got {result!r}"
    )


def test_friendly_time_still_handles_microsecond_precision():
    from display_names import friendly_time
    result = friendly_time("2026-05-06T19:59:07.765154+00:00")
    assert "May" in result and "ET" in result


def test_friendly_time_still_handles_no_subsecond():
    from display_names import friendly_time
    result = friendly_time("2026-05-06T19:59:07")
    assert "May" in result and "ET" in result


def test_sequential_legs_pass_position_intent_open():
    """Caught 2026-05-07: ARCC short legs were async-canceled by
    Alpaca because the sequential fallback omitted position_intent.
    Combo path included it via _alpaca_leg_dict; sequential didn't.
    Every short leg must submit with sell_to_open; every long with
    buy_to_open. Without this Alpaca rejects naked-short option
    opens (the ARCC root cause)."""
    from unittest.mock import MagicMock, patch
    from options_multileg import (
        build_bull_call_spread, execute_multileg_strategy,
    )
    from datetime import date as _date

    strategy = build_bull_call_spread(
        underlying="ARCC",
        expiry=_date(2026, 6, 18),
        lower_strike=20.0,
        upper_strike=21.0,
        qty=1,
    )

    fake_api = MagicMock()
    fake_order = MagicMock()
    fake_order.id = "leg-id"
    fake_api.submit_order.return_value = fake_order

    fake_ctx = MagicMock()
    fake_ctx.db_path = None

    contracts = [
        {"symbol": "ARCC260618C00020000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 20.0},
        {"symbol": "ARCC260618C00021000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 21.0},
    ]
    with patch(
        "options_chain_alpaca.list_available_contracts",
        return_value=contracts,
    ):
        # Force sequential by passing use_combo=False
        result = execute_multileg_strategy(
            fake_api, strategy, fake_ctx, log=False, use_combo=False,
        )

    assert result["action"] == "MULTILEG_OPEN", result
    # Two submit_order calls; each must have position_intent.
    assert fake_api.submit_order.call_count == 2
    for call in fake_api.submit_order.call_args_list:
        kwargs = call.kwargs
        side = kwargs["side"]
        assert "position_intent" in kwargs, (
            f"Sequential leg submit ({side}) missing position_intent — "
            "this is the ARCC root cause."
        )
        # buy → buy_to_open, sell → sell_to_open
        expected = "buy_to_open" if side == "buy" else "sell_to_open"
        assert kwargs["position_intent"] == expected, (
            f"Wrong intent for {side}: got {kwargs['position_intent']}, "
            f"expected {expected}"
        )


def test_sequential_rollback_uses_close_intent():
    """When leg N fails after legs 1..N-1 submitted, rollback must
    submit reverse-side orders WITH close intent (not open). A
    buy_to_open is unwound by sell_to_close; sell_to_open by
    buy_to_close. Without correct intent the rollback would be
    treated as a NEW position open, doubling exposure."""
    from unittest.mock import MagicMock, patch
    from options_multileg import (
        build_bull_call_spread, execute_multileg_strategy,
    )
    from datetime import date as _date

    strategy = build_bull_call_spread(
        underlying="ARCC",
        expiry=_date(2026, 6, 18),
        lower_strike=20.0,
        upper_strike=21.0,
        qty=1,
    )

    fake_api = MagicMock()
    # Leg 0 succeeds; leg 1 raises; both rollbacks succeed.
    submitted_orders = []
    rollback_calls = []

    def submit_side_effect(**kwargs):
        intent = kwargs.get("position_intent", "")
        side = kwargs["side"]
        if "to_open" in intent:
            # opening leg
            if len(submitted_orders) == 0:
                m = MagicMock()
                m.id = "leg-0"
                submitted_orders.append(m)
                return m
            else:
                raise RuntimeError("leg 1 simulated failure")
        else:
            # rollback (close intent)
            rollback_calls.append({"side": side, "intent": intent})
            m = MagicMock()
            m.id = f"rollback-{len(rollback_calls)}"
            return m

    fake_api.submit_order.side_effect = submit_side_effect

    fake_ctx = MagicMock()
    fake_ctx.db_path = None

    contracts = [
        {"symbol": "ARCC260618C00020000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 20.0},
        {"symbol": "ARCC260618C00021000", "expiration_date": "2026-06-18",
         "type": "call", "strike": 21.0},
    ]
    with patch(
        "options_chain_alpaca.list_available_contracts",
        return_value=contracts,
    ):
        result = execute_multileg_strategy(
            fake_api, strategy, fake_ctx, log=False, use_combo=False,
        )

    # Action errored, rollback fired for leg 0.
    assert result["action"] == "ERROR", result
    assert len(rollback_calls) == 1, rollback_calls
    # Leg 0 was a buy_to_open → rollback should be sell_to_close.
    assert rollback_calls[0]["side"] == "sell"
    assert rollback_calls[0]["intent"] == "sell_to_close"


def test_combo_legs_still_pass_open_intent():
    """Regression guard: the combo (atomic) path was already passing
    position_intent via _alpaca_leg_dict. Make sure refactoring the
    intent map into module-level constants didn't break that."""
    from options_multileg import _alpaca_leg_dict, OptionLeg

    long_leg = OptionLeg(
        occ_symbol="X260618C00020000", underlying="X",
        expiry="2026-06-18", strike=20.0, right="C",
        side="buy", qty=1,
    )
    short_leg = OptionLeg(
        occ_symbol="X260618C00021000", underlying="X",
        expiry="2026-06-18", strike=21.0, right="C",
        side="sell", qty=1,
    )
    assert _alpaca_leg_dict(long_leg)["position_intent"] == "buy_to_open"
    assert _alpaca_leg_dict(short_leg)["position_intent"] == "sell_to_open"
