"""Broker-backing guard (2026-07-07): a protective order may neither be
PLACED nor left RESTING unless the conduit's live broker position
actually backs it.

THE INCIDENT: Alpaca zeroed positions and cash on all 3 conduits
overnight (second no-trail wipe in 25 days) with journals intact. The
resting GTC sell stops that legitimately protected Monday's longs fired
into Tuesday's flat book and manufactured real broker shorts (MARA
−6,501, RIVN −690, CRCL −654...). The oversell door couldn't help: it is
journal-only by design, protectives bypassed allowable_sell_qty, and a
resting order can't be vetoed — only never placed, or cancelled in time.
Manual cancels were whack-a-mole: the sweep re-armed 10 stops within one
cycle, purely on journal faith.

Two halves, both pinned here:
  1. Submit-time gate — _submit_protective refuses any protective the
     broker POSITION doesn't back (position-only by design: resting
     orders are sibling slices' stops, 2026-06-09 isolation; fail-
     CLOSED on unreadable position; polling exits backstop the cycle).
  2. Revocation sweep — revoke_unbacked_protective_orders cancels OWN
     resting protectives in the FULL-wipe shape only: broker flat/
     opposite + journal entry still open + ENTRY ORDER VERIFIED FILLED
     (a position provably existed — a pending bracket entry has the
     identical journal shape and must never be disarmed) + no own-exit-
     fill explanation. Journal mutations happen only after the cancel
     is BROKER-CONFIRMED (the cancel helper's lenient bool reads 'not
     cancelable'/'filled' errors as success — trusting it would let the
     order rest with its teeth while the journal forgets it). Alert-
     loud, race-safe (partial mismatch never cancels — that can be an
     in-flight own fill; stripping live protection is the naked-
     position class). Plus a pre-open disarm pass INSIDE the closed-
     market sleep loop (the parked loop only exits AT the open — an
     outer-loop call never sees the window) so overnight wipes are
     defused before 09:30, when stops cannot fire.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from bracket_orders import (
    _broker_backs_protective,
    _submit_protective,
    revoke_unbacked_protective_orders,
)


class FakePosition:
    def __init__(self, qty):
        self.qty = qty


class FakeOrder:
    def __init__(self, side, qty, status="new"):
        self.side = side
        self.qty = qty
        self.status = status
        self.filled_qty = qty if status == "filled" else 0


class NoPosition(Exception):
    def __init__(self):
        super().__init__("position does not exist")
        self.status_code = 404


class FakeApi:
    """positions: {symbol: qty}; open_orders: list[FakeOrder];
    orders_by_id: {oid: FakeOrder}. Records submits and cancels.
    Mock-parity: a real Alpaca cancel flips the order's status to
    'canceled' on subsequent reads — mirrored here (cancel_sticks=False
    simulates a cancel that silently doesn't take, e.g. rate-limited
    or 'not cancelable', in which case status stays live)."""

    def __init__(self, positions=None, open_orders=None, orders_by_id=None,
                 position_error=False, orders_error=False,
                 cancel_sticks=True):
        self.positions = positions or {}
        self.open_orders = open_orders or []
        self.orders_by_id = orders_by_id or {}
        self.position_error = position_error
        self.orders_error = orders_error
        self.cancel_sticks = cancel_sticks
        self.submitted = []
        self.canceled = []

    def get_position(self, symbol):
        if self.position_error:
            raise RuntimeError("broker on fire")
        if symbol not in self.positions:
            raise NoPosition()
        return FakePosition(self.positions[symbol])

    def list_orders(self, status="open", symbols=None, **kw):
        if self.orders_error:
            raise RuntimeError("orders endpoint on fire")
        return self.open_orders

    def get_order(self, oid):
        if oid not in self.orders_by_id:
            raise RuntimeError(f"order {oid} not found")
        return self.orders_by_id[oid]

    def submit_order(self, **kwargs):
        self.submitted.append(kwargs)
        return FakeOrder(kwargs.get("side"), kwargs.get("qty"))

    def cancel_order(self, oid):
        self.canceled.append(oid)
        if self.cancel_sticks and oid in self.orders_by_id:
            self.orders_by_id[oid].status = "canceled"


def _filled_entry(side="buy", qty=0):
    """Broker record of an entry order that verified FILLED — the
    entry-evidence a wipe declaration requires."""
    return FakeOrder(side, qty, status="filled")


# ------------------------------------------------- submit-time gate

def test_backed_sell_protective_is_placed():
    api = FakeApi(positions={"AAPL": 100})
    order = _submit_protective(
        api, {"symbol": "AAPL", "side": "sell", "qty": 100,
              "type": "trailing_stop", "trail_percent": 5},
        "x.db", "AAPL", "trailing stop for AAPL")
    assert order is not None
    assert len(api.submitted) == 1


def test_wipe_rearm_shape_is_refused():
    # THE incident shape: journal faith says protect 1,145 MARA; the
    # broker position is GONE. The stop must never be placed.
    api = FakeApi(positions={})  # 404 -> flat
    order = _submit_protective(
        api, {"symbol": "MARA", "side": "sell", "qty": 1145,
              "type": "trailing_stop", "trail_percent": 5},
        "x.db", "MARA", "trailing stop for MARA")
    assert order is None
    assert api.submitted == []


def test_sibling_resting_orders_never_block_placement():
    # POSITION-ONLY by design: resting sells on a shared conduit are
    # mostly SIBLING slices' stops (2026-06-09 isolation invariant —
    # sibling coverage must never block our placement), and counting
    # our own old stop would refuse every replace/tighten. 100 long
    # with 100 already reserved by others still backs OUR 47.
    api = FakeApi(positions={"NVDA": 100},
                  open_orders=[FakeOrder("sell", 100)])
    order = _submit_protective(
        api, {"symbol": "NVDA", "side": "sell", "qty": 47,
              "type": "trailing_stop", "trail_percent": 5},
        "x.db", "NVDA", "trailing stop for NVDA")
    assert order is not None
    # but a stop LARGER than the whole live position is never backed
    api2 = FakeApi(positions={"NVDA": 100})
    assert _submit_protective(
        api2, {"symbol": "NVDA", "side": "sell", "qty": 101,
               "type": "trailing_stop", "trail_percent": 5},
        "x.db", "NVDA", "trailing stop for NVDA") is None


def test_buy_protective_needs_broker_short():
    api = FakeApi(positions={"TSLA": -50})
    assert _submit_protective(
        api, {"symbol": "TSLA", "side": "buy", "qty": 50,
              "type": "trailing_stop", "trail_percent": 5},
        "x.db", "TSLA", "cover stop for TSLA") is not None
    # wiped short: buy stop would mint an involuntary LONG
    api2 = FakeApi(positions={})
    assert _submit_protective(
        api2, {"symbol": "AMZN", "side": "buy", "qty": 25,
               "type": "trailing_stop", "trail_percent": 5},
        "x.db", "AMZN", "cover stop for AMZN") is None
    assert api2.submitted == []


def test_unreadable_position_fails_closed():
    api = FakeApi(position_error=True)
    backed, detail = _broker_backs_protective(api, "GE", "sell", 10)
    assert backed is False
    assert "unreadable" in detail


def test_open_orders_endpoint_never_consulted():
    # The gate is position-only — it must not even READ resting orders
    # (a broken orders endpoint can't affect placement, and there's no
    # path for sibling reservations to leak into the decision).
    api = FakeApi(positions={"KO": 50}, orders_error=True)
    backed, _ = _broker_backs_protective(api, "KO", "sell", 50)
    assert backed is True


def test_occ_symbols_skip_the_gate():
    # option legs carry Alpaca-side position_intent enforcement
    api = FakeApi(positions={})
    order = _submit_protective(
        api, {"symbol": "NFLX260117C01000000", "side": "sell", "qty": 1,
              "type": "limit", "limit_price": 1.0},
        "x.db", "NFLX260117C01000000", "leg close")
    assert order is not None


# ------------------------------------------------- revocation sweep

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "profile_test.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, timestamp TEXT, "
        "symbol TEXT, side TEXT, qty REAL, price REAL, order_id TEXT, "
        "status TEXT, occ_symbol TEXT, data_quality TEXT, pnl REAL, "
        "protective_stop_order_id TEXT, protective_tp_order_id TEXT, "
        "protective_trailing_order_id TEXT)")
    conn.commit()
    conn.close()
    return path


def _add(path, ts, symbol, side, qty, order_id, status, trail_ptr=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "order_id, status, protective_trailing_order_id) "
        "VALUES (?, ?, ?, ?, 100.0, ?, ?, ?)",
        (ts, symbol, side, qty, order_id, status, trail_ptr))
    conn.commit()
    conn.close()


T0 = "2026-07-06T13:31:00"
T1 = "2026-07-06T13:32:00"


def test_wipe_shape_revokes_own_protectives_and_alerts(db):
    # RIVN incident shape: open long 690, broker flat overnight, a
    # resting trailing stop + its pending_protective row. Revoke, NULL
    # the pointer, terminate the row, write the audit alert.
    _add(db, T0, "RIVN", "buy", 690, "entryRIVN", "open",
         trail_ptr="stopRIVN1")
    _add(db, T1, "RIVN", "sell", 690, "stopRIVN1", "pending_protective")
    api = FakeApi(positions={},  # 404 -> flat
                  orders_by_id={"entryRIVN": _filled_entry("buy", 690),
                                "stopRIVN1": FakeOrder("sell", 690)})
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == ["stopRIVN1"]
    assert out["canceled"] == 1 and out["alerts"] == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    pend = conn.execute("SELECT status, pnl FROM trades "
                        "WHERE order_id='stopRIVN1' AND side='sell'"
                        ).fetchone()
    assert pend["status"] == "canceled" and pend["pnl"] is None
    entry = conn.execute("SELECT status, protective_trailing_order_id "
                         "FROM trades WHERE order_id='entryRIVN'"
                         ).fetchone()
    # entry NOT modified (integrity gate owns the halt/repair), only
    # the dangling pointer cleared
    assert entry["status"] == "open"
    assert entry["protective_trailing_order_id"] is None
    alert = conn.execute("SELECT alert_type, severity FROM audit_alerts"
                         ).fetchone()
    assert alert["alert_type"] == "broker_backing_revoked"
    assert alert["severity"] == "critical"
    conn.close()


def test_own_exit_in_flight_is_not_a_wipe(db):
    # We SOLD it ourselves (verified own fill), entry flip lagging —
    # the reconciler flap class, not a wipe. Nothing cancelled;
    # cancel-on-close cleans up after the flip.
    _add(db, T0, "MSFT", "buy", 13, "entryMSFT", "open")
    _add(db, T1, "MSFT", "sell", 13, "exitMSFT1", "pending_fill")
    _add(db, T1, "MSFT", "sell", 13, "stopMSFT1", "pending_protective")
    api = FakeApi(
        positions={},
        orders_by_id={
            "entryMSFT": _filled_entry("buy", 13),
            "exitMSFT1": FakeOrder("sell", 13, status="filled"),
            "stopMSFT1": FakeOrder("sell", 13),
        })
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == []
    assert out["skipped_own_exit"] == 1


def test_partial_broker_position_never_cancels(db):
    # broker holds SOME (50 of 100): could be an in-flight own fill —
    # cancelling live protection on ambiguity is the naked-position
    # class. Integrity gate owns drift; we do not touch orders.
    _add(db, T0, "PFE", "buy", 100, "entryPFE", "open")
    _add(db, T1, "PFE", "sell", 100, "stopPFE1", "pending_protective")
    api = FakeApi(positions={"PFE": 50},
                  orders_by_id={"stopPFE1": FakeOrder("sell", 100)})
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == []
    assert out["canceled"] == 0


def test_backed_position_untouched(db):
    _add(db, T0, "WMT", "buy", 18, "entryWMT", "open")
    _add(db, T1, "WMT", "sell", 18, "stopWMT1", "pending_protective")
    api = FakeApi(positions={"WMT": 18},
                  orders_by_id={"stopWMT1": FakeOrder("sell", 18)})
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == [] and out["canceled"] == 0


def test_short_direction_production_buy_shape(db):
    # SHORT protectives are journaled side='buy' (bracket_orders
    # close_side — the production shape). Wiped short (broker flat) →
    # the resting buy stop would mint an involuntary LONG; revoke it.
    _add(db, T0, "AMZN", "short", 25, "entryAMZN", "open")
    _add(db, T1, "AMZN", "buy", 25, "stopAMZN1", "pending_protective")
    api = FakeApi(positions={},
                  orders_by_id={"entryAMZN": _filled_entry("sell", 25),
                                "stopAMZN1": FakeOrder("buy", 25)})
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == ["stopAMZN1"]
    assert out["canceled"] == 1
    # backed short: untouched
    _add(db, T0, "ORCL", "short", 115, "entryORCL", "open")
    _add(db, T1, "ORCL", "buy", 115, "stopORCL1", "pending_protective")
    api2 = FakeApi(positions={"ORCL": -115},
                   orders_by_id={"entryORCL": _filled_entry("sell", 115),
                                 "stopORCL1": FakeOrder("buy", 115)})
    out2 = revoke_unbacked_protective_orders(api2, db)
    assert "stopORCL1" not in api2.canceled


def test_fully_filled_stop_is_the_flap_class_not_a_wipe(db):
    # The stop FIRED and fully explains the flatness — that's the
    # entry-flip-lag class the own-exit gate owns. Nothing cancelled,
    # nothing terminated; the fill state machine flips the rows.
    _add(db, T0, "VZ", "buy", 213, "entryVZ", "open")
    _add(db, T1, "VZ", "sell", 213, "stopVZ1", "pending_protective")
    api = FakeApi(positions={},
                  orders_by_id={"entryVZ": _filled_entry("buy", 213),
                                "stopVZ1": FakeOrder("sell", 213,
                                                     status="filled")})
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == []
    assert out["skipped_own_exit"] == 1
    conn = sqlite3.connect(db)
    st = conn.execute("SELECT status FROM trades WHERE "
                      "order_id='stopVZ1' AND side='sell'").fetchone()[0]
    assert st == "pending_protective"
    conn.close()


def test_partially_filled_stop_kept_but_unfilled_sibling_revoked(db):
    # filled_kept backstop: a stop that PARTIALLY fired (fill can't
    # explain the full entry) is left for the fill state machine while
    # an unfilled sibling stop on the same wiped symbol IS revoked.
    _add(db, T0, "NOK", "buy", 4658, "entryNOK", "open")
    _add(db, T1, "NOK", "sell", 1000, "stopNOK1", "pending_protective")
    _add(db, T1, "NOK", "sell", 3658, "stopNOK2", "pending_protective")
    api = FakeApi(positions={},
                  orders_by_id={
                      "entryNOK": _filled_entry("buy", 4658),
                      "stopNOK1": FakeOrder("sell", 1000, status="filled"),
                      "stopNOK2": FakeOrder("sell", 3658),
                  })
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == ["stopNOK2"]
    assert out["filled_kept"] == 1 and out["canceled"] == 1
    conn = sqlite3.connect(db)
    st1 = conn.execute("SELECT status FROM trades WHERE "
                       "order_id='stopNOK1' AND side='sell'").fetchone()[0]
    st2 = conn.execute("SELECT status FROM trades WHERE "
                       "order_id='stopNOK2' AND side='sell'").fetchone()[0]
    assert st1 == "pending_protective" and st2 == "canceled"
    conn.close()


def test_unreadable_broker_skips_symbol(db):
    _add(db, T0, "GE", "buy", 40, "entryGE", "open")
    _add(db, T1, "GE", "sell", 40, "stopGE1", "pending_protective")
    api = FakeApi(position_error=True,
                  orders_by_id={"stopGE1": FakeOrder("sell", 40)})
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == []
    assert out["skipped_unreadable"] == 1


def test_only_own_journal_order_ids_ever_touched(db):
    # A1/order-id truth: the sweep must be structurally incapable of
    # cancelling an id that is not in this profile's own journal.
    _add(db, T0, "F", "buy", 816, "entryF", "open", trail_ptr="stopF1")
    _add(db, T1, "F", "sell", 816, "stopF1", "pending_protective")
    api = FakeApi(positions={},
                  orders_by_id={"entryF": _filled_entry("buy", 816),
                                "stopF1": FakeOrder("sell", 816)})
    revoke_unbacked_protective_orders(api, db)
    assert set(api.canceled) <= {"stopF1"}


def test_pending_bracket_entry_never_revoked(db):
    # Round-1 CRITICAL C2: a bracket entry that hasn't FILLED yet has
    # the exact journal wipe shape — open entry, pending_protective
    # children, broker flat — because the position doesn't exist YET.
    # Cancelling its held children would strip protection from a
    # position about to exist. Entry order 'new' at broker → skip.
    _add(db, T0, "GOOGL", "buy", 16, "entryGOOG", "open",
         trail_ptr="childGOOG1")
    _add(db, T1, "GOOGL", "sell", 16, "childGOOG1", "pending_protective")
    api = FakeApi(positions={},
                  orders_by_id={
                      "entryGOOG": FakeOrder("buy", 16, status="new"),
                      "childGOOG1": FakeOrder("sell", 16, status="held"),
                  })
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == []
    assert out["canceled"] == 0 and out["alerts"] == 0
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='pending_protective'"
    ).fetchone()[0] == 1
    conn.close()


def test_entry_without_order_id_never_revoked(db):
    # Unverifiable entry (no own order_id): a position can't be proven
    # to ever have existed — never cancel on a guess.
    _add(db, T0, "T", "buy", 30, None, "open")
    _add(db, T1, "T", "sell", 30, "stopT0001", "pending_protective")
    api = FakeApi(positions={},
                  orders_by_id={"stopT0001": FakeOrder("sell", 30)})
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == []
    assert out["canceled"] == 0


def test_unconfirmed_cancel_leaves_journal_intact(db):
    # Round-1 CRITICAL C3: the cancel didn't take at the broker (order
    # still live on re-read). Journal must stay untouched — pointer,
    # pending row, everything — so next pass retries; and the state is
    # still alert-loud. Mutating on the lenient cancel bool would let
    # the order rest WITH ITS TEETH while the journal forgets it.
    _add(db, T0, "CRCL", "buy", 654, "entryCRCL", "open",
         trail_ptr="stopCRCL1")
    _add(db, T1, "CRCL", "sell", 654, "stopCRCL1", "pending_protective")
    api = FakeApi(positions={}, cancel_sticks=False,
                  orders_by_id={
                      "entryCRCL": _filled_entry("buy", 654),
                      "stopCRCL1": FakeOrder("sell", 654),
                  })
    out = revoke_unbacked_protective_orders(api, db)
    assert api.canceled == ["stopCRCL1"]  # attempted
    assert out["canceled"] == 0 and out["cancel_pending"] == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    pend = conn.execute("SELECT status FROM trades WHERE "
                        "order_id='stopCRCL1' AND side='sell'").fetchone()
    assert pend["status"] == "pending_protective"  # untouched → retried
    entry = conn.execute("SELECT protective_trailing_order_id FROM trades "
                         "WHERE order_id='entryCRCL'").fetchone()
    assert entry["protective_trailing_order_id"] == "stopCRCL1"
    assert conn.execute("SELECT COUNT(*) FROM audit_alerts").fetchone()[0] \
        == 1  # still alert-loud
    conn.close()
    # next pass with a broker that now honors cancels → completes
    api2 = FakeApi(positions={},
                   orders_by_id={
                       "entryCRCL": _filled_entry("buy", 654),
                       "stopCRCL1": FakeOrder("sell", 654),
                   })
    out2 = revoke_unbacked_protective_orders(api2, db)
    assert out2["canceled"] == 1 and out2["cancel_pending"] == 0


# ------------------------------------------------- structural pins

def _src(name):
    return open(os.path.join(os.path.dirname(__file__), os.pardir,
                             name)).read()


def test_submit_protective_gates_before_submitting():
    src = _src("bracket_orders.py")
    body = src.split("def _submit_protective(")[1].split("\ndef ")[0]
    gate = body.find("_broker_backs_protective(")
    submit = body.find("api.submit_order(")
    assert 0 < gate < submit, (
        "_submit_protective must run the broker-backing gate BEFORE "
        "any submit — protectives bypass the oversell door, so this "
        "is their only pre-flight check (2026-07-07 wipe class)")


def test_reconcile_task_runs_revocation_after_cancel_on_close():
    src = _src("multi_scheduler.py")
    task = src.split("def _task_reconcile_trade_statuses(")[1]
    task = task.split("\ndef ")[0]
    coc = task.find("cancel_orphaned_protective_orders")
    rev = task.find("revoke_unbacked_protective_orders")
    assert 0 < coc < rev, (
        "per-cycle revocation must run in _task_reconcile_trade_"
        "statuses after cancel-on-close (journal-closed and broker-"
        "wiped are complementary halves of the same leak)")


def test_preopen_disarm_fires_inside_the_parked_sleep_loop():
    # Round-1 CRITICAL C1: a market-hours-only fleet spends every
    # night/weekend parked in the inner closed-market sleep loop and
    # exits only AT the open — a disarm call gated on the outer
    # iteration alone is dead code in exactly the incident window.
    # The call must live INSIDE the 60s sleep loop, between the
    # is_market_open break and the sleep.
    src = _src("multi_scheduler.py")
    loop = src.split("def main_loop(")[1]
    assert loop.count("_preopen_disarm_sweep(now)") >= 2, (
        "disarm must be called from BOTH the outer profile branch and "
        "the inner closed-market sleep loop")
    # the sleep-loop block: break-at-open, then disarm, then sleep(60).
    # Window widened 2000→5000 (2026-07-25): the closed-market
    # housekeeping block now sits between the break and the disarm.
    sleep_block = loop.split("Market closed, sleeping until")[1][:5000]
    brk = sleep_block.find("break")
    disarm = sleep_block.find("_preopen_disarm_sweep(now)")
    slp = sleep_block.find("time.sleep(60)")
    assert 0 < brk < disarm < slp, (
        "the parked sleep loop must check the disarm window on every "
        "60s wake-up — otherwise overnight wipes are never disarmed "
        "before stops can fire at 09:30")
    helper = src.split("def _preopen_disarm_sweep(")[1].split("\ndef ")[0]
    assert "next_market_open" in helper
    assert "_last_preopen_disarm_date" in helper  # once per market day
    assert "revoke_unbacked_protective_orders" in helper
