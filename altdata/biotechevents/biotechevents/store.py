"""SQLite storage for biotechevents.

Tables:
  trials           ClinicalTrials.gov registered studies
  trial_changes    History of status/phase transitions per trial
  pdufa_events     FDA decision-deadline events (stubbed — v2 work)
  raw_filings      Every API response cached for re-parse
  scrape_runs      Run history

Same engineering pattern as congresstrades / edgar13f:
  - raw stored before parsing (parser changes don't require re-scraping)
  - parser_version on every parsed row
  - idempotent migrations via try/except on ALTER
  - per-batch commit so mid-run failures don't lose progress
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent / "data" / "biotechevents.db"
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    nct_id              TEXT    PRIMARY KEY,
    brief_title         TEXT    NOT NULL,
    sponsor_name        TEXT,
    sponsor_class       TEXT,                    -- 'INDUSTRY' | 'NIH' | 'OTHER' | etc.
    ticker              TEXT,                    -- best-effort mapping
    phase               TEXT,                    -- 'PHASE1' | 'PHASE2' | 'PHASE3' | 'PHASE4' | 'NA'
    overall_status      TEXT,                    -- 'RECRUITING' | 'ACTIVE_NOT_RECRUITING' | 'COMPLETED' | etc.
    primary_completion_date TEXT,                -- YYYY-MM-DD
    completion_date     TEXT,
    start_date          TEXT,
    last_updated        TEXT,
    enrollment_count    INTEGER,
    conditions_json     TEXT,                    -- JSON array of disease names
    interventions_json  TEXT,                    -- JSON array of drug/device names
    parser_version      TEXT,
    fetched_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trials_sponsor ON trials(sponsor_name);
CREATE INDEX IF NOT EXISTS idx_trials_ticker ON trials(ticker);
CREATE INDEX IF NOT EXISTS idx_trials_phase ON trials(phase);
CREATE INDEX IF NOT EXISTS idx_trials_status ON trials(overall_status);
CREATE INDEX IF NOT EXISTS idx_trials_completion ON trials(primary_completion_date);

-- Track changes over time so we can detect "Phase 2 → Phase 3 transition"
-- and "Recruiting → Suspended" (the actionable signals).
CREATE TABLE IF NOT EXISTS trial_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nct_id          TEXT    NOT NULL,
    field           TEXT    NOT NULL,           -- 'phase' | 'overall_status' | 'primary_completion_date'
    old_value       TEXT,
    new_value       TEXT,
    detected_at     TEXT    NOT NULL,
    FOREIGN KEY (nct_id) REFERENCES trials(nct_id)
);

CREATE INDEX IF NOT EXISTS idx_changes_nct ON trial_changes(nct_id);
CREATE INDEX IF NOT EXISTS idx_changes_detected ON trial_changes(detected_at DESC);

-- Stub for v2: FDA PDUFA events. Schema designed; scraper not yet written.
CREATE TABLE IF NOT EXISTS pdufa_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    drug_name       TEXT    NOT NULL,
    sponsor_company TEXT    NOT NULL,
    ticker          TEXT,
    pdufa_date      TEXT    NOT NULL,           -- YYYY-MM-DD
    action_type     TEXT,                        -- 'NDA' | 'BLA' | 'sNDA' | 'sBLA'
    indication      TEXT,
    outcome         TEXT    DEFAULT 'pending', -- 'pending' | 'approved' | 'crl' | 'withdrawn'
    outcome_date    TEXT,
    source_url      TEXT,
    parser_version  TEXT,
    fetched_at      TEXT    NOT NULL,
    UNIQUE (drug_name, sponsor_company, pdufa_date)
);

CREATE INDEX IF NOT EXISTS idx_pdufa_ticker ON pdufa_events(ticker);
CREATE INDEX IF NOT EXISTS idx_pdufa_date ON pdufa_events(pdufa_date);

CREATE TABLE IF NOT EXISTS raw_filings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,           -- 'clinicaltrials' | 'fda'
    external_id     TEXT    NOT NULL,           -- nct_id or pdufa hash
    source_url      TEXT,
    content_type    TEXT    NOT NULL,           -- 'json' | 'html' | 'pdf'
    payload_text    TEXT,
    fetched_at      TEXT    NOT NULL,
    parse_status    TEXT    DEFAULT 'unparsed',
    parse_error     TEXT,
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_source_status
    ON raw_filings(source, parse_status);

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
    """Idempotent ALTER TABLEs. Add new columns here when schema evolves."""
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
# Trials — upsert with change detection
# ---------------------------------------------------------------------------

def upsert_trial(
    conn: sqlite3.Connection,
    nct_id: str,
    brief_title: str,
    sponsor_name: Optional[str],
    sponsor_class: Optional[str],
    ticker: Optional[str],
    phase: Optional[str],
    overall_status: Optional[str],
    primary_completion_date: Optional[str],
    completion_date: Optional[str],
    start_date: Optional[str],
    last_updated: Optional[str],
    enrollment_count: Optional[int],
    conditions: Optional[List[str]] = None,
    interventions: Optional[List[str]] = None,
    parser_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert or update a trial. Returns dict with `is_new` and `changes`
    (list of detected field changes since last seen, for trial_changes table).
    """
    fetched_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conditions_json = json.dumps(conditions or [], default=str)
    interventions_json = json.dumps(interventions or [], default=str)

    existing = conn.execute(
        "SELECT phase, overall_status, primary_completion_date "
        "FROM trials WHERE nct_id = ?", (nct_id,),
    ).fetchone()

    changes: List[Dict[str, Any]] = []

    if existing is None:
        conn.execute(
            "INSERT INTO trials (nct_id, brief_title, sponsor_name, "
            "sponsor_class, ticker, phase, overall_status, "
            "primary_completion_date, completion_date, start_date, "
            "last_updated, enrollment_count, conditions_json, "
            "interventions_json, parser_version, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (nct_id, brief_title, sponsor_name, sponsor_class, ticker,
             phase, overall_status, primary_completion_date,
             completion_date, start_date, last_updated,
             enrollment_count, conditions_json, interventions_json,
             parser_version, fetched_at),
        )
        return {"is_new": True, "changes": []}

    # Detect changes on the three fields that matter for trading signal
    for field, new_val in (
        ("phase", phase),
        ("overall_status", overall_status),
        ("primary_completion_date", primary_completion_date),
    ):
        old_val = existing[field]
        if (old_val or "") != (new_val or ""):
            changes.append({
                "field": field,
                "old_value": old_val,
                "new_value": new_val,
            })
            conn.execute(
                "INSERT INTO trial_changes (nct_id, field, old_value, "
                "new_value, detected_at) VALUES (?, ?, ?, ?, ?)",
                (nct_id, field, old_val, new_val, fetched_at),
            )

    conn.execute(
        "UPDATE trials SET brief_title = ?, sponsor_name = ?, "
        "sponsor_class = ?, ticker = COALESCE(?, ticker), phase = ?, "
        "overall_status = ?, primary_completion_date = ?, "
        "completion_date = ?, start_date = ?, last_updated = ?, "
        "enrollment_count = ?, conditions_json = ?, "
        "interventions_json = ?, parser_version = ?, fetched_at = ? "
        "WHERE nct_id = ?",
        (brief_title, sponsor_name, sponsor_class, ticker, phase,
         overall_status, primary_completion_date, completion_date,
         start_date, last_updated, enrollment_count,
         conditions_json, interventions_json, parser_version,
         fetched_at, nct_id),
    )
    return {"is_new": False, "changes": changes}


# ---------------------------------------------------------------------------
# Raw filings + run tracking — same shape as edgar13f
# ---------------------------------------------------------------------------

def insert_raw_filing(
    conn: sqlite3.Connection,
    source: str,
    external_id: str,
    content_type: str,
    payload: str,
    source_url: Optional[str] = None,
) -> bool:
    fetched_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "SELECT id FROM raw_filings WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    if cur is None:
        conn.execute(
            "INSERT INTO raw_filings (source, external_id, source_url, "
            "content_type, payload_text, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, external_id, source_url, content_type, payload, fetched_at),
        )
        return True
    conn.execute(
        "UPDATE raw_filings SET source_url = ?, content_type = ?, "
        "payload_text = ?, fetched_at = ? WHERE id = ?",
        (source_url, content_type, payload, fetched_at, cur[0]),
    )
    return False


def mark_raw_parsed(
    conn: sqlite3.Connection,
    source: str,
    external_id: str,
    status: str = "parsed",
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE raw_filings SET parse_status = ?, parse_error = ? "
        "WHERE source = ? AND external_id = ?",
        (status, error, source, external_id),
    )


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


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def query_trials(
    conn: sqlite3.Connection,
    sponsor: Optional[str] = None,
    ticker: Optional[str] = None,
    phase: Optional[str] = None,
    status: Optional[str] = None,
    completion_after: Optional[str] = None,
    completion_before: Optional[str] = None,
    limit: int = 200,
) -> List[sqlite3.Row]:
    clauses = []
    args: List[Any] = []
    if sponsor:
        clauses.append("sponsor_name LIKE ?")
        args.append(f"%{sponsor}%")
    if ticker:
        clauses.append("ticker = ?")
        args.append(ticker.upper())
    if phase:
        clauses.append("phase = ?")
        args.append(phase.upper())
    if status:
        clauses.append("overall_status = ?")
        args.append(status.upper())
    if completion_after:
        clauses.append("primary_completion_date >= ?")
        args.append(completion_after)
    if completion_before:
        clauses.append("primary_completion_date <= ?")
        args.append(completion_before)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM trials {where} "
        f"ORDER BY primary_completion_date ASC LIMIT ?",
        (*args, limit),
    ).fetchall()


def recent_changes(
    conn: sqlite3.Connection, days: int = 7, limit: int = 50,
) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT c.*, t.brief_title, t.sponsor_name, t.ticker FROM trial_changes c "
        "JOIN trials t ON c.nct_id = t.nct_id "
        "WHERE c.detected_at >= datetime('now', ? || ' days') "
        "ORDER BY c.detected_at DESC LIMIT ?",
        (f"-{days}", limit),
    ).fetchall()


def counts_by_phase(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT phase, COUNT(*) n FROM trials GROUP BY phase "
        "ORDER BY n DESC"
    ).fetchall()
    return {(r["phase"] or "(none)"): r["n"] for r in rows}


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
