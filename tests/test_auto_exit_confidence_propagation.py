"""Pin auto-exit confidence/reasoning propagation onto close rows.

Caught 2026-05-10: protective stop-loss / take-profit / pair-exit
close rows had `ai_confidence=NULL` and `ai_reasoning=NULL`, so the
trades-table macro showed only "Auto-exit" with no number — the
operator couldn't tell at a glance what conviction the AI had when
it took the position that just closed. The trade narrative broke at
every close.

This test pins:
1. `get_open_entry_metadata` returns the most-recent open entry
   row's ai_confidence + ai_reasoning, scoped by symbol (or OCC
   for option legs).
2. Returns None values cleanly when no matching entry exists.
3. Returns None values + logs a warning (not silent swallow) when
   the DB read fails — graceful degrade so the close still gets
   logged, with a surfaced failure mode.
4. Stock symbol lookup excludes option-leg rows for the same
   underlying (occ_symbol IS NULL filter).
5. Option OCC lookup matches the exact contract.
6. Closed rows are not returned (status='closed' filter).
"""

import logging
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from journal import init_db
    init_db(path)
    return path


def _insert(db_path, **kwargs):
    defaults = {
        "timestamp": "2026-05-08T10:00:00",
        "symbol": "AAPL",
        "side": "buy",
        "qty": 100,
        "price": 150.0,
        "fill_price": 150.0,
        "order_id": "ord-1",
        "signal_type": "BUY",
        "strategy": "combined",
        "reason": "test",
        "status": "open",
        "ai_confidence": 78,
        "ai_reasoning": "Strong momentum + cheap RSI",
    }
    defaults.update(kwargs)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(["?"] * len(defaults))
    conn = sqlite3.connect(db_path)
    conn.execute(
        f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )
    conn.commit()
    conn.close()


class TestGetOpenEntryMetadata:
    def test_returns_most_recent_open_buy_metadata(self):
        db = _make_db()
        try:
            _insert(db, timestamp="2026-05-07T10:00:00",
                    symbol="AAPL", ai_confidence=65,
                    ai_reasoning="older entry")
            _insert(db, timestamp="2026-05-08T10:00:00",
                    symbol="AAPL", ai_confidence=78,
                    ai_reasoning="Strong momentum + cheap RSI")

            from journal import get_open_entry_metadata
            meta = get_open_entry_metadata(db, "AAPL")
            assert meta["ai_confidence"] == 78
            assert meta["ai_reasoning"] == "Strong momentum + cheap RSI"
        finally:
            os.unlink(db)

    def test_returns_none_when_no_open_entry(self):
        db = _make_db()
        try:
            from journal import get_open_entry_metadata
            meta = get_open_entry_metadata(db, "AAPL")
            assert meta == {"ai_confidence": None, "ai_reasoning": None}
        finally:
            os.unlink(db)

    def test_excludes_closed_rows(self):
        """A closed entry row must not be returned — closed positions
        already had their close logged; auto-exit lookups should only
        find the still-open position they're about to close."""
        db = _make_db()
        try:
            _insert(db, symbol="AAPL", status="closed",
                    ai_confidence=65, ai_reasoning="already closed")

            from journal import get_open_entry_metadata
            meta = get_open_entry_metadata(db, "AAPL")
            assert meta["ai_confidence"] is None
        finally:
            os.unlink(db)

    def test_excludes_sell_rows_keeps_short_rows(self):
        """Entry-side filter is `side IN ('buy','short')`. SELL rows
        are exits, never entries. SHORT is a sell-to-open and counts."""
        db = _make_db()
        try:
            _insert(db, symbol="MSFT", side="sell",
                    ai_confidence=99, ai_reasoning="exit not entry")
            _insert(db, symbol="MSFT", side="short",
                    ai_confidence=72, ai_reasoning="short entry")

            from journal import get_open_entry_metadata
            meta = get_open_entry_metadata(db, "MSFT")
            assert meta["ai_confidence"] == 72
            assert meta["ai_reasoning"] == "short entry"
        finally:
            os.unlink(db)

    def test_stock_lookup_excludes_option_legs_on_same_underlying(self):
        """A stock-symbol lookup must not pull metadata from an
        unrelated option leg with the same underlying — those are
        independent positions with their own conviction."""
        db = _make_db()
        try:
            _insert(db, symbol="AAPL", occ_symbol="AAPL260612C00200000",
                    ai_confidence=55, ai_reasoning="option entry")

            from journal import get_open_entry_metadata
            meta = get_open_entry_metadata(db, "AAPL")
            # No stock entry exists; option leg must not leak in
            assert meta["ai_confidence"] is None
        finally:
            os.unlink(db)

    def test_occ_lookup_matches_exact_contract(self):
        """Option lookup keys on OCC, not underlying. Two contracts
        on the same underlying carry independent metadata."""
        db = _make_db()
        try:
            _insert(db, symbol="AAPL", occ_symbol="AAPL260612C00200000",
                    ai_confidence=72, ai_reasoning="200 call")
            _insert(db, symbol="AAPL", occ_symbol="AAPL260612C00210000",
                    ai_confidence=65, ai_reasoning="210 call")

            from journal import get_open_entry_metadata
            m1 = get_open_entry_metadata(db, "AAPL",
                                         occ_symbol="AAPL260612C00200000")
            m2 = get_open_entry_metadata(db, "AAPL",
                                         occ_symbol="AAPL260612C00210000")
            assert m1["ai_confidence"] == 72
            assert m2["ai_confidence"] == 65
        finally:
            os.unlink(db)

    def test_db_failure_logs_and_returns_none_safely(self, caplog):
        """If the DB read fails, the function logs a warning naming
        the symbol + the underlying error and returns None values,
        so the calling close path still logs the trade row (just
        without the propagated metadata). Honors no-silent-failures."""
        from journal import get_open_entry_metadata

        with patch("journal._get_conn",
                   side_effect=RuntimeError("DB locked")):
            with caplog.at_level(logging.WARNING, logger="root"):
                meta = get_open_entry_metadata(
                    "/nonexistent.db", "AAPL",
                )

        assert meta == {"ai_confidence": None, "ai_reasoning": None}
        matching = [r for r in caplog.records
                    if "AAPL" in r.getMessage()
                    and "DB locked" in r.getMessage()]
        assert matching, (
            f"Expected a warning naming AAPL + the DB error. Got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
