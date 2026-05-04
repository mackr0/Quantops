"""Senate PTR scraper.

Flow:
    1. GET  /search/home/                 → CSRF + agreement form
    2. POST /search/home/                  → accept terms, get session
    3. GET  /search/                       → search page CSRF
    4. POST /search/report/data/           → paginated DataTables JSON
    5. For each hit: GET /search/view/ptr/{uuid}/
         - Electronic filings: HTML table (clean — ticker, asset, type, amount)
         - Paper filings: redirects to /search/view/paper/... with PDF embed

Future-proof design:
    - Every fetched document is stored in `raw_filings` before parsing
      so layout changes only require updating the parser, not re-scraping
    - Parsed rows are tagged `parser_version='senate-html-v1'` so we can
      re-parse historical data after parser improvements
    - The parser itself is a pure function from HTML-string → list[dict]
      — easy to test, easy to replace

Rate limiting:
    2 seconds between requests. Senate is pickier than House and killed
    previous open-source scrapers when they got too aggressive.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from .normalize import (
    extract_ticker,
    normalize_transaction_type,
    parse_amount_range,
)
from .store import (
    finish_run,
    insert_raw_filing,
    insert_trade,
    mark_raw_filing_parsed,
    start_run,
)

logger = logging.getLogger(__name__)


BASE = "https://efdsearch.senate.gov"
USER_AGENT = "CongressTradesScraper/0.1 (public disclosure aggregation; local research)"
PARSER_VERSION = "senate-html-v1"

# Senate is pickier than House; 2s between requests keeps us well below
# observed throttle thresholds while still finishing a year in ~10-15 min.
REQUEST_DELAY_SEC = 2.0


class RateLimitedError(Exception):
    """Raised on HTTP 429 / 403 — stop the run, preserve progress."""


# ---------------------------------------------------------------------------
# Session setup
# ---------------------------------------------------------------------------

class SenateSession:
    """Holds the requests.Session + CSRF token so API calls work.

    Separate from single functions because the session cookies + CSRF
    must persist across multiple requests.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.search_csrf: Optional[str] = None

    def initialize(self):
        """Run the agreement + CSRF handshake. Idempotent."""
        # 1. Agreement form
        self._delay()
        r = self.session.get(f"{BASE}/search/home/", timeout=30)
        r.raise_for_status()
        token = self._extract_csrf(r.text)
        if not token:
            raise RuntimeError(
                "Could not find csrfmiddlewaretoken on Senate home page — "
                "layout may have changed."
            )
        # 2. POST agreement
        self._delay()
        r = self.session.post(
            f"{BASE}/search/home/",
            data={
                "csrfmiddlewaretoken": token,
                "prohibition_agreement": "1",
            },
            headers={"Referer": f"{BASE}/search/home/"},
            timeout=30,
            allow_redirects=True,
        )
        r.raise_for_status()
        # 3. GET search page for its CSRF (different from home's)
        self._delay()
        r = self.session.get(f"{BASE}/search/", timeout=30)
        r.raise_for_status()
        self.search_csrf = self._extract_csrf(r.text)
        if not self.search_csrf:
            raise RuntimeError(
                "Could not find csrfmiddlewaretoken on Senate search page — "
                "session setup failed."
            )
        logger.info("Senate session initialized")

    def search_ptrs(
        self,
        start_date: str,  # 'MM/DD/YYYY'
        end_date: str,
        filer_types: Tuple[int, ...] = (1, 5),  # Senator, Former Senator
        offset: int = 0,
        length: int = 100,
    ) -> Dict[str, Any]:
        """Query the DataTables endpoint for PTR filings."""
        if not self.search_csrf:
            raise RuntimeError("Call initialize() before search_ptrs()")
        self._delay()
        r = self.session.post(
            f"{BASE}/search/report/data/",
            data={
                "csrfmiddlewaretoken": self.search_csrf,
                "report_types": json.dumps([11]),  # 11 = PTR
                "filer_types": json.dumps(list(filer_types)),
                "submitted_start_date": f"{start_date} 00:00:00",
                "submitted_end_date": f"{end_date} 23:59:59",
                "start": offset,
                "length": length,
            },
            headers={
                "X-CSRFToken": self.search_csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/search/",
            },
            timeout=30,
        )
        if r.status_code in (429, 403):
            raise RateLimitedError(
                f"Senate search returned HTTP {r.status_code}. "
                f"Re-run after waiting — cached rows are preserved."
            )
        r.raise_for_status()
        return r.json()

    def fetch_filing(self, url: str) -> requests.Response:
        """Fetch a filing URL. Relative OK — prefixed with BASE automatically."""
        if url.startswith("/"):
            url = BASE + url
        self._delay()
        r = self.session.get(url, timeout=30)
        if r.status_code in (429, 403):
            raise RateLimitedError(
                f"Senate filing fetch returned HTTP {r.status_code} on {url}."
            )
        r.raise_for_status()
        return r

    @staticmethod
    def _extract_csrf(html: str) -> Optional[str]:
        m = re.search(r'csrfmiddlewaretoken"\s*value="([^"]+)', html)
        return m.group(1) if m else None

    @staticmethod
    def _delay():
        time.sleep(REQUEST_DELAY_SEC)


# ---------------------------------------------------------------------------
# Search-results parser (DataTables rows → filing dicts)
# ---------------------------------------------------------------------------

def parse_search_results(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract filing summaries from a DataTables response row.

    Each row is: [first, last, filer_full_name, link_html, date_filed].
    We extract the PTR UUID from the link HTML. Paper filings have a
    different link format we also handle.
    """
    out = []
    for row in data.get("data", []):
        if len(row) < 5:
            continue
        first, last, filer_full, link_html, date_filed = row[:5]
        # Link is HTML like: <a href="/search/view/ptr/UUID/" target="_blank">...
        m = re.search(r'href="([^"]+)"', link_html or "")
        if not m:
            continue
        url = m.group(1)
        # UUID from /search/view/{ptr|paper}/UUID/
        uuid_match = re.search(r"/view/(?:ptr|paper)/([a-f0-9-]+)", url)
        filing_type = "electronic"
        if "/view/paper/" in url:
            filing_type = "paper"
        doc_id = uuid_match.group(1) if uuid_match else url
        out.append({
            "first_name": (first or "").strip(),
            "last_name": (last or "").strip(),
            "filer_name": (filer_full or "").strip(),
            "url": url,
            "doc_id": doc_id,
            "filing_type": filing_type,
            "filing_date": _iso_date(date_filed),
        })
    return out


def _iso_date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


# ---------------------------------------------------------------------------
# Electronic-PTR HTML parser — the common case
# ---------------------------------------------------------------------------

# Columns we expect in the main transactions table (order-independent lookup)
_HEADER_MAP = {
    "transaction date": "txn_date",
    "owner": "owner",
    "ticker": "ticker_raw",
    "asset name": "asset",
    "asset type": "asset_type",
    "type": "tx_type",
    "amount": "amount",
    "comment": "comment",
}


def parse_electronic_ptr(html: str) -> Dict[str, Any]:
    """Parse a Senate electronic PTR HTML into (header, trades).

    Returns dict with:
      member: extracted from page header
      trades: list[dict] — one row per transaction
    """
    soup = BeautifulSoup(html, "html.parser")

    # Senator name from the page header
    header = soup.find("h2") or soup.find("h1")
    raw_name = header.get_text(" ", strip=True) if header else ""
    member = _clean_member_name(raw_name)

    # Find the main transactions table — the one with a 'Transaction Date' header
    target_table = None
    for tbl in soup.find_all("table"):
        thead = tbl.find("thead")
        if not thead:
            continue
        hdr_text = thead.get_text(" ", strip=True).lower()
        if "transaction date" in hdr_text and ("ticker" in hdr_text or "asset" in hdr_text):
            target_table = tbl
            break

    trades: List[Dict[str, Any]] = []
    if target_table is None:
        return {"member": member, "trades": trades}

    # Map column index → canonical key using the header row
    head_cells = [
        c.get_text(" ", strip=True).lower()
        for c in target_table.find("thead").find_all(["th", "td"])
    ]
    col_map: Dict[int, str] = {}
    for i, h in enumerate(head_cells):
        for pattern, key in _HEADER_MAP.items():
            if pattern in h:
                col_map[i] = key
                break

    body = target_table.find("tbody") or target_table
    for tr in body.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cells or all(not c for c in cells):
            continue
        row: Dict[str, Any] = {}
        for idx, val in enumerate(cells):
            key = col_map.get(idx)
            if key:
                row[key] = val
        # Must have at least an asset name OR ticker, plus one of
        # (tx_type, amount) to count as a trade row
        if not (row.get("asset") or row.get("ticker_raw")):
            continue
        if not (row.get("tx_type") or row.get("amount")):
            continue
        trades.append(row)
    return {"member": member, "trades": trades}


def _clean_member_name(raw: str) -> str:
    """'The Honorable James Banks (Banks, James E.)' → 'James Banks'.

    Tolerates multi-space variants in the title (`The  Honorable` with
    extra whitespace from HTML rendering).
    """
    raw = raw.strip()
    # Collapse whitespace FIRST so the honorific regex doesn't miss due to
    # multi-space HTML artifacts.
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"^The Honorable\s+", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*\([^)]*\)\s*$", "", raw)  # drop trailing "(Last, First M.)"
    return raw.strip()


# ---------------------------------------------------------------------------
# Top-level scrape orchestrator
# ---------------------------------------------------------------------------

def scrape_year(
    year: int,
    db_conn: sqlite3.Connection,
    max_filings: Optional[int] = None,
    log_progress_every: int = 20,
) -> Dict[str, int]:
    """Pull all Senate PTRs filed in `year`. Returns stats dict.

    Searches are chunked into monthly windows — Senate's DataTables caps
    return size and date-range can exceed it on busy months.
    """
    run_id = start_run(db_conn, "senate")
    stats = {"filings_seen": 0, "filings_ok": 0, "trades_inserted": 0,
             "dupe_rows": 0, "parse_errors": 0}

    try:
        session = SenateSession()
        session.initialize()

        # Collect all filings first, then fetch each
        all_filings: List[Dict[str, Any]] = []
        for month in range(1, 13):
            last_day = _last_day_of_month(year, month)
            start_str = f"{month:02d}/01/{year}"
            end_str = f"{month:02d}/{last_day:02d}/{year}"
            offset = 0
            while True:
                data = session.search_ptrs(start_str, end_str, offset=offset, length=100)
                batch = parse_search_results(data)
                all_filings.extend(batch)
                if len(batch) < 100:
                    break
                offset += 100
        logger.info("Senate %d: found %d PTR filings across 12 months", year, len(all_filings))

        # Dedup by doc_id in case a filing shows up in multiple months
        seen_ids = set()
        unique_filings = []
        for f in all_filings:
            if f["doc_id"] in seen_ids:
                continue
            seen_ids.add(f["doc_id"])
            unique_filings.append(f)

        if max_filings:
            unique_filings = unique_filings[:max_filings]

        for i, f in enumerate(unique_filings, 1):
            stats["filings_seen"] += 1
            try:
                resp = session.fetch_filing(f["url"])
            except RateLimitedError:
                raise
            except Exception as exc:
                logger.warning("Senate filing fetch failed %s: %s", f["doc_id"], exc)
                continue

            content_type = "html" if "html" in resp.headers.get("Content-Type", "") else "pdf"
            # Persist raw before parsing
            member_name = f'{f["first_name"]} {f["last_name"]}'.strip()
            insert_raw_filing(
                db_conn, chamber="senate", filing_doc_id=f["doc_id"],
                content_type=content_type,
                payload=resp.content if content_type == "pdf" else resp.text,
                source_url=f"{BASE}{f['url']}" if f['url'].startswith('/') else f['url'],
                member_name=member_name,
                filing_type=f["filing_type"],
                filed_on=f["filing_date"],
            )
            db_conn.commit()  # persist raw immediately

            if f["filing_type"] == "paper" or content_type == "pdf":
                # Paper filings use scanned PDFs — skip parsing for v1.
                # The raw PDF is stored so a future parser can read it.
                mark_raw_filing_parsed(db_conn, "senate", f["doc_id"],
                                       status="unparsed",
                                       error="paper filing — scanned PDF")
                db_conn.commit()
                continue

            try:
                parsed = parse_electronic_ptr(resp.text)
            except Exception as exc:
                stats["parse_errors"] += 1
                logger.debug("Senate parse failed %s: %s", f["doc_id"], exc)
                mark_raw_filing_parsed(db_conn, "senate", f["doc_id"],
                                       status="parse_error", error=str(exc))
                db_conn.commit()
                continue

            if not parsed["trades"]:
                mark_raw_filing_parsed(db_conn, "senate", f["doc_id"], status="parsed",
                                       error="no trades extracted")
                db_conn.commit()
                continue

            stats["filings_ok"] += 1
            for t in parsed["trades"]:
                amt_low, amt_high = parse_amount_range(t.get("amount"))
                tx_type = normalize_transaction_type(t.get("tx_type"))
                # Senate surfaces the ticker directly — only fall back to
                # extract_ticker() if the explicit field is empty or obviously bad
                ticker = (t.get("ticker_raw") or "").strip().upper() or None
                if ticker in (None, "--", "N/A", "", "NONE"):
                    ticker = extract_ticker(t.get("asset", "") or "")

                row = {
                    "chamber": "senate",
                    "member_name": parsed["member"] or member_name,
                    "member_state": None,
                    "member_party": None,
                    "filing_doc_id": f["doc_id"],
                    "filing_date": f["filing_date"],
                    "transaction_date": _iso_date(t.get("txn_date")),
                    "ticker": ticker,
                    "asset_description": (t.get("asset") or "")[:500],
                    "asset_type": (t.get("asset_type") or "")[:50] or None,
                    "transaction_type": tx_type,
                    "amount_range": t.get("amount"),
                    "amount_low": amt_low,
                    "amount_high": amt_high,
                    "owner": (t.get("owner") or "")[:30] or None,
                    "source_url": f"{BASE}{f['url']}" if f['url'].startswith('/') else f['url'],
                    "parser_version": PARSER_VERSION,
                }
                if insert_trade(db_conn, row):
                    stats["trades_inserted"] += 1
                else:
                    stats["dupe_rows"] += 1

            mark_raw_filing_parsed(db_conn, "senate", f["doc_id"], status="parsed")
            db_conn.commit()

            if i % log_progress_every == 0:
                logger.info(
                    "  progress: %d/%d filings processed, %d trades inserted, %d dupes",
                    i, len(unique_filings),
                    stats["trades_inserted"], stats["dupe_rows"],
                )

        finish_run(db_conn, run_id, status="ok",
                   rows_inserted=stats["trades_inserted"],
                   rows_seen=stats["filings_seen"])
    except Exception as exc:
        logger.exception("Senate scrape failed for year %s", year)
        finish_run(db_conn, run_id, status="failed",
                   rows_inserted=stats["trades_inserted"],
                   rows_seen=stats["filings_seen"],
                   error=str(exc))
        raise

    return stats


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


# ---------------------------------------------------------------------------
# Legacy entry point — kept so the CLI's stub call path still works
# ---------------------------------------------------------------------------

def scrape_recent(
    db_conn: sqlite3.Connection,
    days_back: int = 30,
    max_filings: int = 50,
) -> Dict[str, int]:
    """Convenience wrapper that scrapes the last `days_back` days."""
    from datetime import datetime as _dt
    year = _dt.utcnow().year
    return scrape_year(year, db_conn, max_filings=max_filings)
