"""2026-06-19 — end-to-end pipeline replay of the re-arm → oversell class,
driven through the REAL protective sweep (bracket_orders.ensure_protective_
stops) and the REAL position reconstruction (journal.get_virtual_positions),
against a minimal fake broker.

FakeBroker is a recording/serving stub, NOT a matching engine — it returns
exactly what you configure and records submit/cancel calls. That keeps it
faithful by construction (there's no fill-simulation logic to get subtly
wrong and hand back false confidence — the exact trap this whole harness
exists to avoid).

The incident: after a bracket stop closed a UWMC long, the sweep re-armed a
protective SELL on the now-flat position and it filled as an oversell short.
The fix makes get_virtual_positions net a closed position to 0, so it never
enters the sweep's snapshot — these tests pin that end to end.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


class _Order:
    def __init__(self, oid="fake-1", order_class="simple"):
        self.id = oid
        self.order_class = order_class
        self.legs = []
        self.status = "new"
        self.replaces = None


class FakeBroker:
    """Records submit/cancel; serves configurable orders/positions. No
    fill engine — faithful because it only echoes configured state."""
    def __init__(self, positions=None):
        self.submitted = []          # list of submit_order kwargs
        self.canceled = []
        self._positions = positions or []

    def submit_order(self, **kwargs):
        self.submitted.append(kwargs)
        return _Order(oid="submitted-%d" % len(self.submitted))

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        return None

    def list_orders(self, **kwargs):
        return []                    # no live protective coverage

    def get_order(self, order_id, nested=False):
        return _Order(oid=order_id, order_class="simple")  # not a bracket

    def list_positions(self):
        return list(self._positions)

    def get_account(self):
        class _A:
            equity = buying_power = cash = portfolio_value = 0.0
            status = "ACTIVE"
        return _A()


class _Ctx:
    def __init__(self, db):
        self.db_path = db
        self.stop_loss_pct = 0.05
        self.short_stop_loss_pct = 0.05
        self.use_trailing_stops = False
        self.use_conviction_tp_override = False
        self.segment = "stocks"
        self.display_name = "test"
        self.is_virtual = True
        self.initial_capital = 250000.0


def _seed(db, rows):
    from journal import init_db
    init_db(db)
    c = sqlite3.connect(db)
    for i, r in enumerate(rows):
        sym, side, qty, px, status = r
        c.execute(
            "INSERT INTO trades (timestamp,symbol,side,qty,price,fill_price,"
            "signal_type,status,order_id) VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-06-19T13:00:0%d" % (i % 10), sym, side, qty, px, px,
             "BUY", status, "oid-%d" % i))
    c.commit()
    c.close()


def _positions(db):
    from journal import get_virtual_positions
    return get_virtual_positions(db, price_fetcher=lambda *a, **k: 10.0)


def test_sweep_does_not_rearm_a_closed_position(tmp_path):
    """The re-arm bug, end to end: a round-trip-closed UWMC must NOT be in
    the sweep's snapshot, so the sweep arms NO protective on it (no naked
    sell that would fill as a short)."""
    from bracket_orders import ensure_protective_stops
    db = str(tmp_path / "quantopsai_profile_1.db")
    _seed(db, [
        ("UWMC", "buy", 20634, 2.47, "closed"),   # entry
        ("UWMC", "sell", 20634, 2.29, "closed"),  # bracket stop fired → flat
    ])
    positions = _positions(db)
    assert not any(p.get("symbol") == "UWMC" for p in positions), (
        "a closed position must not appear in the snapshot")
    api = FakeBroker()
    ensure_protective_stops(api, positions, _Ctx(db), db)
    assert not any(o.get("symbol") == "UWMC" for o in api.submitted), (
        "sweep must NOT re-arm a protective on a closed/flat position")


def test_sweep_still_arms_a_genuinely_held_long(tmp_path):
    """Control: a real open long IS protected (the fix didn't over-suppress
    arming)."""
    from bracket_orders import ensure_protective_stops
    db = str(tmp_path / "quantopsai_profile_2.db")
    _seed(db, [("HELD", "buy", 100, 10.0, "open")])
    positions = _positions(db)
    assert any(p.get("symbol") == "HELD" for p in positions)
    api = FakeBroker()
    ensure_protective_stops(api, positions, _Ctx(db), db)
    assert any(o.get("symbol") == "HELD" for o in api.submitted), (
        "a genuinely-held long must still get a protective order")


def test_oversell_short_visible_to_pipeline_not_phantom(tmp_path):
    """If an oversell DID happen (the re-armed sell filled), the short is
    visible to the pipeline as a real -N position (so equity reflects it),
    not dropped into phantom equity."""
    db = str(tmp_path / "quantopsai_profile_3.db")
    _seed(db, [
        ("UWMC", "buy", 20634, 2.47, "closed"),
        ("UWMC", "sell", 20634, 2.29, "closed"),
        ("UWMC", "sell", 20634, 2.18, "closed"),   # re-armed sell filled
    ])
    net = sum(float(p.get("qty") or 0) for p in _positions(db)
              if p.get("symbol") == "UWMC")
    assert net == -20634.0


def test_rearm_prevented_solely_by_snapshot_netting(tmp_path):
    """Decouple the two re-arm guards (review Finding 3).

    test_sweep_does_not_rearm_a_closed_position uses a 'closed' entry, so
    BOTH guards fire — the get_virtual netting AND the bracket_orders
    entry-row `status='open'` filter — meaning a regression of the netting
    fix alone wouldn't fail it. Here the entry buy STAYS 'open' (so the
    entry-status filter does NOT block), and a closed opposite-side sell
    nets the position to 0. Now the ONLY thing preventing a re-arm is
    get_virtual_positions netting it out of the snapshot — proven below by
    forcing the position back in and watching it arm."""
    from bracket_orders import ensure_protective_stops
    db = str(tmp_path / "quantopsai_profile_4.db")
    _seed(db, [
        ("NETZ", "buy", 100, 10.0, "open"),     # entry stays OPEN
        ("NETZ", "sell", 100, 10.5, "closed"),  # nets the position to 0
    ])
    positions = _positions(db)
    # Guard under test: the netted-flat position is absent from the snapshot.
    assert not any(p.get("symbol") == "NETZ" for p in positions)
    api = FakeBroker()
    ensure_protective_stops(api, positions, _Ctx(db), db)
    assert not any(o.get("symbol") == "NETZ" for o in api.submitted), (
        "a netted-flat position must not be re-armed")
    # Prove the netting is load-bearing: if get_virtual HAD kept the
    # position (a regression of the fix), the 'open' entry means the
    # entry-status filter would NOT save us — it WOULD arm.
    phantom = [{"symbol": "NETZ", "qty": 100, "occ_symbol": None,
                "avg_entry_price": 10.0, "side": "long"}]
    api2 = FakeBroker()
    ensure_protective_stops(api2, phantom, _Ctx(db), db)
    assert any(o.get("symbol") == "NETZ" for o in api2.submitted), (
        "with an 'open' entry, only get_virtual's netting prevents the "
        "re-arm; the entry-status filter alone does not — so the snapshot "
        "fix is the load-bearing guard here")
