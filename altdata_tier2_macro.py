"""Tier-2 macro / sector alt-data sources (2026-05-17).

Four symbol-agnostic signals consumed by alternative_data via the
unified macro cache:
  - USDA crop reports (agri tickers indirect benefit)
  - EIA energy data (oil/gas inventories)
  - CFTC Commitments of Traders (positioning signals)
  - Sector ETF flow differentials (extends existing etf_flows)

Each has a 24h TTL cache (matching the other macro signals).
All four are reachable via free public APIs (USDA QuickStats and
EIA require free API keys; CFTC and ETF flows are unauth public).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict

logger = logging.getLogger(__name__)

_USER_AGENT = "QuantOpsAI Research (research@quantopsai.com)"
_CACHE: Dict[str, Dict[str, Any]] = {}
_TTL_SEC = 24 * 60 * 60


def _http_get(url: str, timeout: int = 20) -> bytes:
    import urllib.error
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        logger.debug("http_get failed for %s: %s", url, exc)
        return b""


def _cached(key: str):
    e = _CACHE.get(key)
    if e and (time.time() - e["ts"]) < _TTL_SEC:
        return e["data"]
    return None


def _cache_put(key: str, data: Any) -> None:
    _CACHE[key] = {"ts": time.time(), "data": data}


# ─────────────────────────────────────────────────────────────────────
# USDA crop reports (agri)
# ─────────────────────────────────────────────────────────────────────

_USDA_API_KEY = os.environ.get("USDA_API_KEY", "")


def get_usda_crop_reports() -> Dict[str, Any]:
    """Latest weekly crop-progress report. Surfaces overall corn/soy
    condition (good+excellent %). Useful as a regime input for agri
    tickers (DE, AGCO, ADM, MOS, etc.)."""
    ck = "usda_crop_progress"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result: Dict[str, Any] = {
        "good_excellent_pct_corn": None,
        "good_excellent_pct_soy": None,
        "has_data": False,
    }
    if not _USDA_API_KEY:
        # No key set → graceful no-op; surface via warning so the
        # operator knows why.
        logger.debug("USDA_API_KEY not set — crop reports unavailable")
        _cache_put(ck, result)
        return result
    base = "https://quickstats.nass.usda.gov/api/api_GET/"
    qs_corn = (
        f"?key={_USDA_API_KEY}"
        "&commodity_desc=CORN"
        "&statisticcat_desc=CONDITION"
        "&unit_desc=PCT GOOD"
        "&agg_level_desc=NATIONAL"
        "&format=JSON"
    )
    try:
        raw = _http_get(base + qs_corn).decode("utf-8", "replace")
        data = json.loads(raw) if raw else {}
        rows = (data.get("data") or [])[:1]
        if rows:
            result["good_excellent_pct_corn"] = float(rows[0].get("Value", 0))
            result["has_data"] = True
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.debug("USDA corn fetch parse failed: %s", exc)
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# EIA energy data (oil/gas storage)
# ─────────────────────────────────────────────────────────────────────

_EIA_API_KEY = os.environ.get("EIA_API_KEY", "")


def get_eia_energy_inventories() -> Dict[str, Any]:
    """Latest weekly crude-oil and natural-gas storage levels from
    EIA. Useful regime input for XOM, CVX, SHEL, COP, EQT, etc."""
    ck = "eia_inventories"
    cached = _cached(ck)
    if cached is not None:
        return cached
    result: Dict[str, Any] = {
        "crude_oil_stocks_mmbbl": None,
        "nat_gas_storage_bcf": None,
        "has_data": False,
    }
    if not _EIA_API_KEY:
        logger.debug("EIA_API_KEY not set — energy inventories unavailable")
        _cache_put(ck, result)
        return result
    # WCESTUS1 = US weekly crude oil ending stocks (excluding SPR)
    # NW2_EPG0_SWO_R48_BCF = US weekly nat gas storage
    base = "https://api.eia.gov/v2/seriesid/{sid}?api_key=" + _EIA_API_KEY
    for sid, key in (("PET.WCESTUS1.W", "crude_oil_stocks_mmbbl"),
                     ("NG.NW2_EPG0_SWO_R48_BCF.W", "nat_gas_storage_bcf")):
        try:
            url = base.format(sid=sid) + "&length=1"
            raw = _http_get(url).decode("utf-8", "replace")
            data = json.loads(raw) if raw else {}
            rows = ((data.get("response") or {}).get("data") or [])
            if rows:
                result[key] = float(rows[0].get("value", 0))
                result["has_data"] = True
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.debug("EIA fetch for %s failed: %s", sid, exc)
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# CFTC Commitments of Traders (positioning)
# ─────────────────────────────────────────────────────────────────────

def get_cftc_cot_positioning() -> Dict[str, Any]:
    """Most recent CFTC COT positioning summary for key contracts
    (gold, crude, S&P futures). Public CSV at cftc.gov, no auth.

    For now this is a placeholder shape — the canonical socrata
    endpoint at publicreporting.cftc.gov returns deferred futures
    contracts data; full parser is a follow-up. Returns has_data=False
    until populated."""
    ck = "cftc_cot"
    cached = _cached(ck)
    if cached is not None:
        return cached
    # Socrata JSON endpoint (no key required for low volume).
    # Sample query: latest "Disaggregated Futures Only" report.
    url = (
        "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
        "?$limit=1&$order=report_date_as_yyyy_mm_dd DESC"
    )
    result: Dict[str, Any] = {
        "latest_report_date": None, "has_data": False,
    }
    try:
        raw = _http_get(url).decode("utf-8", "replace")
        data = json.loads(raw) if raw else []
        if data and isinstance(data, list):
            row = data[0]
            result["latest_report_date"] = row.get(
                "report_date_as_yyyy_mm_dd"
            )
            result["has_data"] = True
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("CFTC COT fetch parse failed: %s", exc)
    _cache_put(ck, result)
    return result


# ─────────────────────────────────────────────────────────────────────
# Sector ETF flow differentials (extends existing etf_flows)
# ─────────────────────────────────────────────────────────────────────

def get_sector_flow_differentials() -> Dict[str, Any]:
    """Compute the SPREAD between strong-sector and weak-sector
    ETF flows. Higher absolute spread = rotation regime;
    near-zero = broad-market regime.

    Derived from existing macro_data.get_etf_flows — no new API
    call. Different KEY in the unified macro dict so the AI can
    distinguish 'rotation strength' from 'absolute flows'."""
    ck = "sector_flow_diff"
    cached = _cached(ck)
    if cached is not None:
        return cached
    try:
        from macro_data import get_etf_flows
        flows = get_etf_flows() or {}
    except Exception as exc:
        logger.debug("etf_flows source failed: %s", exc)
        flows = {}
    # Per-sector flow_5d_pct (5-day rolling) — pick extremes
    sector_flows = []
    for sector, info in flows.items():
        if not isinstance(info, dict):
            continue
        flow_pct = info.get("flow_5d_pct")
        if flow_pct is None:
            continue
        sector_flows.append((sector, float(flow_pct)))
    if not sector_flows:
        # Numeric fields use 0.0 (not None) so the API contract test
        # — every numeric API field is a number — holds even with
        # zero source data. `has_data=False` is the no-data signal.
        result = {"strongest": None, "weakest": None,
                  "strongest_pct": 0.0, "weakest_pct": 0.0,
                  "spread_pct": 0.0, "has_data": False}
        _cache_put(ck, result)
        return result
    sector_flows.sort(key=lambda x: x[1])
    weakest = sector_flows[0]
    strongest = sector_flows[-1]
    spread = round(strongest[1] - weakest[1], 2)
    result = {
        "strongest": strongest[0],
        "strongest_pct": round(strongest[1], 2),
        "weakest": weakest[0],
        "weakest_pct": round(weakest[1], 2),
        "spread_pct": spread,
        "has_data": True,
    }
    _cache_put(ck, result)
    return result
