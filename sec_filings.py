"""SEC EDGAR filings — Form 4 insider data + 10-K/10-Q/8-K semantic analysis.

Phase 4 of the Quant Fund Evolution roadmap (see ROADMAP.md).

The EDGAR system is public and free. Rate limits apply (10 requests/sec)
so we throttle and cache aggressively. We focus on three filing types:

  Form 4  — Insider transactions (existing)
  10-K    — Annual report (full year)
  10-Q    — Quarterly report
  8-K     — Current report (material events, filed within 4 business days)

The power move is detecting LANGUAGE CHANGES between consecutive filings:
  - Risk Factors section (Item 1A) gaining a new paragraph on something
    material is one of the strongest predictive signals in finance.
  - MD&A forward-looking language going from "we expect growth" to
    "results may be materially lower than prior periods" predicts drawdowns.
  - Going concern language appearing anywhere is an immediate short signal.
  - Material weakness disclosures warn of coming accounting problems.

These signals exist in public 10-K/10-Q/8-K filings but require reading
every page to find. LLMs can read them instantly.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

from alternative_data import _get_cached, _set_cached

# ---------------------------------------------------------------------------
# Caching / rate-limiting
# ---------------------------------------------------------------------------

_cache: Dict[str, tuple] = {}
_CACHE_TTL = 86400   # 24 hours
_last_request_ts = 0.0
_MIN_REQUEST_INTERVAL = 0.11   # SEC asks for <10 req/sec


# SEC identifies clients via User-Agent. They ask for email.
_USER_AGENT = "QuantOpsAI Research Bot (mack@mackenziesmith.com)"


# ---------------------------------------------------------------------------
# Form 4 (insider transactions) — existing, kept working
# ---------------------------------------------------------------------------

_EDGAR_RSS_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?"
    "action=getcompany&company={symbol}&type=4&dateb=&owner=include"
    "&count=10&search_text=&action=getcompany&output=atom"
)


def _rate_limited_get(url: str, accept: str = "*/*", timeout: int = 15) -> Optional[bytes]:
    """Fetch a URL while honoring SEC's rate limits."""
    global _last_request_ts
    now = time.time()
    sleep_for = _MIN_REQUEST_INTERVAL - (now - _last_request_ts)
    if sleep_for > 0:
        time.sleep(sleep_for)
    _last_request_ts = time.time()

    try:
        req = Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept": accept,
        })
        with urlopen(req, timeout=timeout) as response:
            return response.read()
    except (URLError, Exception) as exc:
        logger.debug("SEC fetch failed for %s: %s", url, exc)
        return None


def get_sec_insider_filings(symbol: str) -> Dict[str, Any]:
    """Fetch recent Form 4 insider filings from SEC EDGAR.

    Kept for backward compatibility. See alternative_data.get_insider_activity
    for the version that returns signal counts.
    """
    cache_key = f"insider_{symbol}"
    cached = _get_cached(cache_key, "insider")  # 24h TTL, survives restarts
    if cached is not None:
        return cached

    result = {
        "filings_count": 0,
        "recent_filings": [],
        "net_signal": "none",
    }

    try:
        import xml.etree.ElementTree as ET
        url = _EDGAR_RSS_URL.format(symbol=symbol.upper())
        xml_data = _rate_limited_get(url, accept="application/atom+xml")
        if not xml_data:
            return result

        root = ET.fromstring(xml_data)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        buy_count = 0
        sell_count = 0

        for entry in entries[:10]:
            title = entry.findtext("atom:title", "", ns)
            updated = entry.findtext("atom:updated", "", ns)[:10]
            link_el = entry.find("atom:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""

            result["recent_filings"].append({
                "title": title, "date": updated, "url": link,
            })
            result["filings_count"] += 1

            tl = title.lower()
            if "acquisition" in tl or "purchase" in tl:
                buy_count += 1
            elif "disposition" in tl or "sale" in tl:
                sell_count += 1

        if buy_count > sell_count:
            result["net_signal"] = "insider_buying"
        elif sell_count > buy_count:
            result["net_signal"] = "insider_selling"
        elif buy_count > 0 or sell_count > 0:
            result["net_signal"] = "mixed"

    except Exception as exc:
        logger.debug("Form 4 parse failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# CIK lookup — SEC identifies companies by CIK, not ticker
# ---------------------------------------------------------------------------

_CIK_CACHE: Dict[str, str] = {}


def lookup_cik(symbol: str) -> Optional[str]:
    """Resolve a ticker to its 10-digit CIK using SEC's free mapping file.

    The mapping is cached in-process on first call.
    """
    symbol = symbol.upper()
    if symbol in _CIK_CACHE:
        return _CIK_CACHE[symbol]

    if not _CIK_CACHE:
        # Load the whole mapping once
        url = "https://www.sec.gov/files/company_tickers.json"
        data = _rate_limited_get(url)
        if not data:
            return None
        try:
            mapping = json.loads(data)
            for entry in mapping.values():
                ticker = entry.get("ticker", "").upper()
                cik = str(entry.get("cik_str", "")).zfill(10)
                if ticker and cik:
                    _CIK_CACHE[ticker] = cik
        except Exception as exc:
            logger.warning("CIK mapping parse failed: %s", exc)
            return None

    return _CIK_CACHE.get(symbol)


# ---------------------------------------------------------------------------
# Company filings list — 10-K/10-Q/8-K metadata
# ---------------------------------------------------------------------------

def get_company_filings(symbol: str, form_types: Optional[List[str]] = None,
                        since_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch a company's filings metadata from EDGAR.

    Parameters
    ----------
    symbol : str
        Ticker.
    form_types : list[str], optional
        Filter to these form types (e.g. ['10-K', '10-Q', '8-K']).
    since_date : str, optional
        ISO date 'YYYY-MM-DD'. Only return filings on/after this date.

    Returns
    -------
    list of dicts with: accession_number, form_type, filed_date, primary_doc_url
    """
    cache_key = f"filings_{symbol}"
    cached = _get_cached(cache_key, "insider")
    if cached is not None:
        filings = cached
    else:
        filings = _fetch_company_filings(symbol)
        _set_cached(cache_key, filings)

    if form_types:
        filings = [f for f in filings if f["form_type"] in form_types]
    if since_date:
        filings = [f for f in filings if f["filed_date"] >= since_date]

    return filings


def _fetch_company_filings(symbol: str) -> List[Dict[str, Any]]:
    """Download and parse the EDGAR submissions JSON for a ticker."""
    cik = lookup_cik(symbol)
    if not cik:
        return []

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _rate_limited_get(url)
    if not data:
        return []

    try:
        submissions = json.loads(data)
    except Exception as exc:
        logger.warning("Submissions JSON parse failed for %s: %s", symbol, exc)
        return []

    recent = submissions.get("filings", {}).get("recent", {})
    accession = recent.get("accessionNumber", []) or []
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    primary_docs = recent.get("primaryDocument", []) or []

    results = []
    for i in range(min(len(accession), len(forms), len(dates), len(primary_docs))):
        acc = accession[i].replace("-", "")
        results.append({
            "accession_number": accession[i],
            "form_type": forms[i],
            "filed_date": dates[i],
            "primary_doc_url": (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{acc}/{primary_docs[i]}"
            ),
        })

    return results


# ---------------------------------------------------------------------------
# Filing text fetch and section extraction
# ---------------------------------------------------------------------------

def fetch_filing_text(filing_url: str) -> Optional[str]:
    """Fetch a filing document and return its plain text content."""
    cache_key = f"text_{filing_url}"
    cached = _get_cached(cache_key, "insider")  # filings are immutable, 24h cache fine
    if cached is not None:
        return cached

    data = _rate_limited_get(filing_url, accept="text/html", timeout=30)
    if not data:
        return None

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(data, "html.parser")
        # Remove scripts, styles, tables of contents
        for tag in soup(["script", "style", "meta", "link"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # Collapse excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
    except Exception as exc:
        logger.warning("Filing text extract failed for %s: %s", filing_url, exc)
        return None

    _set_cached(cache_key, text)
    return text


# Section markers used in 10-K / 10-Q. Filings use various conventions so we
# look for several variants and take the longest match.
_RISK_FACTORS_STARTS = [
    r"ITEM\s*1A[\.\s]+RISK\s+FACTORS",
    r"RISK\s+FACTORS",
]
_RISK_FACTORS_ENDS = [
    r"ITEM\s*1B[\.\s]",
    r"ITEM\s*2[\.\s]",
    r"UNRESOLVED\s+STAFF\s+COMMENTS",
]

_MDNA_STARTS = [
    r"ITEM\s*7[\.\s]+MANAGEMENT.?S\s+DISCUSSION",
    r"MANAGEMENT.?S\s+DISCUSSION\s+AND\s+ANALYSIS",
]
_MDNA_ENDS = [
    r"ITEM\s*7A[\.\s]",
    r"ITEM\s*8[\.\s]",
    r"QUANTITATIVE\s+AND\s+QUALITATIVE\s+DISCLOSURES",
]


def _extract_section(text: str, starts: List[str], ends: List[str],
                     max_chars: int = 40_000) -> Optional[str]:
    """Pull a single labeled section out of a filing's plain text."""
    if not text:
        return None

    start_pos = -1
    for pat in starts:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            start_pos = m.end()
            break
    if start_pos < 0:
        return None

    end_pos = len(text)
    for pat in ends:
        m = re.search(pat, text[start_pos:], re.IGNORECASE)
        if m:
            end_pos = start_pos + m.start()
            break

    section = text[start_pos:end_pos].strip()
    if len(section) > max_chars:
        section = section[:max_chars]
    return section


def extract_sections(filing_text: str) -> Dict[str, Any]:
    """Extract the material sections we care about from a filing.

    Returns dict with:
        risk_factors: str or None
        mdna: str or None
        going_concern_flag: bool
        material_weakness_flag: bool
    """
    result = {
        "risk_factors": _extract_section(filing_text, _RISK_FACTORS_STARTS, _RISK_FACTORS_ENDS),
        "mdna": _extract_section(filing_text, _MDNA_STARTS, _MDNA_ENDS),
        "going_concern_flag": bool(re.search(r"going\s+concern", filing_text or "", re.IGNORECASE)),
        "material_weakness_flag": bool(re.search(
            r"material\s+weakness(?:es)?\s+in\s+internal\s+control",
            filing_text or "", re.IGNORECASE
        )),
    }
    return result


# ---------------------------------------------------------------------------
# Semantic diff via AI
# ---------------------------------------------------------------------------

def analyze_filing_diff(old_text: Optional[str], new_text: str,
                         section_name: str, ctx: Any = None) -> Dict[str, Any]:
    """Ask the AI to identify material language changes between two filings.

    Returns dict with:
        severity: 'low' | 'medium' | 'high'
        signal: 'concerning' | 'positive' | 'neutral'
        summary: one-sentence human-readable summary
        changes: list of {type, old, new, impact} dicts
    """
    from ai_providers import call_ai

    old = (old_text or "")[:15000]
    new = new_text[:15000]

    if not old:
        prompt = (
            f"You are a forensic accountant reviewing a public company's "
            f"{section_name} section for the first time. Identify any "
            f"unusual or concerning language.\n\n"
            f"SECTION TEXT:\n{new}\n\n"
            f"Respond ONLY with JSON:\n"
            f'{{"severity": "low|medium|high", '
            f'"signal": "concerning|positive|neutral", '
            f'"summary": "one sentence", '
            f'"changes": [{{"type": "added|new_language", "description": "...", "impact": "short|none"}}]}}'
        )
    else:
        prompt = (
            f"You are a forensic accountant comparing two consecutive "
            f"{section_name} filings for the same company. Identify any "
            f"MATERIAL LANGUAGE CHANGES: new risk factors, removed safeguards, "
            f"shifts from positive to cautious language, mentions of going "
            f"concern, material weakness, restatements, or similar. Ignore "
            f"cosmetic changes.\n\n"
            f"PREVIOUS FILING:\n{old}\n\n"
            f"CURRENT FILING:\n{new}\n\n"
            f"Respond ONLY with JSON:\n"
            f'{{"severity": "low|medium|high", '
            f'"signal": "concerning|positive|neutral", '
            f'"summary": "one sentence", '
            f'"changes": [{{"type": "new_risk|removed_language|language_shift", '
            f'"old": "...", "new": "...", "impact": "trade short|avoid|none"}}]}}'
        )

    provider = getattr(ctx, "ai_provider", "anthropic") if ctx else "anthropic"
    model = getattr(ctx, "ai_model", "claude-haiku-4-5-20251001") if ctx else "claude-haiku-4-5-20251001"
    api_key = getattr(ctx, "ai_api_key", "") if ctx else ""

    try:
        raw = call_ai(prompt, provider=provider, model=model, api_key=api_key,
                      max_tokens=1024,
                      db_path=getattr(ctx, "db_path", None),
                      purpose="sec_diff")
        result = json.loads(raw)
    except Exception as exc:
        logger.warning("Filing diff AI call failed: %s", exc)
        return {
            "severity": "low", "signal": "neutral",
            "summary": f"Analysis failed: {exc}", "changes": [],
        }

    # Normalize
    severity = str(result.get("severity", "low")).lower()
    if severity not in ("low", "medium", "high"):
        severity = "low"
    signal = str(result.get("signal", "neutral")).lower()
    if signal not in ("concerning", "positive", "neutral"):
        signal = "neutral"

    return {
        "severity": severity,
        "signal": signal,
        "summary": str(result.get("summary", ""))[:500],
        "changes": result.get("changes", []),
    }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def get_latest_filing_in_db(db_path: str, symbol: str, form_type: str) -> Optional[Dict[str, Any]]:
    """Return the most recent row for (symbol, form_type) from sec_filings_history."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sec_filings_history "
            "WHERE symbol = ? AND form_type = ? "
            "ORDER BY filed_date DESC LIMIT 1",
            (symbol.upper(), form_type),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def save_filing_row(db_path: str, row: Dict[str, Any]) -> None:
    """Persist (or upsert) a row in sec_filings_history."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO sec_filings_history
               (symbol, accession_number, form_type, filed_date, fetched_at,
                filing_url, risk_factors_text, mdna_text,
                going_concern_flag, material_weakness_flag,
                analyzed_at, alert_severity, alert_signal,
                alert_summary, alert_changes_json)
               VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?)""",
            (
                row.get("symbol", "").upper(),
                row.get("accession_number"),
                row.get("form_type"),
                row.get("filed_date"),
                row.get("filing_url"),
                row.get("risk_factors_text"),
                row.get("mdna_text"),
                1 if row.get("going_concern_flag") else 0,
                1 if row.get("material_weakness_flag") else 0,
                row.get("analyzed_at"),
                row.get("alert_severity"),
                row.get("alert_signal"),
                row.get("alert_summary"),
                row.get("alert_changes_json"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_active_alerts(db_path: str, symbols: Optional[List[str]] = None,
                       min_severity: str = "medium") -> List[Dict[str, Any]]:
    """Return filing-based alerts for symbols, filtered by severity.

    'Active' means the alert was analyzed recently (within 90 days).
    """
    import sqlite3
    sev_order = {"low": 0, "medium": 1, "high": 2}
    min_level = sev_order.get(min_severity, 1)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        if symbols:
            q_marks = ",".join("?" * len(symbols))
            sql = (
                f"SELECT * FROM sec_filings_history "
                f"WHERE symbol IN ({q_marks}) "
                f"AND analyzed_at IS NOT NULL "
                f"AND filed_date >= date('now', '-90 days') "
                f"ORDER BY filed_date DESC"
            )
            rows = conn.execute(sql, [s.upper() for s in symbols]).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sec_filings_history "
                "WHERE analyzed_at IS NOT NULL "
                "AND filed_date >= date('now', '-90 days') "
                "ORDER BY filed_date DESC"
            ).fetchall()
        conn.close()

        alerts = []
        for row in rows:
            sev = row["alert_severity"] or "low"
            if sev_order.get(sev, 0) >= min_level:
                alerts.append(dict(row))
        return alerts
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def monitor_symbol(symbol: str, db_path: str, ctx: Any = None,
                    form_types: Optional[List[str]] = None,
                    days_back: int = 180) -> Dict[str, Any]:
    """Check one symbol for new filings, analyze any we haven't seen before.

    Idempotent: if a filing is already in sec_filings_history, we skip it.
    For each NEW filing, we fetch, extract sections, and run the AI diff
    against the most recent previous filing of the same type.

    Returns summary dict with: new_filings, analyzed, errors, alerts.
    """
    from datetime import datetime, timedelta

    if form_types is None:
        form_types = ["10-K", "10-Q", "8-K"]

    since_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    summary = {"symbol": symbol, "new_filings": 0, "analyzed": 0,
               "errors": [], "alerts": []}

    try:
        filings = get_company_filings(symbol, form_types, since_date)
    except Exception as exc:
        summary["errors"].append(f"fetch: {exc}")
        return summary

    import sqlite3
    # Quick lookup of what's already in DB
    existing = set()
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT accession_number FROM sec_filings_history WHERE symbol = ?",
            (symbol.upper(),),
        ).fetchall()
        conn.close()
        existing = {r[0] for r in rows}
    except Exception:
        pass

    for filing in filings:
        if filing["accession_number"] in existing:
            continue

        summary["new_filings"] += 1

        text = fetch_filing_text(filing["primary_doc_url"])
        if not text:
            summary["errors"].append(f"no text for {filing['accession_number']}")
            continue

        sections = extract_sections(text)

        # Find the most recent previous filing of same type for diffing
        prev = get_latest_filing_in_db(db_path, symbol, filing["form_type"])
        prev_risk = prev.get("risk_factors_text") if prev else None

        alert = analyze_filing_diff(
            prev_risk, sections.get("risk_factors") or "",
            section_name=f"{filing['form_type']} risk factors",
            ctx=ctx,
        )
        summary["analyzed"] += 1

        row = {
            "symbol": symbol,
            "accession_number": filing["accession_number"],
            "form_type": filing["form_type"],
            "filed_date": filing["filed_date"],
            "filing_url": filing["primary_doc_url"],
            "risk_factors_text": sections.get("risk_factors"),
            "mdna_text": sections.get("mdna"),
            "going_concern_flag": sections.get("going_concern_flag"),
            "material_weakness_flag": sections.get("material_weakness_flag"),
            "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "alert_severity": alert["severity"],
            "alert_signal": alert["signal"],
            "alert_summary": alert["summary"],
            "alert_changes_json": json.dumps(alert["changes"], default=str),
        }
        save_filing_row(db_path, row)

        if alert["severity"] in ("medium", "high"):
            summary["alerts"].append({
                "symbol": symbol,
                "form": filing["form_type"],
                "date": filing["filed_date"],
                "severity": alert["severity"],
                "signal": alert["signal"],
                "summary": alert["summary"],
            })

    return summary


# ---------------------------------------------------------------------------
# Earnings Call Transcript Sentiment (Signal 6)
# ---------------------------------------------------------------------------

def get_earnings_call_sentiment(symbol: str, ctx: Any = None) -> Dict[str, Any]:
    """Analyze management tone from the most recent 8-K earnings exhibit.

    Finds the latest 8-K filing, extracts exhibit text (typically the
    press release or transcript), and runs it through Haiku for tone
    analysis.

    Cost-gated: cached 30 days (earnings are quarterly).

    Returns dict with:
        tone: str — 'positive', 'neutral', 'cautious', or 'negative'
        key_phrases: list of str — notable management quotes
        has_data: bool
    """
    cache_key = f"transcript_sentiment_{symbol}"
    cached = _get_cached(cache_key, "insider")  # 24h TTL (recheck daily)
    if cached is not None:
        return cached

    result = {
        "tone": "neutral",
        "key_phrases": [],
        "has_data": False,
    }

    try:
        filings = get_company_filings(symbol, form_types=["8-K"])
        if not filings:
            _set_cached(cache_key, result)
            return result

        latest = filings[0]
        doc_url = latest.get("primary_doc_url")
        if not doc_url:
            _set_cached(cache_key, result)
            return result

        text = fetch_filing_text(doc_url)
        if not text or len(text) < 200:
            _set_cached(cache_key, result)
            return result

        # Truncate to keep AI cost low
        excerpt = text[:3000]

        if ctx is not None:
            try:
                from ai_providers import call_ai
                import json as _json
                prompt = (
                    f"Analyze the tone of this earnings press release for {symbol}. "
                    f"Classify as: positive, neutral, cautious, or negative. "
                    f"Extract 2-3 key phrases that indicate management sentiment.\n\n"
                    f"Respond with ONLY a JSON object: "
                    f'{{"tone": "positive|neutral|cautious|negative", '
                    f'"key_phrases": ["phrase1", "phrase2"]}}\n\n'
                    f"Text:\n{excerpt}"
                )
                ai_response = call_ai(
                    prompt,
                    provider=getattr(ctx, "ai_provider", "anthropic"),
                    model=getattr(ctx, "ai_model", "claude-haiku-4-5-20251001"),
                    api_key=getattr(ctx, "ai_api_key", ""),
                    max_tokens=256,
                    db_path=getattr(ctx, "db_path", None),
                    purpose="transcript_sentiment",
                )
                if ai_response:
                    parsed = _json.loads(ai_response)
                    if isinstance(parsed, dict):
                        result["tone"] = parsed.get("tone", "neutral")
                        result["key_phrases"] = parsed.get("key_phrases", [])[:3]
                        result["has_data"] = True
            except Exception as exc:
                logger.debug("Transcript AI analysis failed for %s: %s", symbol, exc)

    except Exception as exc:
        logger.debug("Earnings call sentiment failed for %s: %s", symbol, exc)

    _set_cached(cache_key, result)
    return result
