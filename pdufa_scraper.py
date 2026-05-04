"""OPEN_ITEMS #6 — PDUFA event scraper.

`alternative_data.get_biotech_milestones` queries a `pdufa_events`
table living in `altdata/biotechevents/data/biotechevents.db`. This
module populates that table.

Primary source: **SEC EDGAR full-text search** for "PDUFA date" in 8-K
filings. Public companies disclose PDUFA goal dates in 8-K filings
within hours of receiving them from FDA, so this is the most reliable
forward-looking source. Authoritative (the SEC), free, no anti-bot
challenges, no rate limit beyond the polite SEC fair-use rate.

Fallback: hand-curated PDUFA_FALLBACK_SEED when EDGAR returns nothing.

Previous source (now disabled): BiopharmCatalyst FDA Calendar — sits
behind Cloudflare's "I'm Under Attack" challenge mode. Empirically
verified 2026-05-04: returns 403 with `cf-mitigated: challenge`.
The BiopharmCatalyst parser is kept for legacy reference but
fetch_pdufa_events_from_biopharmcatalyst() is no longer called by
the main path.

Public surface:
  - fetch_pdufa_events() → list of dicts {ticker, drug_name, pdufa_date}
  - sync_pdufa_events_to_altdata_db(events) → writes to the
    biotechevents.db (creates table if missing)
  - run_full_sync() → fetch + sync; returns (n_fetched, n_written)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


# SEC EDGAR full-text search — primary source for PDUFA disclosures.
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
# SEC requires a UA with contact info per their fair-use policy.
SEC_USER_AGENT = "QuantOpsAI mack@mackenziesmith.com"

# BioPharmCatalyst — disabled (Cloudflare challenge as of 2026-05-04)
# but kept for the legacy parser used by tests.
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
    """Create the pdufa_events table if it doesn't exist. Schema matches
    biotechevents/biotechevents/store.py and what
    alternative_data.get_biotech_milestones queries."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pdufa_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            drug_name       TEXT    NOT NULL,
            sponsor_company TEXT    NOT NULL,
            ticker          TEXT,
            pdufa_date      TEXT    NOT NULL,
            action_type     TEXT,
            indication      TEXT,
            outcome         TEXT    DEFAULT 'pending',
            outcome_date    TEXT,
            source_url      TEXT,
            parser_version  TEXT,
            fetched_at      TEXT    NOT NULL,
            UNIQUE (drug_name, sponsor_company, pdufa_date)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdufa_ticker ON pdufa_events(ticker)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdufa_date ON pdufa_events(pdufa_date)"
    )
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
                # Match the existing biotechevents schema:
                # UNIQUE(drug_name, sponsor_company, pdufa_date), and
                # both drug_name + sponsor_company are NOT NULL. EDGAR
                # gives us the ticker reliably; sponsor_company defaults
                # to the ticker when we don't have a company name.
                drug = e.get("drug_name") or "(see filing)"
                sponsor = e.get("sponsor_company") or e["ticker"]
                conn.execute(
                    """INSERT OR REPLACE INTO pdufa_events
                       (drug_name, sponsor_company, ticker, pdufa_date,
                        action_type, source_url, parser_version, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (drug, sponsor, e["ticker"], e["pdufa_date"],
                     e.get("action_type") or None,
                     e.get("source_url", e.get("source", "")),
                     e.get("parser_version", "edgar_8k_v1")),
                )
                written += 1
            except Exception:
                continue
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("PDUFA sync write failed: %s", exc)
    return written


# ---------------------------------------------------------------------------
# Primary source: SEC EDGAR full-text search for 8-K filings
# ---------------------------------------------------------------------------

# Display names from EDGAR look like
#   "Merck & Co., Inc.  (MRK)  (CIK 0000310158)"
# We extract the ticker from the FIRST parenthesized 1-5 char alpha token.
_EDGAR_DISPLAY_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)\s+\(CIK")

# PDUFA date patterns in filing text. Tolerant: catches several
# common phrasings, and date formats handled by _parse_iso_date.
_PDUFA_DATE_PATTERNS = [
    re.compile(
        r"PDUFA[\s\S]{0,80}?(?:goal\s+)?(?:target\s+)?(?:action\s+)?date\s+of\s+"
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"PDUFA[\s\S]{0,80}?(?:goal\s+)?(?:target\s+)?(?:action\s+)?date\s+of\s+"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"PDUFA[\s\S]{0,80}?(?:goal\s+)?(?:target\s+)?(?:action\s+)?date\s+of\s+"
        r"(\d{4}-\d{2}-\d{2})",
        re.IGNORECASE,
    ),
]


def _fetch_edgar_search(query: str, lookback_days: int = 60) -> Dict:
    """Query EDGAR full-text search. Returns parsed JSON or {}."""
    today = datetime.utcnow().date()
    start = today - timedelta(days=lookback_days)
    params = {
        "q": query,
        "forms": "8-K",
        "dateRange": "custom",
        "startdt": start.isoformat(),
        "enddt": today.isoformat(),
    }
    url = f"{EDGAR_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError) as exc:
        logger.warning("EDGAR search failed: %s", exc)
        return {}


def _extract_ticker_from_display(display_name: str) -> Optional[str]:
    """Pull ticker from "Company Name  (TKR)  (CIK 0001234567)"."""
    if not display_name:
        return None
    m = _EDGAR_DISPLAY_TICKER_RE.search(display_name)
    return m.group(1) if m else None


def _build_filing_doc_url(adsh: str, cik: str, filename: str) -> str:
    """Construct the SEC archive URL for a specific filing document."""
    cik_int = str(int(cik))
    adsh_nodash = adsh.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{adsh_nodash}/{filename}"
    )


def _fetch_filing_text(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch an 8-K document and strip HTML to plain text."""
    req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        logger.debug("Filing fetch failed for %s: %s", url, exc)
        return None
    return _strip_tags(html)


def _parse_pdufa_dates_from_text(text: str) -> List[str]:
    """Find PDUFA date strings in filing text. Returns ISO YYYY-MM-DD list."""
    if not text:
        return []
    found = []
    for pat in _PDUFA_DATE_PATTERNS:
        for m in pat.finditer(text):
            iso = _parse_iso_date(m.group(1))
            if iso:
                found.append(iso)
    return list(set(found))


# Heuristics to extract structured detail from 8-K filing text.
# Action type — NDA / BLA / sNDA / sBLA / 510(k) / etc.
_ACTION_TYPE_RE = re.compile(
    r"\b(s?NDA|s?BLA|sNDA|sBLA|NDA|BLA|MAA|510\(k\)|PMA)\b",
)

# Drug-name patterns. 8-K filings tend to phrase the drug a few common ways:
#   "the NDA for [DRUG] with a PDUFA target action date of..."
#   "for the review of [DRUG] for the treatment of..."
#   "regarding [DRUG], the Company..."
#   "...accepted the BLA submission for [DRUG]..."
# We match the drug as a 2-60 char run that doesn't include sentence
# punctuation. Capitalized brand names and lowercase generics both pass.
_DRUG_PATTERNS = [
    re.compile(
        r"(?:NDA|BLA|sNDA|sBLA|application|submission|review)\s+for\s+"
        r"([A-Za-z][A-Za-z0-9\-\s]{1,58}?)\s+"
        r"(?:with|for|in|to|under|that|after|having|and)",
        re.IGNORECASE,
    ),
    re.compile(
        r"PDUFA[\s\S]{0,30}?date\s+for\s+"
        r"([A-Za-z][A-Za-z0-9\-\s]{1,58}?)\s+"
        r"(?:is|of|on|in)",
        re.IGNORECASE,
    ),
    re.compile(
        r"regarding\s+(?:its|the|our)?\s*"
        r"([A-Z][A-Za-z0-9\-]{2,30})"
        r"\s*[\.,]"
    ),
]

# Common false-positive tokens we should never accept as a drug name
# (these come from sentence fragments matching the broad regex above).
_DRUG_FP = {
    "the", "this", "that", "these", "company", "review", "application",
    "submission", "drug", "product", "therapy", "treatment", "fda",
    "biological", "supplemental", "marketing", "investigational",
    "approval", "indication", "candidate", "label",
}


def _parse_drug_and_action_near_pdufa(text: str) -> Tuple[str, str]:
    """Extract a best-effort drug name and action_type (NDA/BLA/...) from
    a window of text around the first "PDUFA" mention. Returns
    ("(see filing)", "") if nothing parseable was found."""
    if not text:
        return "(see filing)", ""

    # Action type — search the whole filing; we only need the first hit.
    action_match = _ACTION_TYPE_RE.search(text)
    action_type = action_match.group(1).upper() if action_match else ""

    # Drug name — restrict the search to a window around "PDUFA"
    # because filings often mention multiple compounds in 8-Ks.
    pdufa_idx = text.upper().find("PDUFA")
    if pdufa_idx == -1:
        return "(see filing)", action_type
    window_start = max(0, pdufa_idx - 400)
    window_end = min(len(text), pdufa_idx + 200)
    window = text[window_start:window_end]

    drug = ""
    for pat in _DRUG_PATTERNS:
        for m in pat.finditer(window):
            candidate = m.group(1).strip()
            # Strip trailing punctuation/commas
            candidate = re.sub(r"[\.,;:]+$", "", candidate).strip()
            if not candidate:
                continue
            # Skip common false-positives
            if candidate.lower() in _DRUG_FP:
                continue
            # Reasonable length range for drug names
            if 2 <= len(candidate) <= 60:
                drug = candidate
                break
        if drug:
            break

    return (drug or "(see filing)"), action_type


def fetch_pdufa_events_from_edgar(
    lookback_days: int = 60,
    max_filings: int = 50,
    polite_sleep_seconds: float = 0.2,
) -> List[Dict[str, str]]:
    """Find PDUFA disclosures in 8-K filings via EDGAR full-text search.

    For each search hit, fetches the linked filing document and extracts
    every PDUFA date phrase. Returns one event per (ticker, date) pair.

    Polite: caps total filings fetched and sleeps between requests so we
    stay well under SEC's 10 req/sec fair-use ceiling.
    """
    response = _fetch_edgar_search('"PDUFA date"', lookback_days)
    hits = response.get("hits", {}).get("hits", [])
    if not hits:
        return []

    events = []
    for hit in hits[:max_filings]:
        source = hit.get("_source", {})
        display_names = source.get("display_names", [])
        ciks = source.get("ciks", [])
        adsh = source.get("adsh", "")
        doc_id = hit.get("_id", "")
        if not display_names or not ciks or not adsh or ":" not in doc_id:
            continue
        ticker = _extract_ticker_from_display(display_names[0])
        if not ticker:
            continue
        filename = doc_id.split(":", 1)[1]
        url = _build_filing_doc_url(adsh, ciks[0], filename)
        text = _fetch_filing_text(url)
        time.sleep(polite_sleep_seconds)
        if not text:
            continue
        dates = _parse_pdufa_dates_from_text(text)
        if not dates:
            continue
        drug_name, action_type = _parse_drug_and_action_near_pdufa(text)
        sponsor = display_names[0].split("(")[0].strip()
        for pdufa_date in dates:
            events.append({
                "ticker": ticker,
                "drug_name": drug_name,
                "sponsor_company": sponsor,
                "pdufa_date": pdufa_date,
                "action_type": action_type,
                "source": "edgar_8k",
                "source_url": url,
            })

    seen = set()
    unique = []
    for e in events:
        key = (e["ticker"], e["pdufa_date"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def fetch_pdufa_events() -> List[Dict[str, str]]:
    """One-shot scrape. EDGAR is primary; falls back to hand-curated seed
    on empty. Caller is the daily scheduler task."""
    edgar_events = fetch_pdufa_events_from_edgar()
    if edgar_events:
        logger.info("PDUFA: EDGAR returned %d events", len(edgar_events))
        return edgar_events
    logger.info(
        "PDUFA: EDGAR returned 0 events; using %d fallback-seed rows",
        len(PDUFA_FALLBACK_SEED),
    )
    return list(PDUFA_FALLBACK_SEED)


def run_full_sync() -> Tuple[int, int]:
    """Fetch then sync. Returns (n_fetched, n_written)."""
    events = fetch_pdufa_events()
    n = sync_pdufa_events_to_altdata_db(events) if events else 0
    return (len(events), n)
