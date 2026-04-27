"""Guardrails for `short_borrow.py` — TECHNICAL_DOCUMENTATION.md §15
deferred-item closure (overnight-short borrow accrual).

Tests:
1. compute_borrow_cost math: notional × bps/day × days, basis-point
   convention, monotonic in all 3 inputs.
2. Sub-1-day shorts get $0 (intraday cover, no overnight fee).
3. Hard-to-borrow override returns the per-symbol rate, not the default.
4. Defensive: zero / negative inputs return $0 cleanly.
5. accrue_for_cover: with no journal entry, returns 0.0 (fail-open).
6. accrue_for_cover end-to-end: seed a 5-day-old sell_short, verify
   cost matches compute_borrow_cost output.
7. trader.check_exits source-level guard: must reference the
   accrue_for_cover hook in the cover branch.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# compute_borrow_cost math
# ---------------------------------------------------------------------------

def test_compute_basic_math():
    import short_borrow as sb
    # 100 shares @ $50 = $5000 notional; 0.5 bps/day × 10 days = 5 bps
    # = 0.05% × $5000 = $2.50
    cost = sb.compute_borrow_cost(100, 50.0, 10, bps_per_day=0.5)
    assert cost == pytest.approx(2.5, abs=0.01)


def test_zero_or_negative_inputs_return_zero():
    import short_borrow as sb
    assert sb.compute_borrow_cost(0, 50.0, 10) == 0.0
    assert sb.compute_borrow_cost(100, 0.0, 10) == 0.0
    assert sb.compute_borrow_cost(100, 50.0, 0) == 0.0
    assert sb.compute_borrow_cost(-100, 50.0, 10) == 0.0


def test_hard_to_borrow_override_used():
    import short_borrow as sb
    # GME hardcoded to 12 bps/day — much higher than default 0.5
    rate_general = sb.get_borrow_rate_bps_per_day("AAPL")
    rate_gme = sb.get_borrow_rate_bps_per_day("GME")
    assert rate_general == sb.DEFAULT_BPS_PER_DAY
    assert rate_gme == 12.0

    # Same trade through compute_borrow_cost: GME costs more
    cost_aapl = sb.compute_borrow_cost(100, 50.0, 5, symbol="AAPL")
    cost_gme = sb.compute_borrow_cost(100, 50.0, 5, symbol="GME")
    assert cost_gme > cost_aapl * 5, (
        "Hard-to-borrow override must materially increase the accrued "
        "cost. GME at 12 bps/day vs default 0.5 bps/day → 24x ratio."
    )


def test_monotonic_in_each_input():
    """Borrow cost should rise with each of: shares, price, days,
    bps_per_day. Sanity check the formula direction."""
    import short_borrow as sb
    base = sb.compute_borrow_cost(100, 50.0, 5, bps_per_day=0.5)
    assert sb.compute_borrow_cost(200, 50.0, 5, bps_per_day=0.5) > base
    assert sb.compute_borrow_cost(100, 100.0, 5, bps_per_day=0.5) > base
    assert sb.compute_borrow_cost(100, 50.0, 10, bps_per_day=0.5) > base
    assert sb.compute_borrow_cost(100, 50.0, 5, bps_per_day=1.0) > base


# ---------------------------------------------------------------------------
# accrue_for_cover end-to-end
# ---------------------------------------------------------------------------

@pytest.fixture
def journal_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            status TEXT DEFAULT 'open'
        )
    """)
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def test_accrue_returns_zero_when_no_journal_entry(journal_db):
    import short_borrow as sb
    # No sell_short rows in the DB — must fail-open with 0.0
    assert sb.accrue_for_cover(journal_db, "TSLA", cover_shares=100) == 0.0


def test_accrue_returns_zero_for_intraday_cover(journal_db):
    """A short opened 2 hours ago and covered now has < 1 calendar
    day held → no overnight borrow → 0.0."""
    import short_borrow as sb
    conn = sqlite3.connect(journal_db)
    entry_ts = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, status) "
        "VALUES (?, 'TSLA', 'sell_short', 100, 200.0, 'open')",
        (entry_ts,),
    )
    conn.commit()
    conn.close()
    assert sb.accrue_for_cover(journal_db, "TSLA", cover_shares=100) == 0.0


def test_accrue_charges_overnight_short(journal_db):
    """Short opened 5 calendar days ago: cost = 100 × 200 × 0.5/10000
    × 5 = $5.00 with default rate."""
    import short_borrow as sb
    conn = sqlite3.connect(journal_db)
    entry_ts = (datetime.utcnow() - timedelta(days=5)).isoformat()
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, status) "
        "VALUES (?, 'TSLA', 'sell_short', 100, 200.0, 'open')",
        (entry_ts,),
    )
    conn.commit()
    conn.close()

    cost = sb.accrue_for_cover(journal_db, "TSLA", cover_shares=100)
    expected = 100 * 200.0 * (0.5 / 10000) * 5
    # Tolerate a few seconds of clock drift
    assert cost == pytest.approx(expected, rel=0.01), (
        f"5-day overnight short on 100 sh @ $200 should accrue "
        f"~${expected:.2f}, got ${cost}"
    )


def test_accrue_returns_zero_with_no_db_path():
    import short_borrow as sb
    assert sb.accrue_for_cover(None, "TSLA", cover_shares=100) == 0.0


# ---------------------------------------------------------------------------
# trader.check_exits source-level integration guard
# ---------------------------------------------------------------------------

def test_check_exits_subtracts_borrow_cost_on_cover():
    """The cover path in trader.check_exits MUST call accrue_for_cover
    and subtract the result from pnl. Otherwise overnight-short P&L
    is over-reported and meta-model labels are biased."""
    import trader
    src = inspect.getsource(trader.check_exits)
    assert "accrue_for_cover" in src, (
        "REGRESSION: trader.check_exits no longer references "
        "accrue_for_cover. Overnight-short P&L will over-report by "
        "the full borrow accrual. See TECHNICAL_DOCUMENTATION.md §15."
    )
    assert "borrow_cost" in src, (
        "REGRESSION: cover path no longer subtracts borrow cost. "
        "The integration was the whole point — without it, the helper "
        "exists but is unused."
    )
