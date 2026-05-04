"""13F-HR scraper — SEC EDGAR.

Flow per filer (CIK):
  1. GET EDGAR's filer filings JSON to list recent 13F-HR filings
  2. For each filing: fetch the filing index, find the informationTable XML
  3. Parse XML → holdings rows
  4. Store raw XML to raw_filings BEFORE parsing (future-proofing)
  5. Insert parsed rows with parser_version tag

Rate limits: SEC publishes 10 req/sec, we use 1 req/sec (matching
the politeness pattern from congresstrades). Sequential — no threads.

Filers we care about (starter set): top hedge funds + sovereign funds +
state pension systems. Hand-curated in `FILERS` dict below; future work
could discover dynamically from EDGAR's full-text search.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests

from .normalize import (
    cusip_to_ticker,
    normalize_cusip,
    normalize_discretion,
    normalize_put_call,
    parse_shares,
    parse_value_dollars,
)
from .store import (
    finish_run,
    insert_filing,
    insert_holding,
    insert_raw_filing,
    mark_raw_parsed,
    start_run,
    upsert_filer,
)

logger = logging.getLogger(__name__)


# SEC requires a User-Agent with contactable email per their docs:
# https://www.sec.gov/os/accessing-edgar-data
USER_AGENT = "edgar13f Research Tool mack@mackenziesmith.com"
BASE = "https://www.sec.gov"
DATA_BASE = "https://data.sec.gov"

# Politeness: 1 req/sec keeps us well below SEC's 10 req/sec limit.
# Matches congresstrades' House scraper pattern.
REQUEST_DELAY_SEC = 1.0

PARSER_VERSION = "13f-xml-v1"


class RateLimitedError(Exception):
    """Raised on HTTP 429/403 — stop the run, preserve progress."""


# ---------------------------------------------------------------------------
# Starter filer list — can be extended over time
# ---------------------------------------------------------------------------

# CIK → (display_name, filer_type). CIKs are 10-digit zero-padded.
FILERS: Dict[str, Tuple[str, str]] = {
    # Buffett
    "0001067983": ("Berkshire Hathaway Inc",                "conglomerate"),
    # Top hedge funds
    "0001336528": ("Renaissance Technologies LLC",          "hedge_fund"),
    "0001350694": ("Bridgewater Associates LP",             "hedge_fund"),
    "0001423053": ("Citadel Advisors LLC",                  "hedge_fund"),
    "0001061768": ("Millennium Management LLC",             "hedge_fund"),
    "0001656456": ("Two Sigma Advisers LP",                 "hedge_fund"),
    "0001167483": ("AQR Capital Management LLC",            "hedge_fund"),
    "0001037389": ("D. E. Shaw & Co., L.P.",                "hedge_fund"),
    "0001103804": ("Pershing Square Capital Management LP", "hedge_fund"),
    "0001582202": ("Tiger Global Management LLC",           "hedge_fund"),
    # Index-fund giants (useful for relative positioning)
    "0000102909": ("Vanguard Group Inc",                    "bank_am"),
    "0001364742": ("BlackRock Inc",                         "bank_am"),
    # Sovereign / pension
    "0001537834": ("Norges Bank",                           "sovereign"),
    "0000919079": ("California Public Employees Retirement System", "pension"),
    # Active managers
    "0001029160": ("T. Rowe Price Associates Inc",          "bank_am"),
    "0000072971": ("Wells Fargo & Company / MN",            "bank_am"),
}


# ---------------------------------------------------------------------------
# Session + HTTP helpers
# ---------------------------------------------------------------------------

class EdgarSession:
    """Thin wrapper around requests.Session with SEC-appropriate headers."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        })

    def get(self, url: str, **kwargs) -> requests.Response:
        time.sleep(REQUEST_DELAY_SEC)
        r = self.session.get(url, timeout=30, **kwargs)
        if r.status_code in (429, 403):
            raise RateLimitedError(
                f"EDGAR returned HTTP {r.status_code} on {url}. Re-run "
                f"after waiting — cached rows are preserved."
            )
        r.raise_for_status()
        return r


# ---------------------------------------------------------------------------
# Filer filings list (via data.sec.gov submissions JSON)
# ---------------------------------------------------------------------------

def list_13f_filings_for_filer(
    session: EdgarSession, cik: str,
) -> List[Dict[str, Any]]:
    """Return list of recent 13F-HR filings for a filer, newest first.

    Uses SEC's structured JSON endpoint which is stable and fast.
    """
    url = f"{DATA_BASE}/submissions/CIK{cik}.json"
    r = session.get(url)
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    acc_nums = recent.get("accessionNumber", [])
    periods = recent.get("periodOfReport", [])
    filed = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    # Defensive: parallel arrays are usually equal-length but schema
    # drift has been observed — tolerate short arrays by falling back
    # on "" for the missing field.
    def _safe(arr, i):
        return arr[i] if i < len(arr) else ""

    out: List[Dict[str, Any]] = []
    for i, form in enumerate(forms):
        if form != "13F-HR":
            continue
        out.append({
            "accession_number": _safe(acc_nums, i),
            "period_of_report": _safe(periods, i),
            "filed_date": _safe(filed, i),
            "primary_document": _safe(primary_docs, i),
        })
    return out


# ---------------------------------------------------------------------------
# Information-table XML parsing
# ---------------------------------------------------------------------------

def build_xml_url(cik: str, accession_number: str, primary_doc: str = "") -> str:
    """Build the EDGAR URL for a filing's primary document.

    Accession numbers come with dashes (0001067983-25-000007); EDGAR
    uses the dash-free form in the URL path.
    """
    acc_no_dashes = accession_number.replace("-", "")
    cik_int = int(cik)
    if primary_doc:
        return f"{BASE}/Archives/edgar/data/{cik_int}/{acc_no_dashes}/{primary_doc}"
    return f"{BASE}/Archives/edgar/data/{cik_int}/{acc_no_dashes}/"


def find_information_table_url(
    session: EdgarSession, cik: str, accession_number: str,
) -> Optional[str]:
    """A 13F-HR filing has multiple attached docs; we want
    `informationtable.xml` (or similarly named). Fall back to parsing
    the filing index if the canonical name differs.
    """
    # Try the standard filename first — fast path
    standard = build_xml_url(cik, accession_number, "infotable.xml")
    r = session.session.head(standard)
    if r.status_code == 200:
        return standard

    # Fall back: fetch the filing index and find the xml link
    index_url = build_xml_url(cik, accession_number, "")
    # SEC uses different Host header for Archives subpath
    session.session.headers["Host"] = "www.sec.gov"
    try:
        idx_r = session.get(index_url + "index.json")
        items = idx_r.json().get("directory", {}).get("item", [])
        # Prefer a file ending in '.xml' with 'informationtable' or 'infotable'
        candidates = [
            i for i in items
            if i["name"].lower().endswith(".xml")
            and ("informationtable" in i["name"].lower()
                 or "infotable" in i["name"].lower())
        ]
        if candidates:
            return index_url + candidates[0]["name"]
        # Last resort: any .xml that isn't the header / primary doc schema
        any_xml = [i for i in items if i["name"].lower().endswith(".xml")
                   and "primary_doc" not in i["name"].lower()]
        if any_xml:
            return index_url + any_xml[0]["name"]
    finally:
        session.session.headers["Host"] = "data.sec.gov"
    return None


def parse_information_table(xml_text: str) -> List[Dict[str, Any]]:
    """Parse the 13F informationTable XML.

    Namespace-agnostic — strips namespace prefixes so parser code doesn't
    brittle on schema URL changes. Returns a list of holding dicts ready
    for insert_holding().
    """
    # Strip ALL namespace declarations + prefixes so ET.find() is readable.
    # 13F XML has both `xmlns:xsi=...` and `xmlns="..."` at the root;
    # missing either leaves ET unable to locate elements without
    # fully-qualified namespace paths.
    xml_no_ns = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", xml_text)
    xml_no_ns = re.sub(r"<(/?)(\w+:)", r"<\1", xml_no_ns)

    root = ET.fromstring(xml_no_ns)
    out = []
    for entry in root.findall(".//infoTable"):
        def t(path: str, default: str = "") -> str:
            node = entry.find(path)
            return (node.text or default).strip() if node is not None else default

        cusip = normalize_cusip(t("cusip"))
        if not cusip:
            continue  # unparseable row, skip

        row = {
            "cusip": cusip,
            "company_name": t("nameOfIssuer")[:200] or "(unknown)",
            "class_title": t("titleOfClass")[:50] or None,
            "shares": parse_shares(t("shrsOrPrnAmt/sshPrnamt")),
            "value_usd": parse_value_dollars(t("value")),
            "put_call": normalize_put_call(t("putCall")),
            "investment_discretion": normalize_discretion(t("investmentDiscretion")),
            "ticker": cusip_to_ticker(cusip),
        }
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Top-level scrape orchestrator
# ---------------------------------------------------------------------------

def scrape_filer(
    cik: str,
    db_conn: sqlite3.Connection,
    max_filings: Optional[int] = None,
) -> Dict[str, int]:
    """Scrape all available 13F-HR filings for one filer. Returns stats."""
    run_id = start_run(db_conn, f"edgar13f:{cik}")
    stats = {"filings_seen": 0, "filings_ok": 0, "holdings_inserted": 0,
             "dupe_holdings": 0, "filings_dupe": 0}

    try:
        session = EdgarSession()
        name, filer_type = FILERS.get(cik, ("(unknown)", None))
        upsert_filer(db_conn, cik=cik, name=name, filer_type=filer_type)

        filings = list_13f_filings_for_filer(session, cik)
        if max_filings:
            filings = filings[:max_filings]

        for f in filings:
            stats["filings_seen"] += 1
            accession = f["accession_number"]

            # Find + fetch the informationTable XML
            xml_url = find_information_table_url(session, cik, accession)
            if not xml_url:
                logger.warning("No infotable.xml for %s %s", cik, accession)
                continue

            # Pull the XML — Archives subpath uses www.sec.gov host
            session.session.headers["Host"] = "www.sec.gov"
            try:
                r = session.get(xml_url)
            finally:
                session.session.headers["Host"] = "data.sec.gov"

            xml_text = r.text

            # Persist raw BEFORE parsing
            insert_raw_filing(
                db_conn, accession_number=accession, cik=cik,
                filing_type="13F-HR", content_type="xml",
                payload=xml_text, source_url=xml_url,
                filed_on=f["filed_date"],
            )
            db_conn.commit()

            # Parse
            try:
                rows = parse_information_table(xml_text)
            except Exception as exc:
                logger.warning("Parse failed for %s: %s", accession, exc)
                mark_raw_parsed(db_conn, accession, "parse_error", str(exc))
                db_conn.commit()
                continue

            # Insert filing summary
            total_value = sum(h["value_usd"] for h in rows if h.get("value_usd"))
            is_new = insert_filing(
                db_conn, accession_number=accession, cik=cik,
                period_of_report=f["period_of_report"],
                filed_date=f["filed_date"],
                total_value_usd=total_value,
                total_positions=len(rows),
                parser_version=PARSER_VERSION,
            )
            if not is_new:
                stats["filings_dupe"] += 1

            stats["filings_ok"] += 1

            for row in rows:
                if insert_holding(
                    db_conn, accession_number=accession,
                    cusip=row["cusip"],
                    company_name=row["company_name"],
                    class_title=row["class_title"],
                    ticker=row["ticker"],
                    shares=row["shares"],
                    value_usd=row["value_usd"],
                    put_call=row["put_call"],
                    investment_discretion=row["investment_discretion"],
                    parser_version=PARSER_VERSION,
                ):
                    stats["holdings_inserted"] += 1
                else:
                    stats["dupe_holdings"] += 1

            mark_raw_parsed(db_conn, accession, "parsed")
            db_conn.commit()
            logger.info(
                "  %s period=%s  positions=%d  total_value=$%d",
                accession, f["period_of_report"], len(rows), total_value,
            )

        finish_run(db_conn, run_id, status="ok",
                   rows_inserted=stats["holdings_inserted"],
                   rows_seen=stats["filings_seen"])
    except RateLimitedError as exc:
        finish_run(db_conn, run_id, status="failed",
                   rows_inserted=stats["holdings_inserted"],
                   rows_seen=stats["filings_seen"],
                   error=f"rate limited: {exc}")
        raise
    except Exception as exc:
        logger.exception("Filer scrape failed: %s", cik)
        finish_run(db_conn, run_id, status="failed",
                   rows_inserted=stats["holdings_inserted"],
                   rows_seen=stats["filings_seen"],
                   error=str(exc))
        raise

    return stats


def scrape_all_filers(
    db_conn: sqlite3.Connection,
    filers: Optional[Iterable[str]] = None,
    max_filings_per_filer: Optional[int] = None,
) -> Dict[str, Dict[str, int]]:
    """Scrape every filer in the FILERS registry (or the supplied list)."""
    cik_list = list(filers) if filers else list(FILERS.keys())
    results: Dict[str, Dict[str, int]] = {}
    for cik in cik_list:
        try:
            results[cik] = scrape_filer(
                cik, db_conn, max_filings=max_filings_per_filer,
            )
        except RateLimitedError:
            logger.error("Rate limited on %s — stopping the run.", cik)
            results[cik] = {"error": "rate_limited"}
            break
        except Exception as exc:
            logger.warning("Filer %s failed: %s", cik, exc)
            results[cik] = {"error": str(exc)}
    return results
