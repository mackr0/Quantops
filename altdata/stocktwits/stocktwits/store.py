"""SQLite storage for stocktwits.

Tables:
  messages                     One row per StockTwits message
  ticker_sentiment_daily       Per-ticker daily aggregates
  trending_snapshots           Top-30 trending tickers per snapshot
  raw_responses                Every API response cached for re-parse
  scrape_runs                  Run history

Same engineering pattern as the other altdata projects (raw before
parse, parser_version tagging, idempotent migrations, change tracking).
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
    Path(__file__).resolve().parent.parent / "data" / "stocktwits.db"
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id          INTEGER PRIMARY KEY,           -- StockTwits message ID
    ticker          TEXT    NOT NULL,
    user_id         INTEGER,
    user_name       TEXT,
    body            TEXT    NOT NULL,
    sentiment       TEXT,                          -- 'bullish' | 'bearish' | NULL
    created_at      TEXT    NOT NULL,              -- StockTwits UTC ISO timestamp
    like_count      INTEGER DEFAULT 0,
    parser_version  TEXT,
    fetched_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_ticker_created
    ON messages(ticker, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_msg_user
    ON messages(user_id);

CREATE TABLE IF NOT EXISTS ticker_sentiment_daily (
    ticker          TEXT    NOT NULL,
    date            TEXT    NOT NULL,              -- YYYY-MM-DD UTC
    n_messages      INTEGER NOT NULL,
    n_bullish       INTEGER NOT NULL,
    n_bearish       INTEGER NOT NULL,
    n_neutral       INTEGER NOT NULL,
    net_sentiment   REAL,                          -- (bullish - bearish) / total
    avg_likes       REAL,
    last_updated    TEXT    NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_sentiment_date
    ON ticker_sentiment_daily(date DESC);

CREATE TABLE IF NOT EXISTS trending_snapshots (
    snapshot_at     TEXT    NOT NULL,              -- when we polled
    rank            INTEGER NOT NULL,
    ticker          TEXT    NOT NULL,
    PRIMARY KEY (snapshot_at, rank)
);

CREATE INDEX IF NOT EXISTS idx_trending_ticker
    ON trending_snapshots(ticker, snapshot_at DESC);

CREATE TABLE IF NOT EXISTS raw_responses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint        TEXT    NOT NULL,              -- 'streams/symbol' | 'trending'
    request_params  TEXT,                          -- JSON of query string
    payload_text    TEXT    NOT NULL,
    fetched_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_endpoint_date
    ON raw_responses(endpoint, fetched_at DESC);

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
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLEs."""
    migrations: List[str] = []
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
# Messages
# ---------------------------------------------------------------------------

def insert_message(
    conn: sqlite3.Connection,
    msg_id: int,
    ticker: str,
    body: str,
    created_at: str,
    user_id: Optional[int] = None,
    user_name: Optional[str] = None,
    sentiment: Optional[str] = None,
    like_count: int = 0,
    parser_version: Optional[str] = None,
) -> bool:
    """Insert a message. Returns True if new, False if already seen
    (StockTwits msg_ids are unique and immutable)."""
    fetched_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "INSERT INTO messages (msg_id, ticker, user_id, user_name, "
            "body, sentiment, created_at, like_count, parser_version, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (msg_id, ticker.upper(), user_id, user_name, body[:2000],
             sentiment, created_at, like_count, parser_version, fetched_at),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------------------------------------------------------------------
# Daily sentiment aggregates
# ---------------------------------------------------------------------------

def upsert_daily_sentiment(
    conn: sqlite3.Connection,
    ticker: str,
    date: str,
) -> Dict[str, Any]:
    """Recompute ticker_sentiment_daily for one (ticker, date) from
    the messages table. Returns the aggregate dict."""
    row = conn.execute(
        "SELECT COUNT(*) n_total, "
        "SUM(CASE WHEN sentiment='bullish' THEN 1 ELSE 0 END) n_bullish, "
        "SUM(CASE WHEN sentiment='bearish' THEN 1 ELSE 0 END) n_bearish, "
        "SUM(CASE WHEN sentiment IS NULL THEN 1 ELSE 0 END) n_neutral, "
        "AVG(like_count) avg_likes "
        "FROM messages WHERE ticker = ? "
        "AND date(substr(created_at, 1, 10)) = ?",
        (ticker.upper(), date),
    ).fetchone()
    n_total = row["n_total"] or 0
    n_bullish = row["n_bullish"] or 0
    n_bearish = row["n_bearish"] or 0
    n_neutral = row["n_neutral"] or 0
    avg_likes = row["avg_likes"] or 0
    net = (n_bullish - n_bearish) / n_total if n_total > 0 else 0.0

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO ticker_sentiment_daily "
        "(ticker, date, n_messages, n_bullish, n_bearish, n_neutral, "
        "net_sentiment, avg_likes, last_updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ticker, date) DO UPDATE SET "
        "n_messages=excluded.n_messages, n_bullish=excluded.n_bullish, "
        "n_bearish=excluded.n_bearish, n_neutral=excluded.n_neutral, "
        "net_sentiment=excluded.net_sentiment, avg_likes=excluded.avg_likes, "
        "last_updated=excluded.last_updated",
        (ticker.upper(), date, n_total, n_bullish, n_bearish, n_neutral,
         net, avg_likes, now),
    )
    return {
        "ticker": ticker.upper(), "date": date, "n_messages": n_total,
        "n_bullish": n_bullish, "n_bearish": n_bearish, "n_neutral": n_neutral,
        "net_sentiment": net, "avg_likes": avg_likes,
    }


# ---------------------------------------------------------------------------
# Trending snapshots
# ---------------------------------------------------------------------------

def insert_trending_snapshot(
    conn: sqlite3.Connection, snapshot_at: str, ranked_tickers: List[str],
) -> int:
    """Insert a trending snapshot — list of tickers in rank order."""
    n = 0
    for rank, ticker in enumerate(ranked_tickers, 1):
        try:
            conn.execute(
                "INSERT INTO trending_snapshots (snapshot_at, rank, ticker) "
                "VALUES (?, ?, ?)",
                (snapshot_at, rank, ticker.upper()),
            )
            n += 1
        except sqlite3.IntegrityError:
            pass
    return n


# ---------------------------------------------------------------------------
# Raw responses + run tracking
# ---------------------------------------------------------------------------

def insert_raw_response(
    conn: sqlite3.Connection,
    endpoint: str,
    payload: str,
    request_params: Optional[Dict[str, Any]] = None,
) -> None:
    fetched_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO raw_responses (endpoint, request_params, payload_text, fetched_at) "
        "VALUES (?, ?, ?, ?)",
        (endpoint, json.dumps(request_params or {}), payload, fetched_at),
    )


def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (source, started_at, status) "
        "VALUES (?, ?, 'running')",
        (source, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str,
    rows_inserted: int = 0, rows_seen: int = 0,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, status=?, "
        "rows_inserted=?, rows_seen=?, error=? WHERE id=?",
        (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), status,
         rows_inserted, rows_seen, error, run_id),
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def query_messages(
    conn: sqlite3.Connection,
    ticker: Optional[str] = None,
    sentiment: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
) -> List[sqlite3.Row]:
    clauses = []
    args: List[Any] = []
    if ticker:
        clauses.append("ticker = ?")
        args.append(ticker.upper())
    if sentiment:
        clauses.append("sentiment = ?")
        args.append(sentiment.lower())
    if since:
        clauses.append("created_at >= ?")
        args.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM messages {where} "
        f"ORDER BY created_at DESC LIMIT ?",
        (*args, limit),
    ).fetchall()


def query_daily_sentiment(
    conn: sqlite3.Connection,
    ticker: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
) -> List[sqlite3.Row]:
    clauses = []
    args: List[Any] = []
    if ticker:
        clauses.append("ticker = ?")
        args.append(ticker.upper())
    if since:
        clauses.append("date >= ?")
        args.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM ticker_sentiment_daily {where} "
        f"ORDER BY date DESC, ticker LIMIT ?",
        (*args, limit),
    ).fetchall()


def latest_trending(
    conn: sqlite3.Connection, limit: int = 30,
) -> List[sqlite3.Row]:
    """Return the most recent trending snapshot's tickers in rank order."""
    return conn.execute(
        "SELECT ticker, rank, snapshot_at FROM trending_snapshots "
        "WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM trending_snapshots) "
        "ORDER BY rank LIMIT ?",
        (limit,),
    ).fetchall()


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
