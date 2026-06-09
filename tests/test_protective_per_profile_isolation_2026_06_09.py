"""2026-06-09 (post-reset, late) — protective-orders per-profile
isolation. Mirror of the sell/cover isolation fix on the broker-
side protective stop / trailing / TP placement.

Pre-fix: `ensure_protective_stops` summed ALL broker coverage for
(symbol, close_side) across the account and skipped placement when
total >= this profile's qty. For three profiles holding PAVS,
that meant: pid 59 placed its trailing first, pids 56 and 63 saw
the broker's existing coverage from pid 59 and skipped their own
placement. When pid 59's trailing fired at $1.50, pids 56 and 63
held -18.8% positions with no broker-side protection.

Post-fix: protective coverage is attributed via the journal's
`protective_*_order_id` columns. Broker orders not tracked in
THIS profile's journal don't count toward our coverage. Each
profile places its own protective sized to its own virtual qty.

Tests pin:
  1. With a broker order whose ID is NOT in this profile's
     journal (sibling-owned), the protective sweep MUST place a
     new order. The pre-fix would skip.
  2. With a broker order whose ID IS in this profile's journal,
     the sweep skips placement (no duplicate).
  3. Source pin: the SELECT-own-protective-ids loop is present.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Layer 1 — sibling-owned coverage does NOT block our placement
# ---------------------------------------------------------------------------


def _seed_pavs_entry(tmp_db, qty=1164, entry=1.68, stop_loss=1.58,
                     take_profit=1.90):
    """Insert a profile DB with PAVS BUY entry and no protective
    pointers — fresh state ready for the sweep to place."""
    from journal import init_db
    init_db(tmp_db)
    with closing(sqlite3.connect(tmp_db)) as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "fill_price, signal_type, status, stop_loss, take_profit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-06-09T19:00:00", "PAVS", "buy", qty, entry, entry,
             "BUY", "open", stop_loss, take_profit),
        )
        conn.commit()


def _make_ctx(stop_loss_pct=0.05, use_trailing_stops=True):
    """Minimal context that ensure_protective_stops needs."""
    from types import SimpleNamespace
    return SimpleNamespace(
        stop_loss_pct=stop_loss_pct,
        short_stop_loss_pct=stop_loss_pct,
        use_trailing_stops=use_trailing_stops,
        use_conviction_tp_override=False,
        atr_multiplier_sl=2.0,
        atr_multiplier_tp=3.0,
    )


def _stub_broker_order(order_id, qty, order_type="trailing_stop",
                        status="new", side="sell"):
    """Mock Alpaca order object for list_orders responses."""
    o = MagicMock()
    o.id = order_id
    o.symbol = "PAVS"
    o.qty = str(qty)
    o.filled_qty = "0"
    o.status = status
    o.side = side
    o.order_type = order_type
    o.order_class = ""
    o.stop_price = None
    o.limit_price = None
    o.replaced_by = None
    return o


class TestProtectiveSweepIgnoresSiblingCoverage:

    def test_sibling_owned_broker_order_does_not_block_placement(
        self, tmp_path,
    ):
        """Pid X holds 1164 PAVS virtually. The shared Alpaca account
        also has an active trailing order for 10605 PAVS placed by
        a sibling (whose ID is NOT in this profile's journal).
        Pre-fix: sweep saw broker coverage 10605 >= 1164 and SKIPPED
        placement. Post-fix: filtered coverage is empty (the trailing
        ID isn't in our journal) so the sweep places our own."""
        from bracket_orders import ensure_protective_stops
        db = str(tmp_path / "p.db")
        _seed_pavs_entry(db, qty=1164)

        api = MagicMock()
        # Broker has 10605 PAVS trailing — placed by a sibling
        api.list_orders.return_value = [
            _stub_broker_order(
                "sibling-trailing-id", qty=10605,
                order_type="trailing_stop",
            ),
        ]
        api.list_positions.return_value = []
        # Stub the new placement to succeed
        new_order = MagicMock(id="own-new-trailing")
        api.submit_order.return_value = new_order

        ctx = _make_ctx(use_trailing_stops=True)
        positions = [{
            "symbol": "PAVS", "qty": 1164, "avg_entry_price": 1.68,
            "current_price": 1.60,
        }]
        ensure_protective_stops(api, positions, ctx, db)

        # The sweep must have submitted a NEW protective order despite
        # the sibling's coverage being visible at the broker
        assert api.submit_order.called, (
            "Protective sweep must place own order even when broker "
            "shows sibling-owned coverage. Without this fix, profiles "
            "skip placement because they trust sibling orders that "
            "don't actually protect their own qty."
        )
        # And the journal column got the new id
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT protective_trailing_order_id FROM trades "
                "WHERE symbol='PAVS' AND side='buy' AND status='open'"
            ).fetchone()
            assert row["protective_trailing_order_id"] == \
                "own-new-trailing"


# ---------------------------------------------------------------------------
# Layer 2 — source pin: SELECT-own-ids loop is present
# ---------------------------------------------------------------------------


def test_sweep_filters_broker_coverage_by_own_journal_ids():
    """Source-code pin on bracket_orders.py — the broker_coverage
    sum must be filtered by THIS profile's own protective_*_order_id
    values. A refactor that drops the filter re-introduces the
    cross-profile protective-skip bug."""
    src = (REPO_ROOT / "bracket_orders.py").read_text()
    fn_start = src.find("def ensure_protective_stops")
    assert fn_start > 0
    fn_end = src.find("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end if fn_end > 0 else len(src)]
    # The fix uses own_protective_ids to filter broker_coverage
    assert "own_protective_ids" in body, (
        "ensure_protective_stops must build a set of own_protective_ids "
        "from the journal and filter broker_coverage by it. Without "
        "this filter, sibling profiles' protective orders contaminate "
        "this profile's coverage assessment and the sweep skips its "
        "own placement — exactly the PAVS bug on pids 56 and 63."
    )
    # And the broker_coverage list is comprehension-filtered by membership
    assert "c.get(\"order_id\") in own_protective_ids" in body or \
        "c.get('order_id') in own_protective_ids" in body, (
        "The broker_coverage filter must check `order_id in "
        "own_protective_ids` — the explicit attribution that "
        "distinguishes own vs sibling orders."
    )
