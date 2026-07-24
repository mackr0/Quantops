"""The aggregate audit's journal lens IS the canonical position lens.

2026-07-24, operator directive: "if the books reconcile perfectly like
they should we need to make our system reflect that." The audit's
`_journal_open_qty_per_symbol` used to carry a PARALLEL FIFO that
counted only open/pending_fill rows, so a closed exit that PARTIALLY
consumed a still-open lot was invisible: acct-56 GOOG read journal −9
vs broker −1 as an ERROR while the books reconciled exactly, and NFLX
read +270 vs +147 while `get_virtual_positions` already showed p214
short 123 (its closed oversell). Every disagreement between the two
FIFO implementations was a false ERROR. The audit now delegates to
`journal.get_virtual_positions` (with `include_unpriced=True` for
quantity truth), so "audit says drift" can only mean "the SYSTEM's
books disagree with the broker" — never "two internal copies disagree
with each other".
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aggregate_audit import _journal_open_qty_per_symbol  # noqa: E402


def _mk_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, status TEXT DEFAULT 'open',
            pnl REAL, fill_price REAL, occ_symbol TEXT
        )
    """)
    for i, (sym, side, qty, price, status) in enumerate(rows):
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "fill_price, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"2026-07-{10+i:02d}T10:00:00", sym, side, qty, price,
             price, status),
        )
    conn.commit()
    conn.close()
    return str(path)


class TestPartialConsumptionVisible:
    def test_closed_covers_partially_consume_open_short(self, tmp_path):
        """The acct-56 GOOG shape: an OPEN 19-share short with 8
        broker-filled CLOSED covers against it. The old live-only lens
        read −19 (covers invisible); the canonical lens reads −11 —
        which is what the books actually say and what the broker holds."""
        db = _mk_db(tmp_path / "goog.db", [
            ("GOOG", "short", 19, 200.0, "open"),
            ("GOOG", "cover", 8, 195.0, "closed"),
        ])
        out = _journal_open_qty_per_symbol(db)
        assert out.get("GOOG") == -11, (
            f"expected -11 (partial consumption visible), got {out}"
        )

    def test_closed_round_trip_nets_flat(self, tmp_path):
        """A completed buy+sell round trip contributes nothing."""
        db = _mk_db(tmp_path / "rt.db", [
            ("AAPL", "buy", 100, 50.0, "closed"),
            ("AAPL", "sell", 100, 55.0, "closed"),
        ])
        assert _journal_open_qty_per_symbol(db).get("AAPL") is None

    def test_closed_oversell_surfaces_as_short(self, tmp_path):
        """The acct-56 NFLX/p214 shape: sells beyond every buy — all
        rows closed — are a REAL account short the audit must count
        (the system's own position lens counts it), so the account
        aggregate reconciles to the broker instead of false-flagging
        the sibling profile's backed lot."""
        db = _mk_db(tmp_path / "nflx.db", [
            ("NFLX", "buy", 269, 60.0, "closed"),
            ("NFLX", "sell", 392, 61.0, "closed"),
        ])
        out = _journal_open_qty_per_symbol(db)
        assert out.get("NFLX") == -123, (
            f"expected -123 (oversell visible as short), got {out}"
        )


class TestSingleLensInvariant:
    def test_audit_delegates_to_get_virtual_positions(self):
        """Structural: the parallel FIFO must not come back. The audit
        computes journal qty through journal.get_virtual_positions —
        the one canonical lens — with include_unpriced quantity truth."""
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir,
            "aggregate_audit.py")).read()
        import re
        m = re.search(
            r"def _journal_open_qty_per_symbol.*?(?=\ndef )", src,
            re.DOTALL)
        assert m, "_journal_open_qty_per_symbol missing"
        body = m.group(0)
        assert "get_virtual_positions" in body, (
            "The audit no longer delegates to the canonical lens — a "
            "parallel FIFO WILL drift and false-flag reconciled books."
        )
        assert "include_unpriced=True" in body, (
            "The audit must request quantity truth (unpriced just-"
            "filled legs count) — the 2026-05-06 broker_orphan class."
        )

    def test_matches_canonical_lens_on_mixed_book(self, tmp_path):
        """Property: for a mixed book, the audit's number equals the
        canonical lens's signed sum — by construction, forever."""
        from journal import get_virtual_positions
        db = _mk_db(tmp_path / "mix.db", [
            ("WMT", "buy", 49, 108.0, "open"),
            ("WMT", "sell", 49, 107.9, "closed"),
            ("WMT", "buy", 49, 107.9, "open"),
            ("TSLA", "short", 10, 300.0, "open"),
        ])
        audit = _journal_open_qty_per_symbol(db)
        lens = {}
        for p in get_virtual_positions(
                db_path=db, price_fetcher=lambda s, side="buy": 1.0,
                include_unpriced=True):
            eff = ((getattr(p, "occ_symbol", None)
                    or getattr(p, "underlying", "")) or "").upper()
            lens[eff] = lens.get(eff, 0.0) + float(
                getattr(p, "qty_signed", 0) or 0)
        lens = {k: v for k, v in lens.items() if abs(v) > 0.001}
        assert audit == lens


class TestUnpricedQtyTruth:
    def test_unpriced_open_leg_counts_for_audit(self, tmp_path):
        """A just-filled option leg with NULL price (backfill lag)
        must count qty-wise — the 2026-05-06 false-broker_orphan pin,
        preserved through the delegation."""
        db = str(tmp_path / "opt.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, symbol TEXT, side TEXT, qty REAL,
                price REAL, status TEXT DEFAULT 'open',
                fill_price REAL, occ_symbol TEXT
            )
        """)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "occ_symbol) VALUES ('2026-05-06T14:56:45', 'WMT', 'buy', "
            "3, NULL, 'WMT260612P00117000')")
        conn.commit(); conn.close()
        out = _journal_open_qty_per_symbol(db)
        assert out.get("WMT260612P00117000") == 3
