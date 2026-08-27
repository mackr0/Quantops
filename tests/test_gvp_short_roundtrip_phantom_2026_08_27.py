"""2026-08-27 — completed SHORT round-trips must never leave phantom
long lots in get_virtual_positions (the CRCL/BABA kill-switch class).

The mechanism: short protective buy-backs are journaled side='buy'.
The 06-18 stream included closed stock BUYS but excluded closed stock
SHORTS, and gvp's buy branch never consumed short lots — so a
completed short round-trip's closed buy-back masqueraded as a long
entry lot, and the NEXT same-symbol long trade's sell consumed the
phantom lot instead of its own entry. Journal-phantom longs of
+106/+139/+156/+187 across three accounts; equity-identity drift equal
to the shielded entries' market value; kill switch. The 06-18 comment
called this state "anomalous data only" — these tests pin that it is
produced by perfectly normal live trading.

The fix: closed stock shorts join the stream (congruent with
compute_leg_realized), buys consume shorts FIRST (cover-first, exactly
like the realized FIFO), and unconsumed closed-origin lots are dropped
symmetrically on both sides.
"""
from __future__ import annotations

import sqlite3

import pytest

from journal import get_virtual_positions, init_db


def _db(tmp_path):
    path = str(tmp_path / "quantopsai_profile_9.db")
    init_db(path)
    return path


def _row(path, ts, symbol, side, qty, fill, status):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "fill_price, status) VALUES (?,?,?,?,?,?,?)",
        (ts, symbol, side, qty, fill, fill, status))
    conn.commit()
    conn.close()


def _net(path, symbol):
    for p in get_virtual_positions(db_path=path,
                                   price_fetcher=lambda s: 100.0):
        # Position objects expose the ticker as .underlying
        # (project_cancel_on_close gotcha) — .symbol is not it.
        name = (getattr(p, "underlying", None)
                or getattr(p, "symbol", None))
        if name == symbol:
            return float(getattr(p, "qty_signed", getattr(p, "qty", 0)))
    return 0.0


class TestShortRoundtripPhantom:
    def test_the_crcl_shape_nets_exactly_the_open_entry(self, tmp_path):
        """THE incident: completed short round-trip (short closed +
        buy-back closed), then a fresh long entry still 'open' whose
        sell already closed. Pre-fix: net +139 phantom. Truth: 0."""
        db = _db(tmp_path)
        _row(db, "2026-08-24T16:08:27", "CRCL", "short", 139, 90.22, "closed")
        _row(db, "2026-08-25T13:31:26", "CRCL", "buy", 139, 92.35, "closed")
        _row(db, "2026-08-26T16:41:39", "CRCL", "buy", 141, 88.28, "open")
        _row(db, "2026-08-26T16:47:56", "CRCL", "sell", 141, 88.49, "closed")
        assert _net(db, "CRCL") == pytest.approx(0.0)

    def test_partial_shield_variant(self, tmp_path):
        """p229's shape: buy-back 138 closed, later 106 entry + 106
        sell. Pre-fix the sell ate the phantom lot and left +106."""
        db = _db(tmp_path)
        _row(db, "2026-08-24T14:24:43", "CRCL", "short", 138, 89.51, "closed")
        _row(db, "2026-08-25T14:34:29", "CRCL", "buy", 138, 91.09, "closed")
        _row(db, "2026-08-26T16:41:36", "CRCL", "buy", 106, 88.30, "open")
        _row(db, "2026-08-26T16:48:02", "CRCL", "sell", 106, 88.51, "closed")
        assert _net(db, "CRCL") == pytest.approx(0.0)

    def test_live_short_with_pending_cover_still_short(self, tmp_path):
        """A genuinely held short (entry open, cover not yet filled)
        must still read as short."""
        db = _db(tmp_path)
        _row(db, "2026-08-26T15:48:53", "CDE", "short", 292, 21.42, "open")
        assert _net(db, "CDE") == pytest.approx(-292.0)

    def test_status_flip_closed_short_without_cover_drops(self, tmp_path):
        """Symmetric cleanup: a short flipped 'closed' with no cover
        row (lifecycle flip) must not linger as a phantom short."""
        db = _db(tmp_path)
        _row(db, "2026-08-24T10:00:00", "XYZ", "short", 50, 10.0, "closed")
        assert _net(db, "XYZ") == pytest.approx(0.0)

    def test_oversell_still_surfaces_as_short(self, tmp_path):
        """The 06-18 UWMC guarantee is untouched: a sell beyond every
        buy ever placed is a REAL short and must be booked."""
        db = _db(tmp_path)
        _row(db, "2026-08-24T10:00:00", "UWMC", "buy", 3672, 5.0, "closed")
        _row(db, "2026-08-24T11:00:00", "UWMC", "sell", 3772, 5.1, "closed")
        assert _net(db, "UWMC") == pytest.approx(-100.0)

    def test_closed_buy_orphan_flip_still_cleans(self, tmp_path):
        """The 06-18 guarantee's other half: a buy flipped 'closed'
        with no sell row must not linger as a phantom long."""
        db = _db(tmp_path)
        _row(db, "2026-08-24T10:00:00", "ABC", "buy", 100, 10.0, "closed")
        assert _net(db, "ABC") == pytest.approx(0.0)

    def test_cover_side_rows_still_consume(self, tmp_path):
        """Explicit side='cover' path unchanged: open short + closed
        cover nets flat."""
        db = _db(tmp_path)
        _row(db, "2026-08-25T18:14:25", "CRCL", "short", 67, 92.66, "open")
        _row(db, "2026-08-25T19:27:34", "CRCL", "cover", 67, 92.58, "closed")
        assert _net(db, "CRCL") == pytest.approx(0.0)


class TestIdentityCongruence:
    def test_gvp_and_leg_realized_agree_on_incident_history(self, tmp_path):
        """The audit-level guarantee: on the exact incident history the
        position lens says FLAT and the realized lens books the full
        economics — the two engines can no longer disagree by a
        phantom's market value."""
        from journal import compute_leg_realized, get_virtual_cash
        db = _db(tmp_path)
        _row(db, "2026-08-24T16:08:27", "CRCL", "short", 139, 90.22, "closed")
        _row(db, "2026-08-25T13:31:26", "CRCL", "buy", 139, 92.35, "closed")
        _row(db, "2026-08-26T16:41:39", "CRCL", "buy", 141, 88.28, "open")
        _row(db, "2026-08-26T16:47:56", "CRCL", "sell", 141, 88.49, "closed")
        assert _net(db, "CRCL") == pytest.approx(0.0)
        legs = compute_leg_realized(db_path=db)
        realized = sum(legs.values())
        # short round trip: (90.22-92.35)*139 = -296.07;
        # long round trip: (88.49-88.28)*141 = +29.61
        assert realized == pytest.approx(-296.07 + 29.61, abs=0.01)
        cash = get_virtual_cash(db_path=db, initial_capital=100_000.0)
        # cash delta must equal realized when flat (identity holds)
        assert cash - 100_000.0 == pytest.approx(realized, abs=0.01)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
