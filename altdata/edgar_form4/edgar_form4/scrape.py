"""Form 4 scraper — SEC EDGAR.

Flow per company (CIK):
  1. GET EDGAR's filer submissions JSON to list recent Form 4 filings
  2. For each NEW filing: build the primary-doc URL, fetch the XML
  3. Persist raw XML to raw_filings BEFORE parsing
  4. Parse XML → reporting_owners + non_derivative_transactions
  5. Insert filing row + per-transaction rows; mark raw as parsed

Rate limits: SEC publishes 10 req/sec; we use 1 req/sec for
politeness (matches the existing edgar13f + congresstrades pattern).

Universe selection: we scrape the companies the QuantOpsAI trade
pipeline actually cares about. Two sources combined:
  - Active profile universes (`segments.py`)
  - Plus any symbol that's appeared as an open position or recent
    candidate (best-effort — fall back to a hard-coded liquid
    basket if those lookups aren't available)

Ticker → CIK is the only "lookup" step. SEC publishes the full
mapping at `https://www.sec.gov/files/company_tickers.json` (free,
no auth, ~10k entries). We bootstrap the local `companies` table
from this file on first run + refresh weekly.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional
from xml.etree import ElementTree as ET

import requests

from .normalize import PARSER_VERSION, parse_form4_xml
from .store import (
    cik_for_ticker,
    finish_run,
    insert_filing,
    insert_raw_filing,
    insert_txn,
    mark_raw_parsed,
    start_run,
    update_last_filings_check,
    upsert_company,
)

logger = logging.getLogger(__name__)


# SEC requires a User-Agent with contactable email per:
# https://www.sec.gov/os/accessing-edgar-data
USER_AGENT = "edgar_form4 Research Tool mack@mackenziesmith.com"
BASE = "https://www.sec.gov"
DATA_BASE = "https://data.sec.gov"
TICKERS_URL = f"{BASE}/files/company_tickers.json"
REQUEST_DELAY_SEC = 1.0


class RateLimitedError(Exception):
    """Raised on HTTP 429/403 — stop the run, preserve progress."""


class EdgarSession:
    """Thin wrapper around requests.Session with SEC-appropriate headers."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        })

    def get(self, url: str, **kwargs) -> requests.Response:
        time.sleep(REQUEST_DELAY_SEC)
        # SEC's two hosts need different Host headers, urllib3 sets
        # them automatically from the URL — don't override.
        r = self.session.get(url, timeout=30, **kwargs)
        if r.status_code in (429, 403):
            raise RateLimitedError(
                f"EDGAR returned HTTP {r.status_code} on {url}. "
                f"Re-run after waiting — cached rows are preserved."
            )
        r.raise_for_status()
        return r


# ── Ticker / CIK bootstrap ───────────────────────────────────────

def refresh_ticker_cik_map(session: EdgarSession, conn) -> int:
    """Pull the SEC's published ticker → CIK map and seed/refresh
    the local `companies` table. Returns count of rows upserted."""
    r = session.get(TICKERS_URL)
    data = r.json()  # {"0": {cik_str: 320193, ticker: "AAPL", title: "Apple Inc"}, ...}
    n = 0
    for _, entry in (data.items() if isinstance(data, dict) else []):
        if not isinstance(entry, dict):
            continue
        cik = str(entry.get("cik_str", "")).zfill(10)
        ticker = entry.get("ticker")
        name = entry.get("title", "")
        if not cik or not name:
            continue
        upsert_company(conn, cik=cik, ticker=ticker, name=name)
        n += 1
    return n


# ── Filer filings list (via data.sec.gov submissions JSON) ────────

def list_form4_filings_for_cik(
    session: EdgarSession, cik: str, max_age_days: int = 90,
) -> List[Dict[str, Any]]:
    """Return list of recent Form 4 filings for a CIK.

    Newest first. `max_age_days` filters by `filingDate` so we don't
    re-process ancient filings on every cycle."""
    url = f"{DATA_BASE}/submissions/CIK{cik}.json"
    r = session.get(url)
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    acc_nums = recent.get("accessionNumber", [])
    filed = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])
    primary_doc_descs = recent.get("primaryDocDescription", [])
    period_reports = recent.get("reportDate", [])

    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()

    def _safe(arr, i):
        return arr[i] if i < len(arr) else ""

    out: List[Dict[str, Any]] = []
    for i, form in enumerate(forms):
        if form != "4":
            continue
        filed_date = _safe(filed, i)
        if filed_date < cutoff:
            continue
        out.append({
            "accession_number": _safe(acc_nums, i),
            "filed_date": filed_date,
            "primary_document": _safe(primary_docs, i),
            "primary_doc_description": _safe(primary_doc_descs, i),
            "period_of_report": _safe(period_reports, i),
        })
    return out


# ── Single-filing fetch + persist ─────────────────────────────────

def _build_xml_url(cik: str, accession_number: str,
                    primary_doc: str) -> str:
    """Form 4 primary XML lives at:
      https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dashes}/{primary_doc}

    SEC's submissions JSON sometimes returns the XSL-styled rendering
    path (e.g. `xslF345X06/doc4.xml`) instead of the raw structured
    XML — the actual XML lives one level up at `doc4.xml`. Strip any
    leading `xsl*/` directory so we always fetch the structured form.
    """
    acc_no_dashes = accession_number.replace("-", "")
    cik_int = int(cik)
    # Strip XSL-renderer prefix if present.
    if "/" in primary_doc:
        head, tail = primary_doc.rsplit("/", 1)
        if head.lower().startswith("xsl"):
            primary_doc = tail
    return f"{BASE}/Archives/edgar/data/{cik_int}/{acc_no_dashes}/{primary_doc}"


def _filing_already_stored(conn, accession_number: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM form4_filings WHERE accession_number = ?",
        (accession_number,),
    ).fetchone()
    return row is not None


def fetch_and_store_filing(
    session: EdgarSession, conn, cik: str, filing: Dict[str, Any],
) -> int:
    """Fetch one Form 4 filing's XML, persist raw, parse, persist
    structured rows. Returns count of transactions inserted (0 if
    already stored or empty)."""
    accession = filing.get("accession_number", "")
    if not accession:
        return 0
    if _filing_already_stored(conn, accession):
        return 0

    primary_doc = filing.get("primary_document", "")
    if not primary_doc or not primary_doc.lower().endswith(".xml"):
        # Form 4 should always have an XML primary doc; older
        # filings sometimes don't. Skip those — we'd need a
        # separate parser for the HTML form.
        return 0

    xml_url = _build_xml_url(cik, accession, primary_doc)
    try:
        r = session.get(xml_url)
        xml_text = r.text
    except RateLimitedError:
        raise
    except Exception as exc:
        logger.debug(
            "Form 4 XML fetch failed for %s: %s: %s",
            accession, type(exc).__name__, exc,
        )
        return 0

    # Persist raw BEFORE parsing — if parser changes later, we can
    # re-process without re-scraping.
    insert_raw_filing(
        conn, accession_number=accession, cik=cik,
        source_url=xml_url, payload_text=xml_text,
        filed_on=filing.get("filed_date"),
    )

    parsed = parse_form4_xml(xml_text)
    if parsed is None:
        mark_raw_parsed(conn, accession, error="xml parse failed")
        return 0

    insert_filing(
        conn,
        accession_number=accession,
        cik=cik,
        filed_date=filing.get("filed_date", ""),
        period_of_report=filing.get("period_of_report") or None,
        primary_document=primary_doc,
        parser_version=PARSER_VERSION,
    )

    n_txns = 0
    for t in parsed.get("non_derivative_transactions", []):
        if not t.get("transaction_date") or not t.get("txn_code"):
            continue
        ok = insert_txn(
            conn,
            accession_number=accession,
            cik=cik,
            rpt_owner_name=t.get("rpt_owner_name", ""),
            transaction_date=t["transaction_date"],
            txn_code=t["txn_code"],
            shares=t.get("shares"),
            price_per_share=t.get("price_per_share"),
            value_usd=t.get("value_usd"),
            is_officer=t.get("is_officer", False),
            is_director=t.get("is_director", False),
            is_ten_percent=t.get("is_ten_percent", False),
            officer_title=t.get("officer_title"),
            acquired_disposed=t.get("acquired_disposed"),
            direct_indirect=t.get("direct_indirect"),
            parser_version=PARSER_VERSION,
        )
        if ok:
            n_txns += 1
    mark_raw_parsed(conn, accession)
    return n_txns


# ── Per-symbol scrape (consumed by daily refresh) ─────────────────

def scrape_company(
    session: EdgarSession, conn, ticker: str,
    max_age_days: int = 90,
) -> Dict[str, Any]:
    """Refresh Form 4 data for a single ticker. Returns counts."""
    result = {"ticker": ticker, "filings_seen": 0, "txns_inserted": 0,
              "cik": None, "error": None}
    cik = cik_for_ticker(conn, ticker)
    if not cik:
        result["error"] = "no CIK mapping (run refresh-tickers first)"
        return result
    result["cik"] = cik
    try:
        filings = list_form4_filings_for_cik(
            session, cik, max_age_days=max_age_days,
        )
    except RateLimitedError:
        raise
    except Exception as exc:
        result["error"] = f"filings list: {type(exc).__name__}: {exc}"
        return result
    result["filings_seen"] = len(filings)
    for filing in filings:
        try:
            n = fetch_and_store_filing(session, conn, cik, filing)
            result["txns_inserted"] += n
        except RateLimitedError:
            raise
        except Exception as exc:
            logger.debug(
                "Form 4 filing %s failed: %s: %s",
                filing.get("accession_number"),
                type(exc).__name__, exc,
            )
    update_last_filings_check(conn, cik)
    return result


def scrape_universe(
    session: EdgarSession, conn, tickers: Iterable[str],
    max_age_days: int = 90,
) -> List[Dict[str, Any]]:
    """Refresh Form 4 data for a set of tickers."""
    out = []
    for ticker in tickers:
        try:
            out.append(scrape_company(
                session, conn, ticker, max_age_days=max_age_days,
            ))
        except RateLimitedError as exc:
            out.append({"ticker": ticker, "error": str(exc)})
            logger.warning("Rate-limited; aborting universe scrape")
            break
    return out
