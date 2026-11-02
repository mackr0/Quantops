"""2026-11-01 — broker FEEs booked per book, and the decomposition
identity made price-free with a $5 gate.

Fees: Alpaca charges account-level FEE activities whose descriptions
name the base ("REG fee for proceed of $25811.31 on 2026-10-23", "CAT
fee for proceed of 6 trades…", "ORF fee for proceed of 2 contracts…",
"OPT TAF fee for proceed of 2 contracts (2 trades)…", "OPT REG fee for
proceed of $1365…", flat "OCC Clearing Fee"). The broker's own FILL
activities for the day reproduce every base (verified on the real
2026-10-23 account-61 day), and each fill maps to one book by its
order_id — so a fee is split pro-rata on the base the broker names,
from the broker's fills, refused (alert) when the books do not
reproduce the base or a fill belongs to no book.

Identity: equity − initial − unrealized = cash − initial + Σ cost
basis — no price in it. The old form compared two price reads and
needed a $250 tolerance that hid a real +$136.92 error for ten days.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from types import SimpleNamespace

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, REPO)

import activities_capture as ac  # noqa: E402


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
                price REAL, order_id TEXT, signal_type TEXT, strategy TEXT,
                reason TEXT, ai_reasoning TEXT, ai_confidence REAL,
                stop_loss REAL, take_profit REAL, status TEXT DEFAULT 'open',
                pnl REAL, decision_price REAL, fill_price REAL,
                slippage_pct REAL, occ_symbol TEXT, option_strategy TEXT,
                expiry TEXT, strike REAL, predicted_slippage_bps REAL,
                adv_at_decision REAL, data_quality TEXT
            );
        """)
    return str(path)


def _seed(db, symbol, side, qty, price, status="closed", occ=None,
          ts="2026-10-23T14:00:00"):
    oid = str(uuid.uuid4())
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "fill_price, status, order_id, occ_symbol) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, symbol, side, qty, price, price, status, oid, occ))
        conn.commit()
    return oid


def _fill(order_id, symbol, side, qty, price):
    return SimpleNamespace(id=str(uuid.uuid4()), activity_type="FILL",
                           order_id=order_id, symbol=symbol, side=side,
                           qty=qty, price=price,
                           transaction_time="2026-10-23T14:00:00Z")


def _fee(aid, net, description, date="2026-10-23"):
    return SimpleNamespace(id=aid, activity_type="FEE", net_amount=net,
                           description=description, date=date, symbol=None,
                           qty=None)


class _Api:
    def __init__(self, fees=(), fills=(), fail_fills=False):
        self.fees, self.fills, self.fail_fills = list(fees), list(fills), fail_fills

    orders = []

    def list_orders(self, **kw):
        return list(self.orders)

    def get_activities(self, activity_types=None, **kw):
        if activity_types == "FEE":
            return list(self.fees)
        if activity_types == "FILL":
            if self.fail_fills:
                raise RuntimeError("rate limit exceeded")
            return list(self.fills)
        return []


def _ctx(pid, db, api, aid=61):
    return SimpleNamespace(profile_id=pid, db_path=db, api=api,
                           alpaca_account_id=aid, display_name=f"P{pid}",
                           is_virtual=True, initial_capital=100_000.0,
                           get_alpaca_api=lambda: api)


def _rows(db, where="1=1"):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM trades WHERE {where} ORDER BY id")]


def _alerts(db):
    try:
        with sqlite3.connect(db) as conn:
            return conn.execute("SELECT alert_type FROM audit_alerts").fetchall()
    except sqlite3.OperationalError:
        return []


def _identity_gap(db, initial=100_000.0, price=0.0):
    from journal import get_virtual_account_info, get_virtual_positions
    pf = lambda s: price  # noqa: E731
    info = get_virtual_account_info(db_path=db, initial_capital=initial, price_fetcher=pf)
    upl = sum(float(p.get("unrealized_pl") or 0)
              for p in get_virtual_positions(db_path=db, price_fetcher=pf))
    with sqlite3.connect(db) as conn:
        realized = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE pnl IS NOT NULL "
            "AND COALESCE(status,'') NOT IN ('canceled','expired','rejected')"
        ).fetchone()[0]
    return round((info["equity"] - initial) - (realized + upl), 2)


@pytest.fixture(autouse=True)
def _fresh():
    ac.clear_memos()
    yield
    ac.clear_memos()


@pytest.fixture
def two_books(tmp_path, monkeypatch):
    a, b = _make_db(tmp_path / "a.db"), _make_db(tmp_path / "b.db")
    monkeypatch.setattr(ac, "_sibling_db_paths", lambda ctx: [(1, a), (2, b)])
    return a, b


def _run(api, *ctxs):
    return [ac.capture_activities_for_profile(c) for c in ctxs]


# ── shapes ──────────────────────────────────────────────────────────

class TestFeeShapes:
    @pytest.mark.parametrize("desc,kind,base,day", [
        ("REG fee for proceed of $25811.31 on 2026-10-23 by PA3UTOJ1X0VK", "REG", 25811.31, "2026-10-23"),
        ("OPT REG fee for proceed of $1365 on 2026-10-30 by PA3UTOJ1X0VK", "OPT_REG", 1365.0, "2026-10-30"),
        ("CAT fee for proceed of 6 trades on 2026-10-23 by PA3UTOJ1X0VK", "CAT", 6.0, "2026-10-23"),
        ("OPT TAF fee for proceed of 2 contracts (2 trades) on 2026-10-30 by PA3UTOJ1X0VK", "OPT_TAF", 2.0, "2026-10-30"),
        ("ORF fee for proceed of 2 contracts on 2026-10-30 by PA3UTOJ1X0VK", "ORF", 2.0, "2026-10-30"),
        ("OCC Clearing Fee", "OCC", None, "2026-10-30"),
        ("TAF fee for proceed of 1332 shares (29 trades) on 2026-09-29 by PA3UTOJ1X0VK", "TAF", 1332.0, "2026-09-29"),
    ])
    def test_known_shapes(self, desc, kind, base, day):
        assert ac._fee_shape(desc, "2026-10-30") == (kind, base, day)

    def test_unknown_shape_is_none(self):
        assert ac._fee_shape("Wire transfer fee", "2026-10-30") is None

    def test_handled(self):
        assert "FEE" in ac._HANDLED_TYPES


# ── attribution ─────────────────────────────────────────────────────

class TestRegFeeBySellProceeds:
    def test_split_to_the_cent_and_identity_holds(self, two_books):
        a, b = two_books
        oa = _seed(a, "XOM", "sell", 100, 50.0)
        ob = _seed(b, "CVX", "sell", 300, 50.0)
        api = _Api(fees=[_fee("fee-reg", -0.40,
                              "REG fee for proceed of $20000 on 2026-10-23 by PA3U")],
                   fills=[_fill(oa, "XOM", "sell", 100, 50.0),
                          _fill(ob, "CVX", "sell", 300, 50.0)])
        sa, sb = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        assert (sa["FEE"], sb["FEE"]) == (1, 1)
        ra = _rows(a, "order_id='fee-reg'")[0]
        rb = _rows(b, "order_id='fee-reg'")[0]
        assert (ra["side"], ra["price"], ra["pnl"], ra["signal_type"]) == ("cash_debit", 0.10, -0.10, "FEE")
        assert (rb["side"], rb["price"], rb["pnl"]) == ("cash_debit", 0.30, -0.30)
        assert round(ra["price"] + rb["price"], 2) == 0.40
        assert _identity_gap(a) == 0.0 and _identity_gap(b) == 0.0
        # idempotent
        _run(api, _ctx(1, a, api))
        assert len(_rows(a, "order_id='fee-reg'")) == 1

    def test_buys_do_not_carry_the_sales_fee(self, two_books):
        a, b = two_books
        oa = _seed(a, "XOM", "sell", 100, 50.0)
        ob = _seed(b, "CVX", "buy", 300, 50.0, status="open")
        api = _Api(fees=[_fee("fee-reg2", -0.10,
                              "REG fee for proceed of $5000 on 2026-10-23 by PA3U")],
                   fills=[_fill(oa, "XOM", "sell", 100, 50.0),
                          _fill(ob, "CVX", "buy", 300, 50.0)])
        sa, sb = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        assert (sa["FEE"], sb["FEE"]) == (1, 0)
        assert _rows(a, "order_id='fee-reg2'")[0]["price"] == 0.10
        assert _rows(b, "order_id='fee-reg2'") == []


class TestCountBasedFees:
    def test_cat_counts_every_fill_event_stock_and_option(self, two_books):
        a, b = two_books
        o1 = _seed(a, "BA", "sell", 66, 183.0)
        o2 = _seed(a, "AMT", "sell", 70, 195.96)
        o3 = _seed(a, "NVDA", "buy", 1, 11.15, occ="NVDA261218C00220000", status="open")
        o4 = _seed(b, "CRH", "buy", 155, 81.75, status="open")
        fills = [_fill(o1, "BA", "sell", 49, 183.27), _fill(o1, "BA", "sell", 17, 183.2),
                 _fill(o2, "AMT", "sell", 70, 195.96),
                 _fill(o3, "NVDA261218C00220000", "buy", 1, 11.15),
                 _fill(o4, "CRH", "buy", 155, 81.75)]
        api = _Api(fees=[_fee("fee-cat", -0.01,
                              "CAT fee for proceed of 5 trades on 2026-10-23 by PA3U")],
                   fills=fills)
        sa, sb = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        # one cent, largest remainder → the book with 4 of 5 fills
        assert (sa["FEE"], sb["FEE"]) == (1, 0)
        assert _rows(a, "order_id='fee-cat'")[0]["price"] == 0.01

    def test_orf_and_occ_by_option_contracts(self, two_books):
        a, b = two_books
        oa = _seed(a, "CF", "buy", 2, 4.6, occ="CF261120C00120000", status="open")
        ob = _seed(b, "GM", "sell", 1, 2.71, occ="GM261218C00080000", status="open")  # sell-to-open
        fills = [_fill(oa, "CF261120C00120000", "buy", 2, 4.6),
                 _fill(ob, "GM261218C00080000", "sell", 1, 2.71)]
        api = _Api(fees=[_fee("fee-orf", -0.05, "ORF fee for proceed of 3 contracts on 2026-10-23 by PA3U"),
                         _fee("fee-occ", -0.03, "OCC Clearing Fee"),
                         _fee("fee-taf", -0.01, "OPT TAF fee for proceed of 1 contracts (1 trades) on 2026-10-23 by PA3U"),
                         _fee("fee-oreg", -0.01, "OPT REG fee for proceed of $271 on 2026-10-23 by PA3U")],
                   fills=fills)
        _run(api, _ctx(1, a, api), _ctx(2, b, api))
        get = lambda db, oid: _rows(db, f"order_id='{oid}'")  # noqa: E731
        assert get(a, "fee-orf")[0]["price"] == 0.03 and get(b, "fee-orf")[0]["price"] == 0.02
        assert get(a, "fee-occ")[0]["price"] == 0.02 and get(b, "fee-occ")[0]["price"] == 0.01
        # TAF and OPT REG are on SALES only → the seller's book alone
        assert get(a, "fee-taf") == [] and get(b, "fee-taf")[0]["price"] == 0.01
        assert get(a, "fee-oreg") == [] and get(b, "fee-oreg")[0]["price"] == 0.01
        assert _identity_gap(a, price=4.6) == 0.0 and _identity_gap(b, price=2.71) == 0.0


class TestFailClosed:
    def test_unowned_fill_refuses_with_one_alert(self, two_books):
        a, b = two_books
        oa = _seed(a, "XOM", "sell", 100, 50.0)
        api = _Api(fees=[_fee("fee-x", -0.40, "REG fee for proceed of $20000 on 2026-10-23 by PA3U")],
                   fills=[_fill(oa, "XOM", "sell", 100, 50.0),
                          _fill(str(uuid.uuid4()), "CVX", "sell", 300, 50.0)])  # nobody's
        sa, _ = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        assert sa["FEE"] == 0 and _rows(a, "order_id='fee-x'") == []
        assert [r[0] for r in _alerts(a)] == ["fee_unattributable"]
        _run(api, _ctx(1, a, api))
        assert len(_alerts(a)) == 1

    def test_base_mismatch_refuses(self, two_books):
        a, b = two_books
        oa = _seed(a, "XOM", "sell", 100, 50.0)
        api = _Api(fees=[_fee("fee-y", -0.40, "REG fee for proceed of $99999 on 2026-10-23 by PA3U")],
                   fills=[_fill(oa, "XOM", "sell", 100, 50.0)])
        sa, _ = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        assert sa["FEE"] == 0 and _alerts(a)

    def test_unknown_shape_refuses(self, two_books):
        a, b = two_books
        api = _Api(fees=[_fee("fee-z", -1.00, "Wire transfer fee")])
        sa, _ = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        assert sa["FEE"] == 0 and _alerts(a)

    def test_fill_fetch_failure_defers_without_alert(self, two_books):
        a, b = two_books
        api = _Api(fees=[_fee("fee-d", -0.40, "REG fee for proceed of $20000 on 2026-10-23 by PA3U")],
                   fail_fills=True)
        sa, _ = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        assert sa["FEE"] == 0 and _alerts(a) == [] and _rows(a) == []

    def test_largest_remainder_sums_exactly(self):
        assert ac._split_cents(5, {1: 2, 2: 1}) == {1: 3, 2: 2}
        assert ac._split_cents(1, {1: 4, 2: 1}) == {1: 1, 2: 0}
        assert ac._split_cents(0, {1: 4, 2: 1}) == {1: 0, 2: 0}
        assert sum(ac._split_cents(157, {1: 3.3, 2: 1.1, 3: 0.7}).values()) == 157


# ── cash parity adjusts only for UNJOURNALED fees ──────────────────

class TestParityFeeAdjustment:
    def test_journaled_fees_are_not_double_counted(self, tmp_path):
        import aggregate_audit as agg
        agg._FEE_NET_CACHE.clear()
        db = _make_db(tmp_path / "p.db")
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO trades (symbol, side, qty, price, order_id, status, pnl) "
                         "VALUES ('FEE','cash_debit',1,0.50,'2026::fee-1','closed',-0.5)")
            conn.execute("INSERT INTO trades (symbol, side, qty, price, order_id, status) "
                         "VALUES ('FEE','cash_debit',1,0.10,'2026::fee-3::voided','canceled')")
            conn.commit()
        api = _Api(fees=[_fee("2026::fee-1", -0.50, "OCC Clearing Fee"),
                         _fee("2026::fee-2", -0.25, "OCC Clearing Fee"),
                         _fee("2026::fee-3", -0.10, "OCC Clearing Fee")])
        api._key_id = "k-test"
        # fee-1 journaled, fee-3 voided (still 'seen' — the writer owns it), fee-2 unjournaled
        assert agg._account_fee_net(api, [db]) == pytest.approx(-0.25)
        assert agg._account_fee_net(api, []) == pytest.approx(-0.85)


# ── price-free identity ─────────────────────────────────────────────

class TestPriceFreeIdentity:
    def _wire(self, monkeypatch, tmp_path, cash, positions, pnl_rows):
        import models
        import client
        import certify_books
        pid = 901
        db = _make_db(tmp_path / f"quantopsai_profile_{pid}.db")
        with sqlite3.connect(db) as conn:
            for pnl in pnl_rows:
                conn.execute("INSERT INTO trades (symbol, side, qty, price, status, pnl) "
                             "VALUES ('X','sell',1,1,'closed',?)", (pnl,))
            conn.commit()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(models, "get_active_profiles",
                            lambda: [{"id": pid, "name": "T", "initial_capital": 100_000.0}])
        monkeypatch.setattr(models, "build_user_context_from_profile",
                            lambda p: SimpleNamespace(profile_id=p, db_path=db, is_virtual=True))
        monkeypatch.setattr(client, "get_account_info", lambda ctx=None, api=None: {"cash": cash, "equity": 0.0})
        monkeypatch.setattr(client, "get_positions", lambda ctx=None, api=None: positions)
        return certify_books

    def test_price_moves_cancel_exactly(self, monkeypatch, tmp_path):
        # bought 100 @ 50 (cash 95,000); marked at 61.37 — any mark gives gap 0
        pos = [{"symbol": "X", "qty": 100, "market_value": 6137.0, "unrealized_pl": 1137.0}]
        cb = self._wire(monkeypatch, tmp_path, 95_000.0, pos, [])
        assert cb.check_decomposition(5.0) == []
        pos[0].update(market_value=4000.0, unrealized_pl=-1000.0)   # price crashed
        assert cb.check_decomposition(5.0) == []

    def test_real_misbooking_of_seven_dollars_is_caught(self, monkeypatch, tmp_path):
        pos = [{"symbol": "X", "qty": 100, "market_value": 6137.0, "unrealized_pl": 1137.0}]
        cb = self._wire(monkeypatch, tmp_path, 95_000.0, pos, [7.0])   # realized nothing was sold
        out = cb.check_decomposition(5.0)
        assert len(out) == 1 and "gap=-7.00" in out[0]

    def test_degraded_book_is_a_finding(self, monkeypatch, tmp_path):
        import client
        cb = self._wire(monkeypatch, tmp_path, 100_000.0, [], [])
        monkeypatch.setattr(client, "get_account_info",
                            lambda ctx=None, api=None: {"cash": 100_000.0, "degraded": True})
        assert any("UNVERIFIABLE" in f for f in cb.check_decomposition(5.0))

    def test_gate_uses_five_dollars(self):
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        assert "check_decomposition(tolerance=5.0)" in src
        assert "tolerance=250.0" not in src


class TestMultiLegFillsResolveThroughTheParent:
    def test_spread_leg_fills_are_owned_by_the_parent_book(self, two_books):
        """p231's 10-21 CDE put spread: journaled under parent 7d1a4a93
        (two MULTILEG rows), filled at the broker under two LEG ids."""
        a, b = two_books
        parent = str(uuid.uuid4()); leg1 = str(uuid.uuid4()); leg2 = str(uuid.uuid4())
        with sqlite3.connect(a) as conn:
            for occ, side, px in (("CDE261127P00015000", "sell", 0.95),
                                  ("CDE261127P00014000", "buy", 0.65)):
                conn.execute("INSERT INTO trades (timestamp, symbol, occ_symbol, side, qty, "
                             "price, fill_price, status, order_id, signal_type) VALUES "
                             "('2026-10-21T15:09:00','CDE',?,?,1,?,?,'open',?,'MULTILEG')",
                             (occ, side, px, px, parent))
            conn.commit()
        api = _Api(fees=[_fee("fee-orf-ml", -0.03, "ORF fee for proceed of 2 contracts on 2026-10-21 by PA3U", date="2026-10-21"),
                         _fee("fee-taf-ml", -0.01, "OPT TAF fee for proceed of 1 contracts (1 trades) on 2026-10-21 by PA3U", date="2026-10-21")],
                   fills=[_fill(leg1, "CDE261127P00015000", "sell_short", 1, 0.95),
                          _fill(leg2, "CDE261127P00014000", "buy", 1, 0.65)])
        api.orders = [SimpleNamespace(id=parent, order_class="mleg", symbol="",
                                      legs=[SimpleNamespace(id=leg1), SimpleNamespace(id=leg2)])]
        sa, sb = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        assert (sa["FEE"], sb["FEE"]) == (2, 0)
        assert _rows(a, "order_id='fee-orf-ml'")[0]["price"] == 0.03
        assert _rows(a, "order_id='fee-taf-ml'")[0]["price"] == 0.01
        assert _alerts(a) == []

    def test_truly_unowned_leg_still_refuses(self, two_books):
        a, b = two_books
        api = _Api(fees=[_fee("fee-x2", -0.03, "ORF fee for proceed of 1 contracts on 2026-10-21 by PA3U", date="2026-10-21")],
                   fills=[_fill(str(uuid.uuid4()), "CDE261127P00015000", "sell_short", 1, 0.95)])
        api.orders = []
        sa, _ = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        assert sa["FEE"] == 0 and [r[0] for r in _alerts(a)] == ["fee_unattributable"]


class TestEquityTafBySharesSold:
    def test_split_by_shares_sold(self, two_books):
        a, b = two_books
        oa = _seed(a, "XOM", "sell", 300, 50.0)
        ob = _seed(b, "CVX", "sell", 100, 50.0)
        oc = _seed(b, "HD", "buy", 500, 10.0, status="open")
        api = _Api(fees=[_fee("fee-taf-eq", -0.04,
                              "TAF fee for proceed of 400 shares (3 trades) on 2026-10-23 by PA3U")],
                   fills=[_fill(oa, "XOM", "sell", 300, 50.0), _fill(ob, "CVX", "sell", 100, 50.0),
                          _fill(oc, "HD", "buy", 500, 10.0)])
        sa, sb = _run(api, _ctx(1, a, api), _ctx(2, b, api))
        assert (sa["FEE"], sb["FEE"]) == (1, 1)
        assert _rows(a, "order_id='fee-taf-eq'")[0]["price"] == 0.03
        assert _rows(b, "order_id='fee-taf-eq'")[0]["price"] == 0.01
