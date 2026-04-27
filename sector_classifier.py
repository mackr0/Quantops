"""Per-symbol sector classification with SQLite cache.

DYNAMIC_UNIVERSE_PLAN.md Step 1 — replaces the hardcoded ~50-symbol
`_SECTOR_MAP` in `market_data._guess_sector()` with:

1. **Cache-first lookup** in master `quantopsai.db` table
   `sector_cache(symbol, sector, fetched_at)`. 7-day TTL.
2. **yfinance** as the primary source on cache miss — `Ticker(sym).info["sector"]`
   returns a GICS sector string we map to our 7 internal keys.
3. **Static fallback map** for the top ~100 symbols when yfinance is
   unreachable or returns nothing useful.
4. **`"tech"` default** when nothing else lands (matches prior
   behavior of `_guess_sector`).

Live `market_data._guess_sector` becomes a thin wrapper around
`get_sector(symbol)` from this module.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# 7-day TTL — sector classification rarely changes for a stable name.
_TTL_SECONDS = 7 * 24 * 3600

MASTER_DB = os.environ.get("QUANTOPSAI_MASTER_DB", "quantopsai.db")

_schema_lock = threading.Lock()
_schema_initialized: set = set()

# Internal seven-key taxonomy used by the sector momentum strategy.
INTERNAL_SECTORS = {
    "tech", "finance", "energy", "healthcare",
    "consumer_disc", "industrial", "comm_services",
}

# GICS / yfinance sector strings → internal key.
_GICS_TO_INTERNAL = {
    "Technology": "tech",
    "Communication Services": "comm_services",
    "Communications": "comm_services",
    "Financial Services": "finance",
    "Financial": "finance",
    "Energy": "energy",
    "Healthcare": "healthcare",
    "Consumer Cyclical": "consumer_disc",
    "Consumer Discretionary": "consumer_disc",
    "Consumer Defensive": "consumer_disc",
    "Industrials": "industrial",
    "Basic Materials": "industrial",
    "Real Estate": "finance",   # closest fit; no separate REIT key
    "Utilities": "industrial",  # closest fit
}

# Hand-curated fallback for the top ~100 symbols. Used only when
# yfinance is unreachable AND the cache is empty. Same shape as the
# old _SECTOR_MAP that lived in market_data — preserved here so
# offline/firewalled environments keep classifying the canonical
# names correctly.
_FALLBACK_MAP = {
    "tech": {
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AMD", "INTC",
        "CRM", "ORCL", "ADBE", "NOW", "DDOG", "NET", "ZS", "SNOW", "MDB",
        "AVGO", "TXN", "MU", "QCOM", "ASML", "AMAT", "LRCX", "KLAC",
        "PLTR", "PANW", "FTNT", "CRWD", "OKTA", "TEAM", "INTU", "WDAY",
    },
    "finance": {
        "SOFI", "HOOD", "AFRM", "UPST", "COIN", "SQ", "V", "MA", "JPM",
        "BAC", "GS", "MS", "WFC", "AXP", "BLK", "SCHW", "ALLY", "LC",
        "C", "USB", "PNC", "TFC", "PYPL", "FIS", "FISV",
    },
    "energy": {
        "RIG", "ET", "AR", "CNX", "BTU", "KOS", "BTE", "OVV", "PLUG",
        "FCEL", "BE", "RUN", "XOM", "CVX", "COP", "OXY", "EOG", "MPC",
        "PSX", "VLO", "SLB",
    },
    "healthcare": {
        "UNH", "JNJ", "PFE", "MRK", "LLY", "AMGN", "GILD", "ISRG",
        "HIMS", "DNA", "WVE", "CRSP", "NTLA", "BEAM", "MRNA", "BNTX",
        "REGN", "VRTX", "BIIB", "BMY", "ABBV",
    },
    "consumer_disc": {
        "TSLA", "NKE", "SBUX", "MCD", "LULU", "DECK", "RIVN",
        "LCID", "NIO", "CVNA", "ETSY", "CHWY", "HD", "LOW", "TJX",
        "BKNG", "MAR", "HLT", "ABNB",
    },
    "industrial": {
        "BA", "RTX", "LMT", "GE", "HON", "CAT", "DE", "JOBY", "GD",
        "NOC", "MMM", "UPS", "FDX", "UNP", "EMR", "ETN", "ROK",
    },
    "comm_services": {
        "NFLX", "DIS", "ROKU", "SNAP", "PINS", "RBLX", "DKNG", "TMUS",
        "T", "VZ", "CMCSA", "WBD", "PARA", "LYV",
    },
}

_DEFAULT_SECTOR = "tech"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _init_schema(db_path: str = MASTER_DB) -> None:
    if not db_path:
        return
    with _schema_lock:
        if db_path in _schema_initialized:
            return
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sector_cache (
                    symbol     TEXT PRIMARY KEY,
                    sector     TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.commit()
            conn.close()
            _schema_initialized.add(db_path)
        except Exception as exc:
            logger.warning("Failed to init sector_cache: %s", exc)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _read_cache(symbol: str, db_path: str = MASTER_DB) -> Optional[str]:
    """Return cached sector for `symbol` if present and not stale.
    None on miss / stale / error."""
    if not symbol or not db_path:
        return None
    _init_schema(db_path)
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT sector, fetched_at FROM sector_cache WHERE symbol = ?",
            (symbol.upper(),),
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    sector, fetched_at = row
    try:
        ts = datetime.fromisoformat(fetched_at)
        if datetime.utcnow() - ts > timedelta(seconds=_TTL_SECONDS):
            return None
    except Exception:
        # If timestamp is malformed, treat as stale.
        return None
    if sector in INTERNAL_SECTORS:
        return sector
    return None


def _write_cache(symbol: str, sector: str, db_path: str = MASTER_DB) -> None:
    if not symbol or not sector or sector not in INTERNAL_SECTORS:
        return
    if not db_path:
        return
    _init_schema(db_path)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO sector_cache "
            "(symbol, sector, fetched_at) VALUES (?, ?, datetime('now'))",
            (symbol.upper(), sector),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("Failed to write sector_cache for %s: %s", symbol, exc)


# ---------------------------------------------------------------------------
# yfinance lookup
# ---------------------------------------------------------------------------

def _yfinance_sector(symbol: str) -> Optional[str]:
    """Return the internal sector key from yfinance's GICS string, or
    None on any failure / unmappable result."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
        gics = info.get("sector")
        if not gics:
            return None
        return _GICS_TO_INTERNAL.get(gics)
    except Exception:
        return None


def _fallback_sector(symbol: str) -> Optional[str]:
    """Look up `symbol` in the in-module hardcoded fallback. None on miss."""
    sym = symbol.upper()
    for sector, names in _FALLBACK_MAP.items():
        if sym in names:
            return sector
    return None


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def get_sector(symbol: str, db_path: str = MASTER_DB) -> str:
    """Return the internal sector key for `symbol`. Order:

    1. Cache hit (≤ 7 days old)
    2. yfinance GICS lookup (writes cache on success)
    3. Fallback map (writes cache on success)
    4. Default "tech"
    """
    if not symbol:
        return _DEFAULT_SECTOR
    sym = symbol.upper()

    cached = _read_cache(sym, db_path)
    if cached:
        return cached

    sector = _yfinance_sector(sym)
    if sector:
        _write_cache(sym, sector, db_path)
        return sector

    fb = _fallback_sector(sym)
    if fb:
        _write_cache(sym, fb, db_path)
        return fb

    return _DEFAULT_SECTOR
