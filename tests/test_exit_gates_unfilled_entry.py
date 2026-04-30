"""Guardrail: check_exits must not submit a sell against an unfilled
limit BUY. This is the bug that hit Large Cap Limit Orders on
2026-04-27 — Check Exits failed with Alpaca's
"cannot open a short sell while a long buy order is open" because
the journal showed an open virtual position before the broker had
filled the entry order.

Tests the helper directly (`_entry_order_filled_at_broker`) plus a
contract test that the gate is wired into the check_exits flow.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def journal_db():
    """Minimal trades-table fixture. Schema matches the prod table for
    the columns this test cares about; the rest of the fields aren't
    touched by `_entry_order_filled_at_broker`."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            order_id TEXT,
            status TEXT DEFAULT 'open'
        )
        """
    )
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def _insert_entry(db, symbol, side, order_id, status="open"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price, order_id, status) "
        "VALUES (?, ?, 100, 50.0, ?, ?)",
        (symbol, side, order_id, status),
    )
    conn.commit()
    conn.close()


def _api_with_status(status):
    api = MagicMock()
    order = MagicMock()
    order.status = status
    api.get_order.return_value = order
    return api


# ---------------------------------------------------------------------------
# Helper-level tests — every relevant Alpaca status
# ---------------------------------------------------------------------------

def test_filled_entry_allows_exit(journal_db):
    from trader import _entry_order_filled_at_broker

    _insert_entry(journal_db, "AAPL", "buy", "ord-filled")
    api = _api_with_status("filled")

    assert _entry_order_filled_at_broker(api, journal_db, "AAPL", is_short=False) is True


def test_partially_filled_entry_allows_exit(journal_db):
    """Partial fill = some shares exist = exit can sell what's there.
    Alpaca won't reject a SELL when even one real share backs it."""
    from trader import _entry_order_filled_at_broker

    _insert_entry(journal_db, "AAPL", "buy", "ord-partial")
    api = _api_with_status("partially_filled")

    assert _entry_order_filled_at_broker(api, journal_db, "AAPL", is_short=False) is True


@pytest.mark.parametrize("pending_status", [
    "new", "accepted", "pending_new", "pending_replace", "pending_cancel",
    "accepted_for_bidding", "held", "suspended",
])
def test_pending_entry_blocks_exit(journal_db, pending_status):
    """The exact bug we're guarding against. An entry stuck in any
    pending state means no real shares — sell would be a short."""
    from trader import _entry_order_filled_at_broker

    _insert_entry(journal_db, "AAPL", "buy", "ord-pending")
    api = _api_with_status(pending_status)

    assert _entry_order_filled_at_broker(api, journal_db, "AAPL", is_short=False) is False, (
        f"Entry in status={pending_status!r} must block the exit. "
        f"Otherwise check_exits will submit a SELL against zero real "
        f"shares and Alpaca rejects with 'cannot open a short sell "
        f"while a long buy order is open'."
    )


def test_short_entry_lookup_uses_sell_short_side(journal_db):
    """For a short position, the entry side is 'sell_short' — the
    helper must look up that side, not 'buy'."""
    from trader import _entry_order_filled_at_broker

    _insert_entry(journal_db, "TSLA", "sell_short", "ord-short")
    api = _api_with_status("filled")

    assert _entry_order_filled_at_broker(api, journal_db, "TSLA", is_short=True) is True


def test_short_entry_pending_blocks_cover(journal_db):
    from trader import _entry_order_filled_at_broker

    _insert_entry(journal_db, "TSLA", "sell_short", "ord-short-pending")
    api = _api_with_status("accepted")

    assert _entry_order_filled_at_broker(api, journal_db, "TSLA", is_short=True) is False


# ---------------------------------------------------------------------------
# Fail-open semantics — uncertain paths must allow the exit, NOT block it
# ---------------------------------------------------------------------------

def test_no_db_path_allows_exit():
    """Some callers pass db_path=None (e.g. legacy cron paths).
    Don't block exits on that — fall back to historical behavior."""
    from trader import _entry_order_filled_at_broker
    api = _api_with_status("filled")
    assert _entry_order_filled_at_broker(api, None, "AAPL", is_short=False) is True


def test_no_matching_journal_row_allows_exit(journal_db):
    """The journal has no open buy row for this symbol — the position
    came from somewhere else (e.g. manual placement). Don't block."""
    from trader import _entry_order_filled_at_broker
    api = _api_with_status("filled")
    assert _entry_order_filled_at_broker(api, journal_db, "ZZZ", is_short=False) is True


def test_journal_row_without_order_id_allows_exit(journal_db):
    """Old rows may have NULL order_id. Can't verify, so allow."""
    from trader import _entry_order_filled_at_broker
    _insert_entry(journal_db, "AAPL", "buy", None)
    api = _api_with_status("filled")
    assert _entry_order_filled_at_broker(api, journal_db, "AAPL", is_short=False) is True


def test_broker_unrecognized_order_id_allows_exit(journal_db):
    """Alpaca's `get_order` raises for cleaned-up old IDs. Don't gate
    the exit on a 404 — that order is gone, the position is real."""
    from trader import _entry_order_filled_at_broker
    _insert_entry(journal_db, "AAPL", "buy", "stale-id")
    api = MagicMock()
    api.get_order.side_effect = RuntimeError("order not found")
    assert _entry_order_filled_at_broker(api, journal_db, "AAPL", is_short=False) is True


def test_journal_query_error_allows_exit(journal_db):
    """If the SQL itself errors, default-allow rather than break exits."""
    from trader import _entry_order_filled_at_broker
    api = _api_with_status("filled")
    # Point at a non-existent file — sqlite returns a connect error path,
    # but the helper swallows it and returns True. We use a known-bad
    # path.
    bad = "/nonexistent/dir/no-such.db"
    # On most systems sqlite3.connect(bad) opens lazily and the SELECT
    # fails. Either way, helper must return True.
    assert _entry_order_filled_at_broker(api, bad, "AAPL", is_short=False) is True


# ---------------------------------------------------------------------------
# Contract test — the gate must be wired into check_exits
# ---------------------------------------------------------------------------

def test_check_exits_calls_the_filled_gate():
    """Source-level guard: if someone removes the call from
    check_exits, this test fails — preventing a silent regression to
    the original bug. The body lives in _process_exit_trigger
    (extracted during the 2026-04-30 resilience refactor)."""
    import trader

    src = (inspect.getsource(trader.check_exits)
           + inspect.getsource(trader._process_exit_trigger))
    assert "_entry_order_filled_at_broker" in src, (
        "REGRESSION: polling-exit code no longer calls "
        "_entry_order_filled_at_broker. This was the gate added on "
        "2026-04-27 to prevent submitting SELLs against zero real "
        "shares for virtual profiles using limit-order entries. "
        "Without it, Large Cap Limit Orders (and any future limit-"
        "order profile) will fail Check Exits with Alpaca's "
        "'cannot open a short sell while a long buy order is open'."
    )
