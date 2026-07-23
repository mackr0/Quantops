"""Regression: the update_fills FIFO lot-close must count ONLY exits the
broker actually FILLED. A resting, still-unfilled protective/pending sell
must never consume an entry lot.

The incident (2026-07-23, acct-56 / p213): COST buy #296 (5 shares) filled
at the broker at 15:55:04. Its bracket parent created two protective
children — a stop (sell 5) and a take-profit (sell 5) — journaled as
`pending_protective` rows with NO fill_price (neither ever filled; the
broker later canceled both, unfilled). The FIFO lot-close in
`_task_update_fills` selected every non-canceled COST buy/sell row and
treated EVERY sell as a completed exit, so #296's OWN two resting children
(5 + 5 = 10 "sold" shares) FIFO-consumed the 5-share lot to zero and
flipped #296 to 'closed' minutes after it filled — while the broker still
held all 5 shares. One row produced both halt findings: acct-56 COST drift
+5 and a −4,601 decomposition gap (a closed buy's cash stays spent while
its position stops counting: 5 × ~$920).

Fix: `_fifo_lots_fully_consumed` — a consumer only consumes when it carries
a `fill_price` (the filled-exit discriminator; resting/unfilled orders are
NULL). These tests exercise the REAL production helper, and a source check
binds production to it so the un-gated walk can't come back.
"""
from __future__ import annotations

import inspect
import re

from multi_scheduler import _fifo_lots_fully_consumed


# Row shape passed by production: (id, side, qty, fill_price)
def _lot(_id, side, qty, fill_price=None):
    return (_id, side, qty, fill_price)


class TestRestingSellNeverCloses:
    def test_incident_296_own_resting_children_do_not_close_it(self):
        """The exact incident shape: a filled BUY lot plus its two
        resting protective children (stop + TP, both fill_price NULL).
        The lot must stay OPEN — the children never sold anything."""
        rows = [
            _lot(296, "buy", 5.0, fill_price=920.27),   # filled entry
            _lot(297, "sell", 5.0, fill_price=None),    # resting STOP
            _lot(298, "sell", 5.0, fill_price=None),    # resting TP
        ]
        closed = _fifo_lots_fully_consumed(rows, opp_side="buy")
        assert closed == [], (
            "COST buy #296 was closed by its OWN resting bracket "
            "children — the 2026-07-23 anonymous-closer bug is back."
        )

    def test_single_resting_trailing_stop_does_not_close_lot(self):
        """A lone resting trailing-stop (fill_price NULL, e.g. #304 the
        naked-bracket re-arm placed) must not consume the lot it
        protects — the currently-armed recurrence the operator hit."""
        rows = [
            _lot(296, "buy", 5.0, fill_price=920.27),
            _lot(304, "sell", 5.0, fill_price=None),  # pending_protective
        ]
        assert _fifo_lots_fully_consumed(rows, opp_side="buy") == []

    def test_p213_full_history_replay_leaves_296_open(self):
        """Replay p213's real all-time COST buy/sell scope as the
        production SELECT returns it: five balanced buy/sell pairs, then
        #296 buy 5, then a resting trailing-stop sell 5 (#304). Under the
        buggy walk #304 tipped the cumulative sells past the buys and
        closed #296; with the fix #296 stays open."""
        rows = [
            _lot(106, "buy", 11.0, 912.48), _lot(111, "sell", 11.0, 910.57),
            _lot(116, "buy", 11.0, 912.83), _lot(125, "sell", 11.0, 909.00),
            _lot(149, "buy", 11.0, 915.22), _lot(152, "sell", 11.0, 913.73),
            _lot(153, "buy", 11.0, 914.24), _lot(205, "sell", 11.0, 925.30),
            _lot(285, "buy", 11.0, 919.96), _lot(295, "sell", 11.0, 921.51),
            _lot(296, "buy", 5.0, 920.27),
            _lot(304, "sell", 5.0, None),   # resting trailing-stop
        ]
        closed = _fifo_lots_fully_consumed(rows, opp_side="buy")
        assert 296 not in closed, (
            "#296 was consumed by the resting trailing-stop #304 — "
            "phantom-close recurrence."
        )
        # The genuinely-closed historical lots (consumed by real filled
        # exits) still close.
        assert set(closed) == {106, 116, 149, 153, 285}


class TestFilledExitsStillClose:
    def test_filled_sell_closes_its_lot(self):
        """Sanity: a real FILLED exit (fill_price set) still consumes
        and closes the lot — the normal path must be untouched."""
        rows = [
            _lot(283, "buy", 11.0, 923.95),
            _lot(286, "sell", 11.0, 919.87),   # filled STRONG_SELL
        ]
        assert _fifo_lots_fully_consumed(rows, opp_side="buy") == [283]

    def test_partial_filled_sell_leaves_lot_open(self):
        """A filled but partial sell leaves the lot open (FIFO qty)."""
        rows = [
            _lot(1, "buy", 322.0, 100.0),
            _lot(2, "buy", 16.0, 100.0),
            _lot(3, "sell", 16.0, 100.0),   # filled, only 16 of 338
        ]
        assert _fifo_lots_fully_consumed(rows, opp_side="buy") == []

    def test_short_cover_filled_closes_but_resting_does_not(self):
        """Mirror case: a resting protective COVER (buy-stop for a short,
        fill_price NULL) must not close the short; a filled cover does."""
        resting = [
            _lot(10, "short", 100.0, 50.0),
            _lot(11, "cover", 100.0, None),   # resting protective cover
        ]
        assert _fifo_lots_fully_consumed(resting, opp_side="short") == []
        filled = [
            _lot(10, "short", 100.0, 50.0),
            _lot(11, "cover", 100.0, 48.0),   # filled cover
        ]
        assert _fifo_lots_fully_consumed(filled, opp_side="short") == [10]

    def test_two_filled_sells_sum_to_close(self):
        """Two filled sells (one historical closed, one just confirmed)
        summing to the lot qty close it."""
        rows = [
            _lot(1, "buy", 100.0, 100.0),
            _lot(2, "sell", 60.0, 101.0),   # filled earlier
            _lot(3, "sell", 40.0, 102.0),   # filled now
        ]
        assert _fifo_lots_fully_consumed(rows, opp_side="buy") == [1]


class TestProductionBinding:
    def test_production_selects_fill_price_and_uses_helper(self):
        """The un-gated inline walk must not return: production must
        SELECT fill_price into the FIFO scope and route lot-closing
        through _fifo_lots_fully_consumed."""
        src = inspect.getsource(
            __import__("multi_scheduler")._task_update_fills)
        assert "_fifo_lots_fully_consumed(" in src, (
            "The FIFO lot-close no longer routes through the "
            "fill-price-gated helper."
        )
        # The FIFO scope SELECT must carry fill_price.
        assert re.search(
            r"SELECT\s+id,\s*side,\s*qty,\s*fill_price\s+FROM\s+trades",
            src,
        ), "The FIFO scope SELECT dropped fill_price — the resting-sell "\
           "discriminator is gone."
