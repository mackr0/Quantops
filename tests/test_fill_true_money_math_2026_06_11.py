"""Fill-true money math (2026-06-11 hyper-accuracy audit).

Three classes fixed together; each pinned here:

1. CASH — get_virtual_account_info:
   (a) never-filled terminal rows are NOT cash flows. The p94
       regression: ONE canceled BBAI protective-TP row that kept its
       trigger price (2,234 @ $4.4632) injected $9,970.79 phantom
       cash — the entire +3.76% its dashboard showed.
   (b) broker fill_price preferred over decision price (WCT: 9,029
       shares × $0.055 slippage = $497 cash error on one trade).

2. POSITIONS — get_virtual_positions lots are valued at fill price
   when reported.

3. REALIZED P&L — recompute_realized_pnl: exit rows get fill-true
   FIFO pnl. Replaces the submit-time estimate (prorated unrealized
   at decision prices) and stamps synthesized exits that carried
   NULL pnl forever (the WCT bracket-stop backfills). Handles
   shorts closed via side='buy' protectives, covers, option 100×
   multiplier, and option sell-to-open.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "p.db")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE trades ("
            " id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT,"
            " side TEXT, qty REAL, price REAL, fill_price REAL,"
            " order_id TEXT, signal_type TEXT, status TEXT,"
            " reason TEXT, occ_symbol TEXT, pnl REAL,"
            " stop_loss REAL, take_profit REAL)")
        conn.commit()
    return path


def _ins(path, ts, symbol, side, qty, price, status,
         fill=None, occ=None, pnl=None):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price,"
            " fill_price, status, occ_symbol, pnl)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, symbol, side, qty, price, fill, status, occ, pnl),
        )
        conn.commit()


def _pnl(path, row_id):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT pnl FROM trades WHERE id=?", (row_id,)
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# (1) Cash
# ---------------------------------------------------------------------------

class TestCash:

    def test_canceled_row_with_price_is_not_cash(self, db):
        """The exact p94 shape: canceled protective TP keeping its
        trigger price must not credit cash."""
        from journal import get_virtual_account_info
        _ins(db, "t1", "BBAI", "buy", 2234, 3.985, "closed",
             fill=3.99)
        _ins(db, "t2", "BBAI", "sell", 2234, 4.075, "closed",
             fill=4.06)
        # The phantom: canceled TP with a real trigger price
        _ins(db, "t3", "BBAI", "sell", 2234, 4.4632, "canceled")
        acct = get_virtual_account_info(db, initial_capital=100000)
        # Cash = 100000 - 2234*3.99 + 2234*4.06 = 100000 + 156.38
        assert abs(acct["cash"] - 100156.38) < 0.02, (
            f"cash={acct['cash']} — canceled row's $9,970 must not "
            "be counted (p94 phantom-cash regression)."
        )

    def test_every_never_filled_status_excluded(self, db):
        from journal import get_virtual_account_info
        for i, status in enumerate(
            ("canceled", "expired", "rejected", "done_for_day",
             "auto_reconciled_phantom_close", "pending_protective")):
            _ins(db, f"t{i}", "XYZ", "sell", 100, 50.0, status)
        acct = get_virtual_account_info(db, initial_capital=100000)
        assert acct["cash"] == 100000, (
            f"cash={acct['cash']} — never-filled rows moved cash."
        )

    def test_fill_price_preferred_in_cash(self, db):
        from journal import get_virtual_account_info
        # Decision $2.215, broker filled $2.27 — cash must reflect
        # the fill (the WCT $497 class).
        _ins(db, "t1", "WCT", "buy", 9029, 2.215, "open", fill=2.27)
        acct = get_virtual_account_info(db, initial_capital=100000)
        assert abs(acct["cash"] - (100000 - 9029 * 2.27)) < 0.02


# ---------------------------------------------------------------------------
# (2) Position lots
# ---------------------------------------------------------------------------

def test_position_entry_uses_fill_price(db):
    from journal import get_virtual_positions
    _ins(db, "t1", "WCT", "buy", 9029, 2.215, "open", fill=2.27)
    pos = get_virtual_positions(db, price_fetcher=lambda s: 2.30)
    assert len(pos) == 1
    assert abs(float(pos[0]["avg_entry_price"]) - 2.27) < 0.001, (
        "avg_entry_price must be the broker fill, not the decision "
        "price."
    )


# ---------------------------------------------------------------------------
# (3) Realized P&L truing
# ---------------------------------------------------------------------------

class TestRealizedPnlTruing:

    def test_estimate_overwritten_with_fill_true_value(self, db):
        """PLUG shape: submit-time estimate said +$77.83 (decision
        prices); both fills were $2.89 → true pnl $0.00."""
        from journal import recompute_realized_pnl
        _ins(db, "t1", "PLUG", "buy", 7783, 2.885, "closed",
             fill=2.89)
        _ins(db, "t2", "PLUG", "sell", 7783, 2.895, "closed",
             fill=2.89, pnl=77.83)
        n = recompute_realized_pnl(db)
        assert n == 1
        assert abs(_pnl(db, 2) - 0.0) < 0.005

    def test_null_pnl_synthesized_exit_stamped(self, db):
        """WCT shape: reconciler-synthesized stop fill had NULL pnl
        forever — realized losses silently missing from /trades."""
        from journal import recompute_realized_pnl
        _ins(db, "t1", "WCT", "buy", 10158, 2.215, "closed",
             fill=2.27)
        _ins(db, "t2", "WCT", "sell", 10158, 2.07, "closed",
             fill=2.07)
        recompute_realized_pnl(db)
        expected = (2.07 - 2.27) * 10158
        assert abs(_pnl(db, 2) - expected) < 0.01, (
            f"pnl={_pnl(db, 2)} expected {expected:.2f}"
        )

    def test_short_closed_by_protective_buy(self, db):
        """Shorts exit via side='buy' protective fills — pnl =
        (short entry − cover price) × qty on the buy row."""
        from journal import recompute_realized_pnl
        _ins(db, "t1", "NU", "short", 1065, 11.61, "closed",
             fill=11.63)
        _ins(db, "t2", "NU", "buy", 1065, 12.82, "closed",
             fill=12.80)
        recompute_realized_pnl(db)
        expected = (11.63 - 12.80) * 1065
        assert abs(_pnl(db, 2) - expected) < 0.01

    def test_option_pnl_uses_contract_multiplier(self, db):
        from journal import recompute_realized_pnl
        occ = "AAPL260717P00260000"
        _ins(db, "t1", "AAPL", "buy", 1, 1.43, "closed",
             fill=1.43, occ=occ)
        _ins(db, "t2", "AAPL", "sell", 1, 1.93, "closed",
             fill=1.93, occ=occ)
        recompute_realized_pnl(db)
        assert abs(_pnl(db, 2) - 50.0) < 0.01  # (1.93-1.43)*1*100

    def test_option_sell_to_open_gets_no_pnl(self, db):
        """A multileg short leg (side='sell', no long lot, OCC set)
        is an OPEN, not a close — it must not be stamped."""
        from journal import recompute_realized_pnl
        _ins(db, "t1", "AAPL", "sell", 1, 3.25, "open",
             fill=3.25, occ="AAPL260717P00275000")
        recompute_realized_pnl(db)
        assert _pnl(db, 1) is None

    def test_never_filled_rows_ignored(self, db):
        from journal import recompute_realized_pnl
        _ins(db, "t1", "BBAI", "buy", 100, 4.0, "closed", fill=4.0)
        _ins(db, "t2", "BBAI", "sell", 100, 4.5, "canceled")
        _ins(db, "t3", "BBAI", "sell", 100, 4.2, "closed", fill=4.2)
        recompute_realized_pnl(db)
        assert _pnl(db, 2) is None, "canceled sell must not realize"
        assert abs(_pnl(db, 3) - 20.0) < 0.01

    def test_idempotent(self, db):
        from journal import recompute_realized_pnl
        _ins(db, "t1", "X", "buy", 10, 1.0, "closed", fill=1.0)
        _ins(db, "t2", "X", "sell", 10, 2.0, "closed", fill=2.0)
        assert recompute_realized_pnl(db) == 1
        assert recompute_realized_pnl(db) == 0  # second pass no-op
