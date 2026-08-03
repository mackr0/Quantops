"""Alt-data enrichment — reconnect scraper DBs to the ticker universe.

The 2026-07-26 connections audit found the three local scraper DBs
were nearly unusable through the `alternative_data` readers, not for
lack of data but for lack of *joins*:

  edgar13f     965k holdings rows, ticker populated on 1.2% (only a
               35-CUSIP hand seed); filings.period_of_report '' on
               all rows (scraper read the wrong submissions-JSON key),
               so the reader mixed quarters when it matched at all.
  biotechevents 50k trials, ticker on 6.4% (hand sponsor map only).
  stocktwits    healthy but tracked 37 tickers vs a ~400-symbol
               trading universe (fixed in stocktwits/cli.py).

This module fixes the first two DB-side, idempotently:

  enrich_13f_periods()   backfill filings.period_of_report from SEC's
                         submissions JSON (accession -> reportDate;
                         ground truth, 1 request per filer).
  enrich_13f_tickers()   stamp holdings.ticker by matching the
                         filing's own issuer name against SEC's
                         company_tickers.json (ticker <-> title for
                         every registrant). Only unambiguous matches,
                         only common-equity class titles (notes/bonds/
                         preferred/warrants stay unstamped so share
                         aggregates stay clean).
  enrich_biotech_tickers() same name match on trials.sponsor_name
                         (INDUSTRY sponsors only) plus a small alias
                         map for subsidiaries filing under a
                         different name than their listed parent.

Run daily by altdata/run-altdata-daily.sh after the scrapers, so new
rows are enriched the same morning they land. Safe to re-run any
time; it only fills empty fields and never overwrites a non-empty
ticker or period.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("altdata_enrich")

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_ALTDATA_BASE = os.environ.get("ALTDATA_BASE_PATH") or os.path.join(
    _REPO_ROOT, "altdata")

# Same UA convention as the scrapers — SEC requires a contactable UA.
_SEC_UA = "quantopsai altdata enrichment mack@mackenziesmith.com"
_SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_SEC_REQUEST_GAP_SEC = 0.5

# company_tickers.json cache — refreshed weekly (new listings are not
# time-critical for quarterly 13F data).
_COMPANY_TICKERS_CACHE = os.path.join(_ALTDATA_BASE, "company_tickers.json")
_COMPANY_TICKERS_MAX_AGE_SEC = 7 * 86400


def _db_path(project: str, db_filename: str) -> str:
    return os.path.join(_ALTDATA_BASE, project, "data", db_filename)


def _fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": _SEC_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Name normalization + SEC registrant index
# ---------------------------------------------------------------------------

_NAME_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "LLC", "LTD",
    "LIMITED", "PLC", "CO", "COMPANY", "HOLDINGS", "HLDGS", "HLDG",
    "GROUP", "GRP", "SA", "AG", "NV", "SE", "GMBH", "SRL", "LP",
    "LLP", "AS", "ASA", "AB", "OYJ", "SPA", "KK", "BHD", "PTE",
    "ADR", "ADS", "NEW",
}


def normalize_company_name(name: Optional[str]) -> str:
    """Canonical form for issuer/sponsor matching: uppercase,
    punctuation stripped, trailing legal-form suffixes removed.
    Empty string when nothing usable remains."""
    if not name:
        return ""
    s = name.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    tokens = s.split()
    while tokens and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def load_sec_name_index(force_refresh: bool = False) -> Dict[str, List[str]]:
    """normalized registrant title -> sorted list of distinct tickers.

    A name mapping to >1 ticker (share classes like GOOGL/GOOG, or a
    genuine collision after normalization) is AMBIGUOUS — callers
    must only stamp on len == 1.
    """
    cache_ok = (
        not force_refresh
        and os.path.exists(_COMPANY_TICKERS_CACHE)
        and (time.time() - os.path.getmtime(_COMPANY_TICKERS_CACHE)
             < _COMPANY_TICKERS_MAX_AGE_SEC)
    )
    data = None
    if cache_ok:
        with open(_COMPANY_TICKERS_CACHE, "rb") as fh:
            raw = fh.read()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            # Corrupt/truncated cache file — self-heal by refetching.
            logger.warning("company_tickers cache unreadable (%s) — "
                           "refetching", exc)
    if data is None:
        raw = _fetch_url(_SEC_TICKER_URL)
        # Parse BEFORE replacing the cache so a bad response can
        # never poison it; a malformed SEC payload raises to the
        # caller (run_all logs and the daily runner reports it).
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"SEC company_tickers.json response is not valid JSON: "
                f"{exc}") from exc
        tmp = _COMPANY_TICKERS_CACHE + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, _COMPANY_TICKERS_CACHE)
    index: Dict[str, set] = {}
    for entry in data.values():
        ticker = (entry.get("ticker") or "").upper().strip()
        norm = normalize_company_name(entry.get("title"))
        if not ticker or not norm:
            continue
        index.setdefault(norm, set()).add(ticker)
    return {k: sorted(v) for k, v in index.items()}


# ---------------------------------------------------------------------------
# 13F: period_of_report backfill (ground truth from SEC submissions)
# ---------------------------------------------------------------------------

def enrich_13f_periods(conn: Optional[sqlite3.Connection] = None) -> int:
    """Backfill filings.period_of_report='' rows from each filer's
    SEC submissions JSON (accession -> reportDate). Returns rows
    updated. One polite request per filer that still has gaps."""
    own = conn is None
    if own:
        conn = sqlite3.connect(_db_path("edgar13f", "edgar13f.db"))
    try:
        ciks = [r[0] for r in conn.execute(
            "SELECT DISTINCT cik FROM filings "
            "WHERE period_of_report IS NULL OR period_of_report = ''")]
        updated = 0
        for cik in ciks:
            url = _SEC_SUBMISSIONS_URL.format(cik=str(cik).zfill(10))
            try:
                data = json.loads(_fetch_url(url))
            except Exception as exc:
                logger.warning("submissions fetch failed for CIK %s: %s",
                               cik, exc)
                continue
            filings_blk = data.get("filings", {})
            acc_to_period: Dict[str, str] = {}

            def _absorb(block: dict, acc_to_period=acc_to_period) -> None:
                # zip() stops at the shorter array, so a drifted/short
                # reportDate array can never misalign accessions.
                for a, p in zip(block.get("accessionNumber") or [],
                                block.get("reportDate") or []):
                    if a and p:
                        acc_to_period.setdefault(a, p)

            _absorb(filings_blk.get("recent", {}))
            # Filings older than the "recent" window live in paginated
            # sidecar files (filings.files[].name) — walk them so old
            # accessions aren't left with '' forever.
            for extra in filings_blk.get("files") or []:
                fname = extra.get("name")
                if not fname:
                    continue
                try:
                    page = json.loads(_fetch_url(
                        f"https://data.sec.gov/submissions/{fname}"))
                except Exception as exc:
                    logger.warning("submissions page %s failed: %s",
                                   fname, exc)
                    continue
                _absorb(page)
                time.sleep(_SEC_REQUEST_GAP_SEC)
            for acc, period in acc_to_period.items():
                if not period:
                    continue
                cur = conn.execute(
                    "UPDATE filings SET period_of_report = ? "
                    "WHERE accession_number = ? AND cik = ? "
                    "AND (period_of_report IS NULL OR period_of_report = '')",
                    (period, acc, cik))
                updated += cur.rowcount
            conn.commit()
            time.sleep(_SEC_REQUEST_GAP_SEC)
        return updated
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 13F: ticker stamping via issuer-name match
# ---------------------------------------------------------------------------

# Class titles that represent common equity (incl. ADRs/ADSs — they
# proxy the listed ticker). Everything else (bonds, notes, preferred,
# warrants, units, rights) stays unstamped so the reader's share
# aggregation is never polluted by debt quantities.
_EQUITY_CLASS_RE = re.compile(
    r"^(COM|CMN|COMMON|ORD|SHS|SH\b|CL\s|CLASS\s|SPONSORED|ADS|ADR|"
    r"DEP(OSITOR|OSITARY)?)", re.IGNORECASE)
_NON_EQUITY_RE = re.compile(
    r"BOND|NOTE|DEB\b|DEBENTURE|PFD|PREF|WARRANT|\bWT\b|UNIT|"
    r"\bRT\b|RIGHT", re.IGNORECASE)


def _is_equity_class(class_title: Optional[str]) -> bool:
    t = (class_title or "").strip()
    if not t:
        return False
    if _NON_EQUITY_RE.search(t):
        return False
    return bool(_EQUITY_CLASS_RE.match(t))


def enrich_13f_tickers(
    conn: Optional[sqlite3.Connection] = None,
    name_index: Optional[Dict[str, List[str]]] = None,
) -> Tuple[int, int]:
    """Stamp holdings.ticker where empty, by unambiguous issuer-name
    match against the SEC registrant index. Returns (rows_updated,
    distinct_cusips_ambiguous_or_unmatched)."""
    own = conn is None
    if own:
        conn = sqlite3.connect(_db_path("edgar13f", "edgar13f.db"))
    try:
        if name_index is None:
            name_index = load_sec_name_index()
        rows = conn.execute(
            "SELECT DISTINCT cusip, company_name, class_title FROM holdings "
            "WHERE (ticker IS NULL OR ticker = '') AND cusip != ''").fetchall()
        cusip_ticker: Dict[str, str] = {}
        skipped = 0
        for cusip, company_name, class_title in rows:
            if not _is_equity_class(class_title):
                continue
            norm = normalize_company_name(company_name)
            candidates = name_index.get(norm) or []
            if len(candidates) == 1:
                prev = cusip_ticker.get(cusip)
                if prev is not None and prev != candidates[0]:
                    # Two filings disagree on the issuer behind one
                    # CUSIP — do not guess.
                    cusip_ticker.pop(cusip, None)
                    skipped += 1
                else:
                    cusip_ticker[cusip] = candidates[0]
            else:
                skipped += 1
        updated = 0
        for cusip, ticker in cusip_ticker.items():
            # A CUSIP identifies one security (class included), so a
            # ticker derived from any equity-class row of this CUSIP
            # applies to every row of it.
            cur = conn.execute(
                "UPDATE holdings SET ticker = ? "
                "WHERE cusip = ? AND (ticker IS NULL OR ticker = '')",
                (ticker, cusip))
            updated += cur.rowcount
        conn.commit()
        return updated, skipped
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Biotech: sponsor-name -> ticker
# ---------------------------------------------------------------------------

# Subsidiaries / research arms that file trials under a name that will
# never match their listed parent's SEC registrant title. Keys are
# normalized (normalize_company_name) PREFIXES of the sponsor name.
_SPONSOR_ALIAS_PREFIXES: List[Tuple[str, str]] = [
    ("SEAGEN", "PFE"),                 # acquired by Pfizer 2023
    ("CELGENE", "BMY"),
    ("KARUNA THERAPEUTICS", "BMY"),
    ("MIRATI THERAPEUTICS", "BMY"),
    ("ALEXION", "AZN"),
    ("JANSSEN", "JNJ"),
    ("GENENTECH", "ROG.SW"),
    ("HOFFMANN LA ROCHE", "ROG.SW"),
    ("ABBOTT", "ABT"),
    ("BOSTON SCIENTIFIC", "BSX"),
    ("ZIMMER BIOMET", "ZBH"),
    ("MERCK SHARP AND DOHME", "MRK"),
    ("IMMUNOVANT", "IMVT"),
    ("PROTHENA BIOSCIENCES", "PRTA"),
]


def sponsor_ticker_for(name: Optional[str],
                       name_index: Dict[str, List[str]]) -> Optional[str]:
    norm = normalize_company_name(name)
    if not norm:
        return None
    for prefix, ticker in _SPONSOR_ALIAS_PREFIXES:
        if norm.startswith(prefix):
            return ticker
    candidates = name_index.get(norm) or []
    if len(candidates) == 1:
        return candidates[0]
    return None


def enrich_biotech_tickers(
    conn: Optional[sqlite3.Connection] = None,
    name_index: Optional[Dict[str, List[str]]] = None,
) -> int:
    """Stamp trials.ticker for INDUSTRY sponsors where empty.
    Universities/NIH/etc. (sponsor_class != INDUSTRY) are correctly
    ticker-less and left alone. Returns rows updated."""
    own = conn is None
    if own:
        conn = sqlite3.connect(_db_path("biotechevents", "biotechevents.db"))
    try:
        if name_index is None:
            name_index = load_sec_name_index()
        sponsors = [r[0] for r in conn.execute(
            "SELECT DISTINCT sponsor_name FROM trials "
            "WHERE (ticker IS NULL OR ticker = '') "
            "AND sponsor_class = 'INDUSTRY'")]
        updated = 0
        for sponsor in sponsors:
            ticker = sponsor_ticker_for(sponsor, name_index)
            if not ticker:
                continue
            cur = conn.execute(
                "UPDATE trials SET ticker = ? "
                "WHERE sponsor_name = ? "
                "AND (ticker IS NULL OR ticker = '')",
                (ticker, sponsor))
            updated += cur.rowcount
        conn.commit()
        return updated
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_all() -> Dict[str, int]:
    stats: Dict[str, int] = {}
    name_index = load_sec_name_index()
    stats["sec_registrant_names"] = len(name_index)

    if os.path.exists(_db_path("edgar13f", "edgar13f.db")):
        stats["13f_periods_backfilled"] = enrich_13f_periods()
        upd, skipped = enrich_13f_tickers(name_index=name_index)
        stats["13f_tickers_stamped"] = upd
        stats["13f_cusips_unmatched"] = skipped
    else:
        logger.warning("edgar13f.db not found — skipping 13F enrichment")

    if os.path.exists(_db_path("biotechevents", "biotechevents.db")):
        stats["biotech_tickers_stamped"] = enrich_biotech_tickers(
            name_index=name_index)
    else:
        logger.warning("biotechevents.db not found — skipping biotech "
                       "enrichment")
    return stats


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    out = run_all()
    for k, v in sorted(out.items()):
        print(f"  {k}: {v}")
