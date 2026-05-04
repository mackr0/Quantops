"""SQLite storage for edgar13f.

One DB file at `data/edgar13f.db`. Three primary tables:

  filers   — one row per filing entity (Berkshire, Renaissance, etc)
  filings  — one row per 13F-HR filing (filer × quarter)
  holdings — one row per (filing, security)

Plus `raw_filings` (every fetched XML preserved so parser changes can
re-process historical data without re-scraping) and `scrape_runs` (run
history for observability).

Design principles lifted verbatim from congresstrades:
  - Append-only on `holdings` (no update on re-scrape; dedup via UNIQUE)
  - parser_version tag on every row so future parser improvements can
    identify which rows to re-process
  - Idempotent migrations via try/except on ALTER TABLE
  - raw_filings persisted BEFORE parsing (so a parser crash doesn't
    lose the raw doc)
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent / "data" / "edgar13f.db"
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS filers (
    cik             TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL,
    aum_usd         INTEGER,
    filer_type      TEXT,
    first_seen      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS filings (
    accession_number    TEXT    PRIMARY KEY,
    cik                 TEXT    NOT NULL,
    period_of_report    TEXT    NOT NULL,  -- YYYY-MM-DD (quarter end)
    filed_date          TEXT    NOT NULL,
    total_value_usd     INTEGER,
    total_positions     INTEGER,
    parser_version      TEXT,
    fetched_at          TEXT    NOT NULL,
    FOREIGN KEY (cik) REFERENCES filers(cik)
);

CREATE INDEX IF NOT EXISTS idx_filings_cik_period
    ON filings(cik, period_of_report DESC);

CREATE INDEX IF NOT EXISTS idx_filings_period
    ON filings(period_of_report DESC);

CREATE TABLE IF NOT EXISTS holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_number TEXT   NOT NULL,
    cusip           TEXT    NOT NULL,
    ticker          TEXT,
    company_name    TEXT    NOT NULL,
    class_title     TEXT,
    shares          INTEGER,
    value_usd       INTEGER,
    put_call        TEXT,                       -- 'PUT' | 'CALL' | NULL
    investment_discretion TEXT,                 -- 'SOLE' | 'SHARED' | 'DFND'
    parser_version  TEXT,
    UNIQUE (accession_number, cusip, class_title, put_call),
    FOREIGN KEY (accession_number) REFERENCES filings(accession_number)
);

CREATE INDEX IF NOT EXISTS idx_holdings_cusip ON holdings(cusip);
CREATE INDEX IF NOT EXISTS idx_holdings_ticker ON holdings(ticker);
CREATE INDEX IF NOT EXISTS idx_holdings_accession ON holdings(accession_number);

CREATE TABLE IF NOT EXISTS raw_filings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_number TEXT   NOT NULL UNIQUE,
    cik             TEXT    NOT NULL,
    filing_type     TEXT    NOT NULL,           -- '13F-HR' usually
    source_url      TEXT,
    content_type    TEXT    NOT NULL,           -- 'xml' | 'html'
    payload_text    TEXT,
    filed_on        TEXT,
    fetched_at      TEXT    NOT NULL,
    parse_status    TEXT    DEFAULT 'unparsed', -- 'unparsed' | 'parsed' | 'parse_error'
    parse_error     TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_accession
    ON raw_filings(accession_number);

CREATE INDEX IF NOT EXISTS idx_raw_status
    ON raw_filings(parse_status);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL,   -- 'running' | 'ok' | 'failed'
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
        _apply_migrations(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLEs. Add new column definitions here when
    schema evolves. `duplicate column` error is the success path."""
    migrations: List[str] = [
        # Example: "ALTER TABLE holdings ADD COLUMN new_col TEXT"
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


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


# ---------------------------------------------------------------------------
# Filers
# ---------------------------------------------------------------------------

def upsert_filer(
    conn: sqlite3.Connection,
    cik: str,
    name: str,
    filer_type: Optional[str] = None,
    aum_usd: Optional[int] = None,
) -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT cik FROM filers WHERE cik = ?", (cik,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO filers (cik, name, filer_type, aum_usd, "
            "first_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cik, name, filer_type, aum_usd, now, now),
        )
    else:
        conn.execute(
            "UPDATE filers SET name = ?, filer_type = COALESCE(?, filer_type), "
            "aum_usd = COALESCE(?, aum_usd), updated_at = ? WHERE cik = ?",
            (name, filer_type, aum_usd, now, cik),
        )


# ---------------------------------------------------------------------------
# Filings
# ---------------------------------------------------------------------------

def insert_filing(
    conn: sqlite3.Connection,
    accession_number: str,
    cik: str,
    period_of_report: str,
    filed_date: str,
    total_value_usd: Optional[int] = None,
    total_positions: Optional[int] = None,
    parser_version: Optional[str] = None,
) -> bool:
    """Insert a filing row. Returns True if new, False if already present."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "INSERT INTO filings (accession_number, cik, period_of_report, "
            "filed_date, total_value_usd, total_positions, parser_version, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (accession_number, cik, period_of_report, filed_date,
             total_value_usd, total_positions, parser_version, now),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------

def insert_holding(
    conn: sqlite3.Connection,
    accession_number: str,
    cusip: str,
    company_name: str,
    class_title: Optional[str] = None,
    ticker: Optional[str] = None,
    shares: Optional[int] = None,
    value_usd: Optional[int] = None,
    put_call: Optional[str] = None,
    investment_discretion: Optional[str] = None,
    parser_version: Optional[str] = None,
) -> bool:
    # SQLite's UNIQUE constraint treats NULLs as distinct (two NULL values
    # are never equal to each other). Since class_title and put_call are
    # commonly NULL in real 13F data, we'd fail to dedup re-parses. Coerce
    # NULLs to empty strings for the UNIQUE constraint to actually fire.
    class_title = class_title or ""
    put_call = put_call or ""
    try:
        conn.execute(
            "INSERT INTO holdings (accession_number, cusip, ticker, company_name, "
            "class_title, shares, value_usd, put_call, investment_discretion, "
            "parser_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (accession_number, cusip, ticker, company_name, class_title,
             shares, value_usd, put_call, investment_discretion, parser_version),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------------------------------------------------------------------
# Raw filings
# ---------------------------------------------------------------------------

def insert_raw_filing(
    conn: sqlite3.Connection,
    accession_number: str,
    cik: str,
    filing_type: str,
    content_type: str,
    payload: str,
    source_url: Optional[str] = None,
    filed_on: Optional[str] = None,
) -> bool:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "SELECT id FROM raw_filings WHERE accession_number = ?",
        (accession_number,),
    ).fetchone()
    if cur is None:
        conn.execute(
            "INSERT INTO raw_filings (accession_number, cik, filing_type, "
            "source_url, content_type, payload_text, filed_on, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (accession_number, cik, filing_type, source_url,
             content_type, payload, filed_on, now),
        )
        return True
    else:
        conn.execute(
            "UPDATE raw_filings SET cik = ?, filing_type = ?, source_url = ?, "
            "content_type = ?, payload_text = ?, filed_on = ?, fetched_at = ? "
            "WHERE id = ?",
            (cik, filing_type, source_url, content_type,
             payload, filed_on, now, cur[0]),
        )
        return False


def mark_raw_parsed(
    conn: sqlite3.Connection,
    accession_number: str,
    status: str = "parsed",
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE raw_filings SET parse_status = ?, parse_error = ? "
        "WHERE accession_number = ?",
        (status, error, accession_number),
    )


# ---------------------------------------------------------------------------
# Scrape runs
# ---------------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (source, started_at, status) "
        "VALUES (?, ?, 'running')",
        (source, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    rows_inserted: int = 0,
    rows_seen: int = 0,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at = ?, status = ?, "
        "rows_inserted = ?, rows_seen = ?, error = ? WHERE id = ?",
        (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), status,
         rows_inserted, rows_seen, error, run_id),
    )


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def query_holdings(
    conn: sqlite3.Connection,
    cusip: Optional[str] = None,
    ticker: Optional[str] = None,
    cik: Optional[str] = None,
    period: Optional[str] = None,
    limit: int = 200,
) -> List[sqlite3.Row]:
    clauses = []
    args: List[Any] = []
    if cusip:
        clauses.append("h.cusip = ?")
        args.append(cusip.upper())
    if ticker:
        clauses.append("h.ticker = ?")
        args.append(ticker.upper())
    if cik:
        clauses.append("f.cik = ?")
        args.append(cik)
    if period:
        clauses.append("f.period_of_report = ?")
        args.append(period)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    return conn.execute(
        f"SELECT h.*, f.period_of_report, f.filed_date, flr.name AS filer_name "
        f"FROM holdings h "
        f"JOIN filings f ON h.accession_number = f.accession_number "
        f"JOIN filers flr ON f.cik = flr.cik "
        f"{where} "
        f"ORDER BY f.period_of_report DESC, h.value_usd DESC "
        f"LIMIT ?",
        (*args, limit),
    ).fetchall()


def counts_by_period(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT period_of_report, COUNT(*) n FROM filings "
        "GROUP BY period_of_report ORDER BY period_of_report DESC"
    ).fetchall()
    return {r["period_of_report"]: r["n"] for r in rows}
