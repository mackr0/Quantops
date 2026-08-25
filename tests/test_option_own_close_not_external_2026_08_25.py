"""2026-08-25 — own option round-trips must NEVER be written off as
externally closed, regardless of fill-state-machine timing.

The incident (Experiment 2, day two): five long option entries were
flipped 'auto_closed_external' after our OWN exit sells filled. Two
half-guards had a gap exactly between them:

  * the 08-10 own-close guard read only LIVE rows, so once the fill
    machine marked the exit row 'closed' the round-trip was invisible
    (one live row -> None);
  * the 07-27 evidence guard accepted any FILL activity for the OCC as
    proof of an external close — including the fill of OUR OWN exit
    order.

The entry premiums vanished from the cash algebra (the
auto_closed_external dead set excludes the row from cash AND from the
leg-realized FIFO): equity-identity drift +$185/+$185/+$1,100.01/
+$4,550 on p229/230/231/238, journal-cash phantoms $2,620.13/$405.02/
$1,265.06 on accounts 61/62/63 — every cent reconciled to the five
pairs.

These tests pin the closed class:
  1. _own_occ_roundtrip sees the round-trip through EVERY fill-bearing
     status (exit already 'closed', or a sibling already mislabeled).
  2. _external_close_evidence refuses FILL activities that are our own
     (or carry no order_id at all).
  3. reconcile_option_orphans books the race state as an OWN close
     ('closed'), never 'auto_closed_external'.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import reconcile_journal_to_broker as rjb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OCC = "JPM261002C00375000"


def _db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "p.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE trades ("
        " id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, qty REAL,"
        " price REAL, fill_price REAL, status TEXT, pnl REAL,"
        " order_id TEXT, occ_symbol TEXT, expiry TEXT, reason TEXT,"
        " timestamp TEXT DEFAULT '2026-08-25T14:00:00',"
        " data_quality TEXT)")
    return conn


def _row(conn, id_, side, qty, status, order_id, occ=OCC,
         expiry="2026-10-02", price=3.8, fill_price=3.8):
    conn.execute(
        "INSERT INTO trades (id, symbol, side, qty, price, fill_price,"
        " status, order_id, occ_symbol, expiry, timestamp)"
        " VALUES (?, 'JPM', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id_, side, qty, price, fill_price, status, order_id, occ,
         expiry, "2026-08-25T14:00:%02d" % (id_ % 60)))
    conn.commit()


class _FilledOrder:
    status = "filled"

    def __init__(self, qty, filled_avg_price=3.8):
        self.filled_qty = qty
        self.filled_avg_price = filled_avg_price


class _FakeApi:
    """Orders always verify filled; activities configurable."""

    def __init__(self, activities=None, orders=None):
        self._acts = activities or []
        self._orders = orders or {}

    def get_order(self, oid):
        return self._orders.get(oid, _FilledOrder(1))

    def get_activities(self, activity_types=None, after=None):
        return [a for a in self._acts
                if getattr(a, "activity_type", None) == activity_types]


def _fill_act(order_id, symbol=OCC):
    return SimpleNamespace(activity_type="FILL", id="act-1",
                           symbol=symbol, order_id=order_id)


# ---------------------------------------------------------------------------
# 1. _own_occ_roundtrip — race-proof net over every fill-bearing row
# ---------------------------------------------------------------------------

class TestOwnRoundtripSeesClosedRows:
    def test_exit_already_closed_still_detected(self, tmp_path):
        """THE incident shape: entry open, exit already 'closed'.
        Pre-fix the live-rows query saw one row and returned None."""
        conn = _db(tmp_path)
        _row(conn, 1, "buy", 1, "open", "entry-1")
        _row(conn, 2, "sell", 1, "closed", "exit-1")
        api = _FakeApi(orders={"entry-1": _FilledOrder(1),
                               "exit-1": _FilledOrder(1)})
        with patch("order_status_cache.get_order_cached",
                   side_effect=lambda a, o: a.get_order(o)):
            ids = rjb._own_occ_roundtrip(api, conn, OCC)
        assert ids == [1], (
            "the open entry must be returned for the own-close flip "
            "even though its exit row is already 'closed'")

    def test_mislabeled_sibling_self_heals(self, tmp_path):
        """A previously mislabeled auto_closed_external row counts in
        the net (its fill is real) and is returned for re-flip."""
        conn = _db(tmp_path)
        _row(conn, 1, "buy", 1, "auto_closed_external", "entry-1")
        _row(conn, 2, "sell", 1, "closed", "exit-1")
        api = _FakeApi()
        with patch("order_status_cache.get_order_cached",
                   side_effect=lambda a, o: a.get_order(o)):
            ids = rjb._own_occ_roundtrip(api, conn, OCC)
        assert ids == [1]

    def test_not_flat_returns_none(self, tmp_path):
        conn = _db(tmp_path)
        _row(conn, 1, "buy", 2, "open", "entry-1")
        _row(conn, 2, "sell", 1, "closed", "exit-1")
        api = _FakeApi(orders={"entry-1": _FilledOrder(2),
                               "exit-1": _FilledOrder(1)})
        with patch("order_status_cache.get_order_cached",
                   side_effect=lambda a, o: a.get_order(o)):
            assert rjb._own_occ_roundtrip(api, conn, OCC) is None

    def test_unverified_order_returns_none(self, tmp_path):
        conn = _db(tmp_path)
        _row(conn, 1, "buy", 1, "open", "entry-1")
        _row(conn, 2, "sell", 1, "closed", "exit-1")
        api = _FakeApi(orders={"entry-1": _FilledOrder(1),
                               "exit-1": None})
        with patch("order_status_cache.get_order_cached",
                   side_effect=lambda a, o: a.get_order(o)):
            assert rjb._own_occ_roundtrip(api, conn, OCC) is None

    def test_all_rows_already_closed_returns_none(self, tmp_path):
        """Nothing to flip -> None (caller treats as no-op)."""
        conn = _db(tmp_path)
        _row(conn, 1, "buy", 1, "closed", "entry-1")
        _row(conn, 2, "sell", 1, "closed", "exit-1")
        api = _FakeApi()
        with patch("order_status_cache.get_order_cached",
                   side_effect=lambda a, o: a.get_order(o)):
            assert rjb._own_occ_roundtrip(api, conn, OCC) is None


# ---------------------------------------------------------------------------
# 2. _external_close_evidence — our own fills are not external evidence
# ---------------------------------------------------------------------------

class TestOwnFillIsNotExternalEvidence:
    def test_own_fill_rejected(self):
        api = _FakeApi(activities=[_fill_act("exit-1")])
        assert rjb._external_close_evidence(
            api, OCC, own_order_ids=["entry-1", "exit-1"]) is None

    def test_orderless_fill_rejected(self):
        """Unverifiable is not proof (07-27 doctrine applied to FILL)."""
        api = _FakeApi(activities=[_fill_act(None)])
        assert rjb._external_close_evidence(
            api, OCC, own_order_ids=["entry-1"]) is None

    def test_foreign_fill_accepted(self):
        api = _FakeApi(activities=[_fill_act("someone-elses-order")])
        ev = rjb._external_close_evidence(
            api, OCC, own_order_ids=["entry-1", "exit-1"])
        assert ev is not None and ev["activity_type"] == "FILL"

    def test_assignment_still_accepted(self):
        act = SimpleNamespace(activity_type="OPASN", id="a-9",
                              symbol=OCC)
        api = _FakeApi(activities=[act])
        ev = rjb._external_close_evidence(
            api, OCC, own_order_ids=["entry-1"])
        assert ev is not None and ev["activity_type"] == "OPASN"


# ---------------------------------------------------------------------------
# 3. reconcile_option_orphans — the race state books an OWN close
# ---------------------------------------------------------------------------

class TestOrphanPassBooksOwnClose:
    def _run(self, conn, api):
        from datetime import date
        with patch("order_status_cache.get_order_cached",
                   side_effect=lambda a, o: a.get_order(o)):
            return rjb.reconcile_option_orphans(
                api, conn, positions=[], today=date(2026, 8, 25),
                apply_changes=True)

    def test_entry_flips_closed_never_external(self, tmp_path):
        conn = _db(tmp_path)
        _row(conn, 1, "buy", 1, "open", "entry-1")
        _row(conn, 2, "sell", 1, "closed", "exit-1")
        # Adversarial: the broker ALSO reports our own exit fill as an
        # activity — pre-fix this was the "evidence" that mislabeled
        # the entry.
        api = _FakeApi(activities=[_fill_act("exit-1")],
                       orders={"entry-1": _FilledOrder(1),
                               "exit-1": _FilledOrder(1)})
        closed = self._run(conn, api)
        assert [c["kind"] for c in closed] == ["own_close"]
        status = conn.execute(
            "SELECT status FROM trades WHERE id=1").fetchone()[0]
        assert status == "closed"
        assert not conn.execute(
            "SELECT 1 FROM trades WHERE status='auto_closed_external'"
        ).fetchone(), "no row may be written off as external"

    def test_genuinely_external_still_labeled(self, tmp_path):
        """A foreign fill with NO own exit row anywhere is a real
        external close and keeps its semantics."""
        conn = _db(tmp_path)
        _row(conn, 1, "buy", 1, "open", "entry-1")
        api = _FakeApi(activities=[_fill_act("foreign-order")],
                       orders={"entry-1": _FilledOrder(1)})
        closed = self._run(conn, api)
        assert [c["new_status"] for c in closed] == [
            "auto_closed_external"]

    def test_external_flip_stamps_broker_fill_price(self, tmp_path):
        """A filled entry written off as external gets its broker fill
        price stamped at flip time (fill machine hadn't yet), so the
        fill-truth invariant keeps its cash flow booked."""
        conn = _db(tmp_path)
        _row(conn, 1, "buy", 1, "open", "entry-1", fill_price=None)
        api = _FakeApi(
            activities=[_fill_act("foreign-order")],
            orders={"entry-1": _FilledOrder(1, filled_avg_price=3.8)})
        self._run(conn, api)
        row = conn.execute(
            "SELECT status, fill_price FROM trades WHERE id=1"
        ).fetchone()
        assert row["status"] == "auto_closed_external"
        assert row["fill_price"] == pytest.approx(3.8)


# ---------------------------------------------------------------------------
# 4. FILL-TRUTH INVARIANT — no status can remove a real fill's money
# ---------------------------------------------------------------------------

class TestFillTruthInvariant:
    """The accounting-layer guarantee (2026-08-25): a fill-bearing row
    stays in the cash algebra AND the realized FIFO no matter what
    status a reconcile path stamps on it. A future labeling mistake —
    on any path, under any timing — is a cosmetic status error, never
    vanished money. This is what makes the incident class structurally
    impossible instead of merely less likely."""

    def _books(self, tmp_path):
        conn = _db(tmp_path)
        # THE incident shape after the mislabel: entry flipped
        # auto_closed_external (fill 3.80), own exit closed (1.85).
        _row(conn, 1, "buy", 1, "auto_closed_external", "entry-1",
             fill_price=3.8)
        _row(conn, 2, "sell", 1, "closed", "exit-1", price=1.85,
             fill_price=1.85)
        conn.close()
        return str(tmp_path / "p.db")

    def test_cash_keeps_mislabeled_fill(self, tmp_path):
        from journal import get_virtual_cash
        db = self._books(tmp_path)
        cash = get_virtual_cash(db_path=db, initial_capital=10_000.0)
        # -380 premium out, +185 proceeds in — books stay true even
        # with the entry mislabeled.
        assert cash == pytest.approx(10_000.0 - 380.0 + 185.0)

    def test_realized_completes_mislabeled_pair(self, tmp_path):
        from journal import compute_leg_realized
        db = self._books(tmp_path)
        legs = compute_leg_realized(db_path=db)
        assert legs is not None
        assert sum(legs.values()) == pytest.approx(-195.0)

    def test_fill_less_external_row_stays_out_of_cash(self, tmp_path):
        """A never-filled row written off as external keeps the old
        exclusion — no phantom cash from decision prices."""
        from journal import get_virtual_cash
        conn = _db(tmp_path)
        _row(conn, 1, "buy", 1, "auto_closed_external", "entry-1",
             fill_price=None)
        conn.close()
        cash = get_virtual_cash(db_path=str(tmp_path / "p.db"),
                                initial_capital=10_000.0)
        assert cash == pytest.approx(10_000.0)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
