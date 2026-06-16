"""2026-06-16 — rigorous per-profile ORDER isolation (A0/A1/A2).

The months-long bug: profiles share an Alpaca account, and several
code paths reached for ACCOUNT-WIDE broker order lists
(`api.list_orders(...)` with no per-profile filter) and then
cancelled or consumed orders from that list. On a shared account
that list contains EVERY sibling profile's orders, so one profile's
exit/maintenance cancelled a sibling's pending order.

The fix is a single load-bearing primitive:
`order_guard.own_broker_order_ids(db_path, symbol)` — the set of
Alpaca order_ids THIS profile's own journal recorded (entry
`order_id` + every `protective_*_order_id`, plus long_vol_hedges).
Every cancel site now intersects the broker's open orders with this
set, so a sibling's order_id — which is never in this profile's
journal — can never be touched.

A0 (foundation): every order this profile submits must have its
order_id journaled before the function returns, or
`own_broker_order_ids` is incomplete and the isolation fails. Tests
here pin both the primitive and the call-site wiring; the broader
A0 atomic-journaling invariant is pinned in
test_trade_order_id_invariant_2026_05_17.py.

See PROFILE_ORDER_ISOLATION.md.
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
# Scaffolding
# ---------------------------------------------------------------------------


def _profile_db(tmp_path, filename="p.db"):
    from journal import init_db
    db = str(tmp_path / filename)
    init_db(db)
    return db


def _insert(db, **cols):
    keys = ", ".join(cols)
    qs = ", ".join(["?"] * len(cols))
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(f"INSERT INTO trades ({keys}) VALUES ({qs})",
                     list(cols.values()))
        conn.commit()


def _order(oid, symbol, side="sell", order_type="limit", status="open"):
    o = MagicMock()
    o.id = oid
    o.symbol = symbol
    o.side = side
    o.type = order_type
    o.status = status
    return o


# ---------------------------------------------------------------------------
# A0 primitive — own_broker_order_ids
# ---------------------------------------------------------------------------


class TestOwnBrokerOrderIds:

    def test_returns_entry_order_id(self, tmp_path):
        from order_guard import own_broker_order_ids
        db = _profile_db(tmp_path)
        _insert(db, symbol="SPCX", side="buy", qty=10, price=5,
                order_id="own-buy-1", status="open")
        assert own_broker_order_ids(db, "SPCX") == {"own-buy-1"}

    def test_returns_protective_order_ids(self, tmp_path):
        from order_guard import own_broker_order_ids
        db = _profile_db(tmp_path)
        _insert(db, symbol="SPCX", side="buy", qty=10, price=5,
                order_id="own-buy-1", status="open",
                protective_stop_order_id="own-stop-1",
                protective_tp_order_id="own-tp-1",
                protective_trailing_order_id="own-trail-1")
        ids = own_broker_order_ids(db, "SPCX")
        assert ids == {"own-buy-1", "own-stop-1", "own-tp-1", "own-trail-1"}

    def test_excludes_other_symbols_when_filtered(self, tmp_path):
        from order_guard import own_broker_order_ids
        db = _profile_db(tmp_path)
        _insert(db, symbol="SPCX", side="buy", qty=10, price=5,
                order_id="spcx-1", status="open")
        _insert(db, symbol="SOUN", side="buy", qty=10, price=5,
                order_id="soun-1", status="open")
        assert own_broker_order_ids(db, "SPCX") == {"spcx-1"}
        assert own_broker_order_ids(db, "SOUN") == {"soun-1"}
        # No symbol filter → both
        assert own_broker_order_ids(db) == {"spcx-1", "soun-1"}

    def test_sibling_order_id_is_never_returned(self, tmp_path):
        """The crux: a sibling's order_id is in the sibling's journal,
        never in THIS profile's. So it can never appear in this
        profile's own-id set — hence never be cancelled."""
        from order_guard import own_broker_order_ids
        own_db = _profile_db(tmp_path, "own.db")
        sib_db = _profile_db(tmp_path, "sibling.db")
        _insert(own_db, symbol="SPCX", side="buy", qty=10, price=5,
                order_id="own-1", status="open")
        _insert(sib_db, symbol="SPCX", side="buy", qty=10, price=5,
                order_id="sibling-1", status="open")
        own_ids = own_broker_order_ids(own_db, "SPCX")
        assert "own-1" in own_ids
        assert "sibling-1" not in own_ids

    def test_includes_long_vol_hedge_ids(self, tmp_path):
        """Long-vol hedge order_ids live in their own table, not
        `trades` — the primitive must still claim them as own."""
        from order_guard import own_broker_order_ids
        db = _profile_db(tmp_path)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "CREATE TABLE long_vol_hedges (id INTEGER PRIMARY KEY, "
                "order_id TEXT, close_order_id TEXT)")
            conn.execute(
                "INSERT INTO long_vol_hedges (order_id, close_order_id) "
                "VALUES ('hedge-open-1', 'hedge-close-1')")
            conn.commit()
        ids = own_broker_order_ids(db)
        assert "hedge-open-1" in ids
        assert "hedge-close-1" in ids

    def test_empty_db_path_returns_empty_set(self):
        from order_guard import own_broker_order_ids
        assert own_broker_order_ids(None) == set()
        assert own_broker_order_ids("") == set()


# ---------------------------------------------------------------------------
# A1 — trader.py exit cancels ONLY own orders (functional + structural)
# ---------------------------------------------------------------------------


class TestExitCancelsOnlyOwn:

    def test_cancel_filter_spares_sibling_order(self, tmp_path):
        """Reproduce the SPCX bug at the decision layer: this profile
        owns 'own-stop-1'; the shared account's open-orders list also
        contains a sibling's 'sibling-tp-1'. Filtering by the own-id
        set cancels only 'own-stop-1' and leaves the sibling's order
        untouched."""
        from order_guard import own_broker_order_ids
        db = _profile_db(tmp_path)
        _insert(db, symbol="SPCX", side="buy", qty=10, price=5,
                order_id="own-buy-1", status="open",
                protective_stop_order_id="own-stop-1")
        own_ids = own_broker_order_ids(db, "SPCX")
        broker_open = [
            _order("own-stop-1", "SPCX", side="sell", order_type="stop"),
            _order("sibling-tp-1", "SPCX", side="sell", order_type="limit"),
        ]
        cancelled = [o.id for o in broker_open if o.id in own_ids]
        assert cancelled == ["own-stop-1"]
        assert "sibling-tp-1" not in cancelled

    def test_trader_exit_gates_cancel_on_own_ids(self):
        """Structural pin: trader.py's pre-exit cancel block must call
        own_broker_order_ids and skip ids not in that set. Without
        this gate the SPCX cross-profile cancel returns."""
        src = (REPO_ROOT / "trader.py").read_text()
        idx = src.find("Cancel any of THIS PROFILE'S OWN open orders")
        assert idx > 0, "exit cancel block comment missing"
        window = src[idx:idx + 1200]
        assert "own_broker_order_ids(db_path, symbol)" in window
        assert "if oo.id not in own_ids" in window

    def test_trader_exit_has_minimal_journal_fallback(self):
        """A0 pin: if the rich log_trade fails after the exit order is
        live, trader.py must fall back to the minimal order_id journal
        (and halt only if even that fails) so the fill can't orphan."""
        src = (REPO_ROOT / "trader.py").read_text()
        assert "_journal_exit_order_id_minimal" in src
        assert "exit_journal_breach" in src


# ---------------------------------------------------------------------------
# A0 — minimal exit-journal fallback actually preserves the order_id
# ---------------------------------------------------------------------------


class TestMinimalExitJournal:

    def test_minimal_journal_writes_order_id(self, tmp_path):
        import trader
        db = _profile_db(tmp_path)
        ok = trader._journal_exit_order_id_minimal(
            db, "SOUN", "sell", 100, 12.5, "live-exit-99",
            "pending_fill", 50.0,
        )
        assert ok is True
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT symbol, side, qty, order_id, status FROM trades "
                "WHERE order_id = 'live-exit-99'").fetchone()
        assert row is not None, "order_id must be journaled"
        assert row[0] == "SOUN" and row[3] == "live-exit-99"

    def test_minimal_journal_returns_false_on_bad_path(self):
        import trader
        ok = trader._journal_exit_order_id_minimal(
            "/nonexistent/dir/x.db", "SOUN", "sell", 1, 1, "x",
            "open", None,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# A2 — multi_scheduler stale-cancel spares siblings (functional)
# ---------------------------------------------------------------------------


class TestStaleCancelOnlyOwn:

    def test_stale_cancel_skips_sibling_order(self, tmp_path):
        """_task_cancel_stale_orders runs once PER PROFILE against the
        shared account. A stale limit order belonging to a sibling
        must survive this profile's sweep."""
        import multi_scheduler
        from datetime import datetime, timezone, timedelta
        db = _profile_db(tmp_path)
        _insert(db, symbol="AAPL", side="buy", qty=10, price=100,
                order_id="own-limit-1", status="open")

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        own = _order("own-limit-1", "AAPL", side="buy", order_type="limit")
        own.created_at = old
        sib = _order("sibling-limit-1", "AAPL", side="buy",
                     order_type="limit")
        sib.created_at = old

        api = MagicMock()
        api.list_orders.return_value = [own, sib]

        ctx = MagicMock()
        ctx.use_limit_orders = True
        ctx.db_path = db
        ctx.display_name = "EXP-A"
        ctx.segment = "seg"
        ctx.profile_id = 7
        ctx.user_id = 1

        import client
        with patch.object(client, "get_api", return_value=api), \
             patch.object(multi_scheduler, "_safe_log_activity"):
            multi_scheduler._task_cancel_stale_orders(ctx)

        cancelled = [c.args[0] for c in api.cancel_order.call_args_list]
        assert cancelled == ["own-limit-1"], (
            f"Only this profile's stale order may be cancelled; got "
            f"{cancelled}"
        )
        assert "sibling-limit-1" not in cancelled

    def test_scheduler_gates_cancel_on_own_ids(self):
        """Structural pin for the stale-cancel task."""
        src = (REPO_ROOT / "multi_scheduler.py").read_text()
        idx = src.find("def _task_cancel_stale_orders")
        assert idx > 0
        body = src[idx:idx + 1600]
        assert "own_broker_order_ids" in body
        assert "if order.id not in own_ids" in body


# ---------------------------------------------------------------------------
# A0 — multileg rollback closes are journaled (no orphan fills)
# ---------------------------------------------------------------------------


class TestMultilegRollbackJournaling:

    def test_rollback_journals_open_and_close(self, tmp_path):
        import options_multileg as oml
        db = _profile_db(tmp_path)
        leg = MagicMock()
        leg.side = "buy"
        leg.qty = 1
        leg.underlying = "AAPL"
        leg.occ_symbol = "AAPL260618C00200000"
        leg.expiry = "2026-06-18"
        leg.strike = 200.0
        oml._journal_rolled_back_leg(
            db, leg, "open-oid-1", "close-oid-1", "leg-failure rollback",
        )
        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute(
                "SELECT order_id, side FROM trades ORDER BY order_id"
            ).fetchall()
        oids = {r[0]: r[1] for r in rows}
        assert oids.get("open-oid-1") == "buy"
        assert oids.get("close-oid-1") == "sell", (
            "rollback close must be journaled with the opposite side so "
            "its broker fill is attributable (nets the leg flat)"
        )

    def test_rollback_loops_call_journal_helper(self):
        """Structural pin: both sequential-open rollback loops must
        journal via _journal_rolled_back_leg."""
        src = (REPO_ROOT / "options_multileg.py").read_text()
        assert src.count("_journal_rolled_back_leg(") >= 3, (
            "expected the helper def + a call in each of the two "
            "rollback loops"
        )
