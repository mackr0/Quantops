"""The 2026-09-18 expiry class, pinned: a worthless-expiry close must
reach LEG-DERIVED realized, or the equity identity drifts by exactly
the lost premium.

Two correct fixes composed into a hole:
  * 2026-07-25 — expiry closes are journaled `sell/buy @ $0` with pnl
    stamped explicitly, BECAUSE the FIFO skips price<=0 rows.
  * 2026-08-24 — the equity-identity audit's realized side moved from
    the pnl column to `journal.compute_leg_realized` (same fill-true
    basis as cash) — the FIFO that skips price<=0 rows.
So the stamped pnl stopped counting, the $0 close never consumed its
lot, and the first held-to-expiry longs afterwards (09-18 monthly:
p229 O 55P −$5, p230 AAPL 145P −$3, p231 XEL 70P −$75 + MRK 140P −$34)
raised "equity identity broken" on penny-exact books.

Same class, fixed in the same pass before it fired: cash-only rows
('dividend' / 'cash_debit') are counted by get_virtual_cash but were
invisible to leg-derived realized.

Every test drives the REAL capture writer into the REAL schema and
reads the REAL audit — the seam between them is what broke.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from activities_capture import (  # noqa: E402
    _write_dividend, _write_option_expiry_or_exercise,
    _write_option_settlement,
)

INIT_CAP = 250_000.0


def _mk_db(path, rows=()):
    from journal import init_db
    init_db(str(path))   # the REAL schema — log_trade writes many cols
    conn = sqlite3.connect(str(path))
    for ts, sym, occ, side, qty, price, status in rows:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, occ_symbol, side, "
            "qty, price, fill_price, status) VALUES (?,?,?,?,?,?,?,?)",
            (ts, sym, occ, side, qty, price, price, status))
    conn.commit()
    conn.close()
    return str(path)


class _Ctx:
    display_name = "test"

    def __init__(self, db_path):
        self.db_path = db_path
        self.initial_capital = INIT_CAP
        self.api = None   # no fetcher: marks fall back, and cancel anyway


def _act(**kw):
    return SimpleNamespace(**kw)


def _audit(db):
    from integrity_audit import audit_equity_identity
    with patch("models.build_user_context_from_profile",
               return_value=_Ctx(db)):
        out = audit_equity_identity(1)
    assert out["errored"] is None, out["errored"]
    return out


class TestWorthlessExpiryReachesRealized:
    def test_long_put_expires_worthless_identity_holds(self, tmp_path):
        """p231's XEL shape: long 1 @0.75, OPEXP removes it at $0.
        Cash is down $75; realized must be −$75; drift exactly 0."""
        occ = "XEL260918P00070000"
        db = _mk_db(tmp_path / "long.db", [
            ("2026-09-16T14:24:11", "XEL", occ, "buy", 1, 0.75, "open")])
        a = _act(id="exp-1", activity_type="OPEXP", symbol=occ, qty=-1)
        assert _write_option_expiry_or_exercise(_Ctx(db), a) is True
        out = _audit(db)
        assert out["realized_total"] == -75.0, out
        assert out["drift"] == 0.0, (
            f"worthless expiry left {out['drift']:+.2f} of drift — the "
            f"$0 close is not reaching leg-derived realized")
        assert out["pnl_column_mismatch"] == 0.0, out

    def test_short_leg_expires_worthless_premium_kept(self, tmp_path):
        """Short 2 @1.37 expires: BUY-to-close @0, realized +274."""
        occ = "CVX260918C00185000"
        db = _mk_db(tmp_path / "short.db", [
            ("2026-09-10T14:00:00", "CVX", occ, "sell", 2, 1.37, "open")])
        a = _act(id="exp-2", activity_type="OPEXP", symbol=occ, qty=2)
        assert _write_option_expiry_or_exercise(_Ctx(db), a) is True
        out = _audit(db)
        assert out["realized_total"] == 274.0, out
        assert out["drift"] == 0.0, out

    def test_partial_lot_history_uses_fifo_basis(self, tmp_path):
        """Round-trip then re-entry on the same OCC (p229's O 55P
        shape, with its expired-unfilled exits in between): only the
        still-open lot's premium is lost at expiry."""
        occ = "O260918P00055000"
        db = _mk_db(tmp_path / "fifo.db", [
            ("2026-09-14T10:00:00", "O", occ, "buy", 1, 0.20, "closed"),
            ("2026-09-14T11:00:00", "O", occ, "sell", 1, 0.30, "closed"),
            ("2026-09-15T15:15:42", "O", occ, "buy", 1, 0.05, "open"),
            ("2026-09-15T15:25:20", "O", occ, "sell", 1, 0.0, "expired"),
        ])
        a = _act(id="exp-3", activity_type="OPEXP", symbol=occ, qty=-1)
        assert _write_option_expiry_or_exercise(_Ctx(db), a) is True
        out = _audit(db)
        assert out["realized_total"] == 5.0, out   # +10 round-trip, −5 expiry
        assert out["drift"] == 0.0, out

    def test_recompute_does_not_disturb_the_stamped_pnl(self, tmp_path):
        """The truing pass now sees the $0 close; its FIFO value must
        agree with what capture stamped (nothing to rewrite)."""
        from journal import recompute_realized_pnl
        occ = "MRK260918P00140000"
        db = _mk_db(tmp_path / "recompute.db", [
            ("2026-09-16T19:52:43", "MRK", occ, "buy", 1, 0.34, "open")])
        a = _act(id="exp-4", activity_type="OPEXP", symbol=occ, qty=-1)
        assert _write_option_expiry_or_exercise(_Ctx(db), a) is True
        recompute_realized_pnl(db)
        pnl = sqlite3.connect(db).execute(
            "SELECT pnl FROM trades WHERE order_id='exp-4'").fetchone()[0]
        assert pnl == -34.0


class TestZeroPriceGateStaysTight:
    def test_unpriced_live_leg_is_still_skipped(self, tmp_path):
        """A $0/NULL price on a LIVE row is a leg awaiting its fill —
        never a close. It must not consume the long it sits beside."""
        from journal import compute_leg_realized
        occ = "XEL260918P00070000"
        db = _mk_db(tmp_path / "pending.db", [
            ("2026-09-16T14:24:11", "XEL", occ, "buy", 1, 0.75, "open"),
            ("2026-09-16T14:32:52", "XEL", occ, "sell", 1, 0.0,
             "pending_fill"),
        ])
        assert compute_leg_realized(db) == {}

    def test_closed_zero_price_row_without_activity_signal_skipped(
            self, tmp_path):
        """Only broker-activity closes (OPEXP/OPASN/OPEXC) are $0 by
        convention. Any other closed $0 row is unpriced data — counting
        it would fabricate a full-premium loss and HIDE the drift that
        should surface it."""
        from journal import compute_leg_realized
        occ = "XEL260918P00070000"
        db = _mk_db(tmp_path / "nosig.db", [
            ("2026-09-16T14:24:11", "XEL", occ, "buy", 1, 0.75, "open"),
            ("2026-09-18T20:00:00", "XEL", occ, "sell", 1, 0.0, "closed"),
        ])
        assert compute_leg_realized(db) == {}

    def test_zero_close_never_opens_a_lot(self, tmp_path):
        """An OPEXP sell with nothing to consume must not become a
        $0-basis short that a later buy would 'cover' for phantom P&L."""
        from journal import compute_leg_realized
        occ = "XEL260918P00070000"
        db = _mk_db(tmp_path / "nolot.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, occ_symbol, side, qty, "
            "price, status, signal_type) VALUES ('2026-09-18T20:00:00', "
            "'XEL', ?, 'sell', 1, 0.0, 'closed', 'OPEXP')", (occ,))
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, occ_symbol, side, qty, "
            "price, fill_price, status) VALUES ('2026-09-19T14:00:00', "
            "'XEL', ?, 'buy', 1, 0.50, 0.50, 'open')", (occ,))
        conn.commit(); conn.close()
        assert compute_leg_realized(db) == {}


class TestCashOnlyRowsReachRealized:
    def test_dividend_credit_identity_holds(self, tmp_path):
        db = _mk_db(tmp_path / "div.db", [
            ("2026-09-01T14:00:00", "KO", None, "buy", 100, 60.0, "open")])
        a = _act(id="div-1", activity_type="DIV", symbol="KO",
                 net_amount=48.5)
        assert _write_dividend(_Ctx(db), a) is True
        out = _audit(db)
        assert out["realized_total"] == 48.5, out
        assert out["drift"] == 0.0, out

    def test_assignment_settlement_pair_identity_holds(self, tmp_path):
        """The CVX 185/190 shape end to end: short leg assigned (+274
        premium kept), settlement +37,000/−38,000. Realized = −726."""
        occ = "CVX260918C00185000"
        db = _mk_db(tmp_path / "settle.db", [
            ("2026-09-10T14:00:00", "CVX", occ, "sell", 2, 1.37, "open")])
        ctx = _Ctx(db)
        assert _write_option_expiry_or_exercise(ctx, _act(
            id="asn-1", activity_type="OPASN", symbol=occ, qty=2)) is True
        for aid, net in (("s-1", 37000.0), ("s-2", -38000.0)):
            assert _write_option_settlement(ctx, _act(
                id=aid, net_amount=net, group_id="g1",
                description="Options Trade"), {"g1": occ})
        out = _audit(db)
        assert out["realized_total"] == -726.0, out
        assert out["drift"] == 0.0, out
        assert out["pnl_column_mismatch"] == 0.0, out


class TestCashAndRealizedStayInLockstep:
    def test_every_cash_side_is_handled_by_leg_realized(self):
        """The structural form of this bug: get_virtual_cash learned a
        side ('cash_debit', 07-25) that compute_leg_realized never did.
        Any side literal the cash math buckets must appear in the
        realized math too — a new cash-affecting side added to one and
        not the other breaks HERE, not as a live identity alarm."""
        import inspect
        import re
        import journal
        cash_src = inspect.getsource(journal.get_virtual_cash)
        leg_src = inspect.getsource(journal.compute_leg_realized)
        cash_sides = set()
        for tup in re.findall(r"side in \(([^)]*)\)", cash_src):
            cash_sides |= set(re.findall(r"\"(\w+)\"", tup))
        assert {"buy", "sell", "short", "cover",
                "dividend", "cash_debit"} <= cash_sides, (
            f"cash-side parse went stale: {cash_sides}")
        missing = {s for s in cash_sides if f'"{s}"' not in leg_src}
        assert not missing, (
            f"get_virtual_cash counts side(s) {sorted(missing)} that "
            f"compute_leg_realized ignores — the equity identity will "
            f"drift by every such row's amount")
