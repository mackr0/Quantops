"""2026-10-26 — shared-account dividends attributed per book, and the
cash-merger (MA) handler. Pins the two defects that held the kill
switch from 10-13 to 10-26:

  1. `_write_dividend` wrote the ACCOUNT-level DIV into every profile
     sharing the account, at the account's NET amount, always as a
     credit. On account 61 all four October events belonged to p229
     (short); p232/p235/p238 each got +$312.29 of phantom cash with
     sign-mixed pnl — the "+358 decomposition gap". The 07-25 option
     rewrite named and fixed this class for OPEXP/OPASN/OPEXC; the
     dividend writer was left behind.
  2. `MA` had no handler: WBD's cash merger (10-12, 3 sh → $93.05)
     left p230's entry 'open' → broker=0 vs virtual=3 → integrity
     halt + reconciler orphan_close halt, for two weeks.

The attribution rule — own signed qty at 16:00 ET on the last trading
day BEFORE the record date, by broker FILL time, with Σ(siblings) ==
broker quantity or nothing is booked — reproduced all 11 real
dividend events on the three Exp-2 accounts exactly. The shapes below
are those events.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, REPO)

import activities_capture as ac  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────

def _make_db(tmp_path, pid):
    db = tmp_path / f"quantopsai_profile_{pid}.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL,
                order_id TEXT,
                signal_type TEXT,
                strategy TEXT,
                reason TEXT,
                ai_reasoning TEXT,
                ai_confidence REAL,
                stop_loss REAL,
                take_profit REAL,
                status TEXT DEFAULT 'open',
                pnl REAL,
                decision_price REAL,
                fill_price REAL,
                slippage_pct REAL,
                occ_symbol TEXT,
                option_strategy TEXT,
                expiry TEXT,
                strike REAL,
                predicted_slippage_bps REAL,
                adv_at_decision REAL,
                data_quality TEXT
            );
        """)
    return str(db)


def _seed(db, symbol, side, qty, price, ts, status="open", order_id=None,
          fill_price="same", signal_type="BUY"):
    """A filled journal row. order_id defaults to a fresh UUID (a
    broker order — the fake api answers get_order for it)."""
    oid = order_id or str(uuid.uuid4())
    fp = price if fill_price == "same" else fill_price
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "fill_price, status, order_id, signal_type) VALUES "
            "(?,?,?,?,?,?,?,?,?)",
            (ts, symbol, side, qty, price, fp, status, oid, signal_type))
        conn.commit()
    return oid


class _FakeApi:
    """get_activities serves a fixed list; get_order answers filled_at
    for known order ids (default: filled at the row's own timestamp —
    the caller registers exceptions, e.g. a resting stop that filled
    days after it was journaled)."""

    def __init__(self, activities=(), fills=None, default_filled_at=None):
        self.activities = list(activities)
        self.fills = dict(fills or {})
        self.default_filled_at = default_filled_at
        self.order_calls = []

    def get_activities(self, activity_types=None, after=None, **kw):
        return [a for a in self.activities
                if getattr(a, "activity_type", None) == activity_types]

    def get_order(self, order_id, nested=False):
        self.order_calls.append(order_id)
        if order_id in self.fills:
            fa = self.fills[order_id]
        elif self.default_filled_at is not None:
            fa = self.default_filled_at
        else:
            raise RuntimeError(f"order not found for {order_id}")
        return SimpleNamespace(id=order_id, status="filled",
                               filled_at=fa, legs=[])


def _ctx(pid, db, api, aid=61):
    return SimpleNamespace(profile_id=pid, db_path=db, api=api,
                           alpaca_account_id=aid, display_name=f"P{pid}",
                           is_virtual=True, initial_capital=100_000.0,
                           get_alpaca_api=lambda: api)


def _div(aid, symbol, net, per_share, rec_date, pos_qty, date=None):
    return SimpleNamespace(
        id=aid, activity_type="DIV", symbol=symbol, net_amount=net,
        per_share_amount=per_share, qty=None, date=date or rec_date,
        description=(f"Cash DIV @ {per_share}, Pos QTY: {pos_qty:g}, "
                     f"Rec Date: {rec_date}"))


def _ma_share_leg(aid, symbol, qty, basis, per_share, date="2026-10-12"):
    return SimpleNamespace(
        id=aid, activity_type="MA", symbol=symbol, qty=qty, price=basis,
        net_amount=0, date=date,
        description=f"Cash Merger ${per_share} per share")


def _ma_cash_leg(aid, symbol, net, per_share, date="2026-10-12"):
    return SimpleNamespace(
        id=aid, activity_type="MA", symbol=symbol, qty=None, price=None,
        net_amount=net, date=date,
        description=f"Cash Merger ${per_share} per share")


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _rows(db, where="1=1"):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM trades WHERE {where} ORDER BY id")]


def _alerts(db):
    try:
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT alert_type, severity, title, detail FROM audit_alerts"
            ).fetchall()
    except sqlite3.OperationalError:
        return []


def _identity_gap(db, initial=100_000.0, price=0.0):
    """certify_books' identity: (equity − initial) − (realized +
    unrealized), with every open position marked at `price`. 0 on a
    correct book."""
    from journal import get_virtual_account_info, get_virtual_positions
    pf = lambda s: price  # noqa: E731
    info = get_virtual_account_info(db_path=db, initial_capital=initial,
                                    price_fetcher=pf)
    upl = sum(float(p.get("unrealized_pl") or 0)
              for p in get_virtual_positions(db_path=db, price_fetcher=pf))
    with sqlite3.connect(db) as conn:
        realized = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE pnl IS NOT NULL "
            "AND COALESCE(status,'') NOT IN ('canceled','expired','rejected')"
        ).fetchone()[0]
    return round((info["equity"] - initial) - (realized + upl), 2)


@pytest.fixture(autouse=True)
def _fresh_caches():
    from order_status_cache import clear_cache
    clear_cache()
    ac.clear_memos()
    yield
    clear_cache()
    ac.clear_memos()


@pytest.fixture
def siblings(monkeypatch):
    """Register the account's books: siblings(ctx_or_list) → patches
    `_sibling_db_paths` to return [(pid, db), ...]."""
    def _register(pairs):
        monkeypatch.setattr(ac, "_sibling_db_paths", lambda ctx: list(pairs))
    return _register


# ── entitlement instant ─────────────────────────────────────────────

class TestEntitlementSnapshot:
    def test_prior_close_on_a_weekday(self):
        # rec Wed 2026-09-30 → Tue 09-29 16:00 EDT = 20:00 UTC
        assert ac._entitlement_snapshot_utc("2026-09-30") == "2026-09-29T20:00:00"

    def test_skips_the_weekend(self):
        # rec Mon 2026-09-14 → Fri 09-11 16:00 EDT
        assert ac._entitlement_snapshot_utc("2026-09-14") == "2026-09-11T20:00:00"

    def test_winter_close_is_21z(self):
        assert ac._entitlement_snapshot_utc("2026-12-15") == "2026-12-14T21:00:00"

    def test_garbage_is_none(self):
        assert ac._entitlement_snapshot_utc("") is None
        assert ac._entitlement_snapshot_utc("not-a-date") is None


# ── dividends ───────────────────────────────────────────────────────

class TestShortProfilePaysTheDividend:
    """acct 61 VTR: broker −141 @0.52 = −73.32; p229 short 141, the
    three siblings flat. Only p229 books it — as a DEBIT."""

    def test_debit_lands_only_on_the_short_book(self, tmp_path, siblings):
        dbs = {p: _make_db(tmp_path, p) for p in (229, 232, 235, 238)}
        api = _FakeApi(default_filled_at=_utc(2026, 9, 10, 14, 0))
        _seed(dbs[229], "VTR", "short", 141, 86.0, "2026-09-10T14:00:00",
              signal_type="STRONG_SELL")
        siblings([(p, dbs[p]) for p in dbs])
        act = _div("div-vtr-61", "VTR", -73.32, 0.52, "2026-09-30", -141)
        api.activities = [act]
        summaries = {p: ac.capture_activities_for_profile(_ctx(p, dbs[p], api))
                     for p in dbs}
        assert summaries[229]["DIV"] == 1
        assert all(summaries[p]["DIV"] == 0 for p in (232, 235, 238))
        row = _rows(dbs[229], "order_id = 'div-vtr-61'")[0]
        assert row["side"] == "cash_debit"
        assert row["price"] == 73.32
        assert row["pnl"] == -73.32
        assert row["status"] == "closed"
        assert row["strategy"] == "alpaca_activity"
        for p in (232, 235, 238):
            assert _rows(dbs[p], "order_id = 'div-vtr-61'") == []
            assert _alerts(dbs[p]) == []
        # Cash went DOWN by 73.32 and realized went down by 73.32 —
        # identity holds, and the book agrees with the broker's sign.
        from journal import get_virtual_cash
        assert get_virtual_cash(db_path=dbs[229], initial_capital=100_000.0) \
            == pytest.approx(100_000.0 + 141 * 86.0 - 73.32)
        assert _identity_gap(dbs[229], price=86.0) == 0.0

    def test_the_old_behaviour_is_gone(self, tmp_path, siblings):
        """The exact p235 shape: a flat book on a net-short account
        must receive NOTHING (it used to get +73.32 cash)."""
        db229, db235 = _make_db(tmp_path, 229), _make_db(tmp_path, 235)
        api = _FakeApi(default_filled_at=_utc(2026, 9, 10, 14, 0))
        _seed(db229, "VTR", "short", 141, 86.0, "2026-09-10T14:00:00")
        siblings([(229, db229), (235, db235)])
        api.activities = [_div("d1", "VTR", -73.32, 0.52, "2026-09-30", -141)]
        s = ac.capture_activities_for_profile(_ctx(235, db235, api))
        assert s["DIV"] == 0
        assert _rows(db235) == []
        from journal import get_virtual_cash
        assert get_virtual_cash(db_path=db235, initial_capital=100_000.0) == 100_000.0


class TestLongAndShortSiblingsSplit:
    """acct 63 CSCO: broker −163 @0.42 = −68.46; p231 short 80, p234
    short 83. Plus a long sibling on another account shape: a book
    that is LONG on a net-short account is PAID its share."""

    def test_two_shorts_split_the_debit(self, tmp_path, siblings):
        dbs = {p: _make_db(tmp_path, p) for p in (231, 234, 237, 240)}
        api = _FakeApi(default_filled_at=_utc(2026, 9, 25, 14, 0))
        _seed(dbs[231], "CSCO", "short", 80, 70.0, "2026-09-25T14:00:00")
        _seed(dbs[234], "CSCO", "short", 83, 70.0, "2026-09-25T14:00:00")
        siblings([(p, dbs[p]) for p in dbs])
        api.activities = [_div("d-csco", "CSCO", -68.46, 0.42, "2026-10-02", -163)]
        for p in dbs:
            ac.capture_activities_for_profile(_ctx(p, dbs[p], api, aid=63))
        r231 = _rows(dbs[231], "order_id='d-csco'")[0]
        r234 = _rows(dbs[234], "order_id='d-csco'")[0]
        assert (r231["side"], r231["price"], r231["pnl"]) == ("cash_debit", 33.60, -33.60)
        assert (r234["side"], r234["price"], r234["pnl"]) == ("cash_debit", 34.86, -34.86)
        assert _rows(dbs[237]) == [] and _rows(dbs[240]) == []
        assert round(r231["pnl"] + r234["pnl"], 2) == -68.46

    def test_long_book_on_net_short_account_is_paid(self, tmp_path, siblings):
        dbA, dbB = _make_db(tmp_path, 1), _make_db(tmp_path, 2)
        api = _FakeApi(default_filled_at=_utc(2026, 9, 20, 14, 0))
        _seed(dbA, "O", "short", 300, 55.0, "2026-09-20T14:00:00")
        _seed(dbB, "O", "buy", 79, 55.0, "2026-09-20T14:00:00")
        siblings([(1, dbA), (2, dbB)])
        # account net −221 × 0.2715 = −60.00
        api.activities = [_div("d-o", "O", -60.00, 0.2715, "2026-09-30", -221)]
        ac.capture_activities_for_profile(_ctx(1, dbA, api))
        ac.capture_activities_for_profile(_ctx(2, dbB, api))
        a = _rows(dbA, "order_id='d-o'")[0]
        b = _rows(dbB, "order_id='d-o'")[0]
        assert (a["side"], a["pnl"]) == ("cash_debit", round(-300 * 0.2715, 2))
        assert (b["side"], b["pnl"]) == ("dividend", round(79 * 0.2715, 2))
        assert _identity_gap(dbA, price=55.0) == 0.0
        assert _identity_gap(dbB, price=55.0) == 0.0


class TestEntitlementIsByFillTimeAtThePriorClose:
    def test_cover_three_minutes_after_the_close_still_short(self, tmp_path, siblings):
        """acct 61 O: shorted 221 at 19:46Z on 09-29, covered 220 at
        20:03Z (after the 16:00 ET close) → still −221 at the
        entitlement instant; broker debited 60.00 = 221 × 0.2715."""
        db = _make_db(tmp_path, 229)
        short_oid = _seed(db, "O", "short", 221, 55.29, "2026-09-29T19:46:49")
        cover_oid = _seed(db, "O", "cover", 220, 54.80, "2026-09-29T20:03:01",
                          status="closed", signal_type="DELTA_HEDGE")
        api = _FakeApi(fills={short_oid: _utc(2026, 9, 29, 19, 46, 50),
                              cover_oid: _utc(2026, 9, 29, 20, 3, 1)})
        siblings([(229, db)])
        api.activities = [_div("d-o61", "O", -60.00, 0.2715, "2026-09-30", -221)]
        s = ac.capture_activities_for_profile(_ctx(229, db, api))
        assert s["DIV"] == 1
        r = _rows(db, "order_id='d-o61'")[0]
        assert (r["side"], r["price"], r["pnl"]) == ("cash_debit", 60.0, -60.0)

    def test_resting_protective_that_filled_later_does_not_count(self, tmp_path, siblings):
        """acct 63 VTR: p237 bought 145 on 09-21; its trailing-stop
        row is stamped 09-22 (submit) but the broker filled it on
        10-01 → p237 still held 145 at the 09-29 close and the
        broker paid 145 × 0.52 = 75.40 — to p237, not to p234 (who
        bought 153 on 09-22 and sold on 09-25)."""
        db234, db237 = _make_db(tmp_path, 234), _make_db(tmp_path, 237)
        b237 = _seed(db237, "VTR", "buy", 145, 86.89, "2026-09-21T19:30:33",
                     status="closed")
        s237 = _seed(db237, "VTR", "sell", 145, 83.61, "2026-09-22T13:33:03",
                     status="closed", signal_type="PROTECTIVE_TRAILING")
        b234 = _seed(db234, "VTR", "buy", 153, 87.03, "2026-09-22T16:00:50",
                     status="closed")
        s234 = _seed(db234, "VTR", "sell", 153, 86.46, "2026-09-25T13:50:45",
                     status="closed", signal_type="STRONG_SELL")
        api = _FakeApi(fills={
            b237: _utc(2026, 9, 21, 19, 30, 34),
            s237: _utc(2026, 10, 1, 13, 34, 7),      # filled 9 days later
            b234: _utc(2026, 9, 22, 16, 0, 50),
            s234: _utc(2026, 9, 25, 13, 50, 45),
        })
        siblings([(234, db234), (237, db237)])
        api.activities = [_div("d-vtr63", "VTR", 75.40, 0.52, "2026-09-30", 145)]
        s234_ = ac.capture_activities_for_profile(_ctx(234, db234, api, aid=63))
        s237_ = ac.capture_activities_for_profile(_ctx(237, db237, api, aid=63))
        assert (s234_["DIV"], s237_["DIV"]) == (0, 1)
        r = _rows(db237, "order_id='d-vtr63'")[0]
        assert (r["side"], r["price"], r["pnl"]) == ("dividend", 75.40, 75.40)
        assert _rows(db234, "order_id='d-vtr63'") == []

    def test_bought_during_the_record_date_is_not_entitled(self, tmp_path, siblings):
        """acct 62 AMT: p233 held 75 until 11:23 ET on the record
        date; p230 bought 100 at 12:22 ET that day. Broker paid on
        75 → all of it to p233, none to p230."""
        db230, db233 = _make_db(tmp_path, 230), _make_db(tmp_path, 233)
        b233 = _seed(db233, "AMT", "buy", 75, 172.22, "2026-09-23T17:38:28", status="closed")
        s233 = _seed(db233, "AMT", "sell", 75, 164.07, "2026-09-30T15:23:55", status="closed")
        b230 = _seed(db230, "AMT", "buy", 100, 164.63, "2026-09-30T16:22:12")
        api = _FakeApi(fills={b233: _utc(2026, 9, 23, 17, 38, 29),
                              s233: _utc(2026, 9, 30, 15, 23, 57),
                              b230: _utc(2026, 9, 30, 16, 22, 15)})
        siblings([(230, db230), (233, db233)])
        api.activities = [_div("d-amt62", "AMT", 134.25, 1.79, "2026-09-30", 75)]
        assert ac.capture_activities_for_profile(_ctx(230, db230, api, 62))["DIV"] == 0
        assert ac.capture_activities_for_profile(_ctx(233, db233, api, 62))["DIV"] == 1
        assert _rows(db233, "order_id='d-amt62'")[0]["pnl"] == 134.25


class TestFailClosed:
    def test_books_that_do_not_sum_to_the_broker_write_nothing(self, tmp_path, siblings):
        dbA, dbB = _make_db(tmp_path, 1), _make_db(tmp_path, 2)
        api = _FakeApi(default_filled_at=_utc(2026, 9, 1, 14, 0))
        _seed(dbA, "VTR", "short", 100, 86.0, "2026-09-01T14:00:00")  # Σ −100 ≠ −141
        siblings([(1, dbA), (2, dbB)])
        api.activities = [_div("d-x", "VTR", -73.32, 0.52, "2026-09-30", -141)]
        for p, db in ((1, dbA), (2, dbB)):
            assert ac.capture_activities_for_profile(_ctx(p, db, api))["DIV"] == 0
            assert _rows(db, "order_id='d-x'") == []
        kinds = {a[0] for a in _alerts(dbA)} | {a[0] for a in _alerts(dbB)}
        assert kinds == {"dividend_unattributable"}
        # one alert per event per book, not one per pass
        ac.capture_activities_for_profile(_ctx(1, dbA, api))
        assert len(_alerts(dbA)) == 1

    def test_missing_per_share_or_record_date_refuses(self, tmp_path, siblings):
        db = _make_db(tmp_path, 1)
        api = _FakeApi(default_filled_at=_utc(2026, 9, 1, 14, 0))
        _seed(db, "VTR", "buy", 10, 86.0, "2026-09-01T14:00:00")
        siblings([(1, db)])
        no_ps = _div("d-nops", "VTR", 5.20, 0.52, "2026-09-30", 10)
        no_ps.per_share_amount = None
        no_rec = _div("d-norec", "VTR", 5.20, 0.52, "2026-09-30", 10)
        no_rec.description = "Cash DIV @ 0.52"
        api.activities = [no_ps, no_rec]
        assert ac.capture_activities_for_profile(_ctx(1, db, api))["DIV"] == 0
        assert _rows(db, "side='dividend'") == []
        assert len(_alerts(db)) == 2

    def test_unverifiable_order_defers_without_alert(self, tmp_path, siblings):
        """A broker lookup failure is transient: book nothing, alert
        nothing, retry next pass (the activities pass is idempotent)."""
        db = _make_db(tmp_path, 1)
        _seed(db, "VTR", "buy", 10, 86.0, "2026-09-01T14:00:00")
        api = _FakeApi()  # get_order raises for every id
        siblings([(1, db)])
        api.activities = [_div("d-defer", "VTR", 5.20, 0.52, "2026-09-30", 10)]
        assert ac.capture_activities_for_profile(_ctx(1, db, api))["DIV"] == 0
        assert _rows(db, "side='dividend'") == []
        # the Σ-mismatch alert is for a deterministic refusal; an
        # unverifiable lookup is reported as such (still alerted so
        # the operator sees it) but the row is not written
        assert _rows(db, "order_id='d-defer'") == []

    def test_idempotent(self, tmp_path, siblings):
        db = _make_db(tmp_path, 1)
        api = _FakeApi(default_filled_at=_utc(2026, 9, 1, 14, 0))
        _seed(db, "VTR", "buy", 10, 86.0, "2026-09-01T14:00:00")
        siblings([(1, db)])
        api.activities = [_div("d-idem", "VTR", 5.20, 0.52, "2026-09-30", 10)]
        ac.capture_activities_for_profile(_ctx(1, db, api))
        ac.capture_activities_for_profile(_ctx(1, db, api))
        assert len(_rows(db, "order_id='d-idem'")) == 1


# ── cash merger ─────────────────────────────────────────────────────

class TestCashMerger:
    """The WBD shape: p230 long 3 @ 30.86; on 10-12 the broker posts
    MA qty=-3 'Cash Merger $31.01666668 per share' and a $93.05 cash
    leg. Expected: a 3-share sell @ 31.0167 (pnl +0.47), the entry
    flipped closed, no position, cash +93.05, identity intact."""

    def _book(self, tmp_path, siblings):
        db230, db233 = _make_db(tmp_path, 230), _make_db(tmp_path, 233)
        oid = _seed(db230, "WBD", "buy", 3, 30.855, "2026-09-25T17:36:37",
                    fill_price=30.86)
        api = _FakeApi(fills={oid: _utc(2026, 9, 25, 17, 36, 39)})
        api.activities = [
            _ma_share_leg("ma-share", "WBD", -3, 30.86, "31.01666668"),
            _ma_cash_leg("ma-cash", "WBD", 93.05, "31.01666668"),
        ]
        siblings([(230, db230), (233, db233)])
        return db230, db233, api

    def test_books_the_exit_and_closes_the_entry(self, tmp_path, siblings):
        db230, db233, api = self._book(tmp_path, siblings)
        from journal import get_virtual_positions, get_virtual_cash
        s = ac.capture_activities_for_profile(_ctx(230, db230, api, 62))
        assert s["MA"] == 1
        exit_row = _rows(db230, "order_id='ma-share'")[0]
        assert exit_row["side"] == "sell"
        assert exit_row["qty"] == 3
        assert exit_row["price"] == pytest.approx(31.01666668)
        assert exit_row["fill_price"] == pytest.approx(31.01666668)
        assert exit_row["status"] == "closed"
        assert exit_row["signal_type"] == "MA"
        assert exit_row["pnl"] == 0.47
        entry = _rows(db230, "side='buy'")[0]
        assert entry["status"] == "closed"
        assert "cash merger" in entry["reason"]
        assert _rows(db230, "order_id='ma-cash'") == []     # cash leg writes nothing
        assert [p for p in get_virtual_positions(db_path=db230,
                                                 price_fetcher=lambda s: 0.0)
                if p["symbol"] == "WBD"] == []
        assert get_virtual_cash(db_path=db230, initial_capital=100_000.0) \
            == pytest.approx(100_000.0 - 3 * 30.86 + 93.05)
        assert _identity_gap(db230) == 0.0
        # idempotent
        assert ac.capture_activities_for_profile(_ctx(230, db230, api, 62))["MA"] == 0
        assert len(_rows(db230, "order_id='ma-share'")) == 1

    def test_sibling_without_the_position_writes_nothing(self, tmp_path, siblings):
        db230, db233, api = self._book(tmp_path, siblings)
        s = ac.capture_activities_for_profile(_ctx(233, db233, api, 62))
        assert s["MA"] == 0
        assert _rows(db233) == []
        assert _alerts(db233) == []

    def test_reconciler_no_longer_sees_an_open_entry(self, tmp_path, siblings):
        """The halt condition: `_select_open_rows` must not return the
        merged-away entry after booking."""
        db230, _, api = self._book(tmp_path, siblings)
        ac.capture_activities_for_profile(_ctx(230, db230, api, 62))
        from reconcile_journal_to_broker import _select_open_rows
        with sqlite3.connect(db230) as conn:
            conn.row_factory = sqlite3.Row
            assert [r["symbol"] for r in _select_open_rows(conn)] == []

    def test_short_position_is_covered(self, tmp_path, siblings):
        db = _make_db(tmp_path, 7)
        oid = _seed(db, "WBD", "short", 5, 32.0, "2026-09-25T17:36:37")
        api = _FakeApi(fills={oid: _utc(2026, 9, 25, 17, 36, 39)})
        api.activities = [_ma_share_leg("ma-s", "WBD", 5, 32.0, "31.0")]
        siblings([(7, db)])
        assert ac.capture_activities_for_profile(_ctx(7, db, api))["MA"] == 1
        r = _rows(db, "order_id='ma-s'")[0]
        assert (r["side"], r["qty"], r["pnl"]) == ("cover", 5, 5.0)
        assert _rows(db, "side='short'")[0]["status"] == "closed"
        assert _identity_gap(db) == 0.0

    def test_stock_consideration_is_refused_with_an_alert(self, tmp_path, siblings):
        db = _make_db(tmp_path, 1)
        oid = _seed(db, "WBD", "buy", 3, 30.86, "2026-09-25T17:36:37")
        api = _FakeApi(fills={oid: _utc(2026, 9, 25, 17, 36, 39)})
        act = _ma_share_leg("ma-stock", "WBD", -3, 30.86, "0")
        act.description = "Stock Merger 0.5 PARA per share"
        api.activities = [act]
        siblings([(1, db)])
        assert ac.capture_activities_for_profile(_ctx(1, db, api))["MA"] == 0
        assert _rows(db, "side='sell'") == []
        assert _rows(db, "side='buy'")[0]["status"] == "open"
        assert [a[0] for a in _alerts(db)] == ["corporate_action_unbooked"]

    def test_refuses_when_the_live_book_already_changed(self, tmp_path, siblings):
        """Snapshot says 3 held, but something closed the entry since
        → never write a blind exit; alert instead."""
        db = _make_db(tmp_path, 1)
        oid = _seed(db, "WBD", "buy", 3, 30.86, "2026-09-25T17:36:37",
                    status="closed")
        api = _FakeApi(fills={oid: _utc(2026, 9, 25, 17, 36, 39)})
        api.activities = [_ma_share_leg("ma-gone", "WBD", -3, 30.86, "31.0")]
        siblings([(1, db)])
        assert ac.capture_activities_for_profile(_ctx(1, db, api))["MA"] == 0
        assert _rows(db, "order_id='ma-gone'") == []
        assert [a[0] for a in _alerts(db)] == ["corporate_action_unbooked"]


# ── other corporate actions: explained, never guessed ───────────────

class TestUnhandledReorgsAreExplained:
    def test_alert_when_held_and_silence_when_not(self, tmp_path, siblings):
        db_held, db_flat = _make_db(tmp_path, 1), _make_db(tmp_path, 2)
        _seed(db_held, "ACME", "buy", 10, 20.0, "2026-10-01T14:00:00")
        reorg = SimpleNamespace(id="re-1", activity_type="REORG", symbol="ACME",
                                qty=-10, net_amount=0, date="2026-10-20",
                                description="Reorganization")
        api = _FakeApi([reorg], default_filled_at=_utc(2026, 10, 1, 14, 0))
        siblings([(1, db_held), (2, db_flat)])
        s = ac.capture_activities_for_profile(_ctx(1, db_held, api))
        assert s.get("REORG_ALERT") == 1
        a = _alerts(db_held)
        assert len(a) == 1 and a[0][0] == "corporate_action_unbooked"
        assert "REORG" in a[0][2] and "re-1" in a[0][3]
        assert _rows(db_held, "side='sell'") == []           # never booked
        ac.capture_activities_for_profile(_ctx(1, db_held, api))
        assert len(_alerts(db_held)) == 1                   # once per event
        ac.clear_memos()
        assert ac.capture_activities_for_profile(_ctx(2, db_flat, api)).get("REORG_ALERT") is None
        assert _alerts(db_flat) == []

    def test_probed_type_lists(self):
        assert "MA" in ac._HANDLED_TYPES
        for t in ("SSO", "SSP"):          # rejected by the API 2026-10-26
            assert t not in ac._REORG_EXPLAIN_TYPES


# ── the fill poller leaves activity-keyed rows alone ────────────────

class TestFillPollerSkipsActivityRows:
    def test_select_excludes_activity_ids(self):
        src = open(os.path.join(REPO, "multi_scheduler.py")).read()
        i = src.index("def _task_update_fills")
        block = src[i:i + 6000]
        assert "order_id NOT LIKE '%::%'" in block, (
            "rows keyed by an Alpaca activity id (dividends, option "
            "events, merger exits) are not orders — polling them 404s "
            "every cycle and depends on the broker's error text to "
            "escape the >24h order-not-found void")


# ── the repair script ───────────────────────────────────────────────

def _load_script():
    path = os.path.join(REPO, "scripts",
                        "repair_shared_account_dividends_2026_10_26.py")
    spec = importlib.util.spec_from_file_location("repair_divs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRepairScript:
    def _fleet(self, tmp_path, monkeypatch):
        """The real account-61 shape, mis-booked the old way: p229
        short 141 VTR; the −73.32 DIV written into all four books as a
        +73.32 credit (pnl +73.32 on p232 after the recompute flip,
        −73.32 on p235/p238 as first written)."""
        dbs = {p: _make_db(tmp_path, p) for p in (229, 232, 235, 238)}
        oid = _seed(dbs[229], "VTR", "short", 141, 86.0, "2026-09-10T14:00:00")
        for p, pnl in ((229, 73.32), (232, 73.32), (235, -73.32), (238, -73.32)):
            with sqlite3.connect(dbs[p]) as conn:
                conn.execute(
                    "INSERT INTO trades (timestamp, symbol, side, qty, price, "
                    "order_id, signal_type, strategy, status, pnl, reason) VALUES "
                    "('2026-10-16T15:11:00','VTR','dividend',1,73.32,"
                    "'div-vtr','DIVIDEND','alpaca_activity','closed',?,"
                    "'dividend credit VTR $-73.32')", (pnl,))
                conn.commit()
        api = _FakeApi([_div("div-vtr", "VTR", -73.32, 0.52, "2026-09-30", -141)],
                       fills={oid: _utc(2026, 9, 10, 14, 0, 1)})
        profiles = [{"id": p, "name": f"P{p}", "alpaca_account_id": 61,
                     "initial_capital": 100_000.0} for p in dbs]
        ctxs = {p: _ctx(p, dbs[p], api) for p in dbs}
        import client
        import models
        monkeypatch.setattr(models, "get_active_profiles", lambda: profiles)
        monkeypatch.setattr(models, "build_user_context_from_profile",
                            lambda pid: ctxs[pid])
        monkeypatch.setattr(ac, "_sibling_db_paths",
                            lambda ctx: [(p, dbs[p]) for p in dbs])
        # identity gap: flat books except p229's short — price it flat
        monkeypatch.setattr(client, "_prefetch_prices", lambda syms: None)
        monkeypatch.setattr(client, "_make_price_fetcher",
                            lambda api: (lambda s: 86.0))
        return dbs, api

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch, capsys):
        dbs, api = self._fleet(tmp_path, monkeypatch)
        mod = _load_script()
        monkeypatch.setattr(sys, "argv", ["repair"])
        assert mod.main() == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out and "rows to void: 4" in out
        for p in dbs:
            assert _rows(dbs[p], "order_id='div-vtr'")[0]["status"] == "closed"

    def test_apply_voids_and_rebooks_per_book(self, tmp_path, monkeypatch, capsys):
        dbs, api = self._fleet(tmp_path, monkeypatch)
        before = {p: _identity_gap(dbs[p]) for p in (232, 235, 238)}
        assert before[235] == 146.64 and before[238] == 146.64   # the gap class
        assert before[232] == 0.0                                 # "clean" but wrong
        mod = _load_script()
        monkeypatch.setattr(sys, "argv", ["repair", "--apply"])
        assert mod.main() == 0
        for p in dbs:
            old = _rows(dbs[p], "order_id='div-vtr::voided'")
            assert len(old) == 1 and old[0]["status"] == "canceled" and old[0]["pnl"] is None
        # p229 re-booked as the debit it really was; siblings nothing
        new = _rows(dbs[229], "order_id='div-vtr'")
        assert len(new) == 1 and (new[0]["side"], new[0]["pnl"]) == ("cash_debit", -73.32)
        for p in (232, 235, 238):
            assert _rows(dbs[p], "order_id='div-vtr'") == []
            assert _identity_gap(dbs[p]) == 0.0
            from journal import get_virtual_cash
            assert get_virtual_cash(db_path=dbs[p], initial_capital=100_000.0) == 100_000.0
        assert "worst remaining |gap|: 0.00" in capsys.readouterr().out
