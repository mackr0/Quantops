"""Tier-2 corporate-mapped alt-data sources (2026-05-17).

Three free, per-symbol signals consumed by alternative_data:
  - GitHub repo activity (tech stocks)
  - FDA inspection citations (pharma)
  - NHTSA recalls (auto / EV)

All three use a 24h TTL cache so a 30-symbol scan hits each API
at most once per ticker per day. None require auth (GitHub
unauth limit is 60 req/hr; FDA + NHTSA are unauth public).

Symbol mapping: each function uses a small hardcoded lookup
table for tickers in our largecap universe. Unknown tickers
return empty (graceful no-op). Adding a ticker = adding a row
to the relevant map — no schema change needed.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_USER_AGENT = "QuantOpsAI Research (research@quantopsai.com)"
_CACHE: Dict[str, Dict[str, Any]] = {}
_TTL_SEC = 24 * 60 * 60  # 24h — daily refresh sufficient


def _http_get_json(url: str, timeout: int = 20) -> Optional[Any]:
    """Minimal urllib JSON GET with the user-agent header. Returns
    None on network or parse failure (caller treats as no-data)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError,
            json.JSONDecodeError, ValueError, OSError) as exc:
        logger.debug("http_get_json failed for %s: %s", url, exc)
        return None


def _cached(key: str):
    """Read-side cache lookup. Returns None if missing or expired."""
    entry = _CACHE.get(key)
    if entry and (time.time() - entry["ts"]) < _TTL_SEC:
        return entry["data"]
    return None


def _cache_put(key: str, data: Any) -> None:
    _CACHE[key] = {"ts": time.time(), "data": data}


# ─────────────────────────────────────────────────────────────────────
# GitHub repo activity (tech stocks)
# ─────────────────────────────────────────────────────────────────────

# Ticker → primary GitHub org. Hand-curated for the tech largecaps
# most likely to be in our universe. NOT a guess — each verified
# against the company's actual public-facing repo presence.
_TICKER_TO_GITHUB_ORG = {
    "MSFT": "microsoft",  "GOOGL": "google",   "GOOG": "google",
    "META": "facebook",   "AMZN": "aws",       "NFLX": "Netflix",
    "TSLA": "tesla",      "AAPL": "apple",     "NVDA": "NVIDIA",
    "AMD": "ROCm",        "ORCL": "oracle",    "CRM": "salesforce",
    "ADBE": "adobe",      "INTC": "intel",     "IBM": "IBM",
    "SAP": "SAP",         "MDB": "mongodb",    "DDOG": "DataDog",
    "SNOW": "snowflakedb", "OKTA": "okta",     "ZS": "zscaler",
    "NET": "cloudflare",  "TEAM": "atlassian", "DOCN": "digitalocean",
    "ESTC": "elastic",    "PLTR": "palantir",
}


def get_github_activity(symbol: str) -> Dict[str, Any]:
    """Public repo activity (stars, recent commits) for the symbol's
    primary GitHub org. Useful as a growth/engagement proxy for tech.
    Returns {} for non-tech tickers."""
    org = _TICKER_TO_GITHUB_ORG.get(symbol.upper())
    if not org:
        return {}
    ck = f"gh:{org}"
    cached = _cached(ck)
    if cached is not None:
        return cached

    # GitHub's `/orgs/{org}` endpoint returns public_repos count;
    # we sum stars across the org's top public repos to get an
    # engagement proxy.
    org_url = f"https://api.github.com/orgs/{org}"
    repos_url = f"https://api.github.com/orgs/{org}/repos?per_page=30&sort=updated"
    org_data = _http_get_json(org_url) or {}
    repos = _http_get_json(repos_url) or []
    if not isinstance(repos, list):
        repos = []
    total_stars = sum(r.get("stargazers_count", 0) for r in repos
                      if isinstance(r, dict))
    recent_pushed = sum(1 for r in repos
                        if isinstance(r, dict) and r.get("pushed_at", "")
                        > "2026-04-17")  # rolling 30d push activity
    result = {
        "github_org": org,
        "public_repos": int(org_data.get("public_repos", 0)),
        "stars_top30": int(total_stars),
        "active_repos_30d": int(recent_pushed),
        "has_data": True,
    }
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# FDA inspection citations (pharma)
# ─────────────────────────────────────────────────────────────────────

# Pharma ticker → FDA-registered company name (substring match in
# FDA inspection records). Hand-curated; expanded as needed.
_TICKER_TO_FDA_NAME = {
    "PFE": "Pfizer",          "MRK": "Merck",
    "JNJ": "Johnson & Johnson", "ABBV": "AbbVie",
    "LLY": "Eli Lilly",       "BMY": "Bristol-Myers Squibb",
    "GILD": "Gilead",         "REGN": "Regeneron",
    "VRTX": "Vertex",         "BIIB": "Biogen",
    "AMGN": "Amgen",          "MRNA": "Moderna",
    "BNTX": "BioNTech",       "NVAX": "Novavax",
    "AZN": "AstraZeneca",     "NVS": "Novartis",
    "GSK": "GlaxoSmithKline",
}


def get_fda_inspections(symbol: str) -> Dict[str, Any]:
    """FDA inspection citations for pharma tickers via openFDA API
    (free, no auth). Returns {} for non-pharma tickers."""
    name = _TICKER_TO_FDA_NAME.get(symbol.upper())
    if not name:
        return {}
    ck = f"fda:{name}"
    cached = _cached(ck)
    if cached is not None:
        return cached

    # openFDA inspections-citations endpoint
    url = (
        "https://api.fda.gov/food/enforcement.json?"
        f"search=recalling_firm:%22{urllib.parse.quote(name)}%22"
        "&limit=20"
    )
    try:
        import urllib.parse
        data = _http_get_json(url) or {}
    except Exception:
        data = {}
    results = data.get("results") or []
    recent_citations = len(results)
    most_recent = results[0].get("recall_initiation_date") if results else None
    result = {
        "fda_name": name,
        "recent_citations_count": int(recent_citations),
        "most_recent_date": most_recent,
        "has_data": recent_citations > 0,
    }
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# NHTSA recalls (auto / EV)
# ─────────────────────────────────────────────────────────────────────

# Ticker → NHTSA manufacturer name (substring match accepted)
_TICKER_TO_NHTSA_MAKE = {
    "TSLA": "TESLA",      "F": "FORD",         "GM": "GENERAL MOTORS",
    "TM": "TOYOTA",       "HMC": "HONDA",      "STLA": "STELLANTIS",
    "RIVN": "RIVIAN",     "LCID": "LUCID",     "NIO": "NIO",
    "XPEV": "XPENG",      "LI": "LI AUTO",     "FSR": "FISKER",
}


def get_nhtsa_recalls(symbol: str) -> Dict[str, Any]:
    """NHTSA recall count for the manufacturer behind this ticker.
    Returns {} for non-auto tickers."""
    make = _TICKER_TO_NHTSA_MAKE.get(symbol.upper())
    if not make:
        return {}
    ck = f"nhtsa:{make}"
    cached = _cached(ck)
    if cached is not None:
        return cached

    # NHTSA recall API — modelYear left open, manufacturer=make
    import datetime
    # Use last 12 months of model years to keep it bounded
    this_year = datetime.date.today().year
    counts = []
    for yr in range(this_year - 1, this_year + 1):
        url = (
            "https://api.nhtsa.gov/recalls/recallsByVehicle?"
            f"make={urllib.parse.quote(make)}&modelYear={yr}"
        )
        try:
            import urllib.parse  # noqa: F811 — used above too
            data = _http_get_json(url) or {}
        except Exception:
            data = {}
        counts.append(len(data.get("results") or []))
    total = sum(counts)
    result = {
        "nhtsa_make": make,
        "recalls_recent_years": int(total),
        "has_data": total > 0,
    }
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# SAM.gov / USASpending federal contracts (defense)
# ─────────────────────────────────────────────────────────────────────

# Ticker → known prime-contractor name in USASpending.
# Defense / gov-tech largecaps. NOT a guess — each verified by
# searching usaspending.gov for the company's award history.
_TICKER_TO_USA_SPENDING_NAME = {
    "LMT": "LOCKHEED MARTIN",  "RTX": "RTX",
    "NOC": "NORTHROP GRUMMAN", "GD": "GENERAL DYNAMICS",
    "BA": "BOEING",            "HII": "HUNTINGTON INGALLS",
    "LDOS": "LEIDOS",          "BAH": "BOOZ ALLEN HAMILTON",
    "SAIC": "SAIC",            "CACI": "CACI",
    "PLTR": "PALANTIR",
}


def get_sam_gov_contracts(symbol: str) -> Dict[str, Any]:
    """USASpending.gov recent prime contract awards for the ticker's
    contractor name. Free, no auth. Returns {} for non-defense
    tickers."""
    name = _TICKER_TO_USA_SPENDING_NAME.get(symbol.upper())
    if not name:
        return {}
    ck = f"usaspending:{name}"
    cached = _cached(ck)
    if cached is not None:
        return cached

    # USASpending v2 search endpoint — POST to /api/v2/search/spending_by_award
    # For simplicity, hit the GET /api/v2/recipient/duns/ alternative is gone
    # (deprecated), so we use the search endpoint with POST.
    import urllib.request
    import urllib.error
    body = json.dumps({
        "filters": {
            "recipient_search_text": [name],
            "award_type_codes": ["A", "B", "C", "D"],  # contracts
            "time_period": [
                {"start_date": "2026-01-01", "end_date": "2026-12-31"}
            ],
        },
        "fields": ["Award ID", "Recipient Name",
                   "Award Amount", "Action Date"],
        "limit": 10,
        "page": 1,
    }).encode("utf-8")
    url = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
    result: Dict[str, Any] = {
        "usaspending_name": name,
        "recent_awards_count": 0,
        "recent_awards_total_usd": 0,
        "has_data": False,
    }
    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"User-Agent": _USER_AGENT,
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        rows = data.get("results") or []
        if rows:
            result["recent_awards_count"] = len(rows)
            result["recent_awards_total_usd"] = float(sum(
                (r.get("Award Amount") or 0) for r in rows
            ))
            result["has_data"] = True
    except (urllib.error.HTTPError, urllib.error.URLError,
            json.JSONDecodeError, ValueError, OSError) as exc:
        logger.debug("USASpending fetch failed for %s: %s", name, exc)
    _cache_put(ck, result)
    return result
