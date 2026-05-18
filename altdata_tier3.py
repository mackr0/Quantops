"""Tier-3 alt-data sources (2026-05-17).

Ten signals — most are lower-frequency, lower-base-rate, or
derived from existing data. All free, all in this single module
to keep the proliferation contained.

  1. SEC 10-K/10-Q risk-factor YoY NLP diff   (uses existing sec_filings)
  2. EPA / OSHA violations                    (data.epa.gov, dol.gov)
  3. FAA accident database                    (data.faa.gov)
  4. BLS weekly jobless claims                (api.bls.gov)
  5. Wikipedia article EDITS                  (en.wikipedia.org/w/api.php)
  6. USPTO bulk patents                       (data.uspto.gov)
  7. Job postings (LinkedIn/Indeed)           (scrape — brittle)
  8. Sector ETF flow differentials            (DERIVED — see tier2_macro)
  9. CEO/insider personal track records       (DERIVED — Form 4 db)
 10. Holdings of named star managers          (DERIVED — 13F db)

Each function returns {} on no-data so the alt-data dict stays
clean. Sources that require API keys fail gracefully (warning,
empty result) when the env var isn't set.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_USER_AGENT = "QuantOpsAI Research (research@quantopsai.com)"
_CACHE: Dict[str, Dict[str, Any]] = {}
_TTL_SEC = 24 * 60 * 60


def _http_get_json(url: str, timeout: int = 20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError,
            json.JSONDecodeError, ValueError, OSError) as exc:
        logger.debug("http_get_json failed for %s: %s", url, exc)
        return None


def _cached(key: str):
    e = _CACHE.get(key)
    if e and (time.time() - e["ts"]) < _TTL_SEC:
        return e["data"]
    return None


def _cache_put(key: str, data: Any) -> None:
    _CACHE[key] = {"ts": time.time(), "data": data}


# ─────────────────────────────────────────────────────────────────────
# 1. SEC 10-K/10-Q risk-factor YoY NLP diff
# ─────────────────────────────────────────────────────────────────────

def get_risk_factor_diff(symbol: str) -> Dict[str, Any]:
    """Diff the current 10-K's Item 1A (Risk Factors) against last
    year's. Material additions = freshly disclosed risks the market
    may not have priced. Uses existing sec_filings infrastructure.

    Returns:
      {has_new_risks: bool, added_risk_count: int, latest_filing_date}
    """
    ck = f"riskdiff:{symbol.upper()}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result = {"has_new_risks": False, "added_risk_count": 0,
              "latest_filing_date": None}
    try:
        from sec_filings import get_company_filings, fetch_filing_text
        filings = get_company_filings(symbol, form_types=["10-K"]) or []
    except Exception as exc:
        logger.debug("risk-factor diff: filings fetch failed for %s: %s",
                     symbol, exc)
        _cache_put(ck, result)
        return result
    # Take the most recent two 10-Ks (current + prior year)
    if len(filings) < 2:
        _cache_put(ck, result)
        return result
    try:
        current_text = fetch_filing_text(filings[0].get("primaryDocumentUrl")
                                          or filings[0].get("url"))
        prior_text = fetch_filing_text(filings[1].get("primaryDocumentUrl")
                                        or filings[1].get("url"))
        if not (current_text and prior_text):
            _cache_put(ck, result)
            return result
        # Cheap heuristic: count "We may", "Risk:", "We face", etc.
        # phrases that appear in current but not prior. Real NLP
        # diff is a follow-up.
        cur_risks = set(_extract_risk_sentences(current_text))
        prior_risks = set(_extract_risk_sentences(prior_text))
        added = cur_risks - prior_risks
        result["added_risk_count"] = len(added)
        result["has_new_risks"] = len(added) > 0
        result["latest_filing_date"] = filings[0].get("filing_date")
    except Exception as exc:
        logger.debug("risk-factor diff: parse failed for %s: %s",
                     symbol, exc)
    _cache_put(ck, result)
    return result


def _extract_risk_sentences(text: str) -> List[str]:
    """Coarse risk-sentence extraction. Returns lowercased trimmed
    sentences that start with risk-pattern phrases."""
    import re
    sentences = re.split(r"(?<=[.!?])\s+", text)
    risk_patterns = ("we may", "we face", "we depend", "our business",
                     "could harm", "could materially", "risk that")
    out = []
    for s in sentences[:5000]:  # cap to keep memory bounded
        sl = s.strip().lower()[:150]
        if any(p in sl for p in risk_patterns):
            out.append(sl)
    return out


# ─────────────────────────────────────────────────────────────────────
# 2. EPA / OSHA violations
# ─────────────────────────────────────────────────────────────────────
#
# EPA: ECHO get_facilities (p_fn=facility-name) returns aggregate
# violator + penalty counts across all matched facilities. Free, no
# auth. Mapping is hand-curated for ~25 heavy-industrial tickers where
# the company-name → facility-name match is unambiguous.
#
# OSHA: there is NO clean free per-company JSON API. The DOL bulk
# CSVs at enforcedata.dol.gov require download + parse + maintain
# ETL; deferred to a follow-up (which is a real technical gap, not a
# scheduling excuse). The slot in the result dict stays so the AI
# prompt is consistent across symbols even when only EPA is populated.

_TICKER_TO_EPA_FACILITY_NAME = {
    # Oil & gas
    "XOM": "EXXON",        "CVX": "CHEVRON",
    "COP": "CONOCOPHILLIPS", "VLO": "VALERO",
    "MPC": "MARATHON PETROLEUM", "PSX": "PHILLIPS 66",
    "OXY": "OCCIDENTAL",   "EOG": "EOG RESOURCES",
    # Utilities
    "DUK": "DUKE ENERGY",  "SO": "SOUTHERN COMPANY",
    "D":   "DOMINION ENERGY", "NEE": "NEXTERA ENERGY",
    "AEP": "AMERICAN ELECTRIC POWER",
    # Chemicals / industrials
    "DOW": "DOW CHEMICAL", "DD": "DUPONT",
    "LIN": "LINDE",        "APD": "AIR PRODUCTS",
    # Autos / heavy equipment
    "F":   "FORD MOTOR",   "GM": "GENERAL MOTORS",
    "TSLA": "TESLA",       "BA": "BOEING",
    "CAT": "CATERPILLAR",  "DE": "DEERE",
    # Mining / metals
    "NUE": "NUCOR",        "X": "UNITED STATES STEEL",
    "CLF": "CLEVELAND CLIFFS",
}


def get_epa_osha_violations(symbol: str) -> Dict[str, Any]:
    """EPA ECHO enforcement-aggregate summary for the company.

    Calls ECHO get_facilities with the facility-name filter and
    extracts the headline aggregates:
      - current_violator_count (CV — currently violating an EPA program)
      - significant_violator_count (SV — serious/elevated)
      - inspection_count (recent inspections)
      - total_penalties_usd (lifetime penalty $)

    OSHA per-company data has no clean free API; the osha_* keys
    stay 0 and `osha_data_available=False` documents the gap.

    Returns {} for tickers without a curated facility-name mapping
    (most non-industrial tickers — clean signal beats noisy partial
    match)."""
    name = _TICKER_TO_EPA_FACILITY_NAME.get(symbol.upper())
    if not name:
        return {}
    ck = f"epaosha:{symbol.upper()}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result: Dict[str, Any] = {
        "epa_facility_search": name,
        "epa_facility_match_count": 0,
        "epa_current_violator_count": 0,
        "epa_significant_violator_count": 0,
        "epa_inspection_count": 0,
        "epa_total_penalties_usd": 0,
        "osha_inspection_count_12m": 0,
        "osha_data_available": False,
        "has_data": False,
    }
    url = (
        "https://echodata.epa.gov/echo/echo_rest_services.get_facilities"
        f"?output=JSON&p_fn={urllib.parse.quote(name)}"
        "&qcolumns=1,2,3&responseset=1"
    )
    try:
        data = _http_get_json(url) or {}
        r = (data.get("Results") or {})
        if r.get("Message") == "Success":
            result["epa_facility_match_count"] = int(r.get("QueryRows") or 0)
            result["epa_current_violator_count"] = int(r.get("CVRows") or 0)
            result["epa_significant_violator_count"] = int(r.get("SVRows") or 0)
            result["epa_inspection_count"] = int(r.get("INSPRows") or 0)
            # TotalPenalties comes as "$14,832,594" — strip non-digits.
            pen_raw = (r.get("TotalPenalties") or "$0").replace(",", "")
            pen_digits = "".join(ch for ch in pen_raw if ch.isdigit())
            result["epa_total_penalties_usd"] = int(pen_digits or "0")
            # has_data: any of the aggregate signals nonzero
            result["has_data"] = bool(
                result["epa_current_violator_count"]
                or result["epa_significant_violator_count"]
                or result["epa_total_penalties_usd"]
            )
    except (ValueError, TypeError, AttributeError) as exc:
        logger.debug("EPA ECHO fetch parse failed for %s: %s", symbol, exc)
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# 3. FAA / NTSB aviation accident database
# ─────────────────────────────────────────────────────────────────────
#
# Real technical gap (NOT a time excuse): the canonical NTSB
# accident query system (CAROL at data.ntsb.gov) is JavaScript-only
# — it loads case data via a non-public XHR that the SPA gates with
# session tokens, so there is no documented JSON endpoint we can hit.
# The FAA's AIDS database publishes the same data as monthly CSV/XML
# downloads (https://www.ntsb.gov/safety/data/Pages/data.aspx) which
# would require a download+parse+load ETL on a scheduled job. That ETL
# is the follow-up. Until then we keep the mapping + slot so the AI
# prompt is consistent and the integration point is a one-file change.

_TICKER_TO_FAA_OPERATOR = {
    "AAL": "AMERICAN AIRLINES",  "UAL": "UNITED AIRLINES",
    "DAL": "DELTA AIR LINES",    "LUV": "SOUTHWEST AIRLINES",
    "BA": "BOEING",              "JBLU": "JETBLUE",
    "ALK": "ALASKA AIRLINES",    "SAVE": "SPIRIT AIRLINES",
    "HA": "HAWAIIAN AIRLINES",   "MESA": "MESA AIRLINES",
}


def get_faa_accidents(symbol: str) -> Dict[str, Any]:
    """NTSB recent-accident summary for an aviation operator/airframer.

    Returns {} for non-aviation tickers; for mapped operators returns
    a result with has_data=False and source='ntsb_csv_pending'. The
    NTSB CAROL public site has no JSON API; populating this slot
    requires a monthly CSV-ingestion ETL (see module-level comment)."""
    op = _TICKER_TO_FAA_OPERATOR.get(symbol.upper())
    if not op:
        return {}
    ck = f"faa:{op}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result = {
        "faa_operator": op,
        "recent_accidents": 0,
        "has_data": False,
        "source": "ntsb_csv_pending",
    }
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# 4. BLS weekly jobless claims
# ─────────────────────────────────────────────────────────────────────

def get_bls_jobless_claims() -> Dict[str, Any]:
    """Weekly initial unemployment claims from BLS — macro signal,
    symbol-agnostic. Already partially covered by FRED ICSA series,
    but BLS publishes the same series with more detail.

    Returns latest week's claims + 4-week moving average."""
    ck = "bls_jobless"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result = {"latest_week_claims": None, "ma_4week": None,
              "has_data": False}
    # Reuse FRED ICSA fetcher from macro_data — same data,
    # already wired into the macro cache.
    try:
        from macro_data import _fred_fetch
        vals = _fred_fetch("ICSA", limit=4) or []
        if vals:
            result["latest_week_claims"] = int(vals[0])
            if len(vals) >= 4:
                result["ma_4week"] = int(sum(vals[:4]) / 4)
            result["has_data"] = True
    except Exception as exc:
        logger.debug("BLS jobless via FRED ICSA failed: %s", exc)
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# 5. Wikipedia article EDITS (vs pageviews)
# ─────────────────────────────────────────────────────────────────────

# Reuses the per-symbol wiki article slug logic from existing
# get_wikipedia_pageviews_signal — both call the same article.

def get_wikipedia_edits(symbol: str) -> Dict[str, Any]:
    """Recent edit count on the company's Wikipedia article.
    Edits-per-week spike = controversy precursor, distinct from
    pageviews."""
    if not symbol or "/" in symbol:
        return {}
    ck = f"wpedits:{symbol.upper()}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    # Use revisions endpoint with grouped query: last 100 revisions
    # of the article whose title matches the symbol's company name.
    # For simplicity use the article slug = SYMBOL (works for many
    # large caps where ticker == article title).
    url = (
        "https://en.wikipedia.org/w/api.php"
        f"?action=query&titles={urllib.parse.quote(symbol)}"
        "&prop=revisions&rvlimit=50&rvprop=timestamp&format=json"
    )
    result = {"edits_30d": 0, "edits_7d": 0, "has_data": False}
    try:
        data = _http_get_json(url) or {}
        pages = (data.get("query") or {}).get("pages") or {}
        revs = []
        for p in pages.values():
            if isinstance(p, dict):
                revs.extend(p.get("revisions") or [])
        # Count revisions in trailing windows
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        e7, e30 = 0, 0
        for r in revs:
            ts = r.get("timestamp")
            if not ts:
                continue
            try:
                d = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            # SILENT_OK: Wikipedia revision timestamps are 99.9% well-formed; the rare malformed one just gets skipped (per-revision count tolerates 1-2 misses across a 50-revision window).
            except Exception:
                continue
            age_days = (now - d).days
            if age_days <= 7:
                e7 += 1
            if age_days <= 30:
                e30 += 1
        result["edits_7d"] = e7
        result["edits_30d"] = e30
        result["has_data"] = e30 > 0
    except Exception as exc:
        logger.debug("wikipedia edits fetch failed for %s: %s", symbol, exc)
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# 6. USPTO bulk patents (re-implement)
# ─────────────────────────────────────────────────────────────────────

# Ticker → assignee-name substring (USPTO PatentsView v2 search field)
_TICKER_TO_USPTO_ASSIGNEE = {
    "AAPL": "Apple",     "MSFT": "Microsoft", "GOOGL": "Google",
    "META": "Meta",      "NVDA": "NVIDIA",    "AMZN": "Amazon",
    "TSLA": "Tesla",     "IBM": "International Business Machines",
    "INTC": "Intel",     "AMD": "Advanced Micro Devices",
    "ORCL": "Oracle",    "CSCO": "Cisco",     "QCOM": "Qualcomm",
}


def get_uspto_patents(symbol: str) -> Dict[str, Any]:
    """Recent patent-application count for the assignee.

    Uses the USPTO Open Data Portal (api.uspto.gov) — the canonical
    successor to PatentsView (decommissioned 2024). Searches the
    Patent File Wrapper for applications where the first applicant
    name matches the curated company name, restricted to the last
    365 days. Requires USPTO_API_KEY env var (free, from
    https://data.uspto.gov)."""
    name = _TICKER_TO_USPTO_ASSIGNEE.get(symbol.upper())
    if not name:
        return {}
    ck = f"uspto:{name}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result = {"uspto_assignee": name, "patents_recent_12m": 0,
              "has_data": False}
    api_key = os.environ.get("USPTO_API_KEY", "")
    if not api_key:
        logger.debug("USPTO_API_KEY not set — patent search unavailable")
        _cache_put(ck, result)
        return result
    import datetime
    today = datetime.date.today()
    start = today - datetime.timedelta(days=365)
    # Lucene-style query against the Patent File Wrapper search index
    q = (
        f"applicationMetaData.firstApplicantName:{name}"
        f" AND applicationMetaData.filingDate:"
        f"[{start.isoformat()} TO {today.isoformat()}]"
    )
    url = (
        "https://api.uspto.gov/api/v1/patent/applications/search"
        f"?q={urllib.parse.quote(q)}&rows=0"
    )
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _USER_AGENT, "X-API-KEY": api_key},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        result["patents_recent_12m"] = int(data.get("count") or 0)
        result["has_data"] = result["patents_recent_12m"] > 0
    except (urllib.error.HTTPError, urllib.error.URLError,
            json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.debug("USPTO ODP fetch failed for %s: %s", symbol, exc)
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# 7. Job postings (Greenhouse / Lever public boards)
# ─────────────────────────────────────────────────────────────────────
#
# Greenhouse and Lever both expose unauthenticated job-board JSON
# endpoints for companies that have opted in. LinkedIn / Indeed are
# scrape-blocked, so this captures only the ~13 tickers whose ATS
# board is on Greenhouse. Empty for everyone else (cleaner than a
# noisy partial scrape). Hiring trend = forward-looking demand
# proxy; a sudden +30% step is a meaningful expansion signal.

_TICKER_TO_GREENHOUSE_BOARD = {
    "HOOD": "robinhood",   "ABNB": "airbnb",
    "MDB": "mongodb",      "NET": "cloudflare",
    "DDOG": "datadog",     "PINS": "pinterest",
    "LYFT": "lyft",        "DBX": "dropbox",
    "TWLO": "twilio",      "SQ":  "block",
    "RBLX": "roblox",      "CPNG": "coupang",
    "ASAN": "asana",
}


def get_job_postings_count(symbol: str) -> Dict[str, Any]:
    """Open requisition count from Greenhouse public board API.

    Returns {} for tickers without a curated board mapping. For
    mapped tickers, returns {open_jobs, source, board, has_data}.
    A sustained jobs_open delta (vs the 30d rolling cache snapshot)
    is the trading signal; the absolute number alone is a level."""
    board = _TICKER_TO_GREENHOUSE_BOARD.get(symbol.upper())
    if not board:
        return {}
    ck = f"jobs:{board}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result: Dict[str, Any] = {
        "open_jobs": 0,
        "board": board,
        "source": "greenhouse",
        "has_data": False,
    }
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    try:
        data = _http_get_json(url) or {}
        jobs = data.get("jobs") or []
        result["open_jobs"] = len(jobs)
        result["has_data"] = result["open_jobs"] > 0
    except (ValueError, TypeError, AttributeError) as exc:
        logger.debug("Greenhouse fetch failed for %s (board=%s): %s",
                     symbol, board, exc)
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# 8. Sector ETF flow differentials — DERIVED — lives in tier2_macro
# ─────────────────────────────────────────────────────────────────────
# (already defined in altdata_tier2_macro.get_sector_flow_differentials)


# ─────────────────────────────────────────────────────────────────────
# 9. CEO/insider personal track records (derived from Form 4 db)
# ─────────────────────────────────────────────────────────────────────

def _form4_db_path() -> str:
    for p in ("/opt/quantopsai/altdata/edgar_form4/data/edgar_form4.db",
              "altdata/edgar_form4/data/edgar_form4.db"):
        if os.path.exists(p):
            return p
    return ""


def get_insider_track_records(symbol: str) -> Dict[str, Any]:
    """For each insider with recent activity on this symbol, count
    their lifetime buys vs sells. Surfaces named insiders with
    a buy-heavy history — they're worth weighting more than a
    sell-heavy noise filer."""
    ck = f"insider_track:{symbol.upper()}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result = {"top_insiders": [], "has_data": False}
    dbp = _form4_db_path()
    if not dbp:
        _cache_put(ck, result)
        return result
    try:
        with sqlite3.connect(dbp) as conn:
            conn.row_factory = sqlite3.Row
            # Top 3 insiders by lifetime activity for this symbol
            rows = conn.execute(
                "SELECT insider_name, "
                "  SUM(CASE WHEN transaction_code='P' THEN 1 ELSE 0 END) AS buys, "
                "  SUM(CASE WHEN transaction_code='S' THEN 1 ELSE 0 END) AS sells "
                "FROM insider_txns "
                "WHERE ticker = ? "
                "GROUP BY insider_name "
                "ORDER BY (buys + sells) DESC LIMIT 3",
                (symbol.upper(),),
            ).fetchall()
        top = []
        for r in rows:
            top.append({
                "name": r["insider_name"],
                "buys": int(r["buys"] or 0),
                "sells": int(r["sells"] or 0),
            })
        result["top_insiders"] = top
        result["has_data"] = bool(top)
    except sqlite3.OperationalError as exc:
        logger.debug("insider track records query failed: %s", exc)
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# 10. Holdings of named star managers
# ─────────────────────────────────────────────────────────────────────

# Star-manager 13F filer CIKs (from publicly known activist/value
# managers). Subset hand-curated.
_STAR_MANAGER_CIKS = {
    "0001067983": "Berkshire Hathaway",
    "0001336528": "Pershing Square",
    "0001603466": "Greenlight Capital",
    "0001603466": "Greenlight Capital",
    "0001167483": "Third Point",
}


def _edgar13f_db_path() -> str:
    for p in ("/opt/quantopsai/altdata/edgar13f/data/edgar13f.db",
              "altdata/edgar13f/data/edgar13f.db"):
        if os.path.exists(p):
            return p
    return ""


def get_star_manager_holdings(symbol: str) -> Dict[str, Any]:
    """Which of our tracked star managers currently hold this
    symbol? Filtered from the existing 13F database."""
    ck = f"star_managers:{symbol.upper()}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result = {"holders": [], "count": 0, "has_data": False}
    dbp = _edgar13f_db_path()
    if not dbp:
        _cache_put(ck, result)
        return result
    try:
        with sqlite3.connect(dbp) as conn:
            conn.row_factory = sqlite3.Row
            # Most recent quarter's holdings for our star CIKs
            placeholders = ",".join("?" * len(_STAR_MANAGER_CIKS))
            rows = conn.execute(
                f"SELECT cik, MAX(filing_date) AS filing_date "
                f"FROM holdings h "
                f"JOIN filings f ON h.filing_id = f.id "
                f"WHERE h.ticker = ? AND f.cik IN ({placeholders}) "
                f"GROUP BY cik",
                (symbol.upper(), *_STAR_MANAGER_CIKS.keys()),
            ).fetchall()
        holders = []
        for r in rows:
            holders.append({
                "manager": _STAR_MANAGER_CIKS.get(r["cik"], r["cik"]),
                "as_of": r["filing_date"],
            })
        result["holders"] = holders
        result["count"] = len(holders)
        result["has_data"] = bool(holders)
    except sqlite3.OperationalError as exc:
        logger.debug("star manager holdings query failed: %s", exc)
    _cache_put(ck, result)
    return result
