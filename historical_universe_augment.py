"""Auto-augmentation of the historical universe (Wave 4 / Issue #10).

When a symbol falls off Alpaca's active asset list (delisted, taken
private, renamed, acquired), this module captures it before the
information is lost. Backtests over windows that include
`last_seen_active` for that symbol pull it back into the universe,
fixing survivorship bias forward in time.

Schema
------
`historical_universe_additions` table (in master `quantopsai.db`):

    symbol               TEXT PRIMARY KEY
    last_seen_active     TEXT NOT NULL  -- YYYY-MM-DD
    first_seen_inactive  TEXT NOT NULL
    segment              TEXT           -- 'micro' | 'small' | 'midcap' |
                                           'largecap' | NULL if unknown

Daily flow
----------
1. `_task_universe_audit` runs daily.
2. It pulls today's Alpaca active US-equity asset set (cached helper
   from `screener.get_active_alpaca_symbols`, no new API calls).
3. It compares today's set to a snapshot from yesterday persisted in
   `daily_active_universe_snapshots`.
4. Symbols in yesterday's set but not today's: insert into
   `historical_universe_additions` (or update last_seen_active if
   already present). Tag them with the segment they belonged to in
   the frozen baseline (`segments_historical`), best-effort.
5. Today's full set is then persisted as today's snapshot for
   tomorrow's diff.

Backtest read path
------------------
`get_augmented_universe(segment_name, start_date)` returns the
frozen historical baseline ∪ {additions where last_seen_active >=
start_date}. This is what backtester.py and rigorous_backtest.py
should call instead of `seg.get("universe")`.

Live trading is NEVER touched — `segments.py` remains the live
source of truth.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Master DB holds the ledger so any backtester can read it without
# needing a per-profile context.
MASTER_DB = os.environ.get("QUANTOPSAI_MASTER_DB", "quantopsai.db")

_schema_lock = threading.Lock()
_schema_initialized = False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _init_schema(db_path: str = MASTER_DB) -> None:
    """Create tables if missing. Idempotent."""
    global _schema_initialized
    with _schema_lock:
        if _schema_initialized:
            return
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS historical_universe_additions (
                    symbol               TEXT PRIMARY KEY,
                    last_seen_active     TEXT NOT NULL,
                    first_seen_inactive  TEXT NOT NULL,
                    segment              TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_active_universe_snapshots (
                    snapshot_date TEXT PRIMARY KEY,
                    symbols_json  TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_hist_universe_last_seen "
                "ON historical_universe_additions(last_seen_active)"
            )
            conn.commit()
            conn.close()
            _schema_initialized = True
        except Exception as exc:
            logger.warning("Failed to init historical_universe schema: %s", exc)


# ---------------------------------------------------------------------------
# Daily snapshot + diff
# ---------------------------------------------------------------------------

def _segment_for_symbol(symbol: str) -> Optional[str]:
    """Best-effort: which frozen-baseline segment did this symbol
    belong to? Returns None if unknown (newer name not in baseline)."""
    try:
        from segments_historical import HISTORICAL_UNIVERSES
        for seg, names in HISTORICAL_UNIVERSES.items():
            if symbol in names:
                return seg
    except Exception:
        pass
    return None


def record_daily_snapshot(active_symbols: Iterable[str],
                          db_path: str = MASTER_DB,
                          snapshot_date: Optional[str] = None) -> int:
    """Persist today's active-asset set as the snapshot for tomorrow's
    diff. Returns the number of symbols recorded.

    Idempotent: re-running on the same date overwrites that date's
    snapshot rather than duplicating.

    `snapshot_date` is for tests — production calls omit it and the
    function uses today's UTC date.
    """
    _init_schema(db_path)
    today = snapshot_date or datetime.utcnow().date().isoformat()
    syms = sorted(set(active_symbols))
    try:
        import json as _json
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO daily_active_universe_snapshots "
            "(snapshot_date, symbols_json) VALUES (?, ?)",
            (today, _json.dumps(syms)),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Failed to record daily snapshot: %s", exc)
        return 0
    return len(syms)


def _load_most_recent_snapshot_before(
    today_iso: str, db_path: str = MASTER_DB,
) -> Optional[Set[str]]:
    """Return the symbol set from the most recent snapshot dated
    strictly before `today_iso`. None if there's no prior snapshot."""
    try:
        import json as _json
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT symbols_json FROM daily_active_universe_snapshots "
            "WHERE snapshot_date < ? "
            "ORDER BY snapshot_date DESC LIMIT 1",
            (today_iso,),
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    try:
        return set(_json.loads(row[0]))
    except Exception:
        return None


def diff_and_record_departures(active_symbols: Iterable[str],
                                db_path: str = MASTER_DB,
                                snapshot_date: Optional[str] = None) -> int:
    """Compare today's active-asset set with the most recent prior
    snapshot. Symbols in prior set but not today's are inserted into
    `historical_universe_additions`. Returns the number of NEW
    departures recorded today (excluding ones already in the table).

    Idempotent: a symbol already present in the additions table has
    its `last_seen_active` updated, never duplicated.

    `snapshot_date` is for tests — production calls omit it.
    """
    _init_schema(db_path)
    today = snapshot_date or datetime.utcnow().date().isoformat()
    today_set = set(active_symbols)

    prior_set = _load_most_recent_snapshot_before(today, db_path)
    if prior_set is None:
        # First-ever snapshot run; nothing to diff against.
        return 0

    departed = prior_set - today_set
    if not departed:
        return 0

    new_count = 0
    try:
        conn = sqlite3.connect(db_path)
        for sym in departed:
            seg = _segment_for_symbol(sym)
            cur = conn.execute(
                "SELECT 1 FROM historical_universe_additions "
                "WHERE symbol = ?",
                (sym,),
            ).fetchone()
            if cur:
                # Already in the ledger — bump last_seen_active.
                conn.execute(
                    "UPDATE historical_universe_additions "
                    "SET last_seen_active = ? "
                    "WHERE symbol = ?",
                    (today, sym),
                )
            else:
                conn.execute(
                    "INSERT INTO historical_universe_additions "
                    "(symbol, last_seen_active, first_seen_inactive, segment) "
                    "VALUES (?, ?, ?, ?)",
                    (sym, today, today, seg),
                )
                new_count += 1
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Failed to record departures: %s", exc)
        return 0
    return new_count


# ---------------------------------------------------------------------------
# Backtest universe read path
# ---------------------------------------------------------------------------

def get_augmented_universe(segment_name: str,
                            start_date: Optional[str] = None,
                            db_path: str = MASTER_DB) -> List[str]:
    """Return the universe a backtest should use for `segment_name`.

    Composition:
    - The frozen historical baseline from `segments_historical`
    - PLUS additions whose `last_seen_active >= start_date`
      (or all additions in the segment if `start_date` is None)

    The frozen baseline carries everything the system has ever
    tracked dead-or-alive as of FROZEN_AT. The additions table
    captures every death observed since then. The union resolves
    survivorship bias for any backtest window that overlaps the
    active period of any departed symbol.
    """
    try:
        from segments_historical import get_historical_universe
        baseline = get_historical_universe(segment_name)
    except Exception:
        baseline = []

    # Fetch additions for this segment.
    additions: List[str] = []
    try:
        _init_schema(db_path)
        conn = sqlite3.connect(db_path)
        if start_date:
            rows = conn.execute(
                "SELECT symbol FROM historical_universe_additions "
                "WHERE segment = ? AND last_seen_active >= ?",
                (segment_name, start_date),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT symbol FROM historical_universe_additions "
                "WHERE segment = ?",
                (segment_name,),
            ).fetchall()
        conn.close()
        additions = [r[0] for r in rows if r and r[0]]
    except Exception:
        additions = []

    # Union, preserve order: baseline first (deterministic for tests),
    # then additions that aren't already in baseline.
    seen = set(baseline)
    out = list(baseline)
    for sym in additions:
        if sym not in seen:
            out.append(sym)
            seen.add(sym)
    return out


def departures_summary(db_path: str = MASTER_DB) -> dict:
    """Counts and basic stats for surfacing in dashboards / backtest
    output. Returns:
        {"total_recorded": int,
         "by_segment": {"small": int, ...},
         "frozen_at": str}
    """
    _init_schema(db_path)
    out = {"total_recorded": 0, "by_segment": {}, "frozen_at": None}
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT segment, COUNT(*) FROM historical_universe_additions "
            "GROUP BY segment"
        ).fetchall()
        conn.close()
        for seg, ct in rows:
            out["total_recorded"] += int(ct)
            if seg:
                out["by_segment"][seg] = int(ct)
    except Exception:
        pass
    try:
        from segments_historical import FROZEN_AT
        out["frozen_at"] = FROZEN_AT
    except Exception:
        pass
    return out
