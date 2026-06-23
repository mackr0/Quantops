"""2026-06-09 broker-side take-profit — REVERTED 2026-06-23.

The 2026-06-09 change made `ensure_protective_stops` place a GTC `limit`
take-profit at `row["take_profit"]` ALONGSIDE the trailing/static stop.
The intent was intra-cycle TP precision the polling path can miss.

Why it was reverted (prod 2026-06-22): Alpaca holds shares per open
sell-side order, so a `limit` TP + a `trailing_stop` for the SAME slice
reserved that slice TWICE. On the shared Alpaca account each profile then
consumed 2× its shares, and the next profile's protective stop could not
place — 51 "insufficient qty available" failures, each leaving a position
NAKED. A categorized broker+journal pull showed `position - sell_reserved
== broker available` on every symbol (no drift; purely the second
reservation). The downside stop is the safety control and must win the
single reservation; the AI's profit target reverts to the per-cycle
polling check in `check_stop_loss_take_profit`.

This file now pins the REVERT (the live behavior is pinned functionally
in test_protective_no_double_reservation_2026_06_23.py):
  - the sweep places NO broker-side take-profit (no `submit_protective_
    take_profit` call, no `type='limit'` order);
  - the sweep actively sunsets any lingering broker-side TP so its
    reservation is freed for the actual stop.

The `submit_protective_take_profit` PRIMITIVE is retained (it's a valid
order helper and still routes through `_submit_protective` for the
hard-to-borrow DAY-order retry); it is simply no longer called by the
protective sweep. `TestSubmitTPHelperUnchanged` below still covers it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Source-level pins (regression protection against silent removal)
# ---------------------------------------------------------------------------


class TestBrokerSideTPReverted:
    """2026-06-23 — the broker-side TP must NOT be placed by the sweep
    (it double-reserves the slice). These invert the original 06-09 pins."""

    def _sweep_body(self):
        src = (REPO_ROOT / "bracket_orders.py").read_text()
        fn_start = src.find("def ensure_protective_stops")
        assert fn_start > 0, "ensure_protective_stops missing"
        fn_end = src.find("\ndef ", fn_start + 1)
        return src[fn_start:fn_end if fn_end > 0 else len(src)]

    def test_sweep_does_not_call_submit_protective_take_profit(self):
        """The sweep must NOT place a broker-side take-profit — a second
        full-qty sell order reserves the slice twice and starves sibling
        profiles' stops into naked exposure (the 2026-06-22 incident)."""
        body = self._sweep_body()
        assert "submit_protective_take_profit(" not in body, (
            "ensure_protective_stops must not place a broker-side TP; "
            "the AI target is enforced by the polling check."
        )

    def test_sweep_builds_no_limit_sell_order(self):
        """No `type='limit'` sell order may be constructed in the sweep."""
        body = self._sweep_body()
        assert '"type": "limit"' not in body, (
            "no broker-side TP limit may be built in the protective sweep"
        )

    def test_sweep_sunsets_lingering_broker_tp(self):
        """Any TP left over from the reverted design is cancelled and the
        column cleared, freeing its reservation for the actual stop."""
        body = self._sweep_body()
        assert "protective_tp_order_id = NULL" in body, (
            "the sweep must sunset (cancel + clear) any lingering "
            "broker-side TP so its share reservation is freed"
        )

    def test_take_profit_column_still_read(self):
        """The entry-row SELECT still reads `take_profit` (the polling
        TP and other readers rely on the column being present); the
        sweep just no longer places a broker order from it."""
        body = self._sweep_body()
        assert "take_profit" in body


class TestSubmitTPHelperUnchanged:
    """The placement helper itself was already correct (defined at
    bracket_orders.py:272). These tests just confirm it's still
    callable and produces a limit order at the requested price."""

    def test_submit_protective_take_profit_signature(self):
        """The function takes (api, symbol, qty, side, limit_price,
        db_path?, entry_trade_id?). Refactor protection."""
        from bracket_orders import submit_protective_take_profit
        import inspect
        sig = inspect.signature(submit_protective_take_profit)
        params = list(sig.parameters.keys())
        for required in ("api", "symbol", "qty", "side", "limit_price"):
            assert required in params, (
                f"submit_protective_take_profit must accept '{required}' "
                f"keyword. Current signature: {params}"
            )

    def test_submit_protective_take_profit_uses_limit_type(self):
        """The function must use type='limit' (not stop) — TP fires
        only when price meets or beats the target, doesn't slip on
        gaps. Source pin so a future refactor doesn't break it.

        2026-06-22 — the broker submit was factored into the shared
        `_submit_protective` helper (so all three protective orders get
        the hard-to-borrow DAY-order retry). The TP body now builds a
        `"type": "limit"` kwargs dict and routes through the helper; the
        helper owns the GTC-first / DAY-fallback time-in-force logic."""
        src = (REPO_ROOT / "bracket_orders.py").read_text()
        fn_start = src.find("def submit_protective_take_profit")
        fn_end = src.find("\ndef ", fn_start + 1)
        body = src[fn_start:fn_end if fn_end > 0 else len(src)]
        assert '"type": "limit"' in body, (
            "submit_protective_take_profit must use type='limit' "
            "so it fills only at/better than the target."
        )
        assert "_submit_protective(" in body, (
            "submit_protective_take_profit must route through "
            "_submit_protective so it inherits GTC-first placement and "
            "the hard-to-borrow DAY-order retry."
        )
        # The shared helper owns the time-in-force contract: GTC first
        # (persists across cycles), DAY fallback only when the broker
        # refuses the GTC because the asset is hard-to-borrow.
        helper_start = src.find("def _submit_protective")
        helper_end = src.find("\ndef ", helper_start + 1)
        helper = src[helper_start:helper_end if helper_end > 0 else len(src)]
        assert 'time_in_force="gtc"' in helper, (
            "GTC so the protective order persists across cycles until "
            "filled or canceled by the broker-truth sweep."
        )
        assert 'time_in_force="day"' in helper, (
            "DAY fallback so a hard-to-borrow name (which rejects GTC) "
            "still gets a working protective order instead of riding naked."
        )
