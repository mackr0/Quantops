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

# Drug-name patterns. 8-K filings phrase the drug in ~7 distinct ways
# observed in real filings (samples collected 2026-05-04 from prod runs):
#   "the NDA for [DRUG] with a PDUFA target action date of..."
#   "FDA Approval of [DRUG] for the Treatment of..."           (ARVN)
#   "approval of [DRUG], an investigational..."                (CAPR)
#   "the [DRUG] NDA" / "the [DRUG] BLA"                        (ALDX)
#   "commercialization of [DRUG] as a treatment of..."         (ACHV)
#   "for [DRUG] for the treatment of presbyopia"               (IRD)
#   "regarding [DRUG], the Company..."
# Each pattern captures the drug as a 2-60 char run that doesn't include
# sentence punctuation. Capitalized brand names and lowercase generics
# both pass.
_DRUG_PATTERNS = [
    # 1. "NDA / BLA / application / submission / review FOR [DRUG] (terminator)"
    re.compile(
        r"(?:s?NDA|s?BLA|application|submission|review)\s+for\s+"
        r"([A-Za-z][A-Za-z0-9\-\s]{1,58}?)\s+"
        r"(?:with|for|in|to|under|that|after|having|and|seeking)",
        re.IGNORECASE,
    ),
    # 2. "PDUFA date for/of [DRUG] (terminator)"
    re.compile(
        r"PDUFA[\s\S]{0,30}?(?:date|target action date)\s+(?:for|of)\s+"
        r"([A-Za-z][A-Za-z0-9\-\s]{1,58}?)\s+"
        r"(?:is|of|on|in|with|for|having|and)",
        re.IGNORECASE,
    ),
    # 3. "Approval / Acceptance / Filing of [DRUG] (terminator)"
    #    Captures both the brand and an optional parenthesized generic.
    re.compile(
        r"(?:Approval|Acceptance|Filing|Submission)\s+of\s+"
        r"([A-Z][A-Za-z0-9\-]{2,40})"
        r"(?:\s*\(([a-z][\w\-]{3,30})\))?",
    ),
    # 4. "the [DRUG] NDA" / "the [DRUG] BLA" / etc. — drug right before app type
    re.compile(
        r"\b(?:the|its|our)\s+([a-z][\w\-]{4,30})\s+"
        r"(?:s?NDA|s?BLA)\b",
        re.IGNORECASE,
    ),
    # 5. "commercialization / development / review / approval / use of [DRUG]"
    #    Followed by "as|for|in|to" terminator (not "of" — that fragments).
    re.compile(
        r"(?:commercialization|development|review|approval|use|seeking[\s\w]+approval)\s+of\s+"
        r"([A-Za-z][\w\-]{3,40}(?:\s+[\w\-]+){0,2}?)\s*"
        r"(?:[\.,]|\s+(?:as|for|in|to)\s+)",
        re.IGNORECASE,
    ),
    # 6. "for [DRUG] for the treatment / management / prevention of"
    #    Two-"for" sentences — captures drug between them. Char class
    #    allows percent and slash for formulations like
    #    "Phentolamine Ophthalmic Solution 0.75%".
    re.compile(
        r"\bfor\s+"
        r"((?!the\b|an\b|a\b)[A-Z][\w\-\s\.\%/]{2,60}?)\s+"
        r"for\s+the\s+(?:treatment|management|prevention)",
    ),
    # 7. "regarding [DRUG], the Company..."
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

# Reject candidates that START with these tokens — catches the
# "the treatment of X" / "for the treatment of X" trap where the
# greedy regex consumed a phrase rather than a drug name.
_BANNED_PREFIX_TOKENS = {
    "the", "an", "a", "this", "its", "our", "their", "for",
    "with", "in", "of", "to", "and", "or", "review", "submission",
}

# FDA program / designation phrases that look like proper nouns
# (capitalized title case) but are NOT drug names. These appear in
# 8-K filings constantly and the greedy phrase regex picks them up.
_FDA_DESIGNATION_PHRASES = {
    "priority review", "breakthrough therapy", "fast track",
    "orphan drug", "rare pediatric", "regenerative medicine",
    "advanced therapy", "complete response", "review designation",
    "advisory committee", "user fee",
}

# WHO INN suffixes that uniquely identify a generic drug name.
# Word-final pattern only — e.g. "...mab" but not "lambda" or "ambush".
# Suffix list uses actual INN stems:
#   -rsen      antisense oligonucleotide (olezarsen, zilganersen, mipomersen)
#   -mab       monoclonal antibody (pembrolizumab)
#   -tinib     kinase inhibitor (osimertinib)
#   -ciclib    CDK inhibitor (palbociclib)
#   -afil      PDE5 inhibitor (sildenafil)
#   -prazole   proton pump inhibitor (omeprazole)
#   -avir      antiviral (remdesivir)
#   -mycin     antibiotic (azithromycin)
#   -lukast    leukotriene antagonist (montelukast)
#   -conazole  azole antifungal (itraconazole)
#   -sartan    angiotensin antagonist (losartan)
#   -statin    HMG-CoA reductase inhibitor (atorvastatin)
#   -sertib    serine/threonine kinase inhibitor
#   -farib     ribonucleotide reductase inhibitor
#   -nib       generic kinase inhibitor (sotorasib)
#   -olol      beta blocker (atenolol)
_DRUG_SUFFIX_RE = re.compile(
    r"\b([A-Za-z][a-z]{4,30}"
    r"(?:mab|tinib|ciclib|afil|rsen|prazole|avir|mycin|lukast|"
    r"conazole|sartan|statin|sertib|farib|nib|olol)"
    r")\b",
    re.IGNORECASE,
)
# Compound-code patterns — XYZ-123 / ARV-471 / mRNA-1647 / BMS-986178.
_COMPOUND_CODE_RE = re.compile(
    r"\b([A-Za-z]{2,8}-\d{2,5}(?:-\d+)?)\b",
)

# SEC filing artifacts that LOOK like compound codes but aren't drugs.
# E.g. "EX-99.1" is Exhibit 99.1, "RULE-10b5", "FORM-8K", "ITEM-2.02".
# These appear constantly in 8-K filings and would otherwise match
# _COMPOUND_CODE_RE.
_SEC_ARTIFACT_PREFIXES = {
    "ex", "form", "rule", "item", "sec", "section", "reg", "regs",
    "file", "def", "para", "appendix", "schedule", "annex",
}


def _parse_drug_and_action_near_phrase(
    text: str, anchor: str
) -> Tuple[str, str]:
    """Extract a best-effort drug name and action_type (NDA/BLA/...) from
    a window of text around the first occurrence of `anchor` (typically
    "PDUFA" or "Advisory"). Returns ("(see filing)", "") if nothing
    parseable was found.

    Used by both the PDUFA fetcher (anchor="PDUFA") and the AdComm
    fetcher (anchor="Advisory"). The 3-pass extraction is identical
    for both — only the anchor differs."""
    if not text:
        return "(see filing)", ""

    # Action type — search the whole filing; we only need the first hit.
    action_match = _ACTION_TYPE_RE.search(text)
    action_type = action_match.group(1).upper() if action_match else ""

    anchor_idx = text.upper().find(anchor.upper())
    if anchor_idx == -1:
        return "(see filing)", action_type
    window_start = max(0, anchor_idx - 500)
    window_end = min(len(text), anchor_idx + 300)
    window = text[window_start:window_end]

    drug = ""

    # Pass 1: phrase-based regexes (NDA for X, PDUFA date for X, etc.)
    for pat in _DRUG_PATTERNS:
        for m in pat.finditer(window):
            candidate = m.group(1).strip()
            candidate = re.sub(r"[\.,;:]+$", "", candidate).strip()
            if not candidate:
                continue
            cl = candidate.lower()
            first_word = cl.split(" ", 1)[0]
            if first_word in _BANNED_PREFIX_TOKENS or cl in _DRUG_FP:
                continue
            if cl in _FDA_DESIGNATION_PHRASES:
                continue
            if any(phrase in cl for phrase in _FDA_DESIGNATION_PHRASES):
                continue
            if 2 <= len(candidate) <= 60:
                drug = candidate
                break
        if drug:
            break

    # Pass 2: WHO INN drug suffixes (-mab, -nib, -rsen, etc.)
    if not drug:
        for m in _DRUG_SUFFIX_RE.finditer(window):
            candidate = m.group(1)
            if candidate.lower() in _DRUG_FP:
                continue
            if 4 <= len(candidate) <= 30:
                drug = candidate
                break

    # Pass 3: compound codes (XYZ-123, ARV-471, mRNA-1647).
    if not drug:
        for m in _COMPOUND_CODE_RE.finditer(window):
            candidate = m.group(1)
            prefix = candidate.split("-", 1)[0].lower()
            if prefix in _SEC_ARTIFACT_PREFIXES:
                continue
            if 3 <= len(candidate) <= 30:
                drug = candidate
                break

    return (drug or "(see filing)"), action_type


def _parse_drug_and_action_near_pdufa(text: str) -> Tuple[str, str]:
    """Backward-compatible PDUFA-specific wrapper. New callers should
    use _parse_drug_and_action_near_phrase directly."""
    return _parse_drug_and_action_near_phrase(text, "PDUFA")


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
    """Fetch then sync. Returns (n_fetched, n_written) for PDUFA only —
    AdComm runs as a side-channel and reports its own counts via the log."""
    events = fetch_pdufa_events()
    n = sync_pdufa_events_to_altdata_db(events) if events else 0
    # AdComm side-channel: same EDGAR API, different query, parallel
    # table. Failures are independent — a broken AdComm parse should
    # not invalidate the PDUFA pull, and vice versa. Table is created
    # eagerly so the AI's read path (alternative_data.get_biotech_milestones)
    # can issue SELECTs against it from day 1, even before any 8-K
    # filing has surfaced an AdComm meeting.
    try:
        _ensure_adcomm_table(_altdata_db_path())
        adcomm_events = fetch_adcomm_events_from_edgar()
        if adcomm_events:
            adcomm_written = sync_adcomm_events_to_altdata_db(adcomm_events)
            logger.info(
                "AdComm: fetched %d, wrote %d",
                len(adcomm_events), adcomm_written,
            )
        else:
            logger.info("AdComm: 0 events in 60-day window")
    except Exception as exc:
        logger.warning("AdComm side-sync failed: %s", exc)
    return (len(events), n)


# ---------------------------------------------------------------------------
# AdComm (FDA Advisory Committee meeting) scraper
# ---------------------------------------------------------------------------
# Companies disclose upcoming Advisory Committee meeting dates in 8-K
# filings (and sometimes the meeting outcome). These are leading
# indicators for PDUFA decisions: an AdComm typically precedes a PDUFA
# date by 1-3 months.
#
# Schema parallels pdufa_events for the same UNIQUE-key pattern.

# Date phrasings observed in real AdComm 8-K filings:
#   "Advisory Committee meeting on May 15, 2026"
#   "AdComm scheduled for May 15"
#   "FDA Advisory Committee meeting on 5/15/2026"
_ADCOMM_DATE_PATTERNS = [
    re.compile(
        r"Advisory\s+Committee[\s\S]{0,80}?(?:meeting|scheduled)\s+(?:on|for)\s+"
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"Advisory\s+Committee[\s\S]{0,80}?(?:meeting|scheduled)\s+(?:on|for)\s+"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"AdComm[\s\S]{0,40}?(?:meeting|scheduled)\s+(?:on|for)\s+"
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE,
    ),
]

# Specific committee names (optional metadata).
_COMMITTEE_NAME_RE = re.compile(
    r"\b(ODAC|GIDAC|BPAC|EMDAC|VRBPAC|CRDAC|DSARM|"
    r"Oncologic Drugs Advisory Committee|"
    r"Cellular,?\s*Tissue,?\s*and\s*Gene\s*Therapies\s*Advisory\s*Committee|"
    r"Antimicrobial Drugs Advisory Committee|"
    r"Cardiovascular and Renal Drugs Advisory Committee|"
    r"Neurological Drugs Advisory Committee|"
    r"Endocrinologic and Metabolic Drugs Advisory Committee|"
    r"Vaccines and Related Biological Products Advisory Committee)\b",
)


def _ensure_adcomm_table(db_path: str) -> None:
    """Create adcomm_events table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS adcomm_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT    NOT NULL,
            sponsor_company TEXT    NOT NULL,
            drug_name       TEXT,
            adcomm_date     TEXT    NOT NULL,
            committee_name  TEXT,
            outcome         TEXT    DEFAULT 'pending',
            outcome_date    TEXT,
            source_url      TEXT,
            parser_version  TEXT,
            fetched_at      TEXT    NOT NULL,
            UNIQUE (ticker, adcomm_date)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_adcomm_ticker ON adcomm_events(ticker)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_adcomm_date ON adcomm_events(adcomm_date)"
    )
    conn.commit()
    conn.close()


def _parse_adcomm_dates_from_text(text: str) -> List[str]:
    """Find AdComm meeting date strings. Returns ISO YYYY-MM-DD list."""
    if not text:
        return []
    found = []
    for pat in _ADCOMM_DATE_PATTERNS:
        for m in pat.finditer(text):
            iso = _parse_iso_date(m.group(1))
            if iso:
                found.append(iso)
    return list(set(found))


def _parse_committee_name(text: str) -> str:
    """Extract a specific committee name (ODAC, BPAC, etc.) if mentioned."""
    if not text:
        return ""
    m = _COMMITTEE_NAME_RE.search(text)
    return m.group(1).strip() if m else ""


def fetch_adcomm_events_from_edgar(
    lookback_days: int = 60,
    max_filings: int = 50,
    polite_sleep_seconds: float = 0.2,
) -> List[Dict[str, str]]:
    """Pull AdComm meeting disclosures from 8-K filings via EDGAR
    full-text search. Returns a list of {ticker, drug_name,
    adcomm_date, committee_name, source_url} dicts."""
    response = _fetch_edgar_search('"Advisory Committee meeting"', lookback_days)
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
        dates = _parse_adcomm_dates_from_text(text)
        if not dates:
            continue
        sponsor = display_names[0].split("(")[0].strip()
        # Reuse the PDUFA drug-name extractor since the surrounding
        # filing text is similar — drug-name patterns work for AdComm
        # 8-Ks too. Just point it at "Advisory" instead of "PDUFA".
        drug_name, _ = _parse_drug_and_action_near_phrase(text, "Advisory")
        committee = _parse_committee_name(text)
        for adcomm_date in dates:
            events.append({
                "ticker": ticker,
                "drug_name": drug_name,
                "sponsor_company": sponsor,
                "adcomm_date": adcomm_date,
                "committee_name": committee,
                "source_url": url,
            })

    seen = set()
    unique = []
    for e in events:
        key = (e["ticker"], e["adcomm_date"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def sync_adcomm_events_to_altdata_db(
    events: List[Dict[str, str]],
    db_path: Optional[str] = None,
) -> int:
    """Upsert AdComm events into adcomm_events. Returns rows written."""
    db_path = db_path or _altdata_db_path()
    if not os.path.exists(os.path.dirname(db_path)):
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        except OSError as exc:
            logger.warning("AdComm sync: mkdir failed: %s", exc)
            return 0
    _ensure_adcomm_table(db_path)
    written = 0
    try:
        conn = sqlite3.connect(db_path)
        for e in events:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO adcomm_events
                       (ticker, sponsor_company, drug_name, adcomm_date,
                        committee_name, source_url, parser_version,
                        fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (e["ticker"], e.get("sponsor_company") or e["ticker"],
                     e.get("drug_name") or "(see filing)",
                     e["adcomm_date"], e.get("committee_name") or None,
                     e.get("source_url", ""), "edgar_8k_v1"),
                )
                written += 1
            except Exception:
                continue
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("AdComm sync write failed: %s", exc)
    return written
