"""Broad-universe SEC 8-K discovery (#1 Tier-1 alt-data, 2026-05-17).

Distinct from `sec_filings.monitor_symbol`, which only watches 8-Ks
for symbols already on the profile's shortlist or held positions.
This module SCANS THE FULL UNIVERSE every day so the AI can react
to material events the screener wouldn't have found.

Source: EDGAR's "recent filings" atom feed at
  https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent
  &type=8-K&output=atom

Updated by EDGAR within a few minutes of each filing. Reading the
atom feed is free, rate-limited via the standard SEC-compliant
headers in `sec_filings._rate_limited_get`.

For each filing we capture:
  - accession number (PK — idempotency)
  - filing_date, accepted_at
  - company_name, cik
  - ticker (resolved from cik via sec_filings.lookup_cik reverse map
    when available — many filers don't have a public ticker)
  - items: list of "Item N.NN" codes from the filing text
    (e.g. ["1.01", "5.02"] = M&A + officer change)
  - source_url

Item-type taxonomy (the high-signal ones we tag explicitly; others
get bucketed as "other"):
  1.01  Material Definitive Agreement (M&A, big contracts)
  1.02  Termination of Material Agreement
  1.03  Bankruptcy / Receivership
  2.01  Acquisition / Disposition of Assets
  2.02  Results of Operations (earnings)
  2.05  Costs from Exit / Restructuring
  3.02  Unregistered Sale of Securities
  4.02  Non-Reliance on Prior Financials (restatement)
  5.02  Departure / Appointment of Officers
  7.01  Reg FD Disclosure
  8.01  Other Material Events
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# EDGAR endpoints. type=8-K filter limits to 8-Ks only; count=100
# is EDGAR's max-per-page. The atom feed returns most-recent first.
_EDGAR_RECENT_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&owner=include"
    "&count=100&output=atom"
)


# High-signal items we tag by code; everything else bucketed as "other"
HIGH_SIGNAL_ITEMS = {
    "1.01": "material_agreement",
    "1.02": "agreement_termination",
    "1.03": "bankruptcy",
    "2.01": "acquisition_disposition",
    "2.02": "earnings",
    "2.05": "restructuring_costs",
    "3.02": "unregistered_sale",
    "4.02": "restatement",  # high-impact — prior financials unreliable
    "5.02": "officer_change",
    "7.01": "reg_fd",
    "8.01": "other_material",
}


_ITEM_RE = re.compile(r"\bItem\s+(\d+\.\d+)", re.IGNORECASE)
# Atom feed entry — minimal parsing without an XML lib to keep deps low.
_ATOM_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_ATOM_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL)
_ATOM_LINK_RE = re.compile(r'<link[^>]*href="([^"]+)"', re.DOTALL)
_ATOM_UPDATED_RE = re.compile(r"<updated[^>]*>(.*?)</updated>", re.DOTALL)
_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def _altdata_db_path() -> str:
    """Resolution mirrors edgar_form4's _altdata_db helper."""
    import os
    for base in ("/opt/quantopsai/altdata/edgar_8k/data",
                 "altdata/edgar_8k/data"):
        if os.path.isdir(base):
            return os.path.join(base, "edgar_8k.db")
    # Default to local dev path; caller will create the dir
    return "altdata/edgar_8k/data/edgar_8k.db"


def _ensure_8k_table(db_path: str) -> None:
    """Create the schema if missing. Idempotent via UNIQUE(accession)."""
    import os
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS recent_8k_filings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                accession TEXT NOT NULL UNIQUE,
                filing_date TEXT NOT NULL,
                accepted_at TEXT,
                company_name TEXT,
                cik TEXT,
                ticker TEXT,
                items_json TEXT,
                source_url TEXT,
                captured_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_8k_ticker_date
                ON recent_8k_filings(ticker, filing_date DESC);
            CREATE INDEX IF NOT EXISTS idx_8k_date
                ON recent_8k_filings(filing_date DESC);
            CREATE INDEX IF NOT EXISTS idx_8k_cik
                ON recent_8k_filings(cik);
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
    """Best-effort XML extraction. Returns list of partial-filing dicts
    (accession, title, link, updated). Filing-text fetch happens later."""
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


def _extract_company_and_cik(title: str) -> Dict[str, str]:
    """EDGAR atom titles look like '8-K - APPLE INC (0000320193) (Filer)'.
    Returns {company_name, cik}.

    Pattern requires SPACE-dash-SPACE before the company name so the
    '-' inside '8-K' doesn't get matched (caught by regression test
    test_company_and_cik_extraction)."""
    # ... <FORM-TYPE> - <COMPANY> (NNNNNNNNNN) (Filer-role)
    m = re.search(r"\s+-\s+(.+?)\s*\((\d{10})\)", title)
    if m:
        return {"company_name": m.group(1).strip(), "cik": m.group(2)}
    return {"company_name": title, "cik": ""}


def _extract_items_from_filing(filing_url: str) -> List[str]:
    """Fetch the filing index page and extract 'Item N.NN' codes.
    Returns sorted unique list of item codes."""
    try:
        from sec_filings import _rate_limited_get
        raw = _rate_limited_get(filing_url)
    except Exception as exc:
        logger.warning(
            "sec_8k_broad: filing fetch failed for %s: %s: %s",
            filing_url, type(exc).__name__, exc,
        )
        return []
    if not raw:
        return []
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return []
    items = sorted(set(_ITEM_RE.findall(text)))
    return items


def scrape_recent_8k_filings(max_filings: int = 100) -> Dict[str, Any]:
    """Pull the recent 8-K atom feed, persist new filings to the
    altdata DB. Returns summary {seen, new, errors}.

    Idempotent — re-running within the same window only inserts
    filings whose accession isn't already in the DB.
    """
    db_path = _altdata_db_path()
    _ensure_8k_table(db_path)
    summary: Dict[str, Any] = {"seen": 0, "new": 0, "errors": 0}
    # Log a scrape_run row so morning_health_check / issues_collector
    # surface failures consistently with other altdata modules.
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO scrape_runs (status) VALUES ('running')"
        )
        run_id = cur.lastrowid
        conn.commit()

    try:
        from sec_filings import _rate_limited_get
        raw = _rate_limited_get(_EDGAR_RECENT_URL)
        if not raw:
            raise RuntimeError("EDGAR recent-feed returned no body")
        feed_text = raw.decode("utf-8", errors="replace")
        entries = _parse_atom_feed(feed_text)[:max_filings]
        summary["seen"] = len(entries)
    except Exception as exc:
        logger.error(
            "sec_8k_broad: feed fetch failed: %s: %s",
            type(exc).__name__, exc,
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE scrape_runs SET status='failed', error=?, "
                "finished_at=datetime('now') WHERE id=?",
                (f"feed: {type(exc).__name__}: {exc}", run_id),
            )
            conn.commit()
        return summary

    # Per-filing processing: parse company info from title, fetch
    # items from filing index, persist new rows.
    from sec_filings import lookup_cik
    # Build reverse map (cik → ticker) once per run for the symbols
    # most likely to be in our universe.
    reverse_cik = _build_reverse_cik_map()

    for entry in entries:
        try:
            meta = _extract_company_and_cik(entry["title"])
            cik = meta["cik"]
            ticker = reverse_cik.get(cik, "")
            items = _extract_items_from_filing(entry["link"])
            with sqlite3.connect(db_path) as conn:
                # ON CONFLICT(accession) DO NOTHING — idempotency
                cur = conn.execute(
                    "INSERT OR IGNORE INTO recent_8k_filings "
                    "(accession, filing_date, accepted_at, "
                    " company_name, cik, ticker, items_json, source_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["accession"],
                        (entry.get("updated") or "")[:10],
                        entry.get("updated", ""),
                        meta["company_name"],
                        cik,
                        ticker,
                        ",".join(items) if items else "",
                        entry["link"],
                    ),
                )
                if cur.rowcount > 0:
                    summary["new"] += 1
                conn.commit()
        except Exception as exc:
            logger.warning(
                "sec_8k_broad: per-filing fail accession=%s: %s: %s",
                entry.get("accession", "?"),
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


def _build_reverse_cik_map() -> Dict[str, str]:
    """cik → ticker reverse lookup. Pulled from edgar_form4's companies
    table when present (most comprehensive locally), fallback empty
    dict. NOT a guess — verified against the actual schema."""
    candidates = (
        "/opt/quantopsai/altdata/edgar_form4/data/edgar_form4.db",
        "altdata/edgar_form4/data/edgar_form4.db",
    )
    import os
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with sqlite3.connect(path) as conn:
                # Schema verified by manual `.schema companies` —
                # has (cik, ticker, company_name) cols
                rows = conn.execute(
                    "SELECT cik, ticker FROM companies "
                    "WHERE ticker IS NOT NULL AND ticker != ''"
                ).fetchall()
                # Pad CIK to 10 chars to match EDGAR atom feed format
                return {str(c).zfill(10): t.upper() for c, t in rows}
        except sqlite3.OperationalError:
            continue
    return {}


def get_recent_8k_events(symbol: str, days: int = 30) -> Dict[str, Any]:
    """Public consumer API — called by alternative_data.

    Returns:
      {
        events: [{date, items: [...], item_tags: [...], company_name, source_url}],
        count: int,
        high_signal_count: int,
      }
    """
    if not symbol or "/" in symbol:
        return {"events": [], "count": 0, "high_signal_count": 0}
    db_path = _altdata_db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cutoff = (datetime.now(tz=timezone.utc)
                      - timedelta(days=days)).date().isoformat()
            rows = conn.execute(
                "SELECT filing_date, company_name, items_json, source_url "
                "FROM recent_8k_filings "
                "WHERE ticker = ? AND filing_date >= ? "
                "ORDER BY filing_date DESC LIMIT 50",
                (symbol.upper(), cutoff),
            ).fetchall()
    except sqlite3.OperationalError:
        return {"events": [], "count": 0, "high_signal_count": 0}

    events = []
    high_signal_count = 0
    for r in rows:
        items = [i.strip() for i in (r["items_json"] or "").split(",") if i.strip()]
        tags = [HIGH_SIGNAL_ITEMS[i] for i in items
                if i in HIGH_SIGNAL_ITEMS]
        if tags:
            high_signal_count += 1
        events.append({
            "date": r["filing_date"],
            "company_name": r["company_name"],
            "items": items,
            "item_tags": tags,
            "source_url": r["source_url"],
        })
    return {
        "events": events,
        "count": len(events),
        "high_signal_count": high_signal_count,
    }
