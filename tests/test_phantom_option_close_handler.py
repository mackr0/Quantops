"""Structural guardrail: when an option close is rejected with a
phantom-journal pattern (broker has no matching position), the
journal row is marked canceled and the operator is notified — once.

The bug class.
A multi-leg combo loses its leg-pair link, OR a manual broker-side
close doesn't reflect back into the journal, OR a reconcile pass
misses a symbol → the journal shows the option as open but the
broker doesn't have it. Each cycle, the exit-checker fires a close
attempt, the broker rejects with 403 "uncovered" or 422 "intent
mismatch", and the journal stays open for the NEXT cycle to retry.
~196 close-rejections per day across all profiles, silently, for
2+ days before this fix.

The handler must:
  1. Match the phantom-error pattern (NOT fire on transient errors).
  2. Mark the matching journal row status='canceled' with a reason.
  3. Fire notify_error so the operator hears about it on first
     occurrence (subject debounce in notify_error itself prevents
     spam from repeated cycles, until the journal is cleaned up).
  4. Do nothing if no matching journal row was found (already
     cleaned up).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def journal_with_open_option(tmp_path):
    """Create a profile DB with one open option position the
    handler will try to clean up."""
    from journal import init_db
    db = str(tmp_path / "quantopsai_profile_999.db")
    init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "signal_type, status, occ_symbol) "
            "VALUES (datetime('now'), 'AAPL', 'buy', 1, 5.50, "
            "'OPTIONS', 'open', 'AAPL260618C00310000')"
        )
        conn.commit()
    return db


class TestPhantomOptionCloseHandler:
    def test_phantom_403_marks_journal_canceled(
            self, journal_with_open_option):
        from trader import _handle_phantom_option_close
        with patch("notifications.notify_error") as mock_notify:
            _handle_phantom_option_close(
                journal_with_open_option,
                occ_symbol="AAPL260618C00310000",
                underlying="AAPL", qty=1,
                rejection_reason=(
                    'Alpaca order rejected (403): '
                    '{"code":40310000,"message":"account not eligible '
                    'to trade uncovered option contracts"}'
                ),
            )
        # Journal row should now be canceled
        with closing(sqlite3.connect(journal_with_open_option)) as conn:
            row = conn.execute(
                "SELECT status, reason FROM trades "
                "WHERE occ_symbol='AAPL260618C00310000'"
            ).fetchone()
        assert row[0] == "canceled", (
            f"Expected status='canceled', got {row[0]!r}"
        )
        assert "phantom" in (row[1] or "").lower()
        # Notify should have fired exactly once
        assert mock_notify.call_count == 1, (
            f"Expected 1 notify_error call, got {mock_notify.call_count}"
        )

    def test_phantom_422_marks_journal_canceled(
            self, journal_with_open_option):
        from trader import _handle_phantom_option_close
        with patch("notifications.notify_error") as mock_notify:
            _handle_phantom_option_close(
                journal_with_open_option,
                occ_symbol="AAPL260618C00310000",
                underlying="AAPL", qty=1,
                rejection_reason=(
                    'Alpaca order rejected (422): '
                    '{"code":42210000,"message":"position intent '
                    'mismatch, inferred: sell_to_open, specified: '
                    'sell_to_close"}'
                ),
            )
        with closing(sqlite3.connect(journal_with_open_option)) as conn:
            row = conn.execute(
                "SELECT status FROM trades "
                "WHERE occ_symbol='AAPL260618C00310000'"
            ).fetchone()
        assert row[0] == "canceled"
        assert mock_notify.call_count == 1

    def test_transient_error_does_not_cancel(
            self, journal_with_open_option):
        """Network blip / throttle / Alpaca 503 must NOT mark the
        journal canceled — the next cycle should retry. Only
        broker-side phantom errors should trigger cleanup."""
        from trader import _handle_phantom_option_close
        with patch("notifications.notify_error") as mock_notify:
            _handle_phantom_option_close(
                journal_with_open_option,
                occ_symbol="AAPL260618C00310000",
                underlying="AAPL", qty=1,
                rejection_reason="Connection reset by peer",
            )
        with closing(sqlite3.connect(journal_with_open_option)) as conn:
            row = conn.execute(
                "SELECT status FROM trades "
                "WHERE occ_symbol='AAPL260618C00310000'"
            ).fetchone()
        assert row[0] == "open", (
            f"Transient error must NOT cancel; got status={row[0]!r}"
        )
        assert mock_notify.call_count == 0

    def test_no_match_in_journal_is_silent(
            self, journal_with_open_option):
        """If the OCC symbol isn't in the journal (already cleaned up
        or never was), do nothing — no error, no notification."""
        from trader import _handle_phantom_option_close
        with patch("notifications.notify_error") as mock_notify:
            _handle_phantom_option_close(
                journal_with_open_option,
                occ_symbol="ZZZ260618C00100000",  # not in journal
                underlying="ZZZ", qty=1,
                rejection_reason=(
                    'Alpaca order rejected (403): '
                    'account not eligible to trade uncovered option'
                ),
            )
        # Notify should NOT fire (nothing to clean up)
        assert mock_notify.call_count == 0

    def test_idempotent_already_canceled(self, journal_with_open_option):
        """Re-running on an already-canceled row is a no-op (no
        second notify, no error)."""
        from trader import _handle_phantom_option_close
        # First run cancels it
        with patch("notifications.notify_error"):
            _handle_phantom_option_close(
                journal_with_open_option,
                occ_symbol="AAPL260618C00310000",
                underlying="AAPL", qty=1,
                rejection_reason=(
                    "account not eligible to trade uncovered option"
                ),
            )
        # Second run should be a no-op
        with patch("notifications.notify_error") as mock_notify:
            _handle_phantom_option_close(
                journal_with_open_option,
                occ_symbol="AAPL260618C00310000",
                underlying="AAPL", qty=1,
                rejection_reason=(
                    "account not eligible to trade uncovered option"
                ),
            )
        assert mock_notify.call_count == 0
