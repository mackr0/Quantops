"""SQLite storage for congressional trade disclosures.

Single-file DB at `data/congress.db`. Append-only for safety: we never
delete or update rows, just insert new filings. Dedup is (chamber,
member_name, filing_doc_id, transaction_date, asset_description) —
same filing re-scraped is idempotent.
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
    Path(__file__).resolve().parent.parent / "data" / "congress.db"
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chamber             TEXT    NOT NULL,   -- 'house' | 'senate'
    member_name         TEXT    NOT NULL,
    member_state        TEXT,
    member_party        TEXT,
    filing_doc_id       TEXT,               -- from house XML or senate filing UUID
    filing_date         TEXT,               -- YYYY-MM-DD
    transaction_date    TEXT,               -- YYYY-MM-DD
    ticker              TEXT,               -- best-guess, may be NULL
    asset_description   TEXT    NOT NULL,   -- raw text
    asset_type          TEXT,               -- 'Stock' | 'Bond' | 'ETF' | etc. (Senate surfaces this explicitly)
    transaction_type    TEXT,               -- 'buy' | 'sell' | 'exchange' | 'partial_sale' | 'other'
    amount_range        TEXT,               -- '$1,001 - $15,000'
    amount_low          INTEGER,
    amount_high         INTEGER,
    owner               TEXT,               -- 'self' | 'spouse' | 'joint' | 'dependent'
    source_url          TEXT,
    raw_json            TEXT,               -- full original record for debugging
    parser_version      TEXT,               -- tag each row with the parser that produced it
    fetched_at          TEXT    NOT NULL,
    UNIQUE (chamber, member_name, filing_doc_id, transaction_date, asset_description, amount_range)
);

-- Raw layer: every fetched filing stored here before parsing. If parsers
-- change (Senate layout drift, House PDF format tweaks), we can re-parse
-- historical data without re-scraping the origin sites. One row per
-- (chamber, filing_doc_id) — upserts on re-fetch.
CREATE TABLE IF NOT EXISTS raw_filings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chamber         TEXT    NOT NULL,
    filing_doc_id   TEXT    NOT NULL,
    member_name     TEXT,
    filing_type     TEXT,                   -- 'ptr' | 'annual' | 'amendment' | etc.
    source_url      TEXT,
    content_type    TEXT    NOT NULL,       -- 'html' | 'json' | 'pdf'
    payload_text    TEXT,                   -- HTML / JSON bodies
    payload_blob    BLOB,                   -- PDF bytes
    filed_on        TEXT,                   -- YYYY-MM-DD (reported filing date from index)
    fetched_at      TEXT    NOT NULL,
    parse_status    TEXT    DEFAULT 'unparsed',  -- 'unparsed' | 'parsed' | 'parse_error'
    parse_error     TEXT,
    UNIQUE (chamber, filing_doc_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_chamber_doc
    ON raw_filings(chamber, filing_doc_id);

CREATE INDEX IF NOT EXISTS idx_raw_parse_status
    ON raw_filings(parse_status);

CREATE INDEX IF NOT EXISTS idx_trades_ticker
    ON trades(ticker);

CREATE INDEX IF NOT EXISTS idx_trades_filing_date
    ON trades(filing_date);

CREATE INDEX IF NOT EXISTS idx_trades_member
    ON trades(member_name);

CREATE INDEX IF NOT EXISTS idx_trades_chamber_date
    ON trades(chamber, filing_date DESC);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chamber        TEXT    NOT NULL,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    status         TEXT    NOT NULL,   -- 'running' | 'ok' | 'failed'
    rows_inserted  INTEGER DEFAULT 0,
    rows_seen      INTEGER DEFAULT 0,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_runs_chamber_date
    ON scrape_runs(chamber, started_at DESC);
"""


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create the DB file + tables if they don't exist. Also applies
    idempotent ALTER TABLE migrations for columns added after initial
    schema — so upgrading an older DB is safe."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after the initial schema.
    Each ALTER is wrapped in a try/except — a duplicate-column error
    means we're already migrated, which is exactly what we want."""
    migrations = [
        "ALTER TABLE trades ADD COLUMN asset_type TEXT",
        "ALTER TABLE trades ADD COLUMN parser_version TEXT",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


@contextmanager
def connect(db_path: str = DEFAULT_DB_PATH):
    """Context manager returning a sqlite3 connection with row_factory set."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_trade(conn: sqlite3.Connection, trade: Dict[str, Any]) -> bool:
    """Insert a single trade row. Returns True if new, False if duplicate.

    `trade` should have the keys matching the schema columns. `raw_json`
    is auto-populated with the full dict for debugging later.
    """
    trade = dict(trade)  # don't mutate caller's dict
    trade.setdefault("fetched_at", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    trade["raw_json"] = json.dumps(trade, default=str)

    cols = (
        "chamber", "member_name", "member_state", "member_party",
        "filing_doc_id", "filing_date", "transaction_date",
        "ticker", "asset_description", "asset_type", "transaction_type",
        "amount_range", "amount_low", "amount_high",
        "owner", "source_url", "raw_json", "parser_version", "fetched_at",
    )
    placeholders = ",".join("?" * len(cols))
    values = tuple(trade.get(c) for c in cols)
    try:
        conn.execute(
            f"INSERT INTO trades ({','.join(cols)}) VALUES ({placeholders})",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        return False  # duplicate — UNIQUE constraint hit


def insert_raw_filing(
    conn: sqlite3.Connection,
    chamber: str,
    filing_doc_id: str,
    content_type: str,
    payload: Any,
    source_url: Optional[str] = None,
    member_name: Optional[str] = None,
    filing_type: Optional[str] = None,
    filed_on: Optional[str] = None,
) -> bool:
    """Store a raw filing document. Upserts on (chamber, filing_doc_id).

    `payload` is bytes for PDFs, str for HTML/JSON. Routes to payload_blob
    vs payload_text accordingly. Returns True if new row, False if updated.
    """
    payload_text = None
    payload_blob = None
    if content_type == "pdf":
        payload_blob = payload if isinstance(payload, (bytes, bytearray)) else bytes(payload)
    else:
        payload_text = payload if isinstance(payload, str) else str(payload)

    fetched_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    cur = conn.execute(
        "SELECT id FROM raw_filings WHERE chamber=? AND filing_doc_id=?",
        (chamber, filing_doc_id),
    ).fetchone()
    if cur is None:
        conn.execute(
            "INSERT INTO raw_filings (chamber, filing_doc_id, member_name, "
            "filing_type, source_url, content_type, payload_text, payload_blob, "
            "filed_on, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chamber, filing_doc_id, member_name, filing_type, source_url,
             content_type, payload_text, payload_blob, filed_on, fetched_at),
        )
        return True
    else:
        conn.execute(
            "UPDATE raw_filings SET member_name=?, filing_type=?, source_url=?, "
            "content_type=?, payload_text=?, payload_blob=?, filed_on=?, "
            "fetched_at=? WHERE id=?",
            (member_name, filing_type, source_url, content_type, payload_text,
             payload_blob, filed_on, fetched_at, cur[0]),
        )
        return False


def mark_raw_filing_parsed(
    conn: sqlite3.Connection,
    chamber: str,
    filing_doc_id: str,
    status: str = "parsed",
    error: Optional[str] = None,
) -> None:
    """Update parse_status on a raw filing so we know what's been processed."""
    conn.execute(
        "UPDATE raw_filings SET parse_status=?, parse_error=? "
        "WHERE chamber=? AND filing_doc_id=?",
        (status, error, chamber, filing_doc_id),
    )


def start_run(conn: sqlite3.Connection, chamber: str) -> int:
    """Record the start of a scrape run. Returns the run id."""
    cur = conn.execute(
        "INSERT INTO scrape_runs (chamber, started_at, status) "
        "VALUES (?, ?, 'running')",
        (chamber, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
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
        (
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            status, rows_inserted, rows_seen, error, run_id,
        ),
    )


def query_trades(
    conn: sqlite3.Connection,
    ticker: Optional[str] = None,
    member: Optional[str] = None,
    chamber: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 200,
) -> List[sqlite3.Row]:
    """Flexible query with optional filters."""
    clauses = []
    args: List[Any] = []
    if ticker:
        clauses.append("ticker = ?")
        args.append(ticker.upper())
    if member:
        clauses.append("member_name LIKE ?")
        args.append(f"%{member}%")
    if chamber:
        clauses.append("chamber = ?")
        args.append(chamber)
    if since:
        clauses.append("filing_date >= ?")
        args.append(since)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT * FROM trades {where} "
        f"ORDER BY filing_date DESC, transaction_date DESC LIMIT ?"
    )
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def counts_by_chamber(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT chamber, COUNT(*) n FROM trades GROUP BY chamber"
    ).fetchall()
    return {r["chamber"]: r["n"] for r in rows}
