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

def get_epa_osha_violations(symbol: str) -> Dict[str, Any]:
    """EPA enforcement actions + OSHA recent inspections for the
    company. Placeholder shape — full implementation needs the
    ticker→FRS-ID (EPA Facility Registry) mapping which doesn't
    exist as a free clean table. Returns has_data=False until
    populated."""
    ck = f"epaosha:{symbol.upper()}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result = {"epa_violation_count_12m": 0, "osha_inspection_count_12m": 0,
              "has_data": False}
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# 3. FAA accident database
# ─────────────────────────────────────────────────────────────────────

# Airlines + airframers
_TICKER_TO_FAA_OPERATOR = {
    "AAL": "AMERICAN AIRLINES",  "UAL": "UNITED AIRLINES",
    "DAL": "DELTA AIR LINES",    "LUV": "SOUTHWEST AIRLINES",
    "BA": "BOEING",              "JBLU": "JETBLUE",
    "ALK": "ALASKA AIRLINES",    "SAVE": "SPIRIT AIRLINES",
    "HA": "HAWAIIAN AIRLINES",   "MESA": "MESA AIRLINES",
}


def get_faa_accidents(symbol: str) -> Dict[str, Any]:
    """FAA recent-accident summary for the operator/airframer.
    Returns {} for non-aviation tickers.

    Note: FAA's accident query API (data.faa.gov) is large.
    Placeholder returns has_data=False; full implementation is
    follow-up."""
    op = _TICKER_TO_FAA_OPERATOR.get(symbol.upper())
    if not op:
        return {}
    ck = f"faa:{op}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result = {"faa_operator": op, "recent_accidents": 0, "has_data": False}
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
    """Recent patent application count for the assignee."""
    name = _TICKER_TO_USPTO_ASSIGNEE.get(symbol.upper())
    if not name:
        return {}
    ck = f"uspto:{name}"
    cached = _cached(ck)
    if cached is not None:
        return cached
    # PatentsView v2 (current canonical free endpoint). NOTE:
    # endpoint exists but auth model changed in Q4 2024 — many
    # queries require an API key now. If no key, return empty.
    result = {"uspto_assignee": name, "patents_recent_12m": 0,
              "has_data": False}
    _USPTO_KEY = os.environ.get("USPTO_API_KEY", "")
    if not _USPTO_KEY:
        _cache_put(ck, result)
        return result
    # Minimal request — placeholder until proper integration
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# 7. Job postings (LinkedIn/Indeed)
# ─────────────────────────────────────────────────────────────────────

def get_job_postings_count(symbol: str) -> Dict[str, Any]:
    """Open requisition count for the company.

    Honest note: both LinkedIn and Indeed actively block scrapers
    and require auth for their search APIs. Without a paid data
    source (Greenhouse aggregator, Levels.fyi paid tier, etc.)
    there's no reliable free way to get this. Returns has_data=False
    as a marker that the slot exists but the data layer is
    intentionally empty. Surfaces in the AI prompt only when
    explicitly populated by a follow-up integration."""
    return {"jobs_open": None, "has_data": False, "source": "unavailable"}


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
