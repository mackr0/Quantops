"""Multileg sequential path: leg↔order↔price association must never
cross (2026-07-09).

CAUGHT LIVE by the aggregate cash-parity audit ($8,076.33 drift on
account 56): the sequential fallback sorts legs BUYS-FIRST before
submitting (the 2026-06-09 uncovered-short 403 fix), but the journal
writer walks strategy.legs (SHORTS-FIRST for credit spreads) and
paired order ids BY INDEX — so every sequential credit spread's legs
were journaled with the OTHER leg's order id and fill price. p215's
MRVL bull put spread was recorded as a $4,040 DEBIT while the broker
collected a $4,040 CREDIT; update_fills would then forever "true" each
row from the wrong order. Combo-path spreads (one shared parent id)
were immune, which is why only the sequential fallback shape drifted.

Pinned here:
  1. Sequential submission journals each leg with the order id AND
     fill price issued for THAT leg's contract, regardless of the
     buys-first submission reorder.
  2. The belt: a leg↔order association whose broker order is for a
     DIFFERENT contract is refused — the row is written unlinked
     (no order id, no price) and an error is logged, so nothing can
     backfill poisoned prices later.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from options_multileg import (
    build_bull_put_spread,
    execute_multileg_strategy,
    _log_strategy_legs,
)

EXPIRY = date(2026, 8, 14)
OCC_SHORT_PUT = "MRVL260814P00230000"   # sell (higher strike)
OCC_LONG_PUT = "MRVL260814P00210000"    # buy (lower strike)


def _mrvl_strategy():
    return build_bull_put_spread(
        underlying="MRVL", expiry=EXPIRY,
        lower_strike=210.0, upper_strike=230.0, qty=8,
    )


def _contracts():
    return [
        {"symbol": OCC_LONG_PUT, "expiration_date": "2026-08-14",
         "type": "put", "strike": 210.0},
        {"symbol": OCC_SHORT_PUT, "expiration_date": "2026-08-14",
         "type": "put", "strike": 230.0},
    ]


def test_sequential_credit_spread_journals_correct_leg_associations():
    # The exact live shape: builder emits SELL leg first; sequential
    # submission reorders BUY first; each contract gets its own order
    # id and its own fill price. Every journaled row must carry the
    # id+price issued for ITS contract.
    strategy = _mrvl_strategy()
    assert strategy.legs[0].side == "sell", (
        "test premise: credit-spread builders emit shorts first")

    order_ids = {OCC_SHORT_PUT: "id-SHORT-230", OCC_LONG_PUT: "id-LONG-210"}
    fills = {"id-SHORT-230": 19.15, "id-LONG-210": 14.10}
    orders = {
        oid: MagicMock(id=oid, symbol=occ, filled_avg_price=fills[oid])
        for occ, oid in order_ids.items()
    }
    submitted_order = []

    def fake_post(api, payload):
        submitted_order.append(payload["symbol"])
        return orders[order_ids[payload["symbol"]]]

    fake_api = MagicMock()
    fake_api.get_order = lambda oid: orders[oid]
    fake_ctx = MagicMock()
    fake_ctx.db_path = "unused.db"
    logged = []

    def fake_log_trade(**kw):
        logged.append(kw)
        return len(logged)

    with patch("options_chain_alpaca.list_available_contracts",
               return_value=_contracts()), \
         patch("options_multileg._submit_alpaca_order_raw", fake_post), \
         patch("journal.log_trade", fake_log_trade):
        result = execute_multileg_strategy(
            fake_api, strategy, fake_ctx, log=True, use_combo=False)

    assert result["action"] == "MULTILEG_OPEN", result
    # premise check: the submission really did reorder buys first
    assert submitted_order[0] == OCC_LONG_PUT, (
        "test premise: sequential path submits the covering long first")
    by_occ = {kw["occ_symbol"]: kw for kw in logged}
    assert by_occ[OCC_SHORT_PUT]["order_id"] == "id-SHORT-230", (
        "the SELL leg must carry ITS OWN order id — the crossed "
        "pairing recorded p215's credit spread as a debit")
    assert by_occ[OCC_SHORT_PUT]["fill_price"] == pytest.approx(19.15)
    assert by_occ[OCC_SHORT_PUT]["side"] == "sell"
    assert by_occ[OCC_LONG_PUT]["order_id"] == "id-LONG-210"
    assert by_occ[OCC_LONG_PUT]["fill_price"] == pytest.approx(14.10)
    assert by_occ[OCC_LONG_PUT]["side"] == "buy"
    # result surface stays aligned to strategy.legs order
    assert result["leg_order_ids"] == ["id-SHORT-230", "id-LONG-210"]


def test_crossed_association_is_refused_not_journaled(caplog):
    # The belt: even if a future refactor reintroduces misalignment,
    # a leg pointed at another contract's order must be written
    # UNLINKED (no id, no price) — never with the wrong fill —
    # because update_fills trues rows from their order id forever.
    strategy = _mrvl_strategy()
    # the builder pads the underlying inside the OCC symbol — derive
    # the exact leg symbols from the strategy itself
    occ_short = next(l.occ_symbol for l in strategy.legs if l.side == "sell")
    occ_long = next(l.occ_symbol for l in strategy.legs if l.side == "buy")
    wrong = {
        # deliberately crossed: SELL leg gets the LONG leg's order
        "id-LONG-210": MagicMock(id="id-LONG-210", symbol=occ_long,
                                 filled_avg_price=14.10),
        "id-SHORT-230": MagicMock(id="id-SHORT-230", symbol=occ_short,
                                  filled_avg_price=19.15),
    }
    fake_api = MagicMock()
    fake_api.get_order = lambda oid: wrong[oid]
    fake_ctx = MagicMock()
    fake_ctx.db_path = "unused.db"
    logged = []

    def fake_log_trade(**kw):
        logged.append(kw)
        return len(logged)

    import logging as _logging
    with patch("journal.log_trade", fake_log_trade), \
         caplog.at_level(_logging.ERROR):
        _log_strategy_legs(
            strategy, None, fake_ctx,
            leg_order_ids=["id-LONG-210", "id-SHORT-230"],  # crossed!
            api=fake_api,
        )

    by_occ = {kw["occ_symbol"]: kw for kw in logged}
    assert by_occ[occ_short]["order_id"] is None, (
        "crossed association must be refused, not journaled")
    assert by_occ[occ_short]["fill_price"] is None
    assert by_occ[occ_long]["order_id"] is None
    assert any("MISMATCH" in r.message for r in caplog.records), (
        "the refusal must be loud")
