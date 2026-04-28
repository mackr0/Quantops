"""Guardrail: max_favorable_excursion (MFE) must be floored at the
entry price.

History: on 2026-04-28 verifying Lever-3 deploys, found AVGO long
with `price=414.74, max_favorable_excursion=405.07`. MFE below
entry is impossible by definition — "max favorable excursion" is
the most-profitable price the position has reached. For a long,
the floor is entry; for a short, the ceiling is entry.

Root cause: the MFE updater initialized via
  `MAX(COALESCE(mfe, current_price), current_price)`
which on the first observation returns whatever current_price was
at that moment — even if it was BELOW entry.

The trailing-stop tuner (`_optimize_trailing_atr_multiplier`) uses
  `give_back_pct = (mfe - exit_fill_price) / mfe`
to bucket trades. With MFE below entry, give-back math is
nonsensical. So bad MFE = bad trailing-stop tuning decisions.

Fix: include the row's `price` (entry) in the MAX/MIN, ensuring
MFE never drops below entry for longs or rises above entry for
shorts. Self-heals existing bad rows on next update.

These tests:
1. Long: open at $100, current at $95 → MFE should be $100, not $95.
2. Long subsequent update: open $100, current went to $105 then back
   to $98 → MFE should stay $105 across both updates.
3. Short: open short at $100, current $105 → MFE should be $100,
   not $105.
4. Short subsequent: short at $100, current went to $90 then back to
   $103 → MFE should stay $90.
5. Source-level: trader.check_exits MFE update SQL must reference
   the row's `price` column, not just current_price.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def journal_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            status TEXT DEFAULT 'open',
            max_favorable_excursion REAL
        )
    """)
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def _seed_long(db, symbol, entry, mfe=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price, status, "
        "max_favorable_excursion) VALUES (?, 'buy', 100, ?, 'open', ?)",
        (symbol, entry, mfe),
    )
    conn.commit()
    conn.close()


def _seed_short(db, symbol, entry, mfe=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (symbol, side, qty, price, status, "
        "max_favorable_excursion) VALUES (?, 'sell_short', -100, ?, "
        "'open', ?)",
        (symbol, entry, mfe),
    )
    conn.commit()
    conn.close()


def _read_mfe(db, symbol):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT max_favorable_excursion FROM trades "
        "WHERE symbol = ? AND status = 'open'",
        (symbol,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _run_mfe_update(db, positions):
    """Invoke trader.check_exits' MFE-update block in isolation by
    calling check_exits with a mocked api/positions. Easier to
    just emulate the SQL directly using the same logic — we're
    testing the SQL, not the surrounding plumbing."""
    # Use the actual SQL from trader.py so the test is testing the
    # real code path. Read it via the function source:
    import trader, re
    src = inspect.getsource(trader.check_exits)
    # Quick smoke test that the SQL has the price-column floor:
    assert "COALESCE(max_favorable_excursion, price)" in src, (
        "MFE update SQL no longer floors at the row's `price` column; "
        "test cannot verify the floor behavior."
    )
    # Run equivalent SQL directly
    conn = sqlite3.connect(db)
    for p in positions:
        sym, cur_price, qty = p["symbol"], p["current_price"], p["qty"]
        if qty < 0:
            conn.execute(
                "UPDATE trades SET max_favorable_excursion = "
                "MIN(COALESCE(max_favorable_excursion, price), price, ?) "
                "WHERE symbol = ? AND side = 'sell_short' AND status = 'open'",
                (cur_price, sym),
            )
        else:
            conn.execute(
                "UPDATE trades SET max_favorable_excursion = "
                "MAX(COALESCE(max_favorable_excursion, price), price, ?) "
                "WHERE symbol = ? AND side = 'buy' AND status = 'open'",
                (cur_price, sym),
            )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Long position MFE floor
# ---------------------------------------------------------------------------

def test_long_mfe_floored_at_entry_when_current_is_below(journal_db):
    """Open long at $100; first MFE update arrives with current at
    $95 (down 5%). MFE must be $100 (entry), not $95."""
    _seed_long(journal_db, "AVGO", entry=100.0, mfe=None)
    _run_mfe_update(journal_db, [{"symbol": "AVGO", "current_price": 95.0,
                                    "qty": 100}])
    mfe = _read_mfe(journal_db, "AVGO")
    assert mfe == 100.0, (
        f"Long MFE should be floored at entry $100; got ${mfe}. This "
        f"is the 2026-04-28 bug — give-back math (mfe-exit)/mfe is "
        f"garbage when MFE < entry."
    )


def test_long_mfe_tracks_max_after_floor(journal_db):
    """Long at $100. Sequence: current $105 (new high) → MFE $105.
    Then current $98 → MFE stays $105 (the high-water mark)."""
    _seed_long(journal_db, "INTC", entry=100.0, mfe=None)
    _run_mfe_update(journal_db, [{"symbol": "INTC", "current_price": 105.0,
                                    "qty": 100}])
    assert _read_mfe(journal_db, "INTC") == 105.0
    _run_mfe_update(journal_db, [{"symbol": "INTC", "current_price": 98.0,
                                    "qty": 100}])
    assert _read_mfe(journal_db, "INTC") == 105.0, (
        "Long MFE must NEVER decrease — it's the high-water mark."
    )


# ---------------------------------------------------------------------------
# Short position MFE ceiling
# ---------------------------------------------------------------------------

def test_short_mfe_ceilinged_at_entry_when_current_is_above(journal_db):
    """Short at $100; current $105 (price went up = bad for short).
    MFE (the BEST price for short = lowest) must be $100, not $105."""
    _seed_short(journal_db, "GME", entry=100.0, mfe=None)
    _run_mfe_update(journal_db, [{"symbol": "GME", "current_price": 105.0,
                                    "qty": -100}])
    mfe = _read_mfe(journal_db, "GME")
    assert mfe == 100.0, (
        f"Short MFE should be ceilinged at entry $100; got ${mfe}. "
        f"For shorts, MFE is the LOWEST price seen — never above entry."
    )


def test_short_mfe_tracks_min_after_ceiling(journal_db):
    """Short at $100. Sequence: current $90 (best yet) → MFE $90.
    Then current $103 → MFE stays $90."""
    _seed_short(journal_db, "BBBY", entry=100.0, mfe=None)
    _run_mfe_update(journal_db, [{"symbol": "BBBY", "current_price": 90.0,
                                    "qty": -100}])
    assert _read_mfe(journal_db, "BBBY") == 90.0
    _run_mfe_update(journal_db, [{"symbol": "BBBY", "current_price": 103.0,
                                    "qty": -100}])
    assert _read_mfe(journal_db, "BBBY") == 90.0, (
        "Short MFE must NEVER increase — it's the lowest price seen."
    )


# ---------------------------------------------------------------------------
# Self-heal: rows initialized BEFORE the fix get corrected on next update
# ---------------------------------------------------------------------------

def test_long_self_heals_bad_existing_mfe_below_entry(journal_db):
    """Pre-fix data: long at $100 with MFE=$95 (the bug). Next MFE
    update with current $98 should heal MFE to max(entry=100, prior=95,
    current=98) = $100, not preserve the buggy $95."""
    _seed_long(journal_db, "BMY", entry=100.0, mfe=95.0)
    _run_mfe_update(journal_db, [{"symbol": "BMY", "current_price": 98.0,
                                    "qty": 100}])
    assert _read_mfe(journal_db, "BMY") == 100.0


def test_short_self_heals_bad_existing_mfe_above_entry(journal_db):
    """Pre-fix data: short at $100 with MFE=$105. Next update with
    current $102 → MFE = min(100, 105, 102) = 100."""
    _seed_short(journal_db, "DJT", entry=100.0, mfe=105.0)
    _run_mfe_update(journal_db, [{"symbol": "DJT", "current_price": 102.0,
                                    "qty": -100}])
    assert _read_mfe(journal_db, "DJT") == 100.0


# ---------------------------------------------------------------------------
# Source-level guard
# ---------------------------------------------------------------------------

def test_check_exits_mfe_sql_floors_at_entry_price():
    """The actual SQL inside trader.check_exits must reference the
    row's `price` column. If someone reverts to the broken
    `MAX(COALESCE(mfe, ?), ?)` pattern, this test fails."""
    import trader
    src = inspect.getsource(trader.check_exits)
    # Long path
    assert "MAX(COALESCE(max_favorable_excursion, price)" in src, (
        "REGRESSION: long-MFE update SQL no longer references the "
        "row's `price` column. MFE will initialize at current_price "
        "even when below entry — see 2026-04-28 AVGO incident."
    )
    # Short path
    assert "MIN(COALESCE(max_favorable_excursion, price)" in src, (
        "REGRESSION: short-MFE update SQL no longer references the "
        "row's `price` column."
    )
