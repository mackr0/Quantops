"""Broad-universe SEC 13D / 13G activist-position discovery
(#2 Tier-1 alt-data, 2026-05-17).

A 13D filing means an investor crossed the 5%-ownership threshold
in a public company with intent to influence management — the
canonical "activist target" signal. 13G is the passive variant
(same 5% threshold, no intent to influence). Together they're
real-time signals filed within 10 days of the trigger date —
much fresher than the quarterly 13F snapshot we already track.

Source: EDGAR atom feed for SC 13D and SC 13G form types
  https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent
  &type=SC+13D&output=atom

For each filing we capture:
  - accession number (PK — idempotency)
  - filing_date, accepted_at
  - filer_name, filer_cik       (who made the move)
  - subject_name, subject_cik   (the target company)
  - subject_ticker              (resolved from cik when possible)
  - form_type ("SC 13D" or "SC 13G")
  - source_url

Mirrors sec_8k_broad's design exactly — same atom-feed pattern,
same idempotency via accession UNIQUE constraint, same cron-shim
shape (altdata/edgar_13dg/edgar_13dg/cli.py delegating to this).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Two atom feeds (one per form type) — EDGAR's getcurrent only takes
# a single type at a time. We hit both per cycle.
_EDGAR_URLS = {
    "SC 13D": (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=SC+13D&owner=include"
        "&count=100&output=atom"
    ),
    "SC 13G": (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=SC+13G&owner=include"
        "&count=100&output=atom"
    ),
}


# Atom feed parsing — same regex toolkit as sec_8k_broad.
_ATOM_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_ATOM_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL)
_ATOM_LINK_RE = re.compile(r'<link[^>]*href="([^"]+)"', re.DOTALL)
_ATOM_UPDATED_RE = re.compile(r"<updated[^>]*>(.*?)</updated>", re.DOTALL)
_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def _altdata_db_path() -> str:
    import os
    for base in ("/opt/quantopsai/altdata/edgar_13dg/data",
                 "altdata/edgar_13dg/data"):
        if os.path.isdir(base):
            return os.path.join(base, "edgar_13dg.db")
    return "altdata/edgar_13dg/data/edgar_13dg.db"


def _ensure_13dg_table(db_path: str) -> None:
    import os
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS recent_13dg_filings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                accession TEXT NOT NULL UNIQUE,
                form_type TEXT NOT NULL,
                filing_date TEXT NOT NULL,
                accepted_at TEXT,
                filer_name TEXT,
                filer_cik TEXT,
                subject_name TEXT,
                subject_cik TEXT,
                subject_ticker TEXT,
                source_url TEXT,
                captured_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_13dg_subject_date
                ON recent_13dg_filings(subject_ticker, filing_date DESC);
            CREATE INDEX IF NOT EXISTS idx_13dg_date
                ON recent_13dg_filings(filing_date DESC);
            CREATE TABLE IF NOT EXISTS scrape_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                rows_seen INTEGER DEFAULT 0,
                rows_new INTEGER DEFAULT 0,
                error TEXT
            );
        """)


def _parse_atom_feed(xml_text: str) -> List[Dict[str, str]]:
    """Same as sec_8k_broad — minimal regex extraction."""
    out = []
    for entry in _ATOM_ENTRY_RE.findall(xml_text):
        title_m = _ATOM_TITLE_RE.search(entry)
        link_m = _ATOM_LINK_RE.search(entry)
        updated_m = _ATOM_UPDATED_RE.search(entry)
        if not (title_m and link_m):
            continue
        link = link_m.group(1)
        accession_m = _ACCESSION_RE.search(link)
        if not accession_m:
            continue
        out.append({
            "accession": accession_m.group(1),
            "title": title_m.group(1).strip(),
            "link": link,
            "updated": updated_m.group(1).strip() if updated_m else "",
        })
    return out


def _extract_filer_and_role(title: str) -> Dict[str, str]:
    """13D atom titles look like
      'SC 13D - ACKMAN PERSHING SQUARE CAPITAL (0001336528) (Filer)'
    For 13D/G filings the title gives the FILER side. We pull the
    name and CIK; the SUBJECT (target company) requires fetching
    the filing index page itself.
    """
    m = re.search(r"\s+-\s+(.+?)\s*\((\d{10})\)", title)
    if m:
        return {"filer_name": m.group(1).strip(),
                "filer_cik": m.group(2)}
    return {"filer_name": title, "filer_cik": ""}


# Pattern for the subject-company link on a 13D filing index page.
# EDGAR renders the subject as another "Filer" entry below the
# primary filer (the activist). The subject's CIK is the second
# CIK on the index page.
_SUBJECT_CIK_RE = re.compile(
    r"CIK=(\d{10}).*?</a>\s*\(Subject", re.DOTALL | re.IGNORECASE
)
_SUBJECT_NAME_RE = re.compile(
    r">\s*([A-Z0-9][^<]+?)\s*</a>\s*CIK=\d{10}\s*\(Subject",
    re.DOTALL | re.IGNORECASE,
)


def _extract_subject_from_filing_page(filing_url: str) -> Dict[str, str]:
    """Fetch the EDGAR filing-index page and extract the subject
    company (the target of the activist position). Returns
    {subject_name, subject_cik} or empty strings on failure."""
    try:
        from sec_filings import _rate_limited_get
        raw = _rate_limited_get(filing_url)
    except Exception as exc:
        logger.warning(
            "sec_13dg: filing-page fetch failed for %s: %s: %s",
            filing_url, type(exc).__name__, exc,
        )
        return {"subject_name": "", "subject_cik": ""}
    if not raw:
        return {"subject_name": "", "subject_cik": ""}
    text = raw.decode("utf-8", errors="replace")
    cik_m = _SUBJECT_CIK_RE.search(text)
    name_m = _SUBJECT_NAME_RE.search(text)
    return {
        "subject_name": name_m.group(1).strip() if name_m else "",
        "subject_cik": cik_m.group(1) if cik_m else "",
    }


def scrape_recent_13dg_filings(max_per_form: int = 100) -> Dict[str, Any]:
    """Pull both 13D and 13G atom feeds, persist new filings.
    Returns {seen, new, errors}. Idempotent via UNIQUE(accession)."""
    db_path = _altdata_db_path()
    _ensure_13dg_table(db_path)
    summary: Dict[str, Any] = {"seen": 0, "new": 0, "errors": 0}
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO scrape_runs (status) VALUES ('running')"
        )
        run_id = cur.lastrowid
        conn.commit()

    # Reuse sec_8k_broad's CIK→ticker map (built from edgar_form4's
    # companies table) since both modules need the same lookup and
    # we'd rather have ONE canonical builder.
    try:
        from sec_8k_broad import _build_reverse_cik_map
        reverse_cik = _build_reverse_cik_map()
    except Exception:
        reverse_cik = {}

    try:
        from sec_filings import _rate_limited_get
        for form_type, url in _EDGAR_URLS.items():
            raw = _rate_limited_get(url)
            if not raw:
                summary["errors"] += 1
                continue
            entries = _parse_atom_feed(raw.decode("utf-8", "replace"))
            entries = entries[:max_per_form]
            summary["seen"] += len(entries)
            for entry in entries:
                try:
                    filer = _extract_filer_and_role(entry["title"])
                    subject = _extract_subject_from_filing_page(
                        entry["link"]
                    )
                    subject_ticker = reverse_cik.get(
                        subject.get("subject_cik", ""), ""
                    )
                    with sqlite3.connect(db_path) as conn:
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO recent_13dg_filings "
                            "(accession, form_type, filing_date, "
                            " accepted_at, filer_name, filer_cik, "
                            " subject_name, subject_cik, "
                            " subject_ticker, source_url) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (
                                entry["accession"],
                                form_type,
                                (entry.get("updated") or "")[:10],
                                entry.get("updated", ""),
                                filer["filer_name"],
                                filer["filer_cik"],
                                subject["subject_name"],
                                subject["subject_cik"],
                                subject_ticker,
                                entry["link"],
                            ),
                        )
                        if cur.rowcount > 0:
                            summary["new"] += 1
                        conn.commit()
                except Exception as exc:
                    logger.warning(
                        "sec_13dg: per-filing fail accession=%s: %s: %s",
                        entry.get("accession", "?"),
                        type(exc).__name__, exc,
                    )
                    summary["errors"] += 1
    except Exception as exc:
        logger.error(
            "sec_13dg: feed fetch failed: %s: %s",
            type(exc).__name__, exc,
        )
        summary["errors"] += 1

    final_status = "ok" if summary["errors"] == 0 else "ok_with_errors"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scrape_runs SET status=?, rows_seen=?, rows_new=?, "
            "finished_at=datetime('now'), "
            "error=CASE WHEN ?=0 THEN NULL ELSE 'see logs' END "
            "WHERE id=?",
            (final_status, summary["seen"], summary["new"],
             summary["errors"], run_id),
        )
        conn.commit()
    return summary


def get_recent_13dg_activist(symbol: str, days: int = 60) -> Dict[str, Any]:
    """Public consumer API — called by alternative_data.

    Returns:
      {
        events: [{date, form_type, filer_name, source_url}, ...],
        count: int,
        has_13d: bool,   # True if any 13D (intent-to-influence) present
      }
    """
    if not symbol or "/" in symbol:
        return {"events": [], "count": 0, "has_13d": False}
    db_path = _altdata_db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cutoff = (datetime.now(tz=timezone.utc)
                      - timedelta(days=days)).date().isoformat()
            rows = conn.execute(
                "SELECT filing_date, form_type, filer_name, source_url "
                "FROM recent_13dg_filings "
                "WHERE subject_ticker = ? AND filing_date >= ? "
                "ORDER BY filing_date DESC LIMIT 20",
                (symbol.upper(), cutoff),
            ).fetchall()
    except sqlite3.OperationalError:
        return {"events": [], "count": 0, "has_13d": False}

    events = [
        {
            "date": r["filing_date"],
            "form_type": r["form_type"],
            "filer_name": r["filer_name"],
            "source_url": r["source_url"],
        }
        for r in rows
    ]
    return {
        "events": events,
        "count": len(events),
        "has_13d": any(e["form_type"] == "SC 13D" for e in events),
    }
