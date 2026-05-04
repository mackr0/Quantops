"""OPEN_ITEMS #6 — PDUFA event scraper.

`alternative_data.get_biotech_milestones` queries a `pdufa_events`
table living in `altdata/biotechevents/data/biotechevents.db`, but the
original scraper that populated that table was deferred per
ALTDATA_INTEGRATION_PLAN.md ("0 PDUFA events").

This module fills the gap. Sources (in fragility order, best first):

  1. BiopharmCatalyst FDA Calendar (biopharmcatalyst.com/calendars/fda-calendar)
     Free, public, aggregates PDUFA goal dates from press releases
     and SEC filings. HTML scrape; layout has been stable for years
     but breakage is the most likely failure mode.

  2. (Future) Direct SEC 8-K text mining for PDUFA mentions —
     authoritative, no third-party fragility, but heavy NLP work.

  3. (Future) FDA AdComm meeting calendar — official, lists drug
     review dates that bracket PDUFA decisions.

Best-effort: any HTTP / parse failure returns 0 rows and the table
stays empty. The biotech_milestones helper already handles "no
upcoming PDUFA" cleanly.

Public surface:
  - fetch_pdufa_events() → list of dicts {ticker, drug_name, pdufa_date}
  - sync_pdufa_events_to_altdata_db(events) → writes to the
    biotechevents.db (creates table if missing)
  - run_full_sync() → fetch + sync; returns (n_fetched, n_written)
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


BIOPHARMCATALYST_URL = "https://www.biopharmcatalyst.com/calendars/fda-calendar"

# Hand-curated fallback: small set of well-known upcoming PDUFA dates
# the user can extend manually. Used when scraping is blocked or fails.
# Format: list of {ticker, drug_name, pdufa_date (YYYY-MM-DD)}.
PDUFA_FALLBACK_SEED: List[Dict[str, str]] = [
    # Empty by default. Operators can extend this list when known
    # PDUFA dates need to land in the system without waiting on the
    # scraper. CHANGELOG should explain any additions.
]


def _ensure_pdufa_table(db_path: str) -> None:
    """Create the pdufa_events table if it doesn't exist. Schema
    matches what alternative_data.get_biotech_milestones queries."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pdufa_events (
            ticker TEXT NOT NULL,
            drug_name TEXT,
            pdufa_date TEXT NOT NULL,
            source TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, drug_name, pdufa_date)
        )
    """)
    conn.commit()
    conn.close()


def _fetch_html(url: str, timeout: int = 15) -> Optional[str]:
    """GET with a polite UA. Returns body text or None on any failure."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "QuantOpsAI/1.0 (mack@mackenziesmith.com)",
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return data.decode("utf-8", "ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        logger.warning("PDUFA fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Parser: BiopharmCatalyst FDA calendar
# ---------------------------------------------------------------------------

# The page renders rows shaped roughly like:
#   <tr>
#     <td>2026-05-15</td>           # PDUFA date (or other event date)
#     <td>PDUFA</td>                # event type tag
#     <td>BMY</td>                  # ticker
#     <td>Eliquis</td>              # drug_name
#     ...
#   </tr>
# Layout has shifted historically; this regex tolerates whitespace
# and minor markup variation.

_ROW_RE = re.compile(
    r"<tr[^>]*>(.*?)</tr>",
    re.DOTALL | re.IGNORECASE,
)
_CELL_RE = re.compile(
    r"<t[dh][^>]*>(.*?)</t[dh]>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", s)).strip()


def _parse_iso_date(token: str) -> Optional[str]:
    """Best-effort date parsing — accepts YYYY-MM-DD, MM/DD/YYYY,
    'Jan 15, 2026', 'Q1 2026', etc. Returns ISO string or None."""
    token = token.strip()
    if not token:
        return None
    # ISO
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", token)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # MM/DD/YYYY
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", token)
    if m:
        mo, d, y = m.group(1), m.group(2), m.group(3)
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    # Mon DD, YYYY or Month DD, YYYY (e.g. "Jan 15, 2026" / "September 1, 2026")
    months = {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }
    m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", token)
    if m:
        mo_name, d, y = m.group(1).lower(), m.group(2), m.group(3)
        if mo_name in months:
            return f"{y}-{months[mo_name]:02d}-{int(d):02d}"
    return None


def parse_biopharmcatalyst(html: str) -> List[Dict[str, str]]:
    """Extract PDUFA rows from the calendar HTML.

    Returns a list of dicts {ticker, drug_name, pdufa_date, source}.
    Best-effort: layout drift returns fewer or zero rows; never raises.
    """
    if not html:
        return []
    out: List[Dict[str, str]] = []
    for m in _ROW_RE.finditer(html):
        row_html = m.group(1)
        cells = [_strip_tags(c.group(1)) for c in _CELL_RE.finditer(row_html)]
        if len(cells) < 3:
            continue
        # Look for a PDUFA tag in any cell
        if not any("PDUFA" in c.upper() for c in cells):
            continue
        # Find the first cell that parses as a date
        pdufa_date = None
        for c in cells:
            d = _parse_iso_date(c)
            if d:
                pdufa_date = d
                break
        if not pdufa_date:
            continue
        # Find a ticker — short uppercase token (1-5 letters), excluding
        # common page-element words.
        EXCLUDE = {"PDUFA", "ADCOMM", "CRL", "FDA", "EMA", "HHS", "IND",
                    "NDA", "BLA", "CMC", "GMP", "REMS", "PHASE",
                    "CHMP", "ODAC"}
        ticker = None
        for c in cells:
            tok = c.strip()
            if (1 <= len(tok) <= 5 and tok.isupper() and tok.isalpha()
                    and tok not in EXCLUDE):
                ticker = tok
                break
        if not ticker:
            continue
        # Drug name — the longest non-tag, non-date, non-ticker cell
        drug_name = ""
        for c in sorted(cells, key=len, reverse=True):
            if c == ticker or c == pdufa_date or "PDUFA" in c.upper():
                continue
            if 2 <= len(c) <= 80 and not _parse_iso_date(c):
                drug_name = c
                break
        out.append({
            "ticker": ticker,
            "drug_name": drug_name or "Unknown",
            "pdufa_date": pdufa_date,
            "source": "biopharmcatalyst",
        })
    # Dedupe (ticker, drug, date)
    seen = set()
    unique = []
    for e in out:
        key = (e["ticker"], e["drug_name"], e["pdufa_date"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


# ---------------------------------------------------------------------------
# Sync to alt-data DB
# ---------------------------------------------------------------------------

def _altdata_db_path() -> str:
    """Path to altdata/biotechevents/data/biotechevents.db (or env override)."""
    base = os.environ.get("ALTDATA_BASE_PATH")
    if not base:
        repo_root = os.path.dirname(os.path.abspath(__file__))
        base = os.path.join(repo_root, "altdata")
    return os.path.join(base, "biotechevents", "data", "biotechevents.db")


def sync_pdufa_events_to_altdata_db(
    events: List[Dict[str, str]],
    db_path: Optional[str] = None,
) -> int:
    """Upsert events into pdufa_events. Returns rows written."""
    db_path = db_path or _altdata_db_path()
    if not os.path.exists(os.path.dirname(db_path)):
        logger.info(
            "PDUFA sync: %s does not exist; creating", os.path.dirname(db_path),
        )
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        except OSError as exc:
            logger.warning("PDUFA sync: mkdir failed: %s", exc)
            return 0
    _ensure_pdufa_table(db_path)
    written = 0
    try:
        conn = sqlite3.connect(db_path)
        for e in events:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO pdufa_events
                       (ticker, drug_name, pdufa_date, source, fetched_at)
                       VALUES (?, ?, ?, ?, datetime('now'))""",
                    (e["ticker"], e.get("drug_name", ""),
                     e["pdufa_date"], e.get("source", "manual")),
                )
                written += 1
            except Exception:
                continue
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("PDUFA sync write failed: %s", exc)
    return written


def fetch_pdufa_events() -> List[Dict[str, str]]:
    """One-shot scrape. Best-effort; HTML failure → empty list +
    fallback seed. Caller is the daily scheduler task."""
    html = _fetch_html(BIOPHARMCATALYST_URL)
    parsed = parse_biopharmcatalyst(html or "")
    if parsed:
        return parsed
    logger.info(
        "PDUFA: no rows parsed; using %d fallback-seed rows",
        len(PDUFA_FALLBACK_SEED),
    )
    return list(PDUFA_FALLBACK_SEED)


def run_full_sync() -> Tuple[int, int]:
    """Fetch then sync. Returns (n_fetched, n_written)."""
    events = fetch_pdufa_events()
    n = sync_pdufa_events_to_altdata_db(events) if events else 0
    return (len(events), n)
