"""SQLite storage for edgar_form4.

One DB file at `data/edgar_form4.db`. Four primary tables:

  companies     — ticker → CIK mapping (one row per public company)
  form4_filings — one row per Form 4 (accession_number primary key)
  insider_txns  — one row per transaction within a Form 4
                  (a single Form 4 can disclose multiple txns)
  raw_filings   — XML preserved before parsing (so parser changes can
                  re-process historical data without re-scraping)

Plus `scrape_runs` for observability.

Design principles lifted from edgar13f + congresstrades:
  - Append-only on insider_txns (no update on re-scrape; dedup via UNIQUE)
  - parser_version tag on every row so future parser improvements can
    identify which rows to re-process
  - Idempotent migrations via try/except on ALTER TABLE
  - raw_filings persisted BEFORE parsing (so a parser crash doesn't
    lose the raw doc)
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent / "data" / "edgar_form4.db"
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    cik             TEXT    PRIMARY KEY,    -- 10-digit zero-padded
    ticker          TEXT,                    -- may be NULL for unmapped
    name            TEXT    NOT NULL,
    last_filings_check TEXT,
    fetched_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_companies_ticker
    ON companies(ticker);

CREATE TABLE IF NOT EXISTS form4_filings (
    accession_number TEXT    PRIMARY KEY,
    cik              TEXT    NOT NULL,
    filed_date       TEXT    NOT NULL,
    period_of_report TEXT,                  -- transaction date when present
    primary_document TEXT,
    parser_version   TEXT,
    fetched_at       TEXT    NOT NULL,
    FOREIGN KEY (cik) REFERENCES companies(cik)
);

CREATE INDEX IF NOT EXISTS idx_filings_cik_filed
    ON form4_filings(cik, filed_date DESC);

CREATE INDEX IF NOT EXISTS idx_filings_filed
    ON form4_filings(filed_date DESC);

CREATE TABLE IF NOT EXISTS insider_txns (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_number   TEXT    NOT NULL,
    cik                TEXT    NOT NULL,
    -- Insider identity
    rpt_owner_name     TEXT    NOT NULL,
    is_officer         INTEGER NOT NULL DEFAULT 0,
    is_director        INTEGER NOT NULL DEFAULT 0,
    is_ten_percent     INTEGER NOT NULL DEFAULT 0,
    officer_title      TEXT,
    -- Transaction details
    transaction_date   TEXT    NOT NULL,
    txn_code           TEXT    NOT NULL,   -- P, S, A, M, F, D, G, etc.
    shares             REAL,
    price_per_share    REAL,
    value_usd          REAL,
    acquired_disposed  TEXT,               -- 'A' (acquired) or 'D' (disposed)
    direct_indirect    TEXT,               -- 'D' or 'I'
    parser_version     TEXT,
    -- Dedup composite: an insider doesn't file the same txn-date+code+share
    -- count for the same security under the same accession twice.
    UNIQUE (accession_number, rpt_owner_name, transaction_date,
            txn_code, shares),
    FOREIGN KEY (accession_number) REFERENCES form4_filings(accession_number)
);

CREATE INDEX IF NOT EXISTS idx_txns_cik_date
    ON insider_txns(cik, transaction_date DESC);

CREATE INDEX IF NOT EXISTS idx_txns_code
    ON insider_txns(txn_code);

CREATE INDEX IF NOT EXISTS idx_txns_accession
    ON insider_txns(accession_number);

CREATE TABLE IF NOT EXISTS raw_filings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_number TEXT   NOT NULL UNIQUE,
    cik             TEXT    NOT NULL,
    source_url      TEXT,
    payload_text    TEXT,
    filed_on        TEXT,
    fetched_at      TEXT    NOT NULL,
    parse_status    TEXT    DEFAULT 'unparsed',
    parse_error     TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_status
    ON raw_filings(parse_status);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL,
    rows_inserted   INTEGER DEFAULT 0,
    rows_seen       INTEGER DEFAULT 0,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started
    ON scrape_runs(started_at DESC);
"""


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create DB file + tables. Idempotent."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(db_path: str = DEFAULT_DB_PATH):
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Companies / CIK mapping ──────────────────────────────────────

def _prefer_ticker(existing: Optional[str], incoming: Optional[str]) -> Optional[str]:
    """When SEC's `company_tickers.json` has multiple entries per CIK
    (one per share class: e.g., JPM common + JPM-PM preferred + JPM-PC
    + JPM-PD), we want the COMMON share ticker — usually the shortest
    and without a hyphen. Return whichever of (existing, incoming) is
    the cleanest match.

    Heuristic: prefer no-hyphen over hyphen; then shorter over longer;
    then alphabetical (deterministic tie-break)."""
    if not incoming:
        return existing
    if not existing:
        return incoming
    e_hyphen = "-" in existing
    i_hyphen = "-" in incoming
    if e_hyphen and not i_hyphen:
        return incoming
    if not e_hyphen and i_hyphen:
        return existing
    if len(incoming) < len(existing):
        return incoming
    if len(existing) < len(incoming):
        return existing
    return min(existing, incoming)


def upsert_company(
    conn: sqlite3.Connection,
    cik: str, name: str, ticker: Optional[str] = None,
) -> None:
    """Insert or update a (cik → ticker) mapping. ticker may be NULL
    for filers without a mapped ticker (unusual but possible — funds,
    foreign subsidiaries, etc.).

    When a CIK already has a ticker mapped and we're upserting a new
    one (common case: SEC publishes one entry per share class), the
    cleanest ticker wins — see `_prefer_ticker`."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT cik, ticker FROM companies WHERE cik = ?", (cik,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO companies (cik, ticker, name, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (cik, ticker, name, now),
        )
    else:
        existing_ticker = row[1]
        best_ticker = _prefer_ticker(existing_ticker, ticker)
        conn.execute(
            "UPDATE companies SET ticker = ?, "
            "name = ?, fetched_at = ? WHERE cik = ?",
            (best_ticker, name, now, cik),
        )


def cik_for_ticker(
    conn: sqlite3.Connection, ticker: str,
) -> Optional[str]:
    """Look up CIK for a ticker. Returns None if unmapped."""
    row = conn.execute(
        "SELECT cik FROM companies WHERE upper(ticker) = upper(?) LIMIT 1",
        (ticker,),
    ).fetchone()
    return row[0] if row else None


def update_last_filings_check(
    conn: sqlite3.Connection, cik: str,
) -> None:
    """Mark when we last polled this company's filings index."""
    conn.execute(
        "UPDATE companies SET last_filings_check = datetime('now') "
        "WHERE cik = ?",
        (cik,),
    )


# ── Filings ──────────────────────────────────────────────────────

def insert_filing(
    conn: sqlite3.Connection,
    accession_number: str, cik: str, filed_date: str,
    period_of_report: Optional[str] = None,
    primary_document: Optional[str] = None,
    parser_version: Optional[str] = None,
) -> bool:
    """Insert a Form 4 filing row. Returns True if new, False if dedup'd."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "INSERT INTO form4_filings (accession_number, cik, filed_date, "
            "period_of_report, primary_document, parser_version, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (accession_number, cik, filed_date, period_of_report,
             primary_document, parser_version, now),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# ── Transactions ─────────────────────────────────────────────────

def insert_txn(
    conn: sqlite3.Connection,
    accession_number: str, cik: str,
    rpt_owner_name: str, transaction_date: str, txn_code: str,
    shares: Optional[float] = None,
    price_per_share: Optional[float] = None,
    value_usd: Optional[float] = None,
    is_officer: bool = False,
    is_director: bool = False,
    is_ten_percent: bool = False,
    officer_title: Optional[str] = None,
    acquired_disposed: Optional[str] = None,
    direct_indirect: Optional[str] = None,
    parser_version: Optional[str] = None,
) -> bool:
    """Insert one insider transaction. Dedup'd on
    (accession, rpt_owner, txn_date, txn_code, shares)."""
    try:
        conn.execute(
            "INSERT INTO insider_txns ("
            "accession_number, cik, rpt_owner_name, "
            "is_officer, is_director, is_ten_percent, officer_title, "
            "transaction_date, txn_code, shares, price_per_share, "
            "value_usd, acquired_disposed, direct_indirect, "
            "parser_version) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (accession_number, cik, rpt_owner_name,
             1 if is_officer else 0,
             1 if is_director else 0,
             1 if is_ten_percent else 0,
             officer_title,
             transaction_date, txn_code, shares, price_per_share,
             value_usd, acquired_disposed, direct_indirect,
             parser_version),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# ── Raw filings preservation ─────────────────────────────────────

def insert_raw_filing(
    conn: sqlite3.Connection,
    accession_number: str, cik: str,
    payload_text: str,
    source_url: Optional[str] = None,
    filed_on: Optional[str] = None,
) -> bool:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "INSERT INTO raw_filings (accession_number, cik, "
            "source_url, payload_text, filed_on, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (accession_number, cik, source_url, payload_text,
             filed_on, now),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def mark_raw_parsed(
    conn: sqlite3.Connection, accession_number: str,
    error: Optional[str] = None,
) -> None:
    status = "parse_error" if error else "parsed"
    conn.execute(
        "UPDATE raw_filings SET parse_status = ?, parse_error = ? "
        "WHERE accession_number = ?",
        (status, error, accession_number),
    )


# ── Reader queries (consumed by alternative_data.get_insider_form4) ─

def get_recent_insider_activity(
    conn: sqlite3.Connection, ticker: str, lookback_days: int = 90,
) -> Dict[str, Any]:
    """Return the aggregate insider summary expected by the QuantOpsAI
    trade pipeline (matches the shape of the legacy
    `alternative_data.get_insider_activity()`)."""
    result = {
        "recent_buys": 0, "recent_sells": 0,
        "net_direction": "neutral", "notable": None,
        "total_buy_value": 0.0, "total_sell_value": 0.0,
        "cluster_count": 0,
    }
    cik = cik_for_ticker(conn, ticker)
    if not cik:
        return result

    # Open-market purchases (P) and sales (S) — these are the
    # signal codes traders actually care about.
    rows = conn.execute(
        "SELECT rpt_owner_name, officer_title, txn_code, shares, "
        "price_per_share, value_usd, transaction_date "
        "FROM insider_txns "
        "WHERE cik = ? AND txn_code IN ('P', 'S') "
        "AND date(transaction_date) >= date('now', ?) "
        "ORDER BY transaction_date DESC",
        (cik, f"-{int(lookback_days)} days"),
    ).fetchall()

    buy_value = 0.0
    sell_value = 0.0
    biggest_buy = None  # (value, owner, title, date)
    for r in rows:
        val = float(r["value_usd"] or 0)
        if r["txn_code"] == "P":
            result["recent_buys"] += 1
            buy_value += val
            if biggest_buy is None or val > biggest_buy[0]:
                biggest_buy = (val, r["rpt_owner_name"],
                               r["officer_title"], r["transaction_date"])
        else:  # "S"
            result["recent_sells"] += 1
            sell_value += val
    result["total_buy_value"] = round(buy_value, 2)
    result["total_sell_value"] = round(sell_value, 2)

    if result["recent_buys"] > result["recent_sells"] * 1.5:
        result["net_direction"] = "buying"
    elif result["recent_sells"] > result["recent_buys"] * 1.5:
        result["net_direction"] = "selling"

    if biggest_buy and biggest_buy[0] >= 100_000:
        title = biggest_buy[2] or "Insider"
        result["notable"] = (
            f"{title} {biggest_buy[1]} bought "
            f"${biggest_buy[0]:,.0f} on "
            f"{biggest_buy[3][:10]}"
        )

    # Cluster: distinct insiders BUYING within last 14 days.
    cluster = conn.execute(
        "SELECT COUNT(DISTINCT rpt_owner_name) FROM insider_txns "
        "WHERE cik = ? AND txn_code = 'P' "
        "AND date(transaction_date) >= date('now', '-14 days')",
        (cik,),
    ).fetchone()
    result["cluster_count"] = int(cluster[0] or 0)
    return result


def counts_by_date(
    conn: sqlite3.Connection, days: int = 30,
) -> List[Dict[str, Any]]:
    """Reporting helper: filings per day over the last N days."""
    rows = conn.execute(
        "SELECT date(filed_date) AS d, COUNT(*) AS n "
        "FROM form4_filings WHERE date(filed_date) >= date('now', ?) "
        "GROUP BY d ORDER BY d DESC",
        (f"-{int(days)} days",),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Scrape runs ──────────────────────────────────────────────────

_ORPHAN_RUN_MAX_AGE_HOURS = 6


def _sweep_orphaned_runs(conn: sqlite3.Connection) -> int:
    """Mark any `status='running'` rows older than the orphan-age
    threshold as `status='killed'` with a finished_at timestamp.

    Pre-2026-05-16 the script had no protection against zombies:
    a SIGKILL/OOM/disconnect mid-run left the row at `running`
    forever, polluting the runs history and confusing the issues
    page. This sweep runs at the top of every new `start_run` so
    a fresh invocation cleans up its predecessors' wreckage.
    """
    note = (" [auto-marked killed by _sweep_orphaned_runs — "
            "process likely SIGKILL/OOM/disconnect]")
    age_clause = f"-{_ORPHAN_RUN_MAX_AGE_HOURS} hours"
    cur = conn.execute(
        "UPDATE scrape_runs SET status = 'killed', "
        "finished_at = datetime('now'), "
        "error = COALESCE(error, '') || ? "
        "WHERE status = 'running' "
        "  AND started_at < datetime('now', ?)",
        (note, age_clause),
    )
    return cur.rowcount or 0


def start_run(conn: sqlite3.Connection, source: str) -> int:
    # Clean up zombies left by previous runs that died without
    # calling finish_run (SIGKILL, OOM, network drop). Any row with
    # status='running' older than 6h gets marked 'killed' with a
    # diagnostic error note. Quiet on no-ops; logs INFO on any sweep.
    n_swept = _sweep_orphaned_runs(conn)
    if n_swept > 0:
        import logging
        logging.getLogger(__name__).info(
            "edgar_form4: swept %d orphaned scrape_runs row(s) "
            "(status='running' older than %dh)",
            n_swept, _ORPHAN_RUN_MAX_AGE_HOURS,
        )
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO scrape_runs (source, started_at, status) "
        "VALUES (?, ?, 'running')",
        (source, now),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str,
    rows_inserted: int = 0, rows_seen: int = 0,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at = datetime('now'), "
        "status = ?, rows_inserted = ?, rows_seen = ?, error = ? "
        "WHERE id = ?",
        (status, rows_inserted, rows_seen, error, run_id),
    )


def recent_runs(
    conn: sqlite3.Connection, limit: int = 20,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
