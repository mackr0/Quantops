"""Guardrail: backtests must read the historical-augmented universe,
not the live curated-to-tradeable-today list.

History: 2026-04-27 methodology audit closure. Wave 4 / Issue #10 of
METHODOLOGY_FIX_PLAN.md. Backtests previously read
`segments.SEGMENTS[market]["universe"]`, the same list live trading
uses. That list is curated to tradeable-today, so every backtest
silently excluded delisted/renamed/acquired names, biasing measured
performance UP (survivorship bias).

Fix:
1. `segments_historical.py` — frozen baseline of the live lists as of
   2026-04-27, includes everything the system has tracked dead-or-alive.
2. `historical_universe_augment.py` — daily diff of Alpaca's active
   asset set; departures persisted in `historical_universe_additions`.
   `get_augmented_universe(seg, start_date)` returns baseline ∪
   additions whose `last_seen_active >= start_date`.
3. `backtester.py` (3 sites) and `rigorous_backtest.py` updated to
   call `get_augmented_universe`. Live trading paths untouched.

These tests prove:

A. The historical baseline file exists and is non-empty for the four
   equity segments (crypto excluded by design).
B. `record_daily_snapshot` + `diff_and_record_departures` round-trip
   correctly: yesterday's set minus today's set = departures.
C. Re-running the daily diff is idempotent — doesn't duplicate rows.
D. `get_augmented_universe` returns baseline ∪ additions when an
   addition's `last_seen_active >= start_date`.
E. `get_augmented_universe` excludes additions whose
   `last_seen_active < start_date` (we don't pull in symbols that
   died long before the backtest window).
F. Source-level guards: backtester.py and rigorous_backtest.py use
   `get_augmented_universe` and NOT just `seg.get("universe")` for
   non-crypto segments.
G. Live trading paths (`multi_scheduler.run_full_screen_for_segment`
   etc.) do NOT call `get_augmented_universe` — that helper is
   strictly backtest-only.
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
import sqlite3
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Test (A): frozen baseline exists and is sane
# ---------------------------------------------------------------------------

def test_segments_historical_module_exists():
    import segments_historical
    assert hasattr(segments_historical, "MICRO_CAP_UNIVERSE")
    assert hasattr(segments_historical, "SMALL_CAP_UNIVERSE")
    assert hasattr(segments_historical, "MID_CAP_UNIVERSE")
    assert hasattr(segments_historical, "LARGE_CAP_UNIVERSE")
    assert hasattr(segments_historical, "HISTORICAL_UNIVERSES")
    assert hasattr(segments_historical, "FROZEN_AT")


def test_segments_historical_includes_known_dead_tickers():
    """The whole point of the frozen baseline is to include names
    that have since gone dark. SQ, PARA, CFLT, X, AZUL, GPS are the
    canonical examples from the 2026-04-23 / 04-24 fixes. They MUST
    be in the historical baseline; otherwise the survivorship-bias
    fix is symbolic rather than real."""
    import segments_historical
    all_symbols = set()
    for seg_name, lst in segments_historical.HISTORICAL_UNIVERSES.items():
        all_symbols.update(lst)

    must_have = {"SQ", "PARA", "CFLT", "X", "AZUL", "GPS"}
    missing = must_have - all_symbols
    assert not missing, (
        f"Frozen historical baseline is missing known-dead tickers: "
        f"{missing}. The 2026-04-27 freeze captured what segments.py "
        f"contained that day; if any of these are absent the freeze "
        f"didn't actually capture the hand-curated state."
    )


def test_segments_historical_excludes_crypto():
    """Crypto is small, stable, doesn't have a 'delisted' analog in
    the same way equities do. It stays only in segments.py."""
    import segments_historical
    assert "crypto" not in segments_historical.HISTORICAL_UNIVERSES


# ---------------------------------------------------------------------------
# Tests (B), (C): daily snapshot + diff round-trip
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_master_db():
    """Isolated master DB for the universe-audit ledger so tests
    don't read/write the real prod path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Force the augment module to use this path.
    import historical_universe_augment as hua
    importlib.reload(hua)
    hua._schema_initialized = False
    hua.MASTER_DB = path
    yield path
    try:
        os.unlink(path)
    except Exception:
        pass


def test_diff_records_departures_after_snapshot(fresh_master_db):
    import historical_universe_augment as hua
    yesterday = ["AAA", "BBB", "CCC", "DDD"]
    today = ["AAA", "CCC", "DDD"]   # BBB departed

    # snapshot_date param lets the test step through two distinct
    # calendar days without waiting 24 hours.
    hua.record_daily_snapshot(
        yesterday, db_path=fresh_master_db, snapshot_date="2026-04-26",
    )
    new_count = hua.diff_and_record_departures(
        today, db_path=fresh_master_db, snapshot_date="2026-04-27",
    )
    assert new_count == 1, (
        f"Expected exactly 1 new departure (BBB), got {new_count}"
    )

    conn = sqlite3.connect(fresh_master_db)
    rows = conn.execute(
        "SELECT symbol FROM historical_universe_additions"
    ).fetchall()
    conn.close()
    assert {r[0] for r in rows} == {"BBB"}


def test_diff_is_idempotent_on_rerun(fresh_master_db):
    """If the diff runs again with the same `today` against the same
    prior snapshot, no new rows should be inserted; existing
    `last_seen_active` may be bumped."""
    import historical_universe_augment as hua
    hua.record_daily_snapshot(
        ["A", "B"], db_path=fresh_master_db, snapshot_date="2026-04-26",
    )
    n1 = hua.diff_and_record_departures(
        ["A"], db_path=fresh_master_db, snapshot_date="2026-04-27",
    )
    n2 = hua.diff_and_record_departures(
        ["A"], db_path=fresh_master_db, snapshot_date="2026-04-27",
    )

    assert n1 == 1
    assert n2 == 0, (
        "Re-running the diff with identical inputs must NOT insert "
        "duplicate rows. Idempotency contract."
    )


def test_diff_first_run_with_no_prior_snapshot_records_nothing(fresh_master_db):
    """First-ever run has nothing to compare against. Must not flag
    every active symbol as a departure."""
    import historical_universe_augment as hua
    n = hua.diff_and_record_departures(
        ["A", "B", "C"], db_path=fresh_master_db,
        snapshot_date="2026-04-27",
    )
    assert n == 0


# ---------------------------------------------------------------------------
# Tests (D), (E): get_augmented_universe pulls in departures
# ---------------------------------------------------------------------------

def test_augmented_universe_includes_recent_departures(fresh_master_db):
    """A symbol marked departed AFTER the backtest start_date should
    appear in the augmented universe."""
    import historical_universe_augment as hua

    # Manually seed a departure from 2025-01-15 in the small segment
    conn = sqlite3.connect(fresh_master_db)
    hua._init_schema(fresh_master_db)
    conn.execute(
        "INSERT INTO historical_universe_additions "
        "(symbol, last_seen_active, first_seen_inactive, segment) "
        "VALUES (?, ?, ?, ?)",
        ("DEAD1", "2025-01-15", "2025-01-16", "small"),
    )
    conn.commit()
    conn.close()

    universe = hua.get_augmented_universe(
        "small", start_date="2024-06-01", db_path=fresh_master_db,
    )
    assert "DEAD1" in universe


def test_augmented_universe_excludes_pre_window_departures(fresh_master_db):
    """A symbol that died long before the backtest window starts is
    NOT relevant to that window — exclude it."""
    import historical_universe_augment as hua

    conn = sqlite3.connect(fresh_master_db)
    hua._init_schema(fresh_master_db)
    conn.execute(
        "INSERT INTO historical_universe_additions "
        "(symbol, last_seen_active, first_seen_inactive, segment) "
        "VALUES (?, ?, ?, ?)",
        ("OLDDEAD", "2020-03-15", "2020-03-16", "small"),
    )
    conn.commit()
    conn.close()

    universe = hua.get_augmented_universe(
        "small", start_date="2024-06-01", db_path=fresh_master_db,
    )
    assert "OLDDEAD" not in universe


def test_augmented_universe_returns_baseline_for_unknown_segment(fresh_master_db):
    """Crypto isn't in the historical baseline (by design). Should
    return an empty list; callers fall back to the live universe."""
    import historical_universe_augment as hua
    universe = hua.get_augmented_universe("crypto", db_path=fresh_master_db)
    assert universe == []


# ---------------------------------------------------------------------------
# Test (F): backtester source uses get_augmented_universe
# ---------------------------------------------------------------------------

def test_rigorous_backtest_uses_augmented_universe():
    """Source-level guard: rigorous_backtest.validate_strategy must
    call get_augmented_universe for non-crypto markets, otherwise
    backtests slip back to the survivorship-biased live list."""
    import rigorous_backtest
    src = inspect.getsource(rigorous_backtest.validate_strategy)
    assert "get_augmented_universe" in src, (
        "REGRESSION: rigorous_backtest no longer calls "
        "get_augmented_universe. Backtests will read the live "
        "(survivor-biased) universe again. See "
        "METHODOLOGY_FIX_PLAN.md Wave 4 / Issue #10."
    )


def test_backtester_uses_augmented_universe():
    """All three backtester entry points (backtest_strategy,
    _fetch_universe_batch, validate_strategy_with_params) must
    pull from the augmented universe."""
    import backtester
    full_src = inspect.getsource(backtester)
    # Count occurrences — must appear at least 3 times (once per
    # backtest entry point).
    n = full_src.count("get_augmented_universe")
    assert n >= 3, (
        f"backtester.py only references get_augmented_universe {n} "
        f"times; expected at least 3 (one per backtest entry point). "
        f"Some path is still reading from segments.py directly, which "
        f"means that backtest will silently survivorship-bias its "
        f"results."
    )


# ---------------------------------------------------------------------------
# Test (G): live paths do NOT use get_augmented_universe
# ---------------------------------------------------------------------------

def test_live_trading_does_not_use_augmented_universe():
    """The augmented universe is for backtests only. Live trading
    must not read it — it would inject delisted symbols into
    `screen_dynamic_universe`'s sample, which would then hit yfinance
    fallback paths and reproduce the dead-ticker spam fixed on
    2026-04-23."""
    import multi_scheduler
    src = inspect.getsource(multi_scheduler)
    # The sole exception: the daily _task_universe_audit imports the
    # module to record snapshots. That's fine — it doesn't call
    # get_augmented_universe (which is the read path), only
    # record_daily_snapshot + diff_and_record_departures (the write
    # path).
    assert "get_augmented_universe" not in src, (
        "REGRESSION: multi_scheduler.py is now reading the augmented "
        "universe. That helper is for BACKTESTS ONLY — it includes "
        "delisted symbols by design, which would re-introduce the "
        "yfinance dead-ticker spam if used in live paths. Use "
        "screener.get_active_alpaca_symbols for live filtering."
    )


def test_screener_does_not_use_augmented_universe():
    """Same constraint for screener.py."""
    import screener
    src = inspect.getsource(screener)
    assert "get_augmented_universe" not in src, (
        "REGRESSION: screener.py is using the backtest-only "
        "augmented-universe helper. Live screening must use "
        "Alpaca-active filtering, not backtest history."
    )
