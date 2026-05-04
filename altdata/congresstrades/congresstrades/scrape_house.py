"""House PTR scraper.

Data source: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip

This ZIP file is published by the House Clerk's office and contains:
  - `{YEAR}FD.xml`: an index of every financial disclosure filed that year,
    with filer name, state, party, doc_id, and filing type (e.g. `PTR`).
  - A PDF for each filing, named by doc_id.

Our flow:
  1. Download the yearly ZIP (cached locally to avoid re-download)
  2. Parse the XML index, filter to `filing_type='PTR'`
  3. For each PTR, extract trades from the PDF via pdfplumber
  4. Normalize (ticker, amount ranges, transaction types)
  5. Insert into sqlite (dedup by UNIQUE constraint)

Known pitfalls:
  - PDFs vary in layout per member. Simple table-extraction works for most
    but some use scanned / non-standard layouts — we log and skip those.
  - Asset descriptions are free-text. See normalize.extract_ticker for
    the best-effort mapping.
  - Amounts are disclosed as RANGES, not exact values (e.g. "$1,001 - $15,000").
"""

from __future__ import annotations

import io
import logging
import os
import re
import sqlite3
import time
import zipfile
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from xml.etree import ElementTree as ET

import requests


# Politeness throttle between PDF downloads. 0.4s → ~2.5 req/sec, well
# below any sane rate limit for a public gov records site. Full year
# (~515 PTRs) at this rate takes ~4 min of request time + parse overhead.
_PDF_REQUEST_DELAY_SEC = 0.4


class RateLimitedError(Exception):
    """Raised when the server returns 429 or 403, indicating we should
    stop and back off. Partial results remain in the cache + DB."""

from .normalize import (
    extract_ticker,
    normalize_transaction_type,
    parse_amount_range,
)
from .store import finish_run, insert_trade, start_run

logger = logging.getLogger(__name__)


BASE_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs"
# Individual PTR PDFs live on a separate path, not under BASE_URL. The
# yearly ZIP index is at financial-pdfs/, but each PTR is at ptr-pdfs/.
# Other filing types (annual PFDs, amendments) have their own paths too.
PTR_PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs"

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# House clerk sees a real User-Agent — avoid default python-requests signature
_UA = "CongressTradesScraper/0.1 (public disclosure aggregation; +local research)"


# ---------------------------------------------------------------------------
# ZIP download + caching
# ---------------------------------------------------------------------------

def _zip_path_for_year(year: int) -> Path:
    return CACHE_DIR / f"{year}FD.zip"


def _download_year_zip(year: int, force: bool = False) -> Path:
    """Download the yearly House disclosures ZIP. Caches to `data/cache/`."""
    target = _zip_path_for_year(year)
    if target.exists() and not force and target.stat().st_size > 0:
        logger.info("House %d zip already cached (%.1f MB)",
                    year, target.stat().st_size / 1e6)
        return target

    url = f"{BASE_URL}/{year}FD.zip"
    logger.info("Downloading %s ...", url)
    r = requests.get(url, headers={"User-Agent": _UA}, timeout=60)
    r.raise_for_status()
    target.write_bytes(r.content)
    logger.info("  saved %.1f MB to %s", len(r.content) / 1e6, target)
    return target


# ---------------------------------------------------------------------------
# XML index parsing
# ---------------------------------------------------------------------------

def _parse_index(zip_path: Path, year: int) -> List[Dict[str, Any]]:
    """Return list of filing dicts from the XML index inside the ZIP.

    Each filing has: doc_id, filer name parts, state, filing_type,
    filing_date.
    """
    with zipfile.ZipFile(zip_path) as zf:
        xml_name = f"{year}FD.xml"
        if xml_name not in zf.namelist():
            # Some years use other casings / names
            matches = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not matches:
                raise FileNotFoundError(f"No XML index in {zip_path}")
            xml_name = matches[0]
        with zf.open(xml_name) as fp:
            tree = ET.parse(fp)
    root = tree.getroot()

    filings = []
    for member in root.findall("Member"):
        rec = {child.tag: (child.text or "").strip() for child in member}
        filings.append({
            "doc_id": rec.get("DocID") or "",
            "last_name": rec.get("Last") or "",
            "first_name": rec.get("First") or "",
            "prefix": rec.get("Prefix") or "",
            "suffix": rec.get("Suffix") or "",
            "state_dst": rec.get("StateDst") or "",
            "filing_type": rec.get("FilingType") or "",
            "filing_date": _iso_date(rec.get("FilingDate")),
            "year": rec.get("Year") or str(year),
        })
    return filings


def _iso_date(s: Optional[str]) -> Optional[str]:
    """Normalize common MM/DD/YYYY or YYYY-MM-DD into YYYY-MM-DD."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s  # best-effort fallback


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------

def _pdf_url(doc_id: str, year: int) -> str:
    # PTR PDFs: ptr-pdfs/{year}/{doc_id}.pdf (NOT under BASE_URL)
    return f"{PTR_PDF_URL}/{year}/{doc_id}.pdf"


def _pdf_path(doc_id: str, year: int) -> Path:
    return CACHE_DIR / f"pdf_{year}_{doc_id}.pdf"


def _download_pdf(doc_id: str, year: int) -> Optional[Path]:
    """Fetch a single PTR PDF, caching locally. Politeness throttle baked in.

    Raises RateLimitedError if the server returns 429 (too many requests)
    or 403 (forbidden) — those are signals to abort the whole run rather
    than silently skip hundreds of PDFs.
    """
    local = _pdf_path(doc_id, year)
    if local.exists() and local.stat().st_size > 0:
        return local  # cache hit — no network, no throttle

    url = _pdf_url(doc_id, year)
    time.sleep(_PDF_REQUEST_DELAY_SEC)  # politeness throttle
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
        if r.status_code in (429, 403):
            raise RateLimitedError(
                f"Server returned HTTP {r.status_code} on {doc_id}. "
                f"Stopping run to avoid further rate-limiting. "
                f"Cached PDFs + DB rows are preserved; re-run after "
                f"waiting a few hours will pick up where we left off."
            )
        r.raise_for_status()
        local.write_bytes(r.content)
        return local
    except RateLimitedError:
        raise  # propagate up — caller aborts the run
    except Exception as exc:
        logger.debug("PDF fetch failed for %s: %s", doc_id, exc)
        return None


def _extract_trades_from_pdf(pdf_path: Path) -> List[Dict[str, Any]]:
    """Return list of raw trade records parsed from a PTR PDF.

    Table columns vary by member; we look for the canonical headers
    (Asset, Transaction Type, Date, Notification Date, Amount) and
    extract whatever is in that row. Best-effort — returns [] for
    scanned/unparsable PDFs.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — cannot parse PDFs")
        return []

    trades: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables() or []
                for table in tables:
                    trades.extend(_parse_table(table))
    except Exception as exc:
        logger.debug("PDF parse failed %s: %s", pdf_path.name, exc)
        return []

    return trades


# Common header tokens we're looking for in PTR tables
_HEADER_HINTS = {
    "asset": ["asset", "asset name", "description of asset"],
    "type": ["transaction type", "type", "txn type"],
    "date": ["transaction date", "date"],
    "notification": ["notification date", "date notified"],
    "amount": ["amount", "transaction amount", "value"],
    "owner": ["owner", "filer"],
}


def _parse_table(table: List[List[Optional[str]]]) -> List[Dict[str, Any]]:
    """Parse a single extracted table into trade records."""
    if not table or len(table) < 2:
        return []

    # Find header row: the first row that has at least 2 of our known headers
    header_idx = -1
    col_map: Dict[str, int] = {}
    for i, row in enumerate(table[:3]):  # headers usually in first 3 rows
        if not row:
            continue
        cells = [(c or "").strip().lower() for c in row]
        map_candidate: Dict[str, int] = {}
        for key, hints in _HEADER_HINTS.items():
            for j, cell in enumerate(cells):
                if any(h in cell for h in hints):
                    map_candidate[key] = j
                    break
        if len(map_candidate) >= 2:  # "asset" + "amount" at minimum
            header_idx = i
            col_map = map_candidate
            break

    if header_idx < 0:
        return []

    trades = []
    for row in table[header_idx + 1:]:
        if not row or all(not (c or "").strip() for c in row):
            continue
        cells = [(c or "").strip() for c in row]

        def pick(key: str) -> Optional[str]:
            idx = col_map.get(key)
            if idx is None or idx >= len(cells):
                return None
            return cells[idx] or None

        asset = pick("asset")
        if not asset or len(asset) < 3:
            continue  # skip empty noise rows

        tx_type = pick("type")
        tx_date = pick("date")
        amount = pick("amount")

        # Continuation-row / orphan handling: pdfplumber sometimes splits
        # a single logical trade into two rows when the asset name wraps.
        # A row with asset text but NONE of the transaction fields
        # (type/date/amount) is either:
        #   - a continuation of the previous trade → merge its asset text
        #   - an orphan (no prior trade to merge into) → drop as noise
        # Either way, it should NOT become its own trade row with empty
        # Type/Date/Amount fields.
        if not tx_type and not tx_date and not amount:
            if trades:
                prev = trades[-1]
                prev["asset_description"] = (
                    prev["asset_description"] + " " + asset
                ).strip()
            # else: orphan — just skip
            continue

        trades.append({
            "asset_description": asset,
            "transaction_type_raw": tx_type,
            "transaction_date_raw": tx_date,
            "notification_date_raw": pick("notification"),
            "amount_raw": amount,
            "owner_raw": pick("owner"),
        })
    return trades


# ---------------------------------------------------------------------------
# Top-level scrape orchestrator
# ---------------------------------------------------------------------------

def scrape_year(
    year: int,
    db_conn: sqlite3.Connection,
    max_filings: Optional[int] = None,
    force_zip_refresh: bool = False,
    log_progress_every: int = 25,
) -> Dict[str, int]:
    """Scrape all PTR filings for a given year. Returns stats dict.

    `max_filings` caps processing (useful for first-run smoke tests —
    set to 5-10 for a quick validation pass).
    """
    run_id = start_run(db_conn, "house")

    stats = {"filings_seen": 0, "filings_ok": 0, "trades_inserted": 0,
             "pdfs_failed": 0, "dupe_rows": 0}

    try:
        zip_path = _download_year_zip(year, force=force_zip_refresh)
        filings = _parse_index(zip_path, year)
        ptrs = [f for f in filings if f["filing_type"] == "P"]
        logger.info(
            "House %d: %d total filings, %d PTRs", year, len(filings), len(ptrs)
        )

        if max_filings:
            ptrs = ptrs[:max_filings]

        for i, f in enumerate(ptrs, 1):
            stats["filings_seen"] += 1
            pdf_path = _download_pdf(f["doc_id"], year)
            if not pdf_path:
                stats["pdfs_failed"] += 1
                continue

            trades = _extract_trades_from_pdf(pdf_path)
            if not trades:
                continue

            member_name = " ".join(
                p for p in (f["first_name"], f["last_name"]) if p
            )
            stats["filings_ok"] += 1

            for t in trades:
                amt_low, amt_high = parse_amount_range(t.get("amount_raw"))
                tx_type = normalize_transaction_type(t.get("transaction_type_raw"))
                ticker = extract_ticker(t["asset_description"])

                row = {
                    "chamber": "house",
                    "member_name": member_name,
                    "member_state": f["state_dst"],
                    "member_party": None,  # XML doesn't include party
                    "filing_doc_id": f["doc_id"],
                    "filing_date": f["filing_date"],
                    "transaction_date": _iso_date(t.get("transaction_date_raw")),
                    "ticker": ticker,
                    "asset_description": t["asset_description"][:500],
                    "transaction_type": tx_type,
                    "amount_range": t.get("amount_raw"),
                    "amount_low": amt_low,
                    "amount_high": amt_high,
                    "owner": (t.get("owner_raw") or "").upper()[:30] or None,
                    "source_url": _pdf_url(f["doc_id"], year),
                }
                if insert_trade(db_conn, row):
                    stats["trades_inserted"] += 1
                else:
                    stats["dupe_rows"] += 1

            # Commit per filing so a mid-run failure doesn't lose the
            # prior filings' inserted trades. Cheap — we're doing at most
            # 500-2000 commits across a full year, each on a tiny batch.
            db_conn.commit()

            if i % log_progress_every == 0:
                logger.info(
                    "  progress: %d/%d filings processed, %d trades inserted, %d dupes",
                    i, len(ptrs), stats["trades_inserted"], stats["dupe_rows"],
                )

        finish_run(
            db_conn, run_id,
            status="ok",
            rows_inserted=stats["trades_inserted"],
            rows_seen=stats["filings_seen"],
        )
    except Exception as exc:
        logger.exception("House scrape failed for year %s", year)
        finish_run(db_conn, run_id, status="failed",
                   rows_inserted=stats["trades_inserted"],
                   rows_seen=stats["filings_seen"],
                   error=str(exc))
        raise

    return stats
