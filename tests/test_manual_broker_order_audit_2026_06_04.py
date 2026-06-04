"""Manual broker-side order detector — D in the 2026-06-04
orphan-prevention list.

Detects orders placed at the broker outside this system (Alpaca.com
UI clicks, external scripts using the API directly). Those bypass
every atomic-placement contract this codebase enforces and would
otherwise only surface when their fill arrives and the reconciler
classifies it as phantom-source orphan.

Per-account diff:
  live_broker_order_ids on account A
    MINUS
  union(journaled_order_ids for every profile routing to A)
= manual orders

Tests pin:
  1. Order placed by the system (id is in some profile's journal):
     NOT flagged.
  2. Order at broker with no journal row (any profile on that
     account): flagged as manual.
  3. Multi-profile-per-account: an order journaled by profile X
     but not Y is still "known" — only the union must match.
  4. protective_*_order_id columns also count as journaled
     (entry-row pointers reference live broker protectives).
  5. Filled / canceled broker orders are excluded — historical
     and the reconciler handles those.
  6. API errors don't crash; account returns 0 known + 0 manual.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_journal(tmp_path, name, order_ids=(), pointer_ids=()):
    """Build a profile journal DB with the given order_ids in `trades.order_id`
    and pointer_ids in `protective_trailing_order_id`. Returns the db path."""
    db = tmp_path / f"{name}.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            status TEXT,
            protective_stop_order_id TEXT,
            protective_tp_order_id TEXT,
            protective_trailing_order_id TEXT
        )
    """)
    for oid in order_ids:
        conn.execute(
            "INSERT INTO trades (order_id, status) VALUES (?, 'open')",
            (oid,),
        )
    for pid in pointer_ids:
        conn.execute(
            "INSERT INTO trades (status, "
            "protective_trailing_order_id) VALUES ('open', ?)",
            (pid,),
        )
    conn.commit()
    conn.close()
    return str(db)


def _broker_order(oid, status="new", symbol="X", side="sell", qty=100,
                   order_type="trailing_stop"):
    o = MagicMock()
    o.id = oid
    o.status = status
    o.symbol = symbol
    o.side = side
    o.qty = qty
    o.order_type = order_type
    o.created_at = "2026-06-04T13:00:00Z"
    return o


def _patch_build_ctx(profiles, api):
    """Patch build_user_context_from_profile to return SimpleNamespaces
    keyed on the profiles dict {pid: {db_path, alpaca_account_id}}."""
    def fake_build(pid):
        spec = profiles[pid]
        ns = SimpleNamespace()
        ns.profile_id = pid
        ns.db_path = spec["db_path"]
        ns.alpaca_account_id = spec["alpaca_account_id"]
        ns.get_alpaca_api = lambda: api  # all profiles share the same api
        return ns
    return patch("models.build_user_context_from_profile",
                  side_effect=fake_build)


# ---------------------------------------------------------------------------
# 1. System-placed order (in journal) is NOT flagged
# ---------------------------------------------------------------------------

def test_system_placed_order_not_flagged(tmp_path):
    """A broker order whose id IS in a profile's journal is recognized
    as system-placed — not a manual order."""
    from aggregate_audit import audit_manual_broker_orders
    db = _make_journal(tmp_path, "p1", order_ids=["sys-oid-1"])
    api = MagicMock()
    api.list_orders.return_value = [_broker_order("sys-oid-1", "new")]
    profiles = {1: {"db_path": db, "alpaca_account_id": 13}}
    with _patch_build_ctx(profiles, api):
        out = audit_manual_broker_orders(profile_ids=[1])
    assert out["manual"] == [], (
        "Order matching a journaled id must not be flagged as manual."
    )
    assert out["accounts"][13]["journal_known"] == 1


# ---------------------------------------------------------------------------
# 2. Manual broker-side order IS flagged
# ---------------------------------------------------------------------------

def test_manual_order_flagged(tmp_path):
    """An order at the broker with no journal row anywhere on the
    account is flagged as manual."""
    from aggregate_audit import audit_manual_broker_orders
    db = _make_journal(tmp_path, "p1", order_ids=["sys-oid"])
    api = MagicMock()
    api.list_orders.return_value = [
        _broker_order("sys-oid", "new", symbol="AAPL"),
        _broker_order("manual-oid", "new", symbol="TSLA",
                       qty=10, order_type="market"),
    ]
    profiles = {1: {"db_path": db, "alpaca_account_id": 13}}
    with _patch_build_ctx(profiles, api):
        out = audit_manual_broker_orders(profile_ids=[1])
    assert len(out["manual"]) == 1
    m = out["manual"][0]
    assert m["order_id"] == "manual-oid"
    assert m["symbol"] == "TSLA"
    assert m["account"] == 13
    assert m["type"] == "market"


# ---------------------------------------------------------------------------
# 3. Multi-profile: union counts (only one profile needs to have the id)
# ---------------------------------------------------------------------------

def test_multi_profile_union_recognizes_journaled_order(tmp_path):
    """Two profiles route to the same Alpaca account. Profile 2
    journaled the order; profile 1 didn't. The order is still
    known — only the union over the account must match."""
    from aggregate_audit import audit_manual_broker_orders
    db1 = _make_journal(tmp_path, "p1", order_ids=[])
    db2 = _make_journal(tmp_path, "p2", order_ids=["shared-oid"])
    api = MagicMock()
    api.list_orders.return_value = [_broker_order("shared-oid", "new")]
    profiles = {
        1: {"db_path": db1, "alpaca_account_id": 13},
        2: {"db_path": db2, "alpaca_account_id": 13},
    }
    with _patch_build_ctx(profiles, api):
        out = audit_manual_broker_orders(profile_ids=[1, 2])
    assert out["manual"] == []


# ---------------------------------------------------------------------------
# 4. protective_*_order_id columns also count as journaled
# ---------------------------------------------------------------------------

def test_protective_pointer_columns_count_as_journaled(tmp_path):
    """A broker order whose id is on a journal entry's
    protective_trailing_order_id pointer (not just trades.order_id)
    IS journaled — must not be flagged."""
    from aggregate_audit import audit_manual_broker_orders
    db = _make_journal(tmp_path, "p1", pointer_ids=["pointer-oid"])
    api = MagicMock()
    api.list_orders.return_value = [_broker_order("pointer-oid", "new")]
    profiles = {1: {"db_path": db, "alpaca_account_id": 13}}
    with _patch_build_ctx(profiles, api):
        out = audit_manual_broker_orders(profile_ids=[1])
    assert out["manual"] == []


# ---------------------------------------------------------------------------
# 5. Historical (filled / canceled) orders are not in the active set
# ---------------------------------------------------------------------------

def test_filled_orders_excluded_from_audit(tmp_path):
    """list_orders is called with status='open' by the audit — the
    broker won't return filled/canceled orders. Even if it did,
    they'd be filtered by _BROKER_ACTIVE_STATUSES. Verify directly
    by passing a filled order in the mock response."""
    from aggregate_audit import audit_manual_broker_orders
    db = _make_journal(tmp_path, "p1")
    api = MagicMock()
    # The mock ignores the status filter; we verify our code excludes
    # filled orders regardless of what list_orders returns.
    api.list_orders.return_value = [
        _broker_order("filled-oid", "filled"),
        _broker_order("canceled-oid", "canceled"),
        _broker_order("expired-oid", "expired"),
    ]
    profiles = {1: {"db_path": db, "alpaca_account_id": 13}}
    with _patch_build_ctx(profiles, api):
        out = audit_manual_broker_orders(profile_ids=[1])
    assert out["manual"] == [], (
        "Historical (filled/canceled/expired) orders must be excluded "
        "even if list_orders returns them — they're not currently "
        "actionable and the reconciler handles fill attribution."
    )


# ---------------------------------------------------------------------------
# 6. API errors don't crash
# ---------------------------------------------------------------------------

def test_api_error_returns_empty_for_that_account(tmp_path):
    """If list_orders raises, the audit logs a warning and the account
    contributes 0 manual orders + 0 known — doesn't crash."""
    from aggregate_audit import audit_manual_broker_orders
    db = _make_journal(tmp_path, "p1", order_ids=["sys"])
    api = MagicMock()
    api.list_orders.side_effect = Exception("api down")
    profiles = {1: {"db_path": db, "alpaca_account_id": 13}}
    with _patch_build_ctx(profiles, api):
        out = audit_manual_broker_orders(profile_ids=[1])
    assert out["manual"] == []
    assert out["accounts"][13]["total_broker_active"] == 0


# ---------------------------------------------------------------------------
# 7. Young-order grace window (race-condition false-positive guard)
# ---------------------------------------------------------------------------

def test_young_broker_order_excluded_from_audit(tmp_path):
    """An order whose broker `created_at` is within the grace window
    (default 60s) is in the submit→journal race for our own cycle —
    must be excluded so the audit doesn't fire false positives every
    time the system places orders. Caught 2026-06-04 13:30:49 when
    the post-reset opening cycle triggered a false email alert."""
    from aggregate_audit import audit_manual_broker_orders
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    recent_iso = (now - dt.timedelta(seconds=10)).isoformat()
    db = _make_journal(tmp_path, "p1")  # no journal rows
    api = MagicMock()
    young_order = _broker_order("just-submitted", "new")
    young_order.created_at = recent_iso
    api.list_orders.return_value = [young_order]
    profiles = {1: {"db_path": db, "alpaca_account_id": 13}}
    with _patch_build_ctx(profiles, api):
        out = audit_manual_broker_orders(profile_ids=[1])
    assert out["manual"] == [], (
        "Order created within the grace window must be excluded — the "
        "system's own submit→journal race would otherwise produce a "
        "false-positive 'manual order' alert on every cycle."
    )


def test_older_broker_order_still_flagged_as_manual(tmp_path):
    """An order older than the grace window (60s) is past the
    submit→journal race — if it's still not in any journal, it IS
    a manual order and must be flagged."""
    from aggregate_audit import audit_manual_broker_orders
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    old_iso = (now - dt.timedelta(seconds=180)).isoformat()
    db = _make_journal(tmp_path, "p1")
    api = MagicMock()
    old_order = _broker_order("real-manual", "new")
    old_order.created_at = old_iso
    api.list_orders.return_value = [old_order]
    profiles = {1: {"db_path": db, "alpaca_account_id": 13}}
    with _patch_build_ctx(profiles, api):
        out = audit_manual_broker_orders(profile_ids=[1])
    assert len(out["manual"]) == 1
    assert out["manual"][0]["order_id"] == "real-manual"


def test_unparseable_created_at_does_not_grant_grace():
    """If `created_at` can't be parsed, the grace window can't be
    established — default to NOT granting grace (so a malformed
    timestamp doesn't silently bypass detection)."""
    from aggregate_audit import _is_within_grace_window
    assert _is_within_grace_window("") is False
    assert _is_within_grace_window("not-a-timestamp") is False
    assert _is_within_grace_window(None) is False


# ---------------------------------------------------------------------------
# 8. Summary formatter
# ---------------------------------------------------------------------------

def test_format_summary_zero_manual():
    from aggregate_audit import format_manual_orders_summary
    out = format_manual_orders_summary({"manual": []})
    assert "0 manual orders" in out


def test_format_summary_with_manual():
    from aggregate_audit import format_manual_orders_summary
    audit = {"manual": [
        {"account": 13, "symbol": "TSLA", "side": "sell", "qty": 50.0,
         "type": "limit", "order_id": "abcd1234ffffeeee",
         "status": "new", "created_at": "..."},
    ]}
    out = format_manual_orders_summary(audit)
    assert "1 manual broker order" in out
    assert "acct13" in out
    assert "TSLA" in out
    assert "abcd1234" in out
