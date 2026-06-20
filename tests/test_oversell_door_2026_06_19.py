"""2026-06-19 — THE DOOR: the single, unbypassable oversell gate.

The 2026-06-18 phantom equity happened because a re-armed protective SELL
reached the broker through a path (bracket_orders) that had no oversell
guard, and filled as a real unowned short. The guard that would have
refused it (order_guard.allowable_sell_qty — "a profile may sell only what
its OWN journal holds") already existed, but was wired into only the
AI-driven exit paths. The fix wraps the single per-profile api factory
(user_context.get_alpaca_api) so EVERY submit_order — protective sweep,
stat-arb, delta hedger, anything — passes through the gate.

These tests prove: a naked sell on a flat book is REFUSED before the broker
sees it; a genuine close passes; a deliberate short (intent='open_short')
passes; the internal intent marker never reaches the broker; and the gate
reads ONLY this profile's own journal (never the shared-account aggregate).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


class _Order:
    def __init__(self, oid="x"):
        self.id = oid
        self.order_class = "simple"
        self.legs = []
        self.status = "new"
        self.replaces = None


class FakeBroker:
    """Records submit/cancel; never a matching engine. list_positions
    returns a deliberately HUGE aggregate to prove the door ignores it —
    the door must read the profile's own journal, not the broker pool."""
    def __init__(self, aggregate=None):
        self.submitted = []
        self.canceled = []
        self._aggregate = aggregate or []

    def submit_order(self, **kwargs):
        self.submitted.append(kwargs)
        return _Order("sub-%d" % len(self.submitted))

    def cancel_order(self, order_id):
        self.canceled.append(order_id)

    def list_orders(self, **kwargs):
        return []

    def get_order(self, order_id, nested=False):
        return _Order(order_id)

    def list_positions(self):
        return list(self._aggregate)

    def get_account(self):
        class _A:
            equity = buying_power = cash = portfolio_value = 0.0
            status = "ACTIVE"
        return _A()


class _Ctx:
    def __init__(self, db, name="exp"):
        self.db_path = db
        self.display_name = name
        self.is_virtual = True


def _seed(db, rows):
    from journal import init_db
    init_db(db)
    c = sqlite3.connect(db)
    for i, (sym, side, qty, px, status) in enumerate(rows):
        c.execute(
            "INSERT INTO trades (timestamp,symbol,side,qty,price,fill_price,"
            "signal_type,status,order_id) VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-06-19T13:00:0%d" % (i % 10), sym, side, qty, px, px,
             "BUY", status, "oid-%d" % i))
    c.commit()
    c.close()


# ── the incident, prevented ────────────────────────────────────────────

def test_door_refuses_rearmed_naked_sell(tmp_path):
    """The exact phantom vector: UWMC bought then fully closed (own journal
    long = 0). A re-armed protective SELL must be REFUSED before the broker
    sees it."""
    from order_guard import assert_sell_within_own_book, OversellGuardError
    db = str(tmp_path / "quantopsai_profile_1.db")
    _seed(db, [
        ("UWMC", "buy", 20634, 2.47, "closed"),
        ("UWMC", "sell", 20634, 2.29, "closed"),   # flat now
    ])
    api = FakeBroker()
    kwargs = {"symbol": "UWMC", "qty": 20634, "side": "sell", "type": "stop"}
    with pytest.raises(OversellGuardError):
        assert_sell_within_own_book(api, _Ctx(db), kwargs)
    assert api.submitted == []  # never reached the broker


def test_guarded_api_blocks_naked_sell_end_to_end(tmp_path):
    """Through the actual GuardedAlpacaApi wrapper: a naked sell raises and
    nothing is recorded by the broker stub."""
    from order_guard import guarded_api, OversellGuardError
    db = str(tmp_path / "quantopsai_profile_2.db")
    _seed(db, [("UWMC", "buy", 100, 2.0, "closed"),
               ("UWMC", "sell", 100, 2.1, "closed")])
    api = guarded_api(FakeBroker(), _Ctx(db))
    with pytest.raises(OversellGuardError):
        api.submit_order(symbol="UWMC", qty=100, side="sell", type="market")
    assert api.unwrapped.submitted == []


def test_door_ignores_broker_aggregate_reads_only_own_journal(tmp_path):
    """The crux of profile isolation: even if the shared Alpaca account
    holds a HUGE aggregate of UWMC (siblings' longs), this profile — which
    holds 0 in its own journal — still cannot sell it."""
    from order_guard import assert_sell_within_own_book, OversellGuardError

    class _Pos:
        symbol = "UWMC"
        qty = 999999  # the account aggregate across sibling profiles
    db = str(tmp_path / "quantopsai_profile_3.db")
    _seed(db, [("UWMC", "buy", 50, 2.0, "closed"),
               ("UWMC", "sell", 50, 2.1, "closed")])  # own = 0
    api = FakeBroker(aggregate=[_Pos()])
    with pytest.raises(OversellGuardError):
        assert_sell_within_own_book(
            api, _Ctx(db), {"symbol": "UWMC", "qty": 100, "side": "sell"})


# ── genuine flows pass ─────────────────────────────────────────────────

def test_door_allows_genuine_close(tmp_path):
    """Selling exactly what the profile holds is a real close — allowed."""
    from order_guard import guarded_api
    db = str(tmp_path / "quantopsai_profile_4.db")
    _seed(db, [("HELD", "buy", 100, 10.0, "open")])
    api = guarded_api(FakeBroker(), _Ctx(db))
    api.submit_order(symbol="HELD", qty=100, side="sell", type="market")
    assert any(o["symbol"] == "HELD" for o in api.unwrapped.submitted)


def test_door_allows_declared_short(tmp_path):
    """A deliberate short entry (intent='open_short') on a flat book is
    allowed — and the intent marker is STRIPPED before the broker sees it."""
    from order_guard import guarded_api
    db = str(tmp_path / "quantopsai_profile_5.db")
    _seed(db, [])  # holds nothing
    api = guarded_api(FakeBroker(), _Ctx(db))
    api.submit_order(symbol="TSLA", qty=10, side="sell", type="market",
                     intent="open_short")
    sub = api.unwrapped.submitted
    assert len(sub) == 1 and sub[0]["symbol"] == "TSLA"
    assert "intent" not in sub[0], "intent must never reach the broker"


def test_door_refuses_undeclared_oversell(tmp_path):
    """Holds 50, tries to sell 100 with no short intent — refused (a close
    must not exceed the held qty)."""
    from order_guard import assert_sell_within_own_book, OversellGuardError
    db = str(tmp_path / "quantopsai_profile_6.db")
    _seed(db, [("ABC", "buy", 50, 5.0, "open")])
    with pytest.raises(OversellGuardError):
        assert_sell_within_own_book(
            FakeBroker(), _Ctx(db), {"symbol": "ABC", "qty": 100,
                                     "side": "sell"})


def test_door_skips_options_and_buys(tmp_path):
    """Options carry their own intent enforcement; buys can't oversell a
    long. Neither is gated by the stock sell door."""
    from order_guard import assert_sell_within_own_book
    db = str(tmp_path / "quantopsai_profile_7.db")
    _seed(db, [])
    # an option sell (OCC symbol) — not gated
    assert_sell_within_own_book(
        FakeBroker(), _Ctx(db),
        {"symbol": "SPY260116C00500000", "qty": 2, "side": "sell"})
    # a buy — not gated
    assert_sell_within_own_book(
        FakeBroker(), _Ctx(db), {"symbol": "ZZZ", "qty": 100, "side": "buy"})


def test_guarded_api_delegates_other_methods(tmp_path):
    """Everything except submit_order delegates straight through."""
    from order_guard import guarded_api
    db = str(tmp_path / "quantopsai_profile_8.db")
    _seed(db, [])
    api = guarded_api(FakeBroker(), _Ctx(db))
    assert api.list_orders() == []
    assert api.get_account().status == "ACTIVE"
    assert api.list_positions() == []


def test_guarded_api_is_idempotent(tmp_path):
    from order_guard import guarded_api, GuardedAlpacaApi
    db = str(tmp_path / "quantopsai_profile_9.db")
    _seed(db, [])
    once = guarded_api(FakeBroker(), _Ctx(db))
    twice = guarded_api(once, _Ctx(db))
    assert twice is once and isinstance(once, GuardedAlpacaApi)


# ── structural: the door cannot be bypassed ────────────────────────────

def test_factory_returns_guarded_api():
    """user_context.get_alpaca_api must wrap its client in the door — the
    single point that makes the gate universal. (Source-level check so we
    don't need real Alpaca credentials.)"""
    import inspect
    import user_context
    src = inspect.getsource(user_context.UserContext.get_alpaca_api)
    assert "guarded_api(" in src, (
        "get_alpaca_api must return guarded_api(...) — otherwise order "
        "paths get an unguarded client and the door is bypassable")


def test_order_modules_do_not_build_their_own_rest_client():
    """No order-submitting module may construct its own tradeapi.REST — it
    must receive `api` from the guarded factory. A new raw client would be
    an unguarded door (the exact bypass this fix closes).

    The list of order modules is DERIVED (every module that actually calls
    .submit_order, minus the factory/data files), so a newly-added order
    module — or multi_scheduler, or a pipelines/ module — can't silently
    escape coverage. (Fix-the-class: pin the contract, not a fixed list.)"""
    import os
    import re
    import glob
    # Files that LEGITIMATELY build a raw REST: the guarded factory itself
    # and pure data / credential-resolution paths (no order submission).
    factory_or_data = {
        "order_guard.py", "user_context.py", "client.py", "segments.py",
        "market_data.py", "views.py",
    }
    candidates = (glob.glob(os.path.join(str(REPO), "*.py"))
                  + glob.glob(os.path.join(str(REPO), "pipelines", "*.py")))
    order_modules = []
    for path in candidates:
        base = os.path.basename(path)
        if base.startswith("test_") or base in factory_or_data:
            continue
        code = "\n".join(line.split("#", 1)[0]
                         for line in Path(path).read_text().splitlines())
        if re.search(r"\.submit_order\s*\(", code):
            order_modules.append(path)
    assert order_modules, "scanner found no order modules — likely broken"
    # multi_scheduler must be among them (it has a real submit_order) —
    # the omission the review caught.
    assert any(os.path.basename(p) == "multi_scheduler.py"
               for p in order_modules), order_modules
    offenders = []
    for path in order_modules:
        code = "\n".join(line.split("#", 1)[0]
                         for line in Path(path).read_text().splitlines())
        if re.search(r"\btradeapi\.REST\s*\(", code) or \
           re.search(r"(?<![\w.])REST\s*\(", code):
            offenders.append(os.path.basename(path))
    assert offenders == [], (
        "these order modules build their own broker client, bypassing the "
        "oversell door: %s" % offenders)


def test_door_refuses_positional_submit(tmp_path):
    """A positional submit_order(symbol, qty, side) would let `side` slip
    past the kwargs-only gate — it must be refused outright."""
    from order_guard import guarded_api
    db = str(tmp_path / "quantopsai_profile_p.db")
    _seed(db, [])
    api = guarded_api(FakeBroker(), _Ctx(db))
    with pytest.raises(TypeError):
        api.submit_order("AAPL", 100, "sell")  # positional → blocked
    assert api.unwrapped.submitted == []


def test_no_ctx_client_refuses_order_submission(tmp_path):
    """The no-ctx client (client.get_api(None)) is for read-only data; it
    has no journal to oversell-check against, so submit_order must refuse
    rather than send an unchecked order."""
    from order_guard import guarded_api, OversellGuardError
    api = guarded_api(FakeBroker(), None)
    assert api.list_positions() == []  # reads still delegate
    with pytest.raises(OversellGuardError):
        api.submit_order(symbol="AAPL", qty=100, side="sell", type="market")
