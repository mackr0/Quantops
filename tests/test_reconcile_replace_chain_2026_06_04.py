"""Reconciler walks Alpaca's REPLACE chain forward to detect protective
fills under post-replacement order_ids (2026-06-04).

Background
----------
The structural fix (b77e4d5, 2026-05-21) made every protective placement
write a `pending_protective` trades row keyed by the order_id returned
from `api.submit_order`. The reconciler then UPDATEs that row on fill.

Gap discovered 2026-05-22 (pid21 NIO) and again 2026-06-03 across 10
profiles: Alpaca silently REPLACES trailing-stop orders as the trail
bumps. The parent's status becomes 'replaced' with a `replaced_by`
pointer to the successor. When the chain's terminal order finally fills,
the fill arrives under that terminal id — not the placement id the
journal recorded. The reconciler's pending_protective lookup (by
order_id PRIMARY KEY) missed entirely; the fill fell through to the
fuzzy fallback path which classifies it as `backfill_sell`, and the
safety net HALTED the profile.

Fix
---
`_detect_protective_fill` now walks `replaced_by` forward from the
journaled placement id to find the chain's terminal order. If the
terminal is `filled` with matching side/qty, it returns the fill data
but uses the ORIGINAL placement id as `detail["order_id"]` — so the
reconciler's existing pending_protective UPDATE path matches by primary
key. The terminal id is added to `used_sell_ids` so the cross-profile
fuzzy fallback can't double-attribute the same fill.

Tests pin
---------
  1. Replace chain (placement → terminal=filled) detects fill;
     detail.order_id == placement_id (the journaled one).
  2. End-to-end reconcile_with_ctx flips pending_protective row to
     closed under the journaled id; no synthesis halt.
  3. Chain ending in 'canceled' (not filled) returns no fill.
  4. Chain max depth respected — pathological loops don't hang.
  5. Broken chain ('replaced' status but no replaced_by) returns no
     fill (fail-safe; falls through to fuzzy fallback).
  6. Walk that crosses replacement adds terminal id to used_sell_ids
     (cross-profile dedup).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_journal_db(tmp_path):
    """Schema mirrors the prod trades table with protective_*_order_id
    columns the reconciler walks via _detect_protective_fill."""
    p = tmp_path / "journal.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            strategy TEXT,
            reason TEXT,
            status TEXT,
            pnl REAL,
            fill_price REAL,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT,
            occ_symbol TEXT
        )
    """)
    conn.commit()
    return str(p), conn


def _ctx(api, db_path, name="Test", profile_id=99, alpaca_account_id=1):
    ctx = SimpleNamespace()
    ctx.api = api
    ctx.get_alpaca_api = lambda: api
    ctx.db_path = db_path
    ctx.display_name = name
    ctx.profile_id = profile_id
    ctx.alpaca_account_id = alpaca_account_id
    return ctx


def _broker_order(oid, status, replaced_by=None, side="sell",
                   filled_qty=0, filled_avg_price=0, filled_at=None,
                   order_type="trailing_stop", qty=100, symbol="CRM"):
    o = MagicMock()
    o.id = oid
    o.side = side
    o.status = status
    o.qty = qty
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.filled_at = filled_at
    o.order_type = order_type
    o.symbol = symbol
    o.replaced_by = replaced_by
    return o


def _api_with_chain(chain):
    """Build a MagicMock API whose get_order returns from a dict keyed
    on order_id. chain is {oid: order_object}."""
    api = MagicMock()
    api.get_order.side_effect = lambda oid: chain[oid]
    return api


# ---------------------------------------------------------------------------
# 1. Replace chain detection (the core fix)
# ---------------------------------------------------------------------------

def test_replace_chain_terminal_filled_returns_journaled_oid():
    """Chain: placement → middle → terminal(filled).
    detail.order_id MUST be placement (the journaled id), so the
    reconciler's pending_protective UPDATE can match the existing row."""
    from reconcile_journal_to_broker import _detect_protective_fill
    fill_time = datetime(2026, 6, 3, 19, 30, tzinfo=timezone.utc)
    chain = {
        "placement-id": _broker_order(
            "placement-id", "replaced", replaced_by="middle-id"),
        "middle-id": _broker_order(
            "middle-id", "replaced", replaced_by="terminal-id"),
        "terminal-id": _broker_order(
            "terminal-id", "filled",
            filled_qty=127, filled_avg_price=172.84,
            filled_at=fill_time),
    }
    api = _api_with_chain(chain)
    row = {
        "id": 25, "symbol": "CRM", "side": "buy", "qty": 127.0,
        "timestamp": "2026-05-20T14:11:22",
        "protective_stop_order_id": None,
        "protective_tp_order_id": None,
        "protective_trailing_order_id": "placement-id",
    }
    used = set()
    kind, detail = _detect_protective_fill(api, row, used)
    assert kind == "backfill_full", (
        "Replace chain ending at filled terminal must classify as full "
        "backfill — without this, the fuzzy fallback runs and the fill "
        "is recorded under the terminal id with no matching pending row."
    )
    assert detail["order_id"] == "placement-id", (
        "Detail MUST carry the journaled placement id, not the terminal "
        "id. The reconciler's pending_protective UPDATE path matches "
        "by order_id PRIMARY KEY; if we return terminal-id, the UPDATE "
        "misses and the safety net halts the profile."
    )
    assert detail["filled_avg_price"] == 172.84
    assert detail["filled_qty"] == 127
    # Terminal id MUST be added to used set so a sibling profile's
    # fuzzy fallback can't double-attribute the same broker fill.
    assert "terminal-id" in used


# ---------------------------------------------------------------------------
# 2. End-to-end: pending_protective UPDATE under journaled id
# ---------------------------------------------------------------------------

def test_reconcile_closes_pending_protective_via_replace_chain(tmp_path):
    """The pid15 CRM scenario: pending_protective row exists with
    placement order_id, broker replaced it twice, terminal fills.
    Reconciler should flip pending row to closed, close entry, NO halt."""
    from reconcile_journal_to_broker import reconcile_with_ctx
    db, conn = _make_journal_db(tmp_path)
    # Entry BUY with protective_trailing_order_id pointing at placement.
    conn.execute(
        "INSERT INTO trades (id, timestamp, symbol, side, qty, price, "
        "order_id, status, protective_trailing_order_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (25, "2026-05-20T14:11:22", "CRM", "buy", 127.0, 175.00,
         "entry-buy-oid", "open", "placement-id"),
    )
    # Pre-journaled pending_protective row, keyed by placement id.
    conn.execute(
        "INSERT INTO trades (id, timestamp, symbol, side, qty, price, "
        "order_id, status, signal_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (83, "2026-06-03T13:57:06", "CRM", "sell", 127.0, None,
         "placement-id", "pending_protective", "PROTECTIVE_TRAILING"),
    )
    conn.commit()
    conn.close()

    fill_time = datetime(2026, 6, 3, 19, 30, tzinfo=timezone.utc)
    chain = {
        "entry-buy-oid": _broker_order(
            "entry-buy-oid", "filled", side="buy",
            filled_qty=127, qty=127),
        "placement-id": _broker_order(
            "placement-id", "replaced", replaced_by="middle-id"),
        "middle-id": _broker_order(
            "middle-id", "replaced", replaced_by="terminal-id"),
        "terminal-id": _broker_order(
            "terminal-id", "filled",
            filled_qty=127, filled_avg_price=172.84,
            filled_at=fill_time),
    }
    api = _api_with_chain(chain)
    api.list_positions.return_value = []  # broker no longer holds CRM
    api.list_orders.return_value = []     # no open orders for fuzzy fallback

    ctx = _ctx(api, db)
    res = reconcile_with_ctx(ctx, apply_changes=True)
    # The synthesis halt counter MUST stay 0 — the pending row UPDATE
    # path absorbed the fill, no synthesis needed.
    assert res.get("halted_synthesis_count", 0) == 0, (
        f"Expected no halt, got halted_synthesis_count="
        f"{res.get('halted_synthesis_count')}. The replace-chain "
        f"walk should have detected the fill under the journaled id "
        f"and the pending_protective UPDATE should have closed it."
    )
    # Pending row #83 should now be closed at the fill price.
    conn = sqlite3.connect(db)
    pending_after = conn.execute(
        "SELECT status, price, fill_price FROM trades WHERE id=83"
    ).fetchone()
    assert pending_after[0] == "closed", (
        "Pending_protective row must flip to closed once the chain "
        "walk identifies the fill."
    )
    assert pending_after[1] == 172.84
    # Entry BUY #25 should be closed too.
    entry_after = conn.execute(
        "SELECT status FROM trades WHERE id=25"
    ).fetchone()[0]
    assert entry_after == "closed"
    conn.close()


# ---------------------------------------------------------------------------
# 3. Chain ending non-filled → no fill detected
# ---------------------------------------------------------------------------

def test_replace_chain_terminal_canceled_returns_no_fill():
    """If the chain ends at canceled (not filled), no fill is reported.
    Falls through to fuzzy fallback at the caller."""
    from reconcile_journal_to_broker import _detect_protective_fill
    chain = {
        "placement-id": _broker_order(
            "placement-id", "replaced", replaced_by="terminal-id"),
        "terminal-id": _broker_order("terminal-id", "canceled"),
    }
    api = _api_with_chain(chain)
    row = {
        "id": 25, "symbol": "CRM", "side": "buy", "qty": 100.0,
        "timestamp": "2026-05-20T14:11:22",
        "protective_stop_order_id": None,
        "protective_tp_order_id": None,
        "protective_trailing_order_id": "placement-id",
    }
    api.list_orders.return_value = []  # block fuzzy fallback
    kind, _ = _detect_protective_fill(api, row, set())
    assert kind is None


# ---------------------------------------------------------------------------
# 4. Max depth — pathological chain doesn't hang
# ---------------------------------------------------------------------------

def test_replace_chain_max_depth_returns_no_fill():
    """A chain longer than max_depth bails out safely.

    Bumped 2026-06-04 to track _REPLACE_CHAIN_MAX_DEPTH=50 — with the
    proactive sync sweep running every cycle, chain depth at fill
    time should be ~1. 50 is the generous safety margin; we test
    well past it (60) to ensure the bail-out still fires."""
    from reconcile_journal_to_broker import (
        _walk_replace_chain_forward, _REPLACE_CHAIN_MAX_DEPTH,
    )
    n = _REPLACE_CHAIN_MAX_DEPTH + 10  # well past the bail-out
    chain = {f"oid-{i}": _broker_order(
        f"oid-{i}", "replaced", replaced_by=f"oid-{i+1}")
        for i in range(n)}
    chain[f"oid-{n}"] = _broker_order(
        f"oid-{n}", "filled", filled_qty=100, filled_avg_price=10.0)
    api = _api_with_chain(chain)
    order, depth = _walk_replace_chain_forward(api, "oid-0")
    # Returns None (couldn't resolve within max_depth)
    assert order is None
    assert depth == _REPLACE_CHAIN_MAX_DEPTH


# ---------------------------------------------------------------------------
# 5. Broken chain (no replaced_by) returns no fill
# ---------------------------------------------------------------------------

def test_replace_chain_broken_returns_no_fill():
    """Order is in 'replaced' status but exposes no replaced_by —
    treat as unresolved (don't fabricate a fill)."""
    from reconcile_journal_to_broker import _walk_replace_chain_forward
    chain = {
        "placement-id": _broker_order(
            "placement-id", "replaced", replaced_by=None),
    }
    api = _api_with_chain(chain)
    order, depth = _walk_replace_chain_forward(api, "placement-id")
    assert order is None


# ---------------------------------------------------------------------------
# 6. No-replacement case unchanged (existing behavior preserved)
# ---------------------------------------------------------------------------

def test_already_filled_no_chain_walk_needed():
    """When the journaled id IS itself the filled order (no replace),
    the walk returns at depth=0 and detection proceeds normally."""
    from reconcile_journal_to_broker import _detect_protective_fill
    fill_time = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)
    chain = {
        "placement-id": _broker_order(
            "placement-id", "filled",
            filled_qty=50, filled_avg_price=99.95,
            filled_at=fill_time),
    }
    api = _api_with_chain(chain)
    row = {
        "id": 7, "symbol": "AAPL", "side": "buy", "qty": 50.0,
        "timestamp": "2026-05-20T14:00:00",
        "protective_stop_order_id": None,
        "protective_tp_order_id": None,
        "protective_trailing_order_id": "placement-id",
    }
    used = set()
    kind, detail = _detect_protective_fill(api, row, used)
    assert kind == "backfill_full"
    assert detail["order_id"] == "placement-id"
    # When depth==0, terminal_oid == stop_oid, so we should NOT add it
    # again (deduplication is implicit but verify no spurious adds).
    assert used == set()
