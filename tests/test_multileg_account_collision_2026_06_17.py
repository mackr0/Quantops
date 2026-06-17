"""2026-06-17 — account-level position-intent guard.

Alpaca enforces position_intent across the WHOLE account, not per
profile. On the shared conduit a sibling profile's leg can leave the
account net-opposite at a strike a new spread wants to OPEN — buy-to-open
where the account is net-short reads as buy-to-close and Alpaca rejects
(422 "position intent mismatch"). execute_multileg_strategy now checks
the broker's account-level option positions and SKIPs pre-submit with an
accurate reason, instead of submitting a doomed combo and blaming
"journal drift" (the books reconcile — it's the shared-conduit
constraint). It must NOT over-block a legitimate add-to-existing-position.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

EXPIRY = date(2099, 1, 16)


def _stub_order(order_id="ord-x"):
    o = MagicMock()
    o.id = order_id
    return o


def _spread():
    from options_multileg import build_bear_call_spread
    # sell the $15 call (short), buy the $17 call (long)
    return build_bear_call_spread("NOK", EXPIRY, 15, 17, qty=1)


def _pos(occ, qty):
    p = MagicMock()
    p.symbol = occ
    p.qty = str(qty)
    return p


def test_buy_to_open_into_account_short_is_skipped_pre_submit():
    from options_multileg import execute_multileg_strategy
    strategy = _spread()
    buy_leg = next(l for l in strategy.legs if l.side == "buy")
    # the account is net-SHORT the buy leg's contract (a sibling's leg)
    api = MagicMock()
    api.list_positions.return_value = [_pos(buy_leg.occ_symbol, -1)]
    submit_called = [False]

    def _submit(*a, **k):
        submit_called[0] = True
        return _stub_order()

    with patch("options_multileg._combo_submit_with_retry", side_effect=_submit), \
         patch("options_multileg._submit_alpaca_order_raw", side_effect=_submit), \
         patch("options_chain_alpaca.list_available_contracts", return_value=[]):
        result = execute_multileg_strategy(
            api, strategy, ctx=SimpleNamespace(db_path=None),
            log=False, use_combo=True)

    assert result["action"] == "SKIP"
    assert "collision" in result["reason"].lower(), result["reason"]
    assert "drift" not in result["reason"].lower() or "not" in result["reason"].lower()
    assert submit_called[0] is False, "must skip BEFORE any broker submit"


def test_sell_to_open_into_account_long_is_skipped():
    from options_multileg import execute_multileg_strategy
    strategy = _spread()
    sell_leg = next(l for l in strategy.legs if l.side == "sell")
    api = MagicMock()
    # account net-LONG the short leg's contract → sell-to-open collides
    api.list_positions.return_value = [_pos(sell_leg.occ_symbol, 2)]
    with patch("options_multileg._combo_submit_with_retry", side_effect=AssertionError("should not submit")), \
         patch("options_chain_alpaca.list_available_contracts", return_value=[]):
        result = execute_multileg_strategy(
            api, strategy, ctx=SimpleNamespace(db_path=None),
            log=False, use_combo=True)
    assert result["action"] == "SKIP" and "collision" in result["reason"].lower()


def test_add_to_existing_long_is_not_blocked():
    """buy-to-open at a strike the account is already net-LONG is a
    legitimate add — the collision guard must NOT block it."""
    from options_multileg import execute_multileg_strategy
    strategy = _spread()
    buy_leg = next(l for l in strategy.legs if l.side == "buy")
    api = MagicMock()
    api.list_positions.return_value = [_pos(buy_leg.occ_symbol, 1)]  # net LONG
    submit_called = [False]

    def _submit(*a, **k):
        submit_called[0] = True
        return _stub_order()

    with patch("options_multileg._combo_submit_with_retry", side_effect=_submit), \
         patch("options_chain_alpaca.list_available_contracts", return_value=[]):
        execute_multileg_strategy(
            api, strategy, ctx=SimpleNamespace(db_path=None),
            log=False, use_combo=True)

    assert submit_called[0] is True, (
        "buy-to-open into an existing LONG is a valid add and must not be "
        "blocked by the account-collision guard")


def test_guard_is_present_structurally():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "options_multileg.py").read_text()
    i = src.find("def execute_multileg_strategy")
    body = src[i:]
    assert "api.list_positions()" in body
    assert 'side == "buy" and net < -1e-9' in body
    assert 'side == "sell" and net > 1e-9' in body
