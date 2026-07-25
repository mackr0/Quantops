"""The 2026-07-24 expiry-weekend class, pinned: broker-initiated option
events must book into exactly the OWNING profile, with the right side,
the right pnl, their settlement cash — and within the hour, weekends
included.

Friday's expiry (the first real assignment this system received) hit
every latent defect in the 2026-07-20 capture path at once:
  1. no ownership gate — the CVX 185C assignment was written into ALL
     FIVE acct-56 profiles (only p212 held the spread)
  2. hardcoded side='sell' — wrong for a short leg (adds a phantom
     short instead of closing one)
  3. `qty <= 0` guard — Alpaca reports expiring LONG legs with
     negative qty, so every long-leg expiry was silently skipped
     (FCX 70C x5 never booked)
  4. pnl never realized — recompute_realized_pnl skips price<=0 rows,
     so the claimed "FIFO attributes the premium" never happened
  5. 'OPXRC' was never a valid Alpaca activity type (rejected on every
     run since 07-20) — exercises were uncapturable; the CVX 190C
     exercise ALSO only appears as a MISC row, not any typed stream
  6. settlement cash (MISC 'Options Trade': +37,000/-38,000 = net
     -1,000, the fully-ITM spread's width) had nowhere to go
  7. everything above sat unbooked ALL WEEKEND because every booking
     task was market-hours-only (activities post ~01:00 UTC)
  8. an expired-unfilled exit kept its estimated pnl (+35), breaking
     the equity identity by exactly that amount
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from activities_capture import (  # noqa: E402
    _write_option_expiry_or_exercise, _write_option_settlement,
    _HANDLED_TYPES,
)


def _mk_db(path, rows=()):
    from journal import init_db
    init_db(str(path))   # the REAL schema — log_trade writes many cols
    conn = sqlite3.connect(str(path))
    for sym, occ, side, qty, price, status in rows:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, occ_symbol, side, "
            "qty, price, fill_price, status) VALUES "
            "('2026-07-15T14:00:00', ?, ?, ?, ?, ?, ?, ?)",
            (sym, occ, side, qty, price, price, status))
    conn.commit()
    conn.close()
    return str(path)


def _ctx(db):
    return SimpleNamespace(db_path=db, display_name="test")


def _act(**kw):
    return SimpleNamespace(**kw)


OCC = "CVX260724C00185000"


class TestOwnershipGate:
    def test_foreign_profile_books_nothing(self, tmp_path):
        """The 5x-duplication shape: a profile with NO rows for the
        OCC must not book the account-level event."""
        db = _mk_db(tmp_path / "foreign.db")
        a = _act(id="act-1", activity_type="OPASN", symbol=OCC, qty=2)
        assert _write_option_expiry_or_exercise(_ctx(db), a) is False
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0

    def test_voided_rows_do_not_confer_settlement_ownership(self, tmp_path):
        """The leak found live on first run: canceled duplicate rows
        must not make a profile 'own' a settlement."""
        db = _mk_db(tmp_path / "voided.db",
                    [("CVX", OCC, "sell", 2, 0.0, "canceled")])
        a = _act(id="act-2", net_amount=37000.0, group_id="g1",
                 description="Options Trade")
        assert _write_option_settlement(
            _ctx(db), a, {"g1": OCC}) is False


class TestDirectionAndQty:
    def test_short_leg_closes_with_buy_and_positive_pnl(self, tmp_path):
        """p212's shape: short 2 @1.37 -> assignment closes with a
        BUY @0 and realized +274 (premium kept)."""
        db = _mk_db(tmp_path / "short.db",
                    [("CVX", OCC, "sell", 2, 1.37, "open")])
        a = _act(id="act-3", activity_type="OPASN", symbol=OCC, qty=2)
        assert _write_option_expiry_or_exercise(_ctx(db), a) is True
        conn = sqlite3.connect(db)
        side, pnl = conn.execute(
            "SELECT side, pnl FROM trades WHERE order_id='act-3'"
        ).fetchone()
        assert side == "buy", "short-leg close must be buy-to-close"
        assert pnl == 274.0
        # origin leg closed too
        st = conn.execute(
            "SELECT status FROM trades WHERE id=1").fetchone()[0]
        assert st == "closed"

    def test_long_leg_negative_qty_books_sell_with_loss(self, tmp_path):
        """The FCX shape: long 5 @0.12, OPEXP reports qty=-5 — must
        book (not skip) a SELL @0 with pnl -60."""
        occ = "FCX260724C00070000"
        db = _mk_db(tmp_path / "long.db",
                    [("FCX", occ, "buy", 5, 0.12, "open")])
        a = _act(id="act-4", activity_type="OPEXP", symbol=occ, qty=-5)
        assert _write_option_expiry_or_exercise(_ctx(db), a) is True
        conn = sqlite3.connect(db)
        side, pnl = conn.execute(
            "SELECT side, pnl FROM trades WHERE order_id='act-4'"
        ).fetchone()
        assert side == "sell"
        assert pnl == -60.0

    def test_idempotent(self, tmp_path):
        db = _mk_db(tmp_path / "idem.db",
                    [("CVX", OCC, "sell", 2, 1.37, "open")])
        a = _act(id="act-5", activity_type="OPASN", symbol=OCC, qty=2)
        assert _write_option_expiry_or_exercise(_ctx(db), a) is True
        assert _write_option_expiry_or_exercise(_ctx(db), a) is False


class TestSettlementCash:
    def test_debit_and_credit_book_cash_only_rows(self, tmp_path):
        """The CVX pair: +37,000 (dividend side) and -38,000
        (cash_debit side), each pnl=net, attributed via group->OCC."""
        db = _mk_db(tmp_path / "settle.db",
                    [("CVX", OCC, "sell", 2, 1.37, "closed")])
        credit = _act(id="s-1", net_amount=37000.0, group_id="g1",
                      description="Options Trade")
        debit = _act(id="s-2", net_amount=-38000.0, group_id="g1",
                     description="Options Trade")
        assert _write_option_settlement(_ctx(db), credit, {"g1": OCC})
        assert _write_option_settlement(_ctx(db), debit, {"g1": OCC})
        conn = sqlite3.connect(db)
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT order_id, side, pnl FROM trades "
            "WHERE signal_type='OPTION_SETTLEMENT'")}
        assert rows["s-1"] == ("dividend", 37000.0)
        assert rows["s-2"] == ("cash_debit", -38000.0)

    def test_unattributable_settlement_refused(self, tmp_path):
        db = _mk_db(tmp_path / "noattr.db",
                    [("CVX", OCC, "sell", 2, 1.37, "closed")])
        a = _act(id="s-3", net_amount=-1000.0, group_id="unknown",
                 description="Options Trade")
        assert _write_option_settlement(_ctx(db), a, {"g1": OCC}) is False

    def test_cash_debit_side_reduces_virtual_cash(self, tmp_path):
        """journal.get_virtual_cash must debit 'cash_debit' rows —
        the primitive the settlement booking relies on."""
        from journal import get_virtual_cash
        db = _mk_db(tmp_path / "cash.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "fill_price, status) VALUES "
            "('2026-07-24T20:00:00', 'CVX', 'cash_debit', 1, 1000.0, "
            "1000.0, 'closed')")
        conn.commit(); conn.close()
        assert get_virtual_cash(db_path=db, initial_capital=10000.0) == 9000.0


class TestStructural:
    def test_opxrc_gone_opexc_handled(self):
        assert "OPXRC" not in _HANDLED_TYPES, (
            "OPXRC is not a valid Alpaca activity type — it was "
            "rejected on every capture run since 2026-07-20."
        )
        assert "OPEXC" in _HANDLED_TYPES

    def test_misc_stream_processed(self):
        """Exercises can appear ONLY as MISC rows (the CVX 190C did);
        the capture must process the MISC stream."""
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir,
            "activities_capture.py")).read()
        assert "Options Exercise" in src and "Options Trade" in src

    def test_scheduler_has_closed_market_housekeeping(self):
        """The weekend gap: the sleep loop must run the hourly
        housekeeping (fills heal + activities capture)."""
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir,
            "multi_scheduler.py")).read()
        assert "_closed_market_housekeeping()" in src, (
            "The closed-market housekeeping call left the sleep loop — "
            "broker-initiated events will again sit unbooked all "
            "weekend."
        )

    def test_healer_clears_pnl_on_terminal_unfilled(self):
        """An expired/canceled-unfilled row must have its estimated
        pnl cleared (the p211 #353 +35 identity break)."""
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir,
            "multi_scheduler.py")).read()
        import re
        assert re.search(
            r"UPDATE trades SET status = \?, price = 0,\s*\"?\s*\"?pnl = NULL",
            src), (
            "The terminal-unfilled marking no longer clears pnl — a "
            "stale estimate on a dead row breaks the equity identity."
        )
