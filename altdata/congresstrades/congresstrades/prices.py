"""Daily-close price cache for P&L analysis.

Pulls from yfinance with strict politeness (1 req/sec, sequential, no
threads) and caches one CSV per ticker in data/cache/prices/. Re-runs
are free — cached tickers are skipped unless `force=True`.

This is the Yahoo-interface layer, intentionally isolated so tests can
mock it cleanly. No network I/O happens when `fetch_ticker()` is injected
by the caller — which is exactly how the test suite works.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "prices"

# Politeness: Yahoo has been flaky under load. 1 req/sec matches what
# we validated works across the ~600-ticker bulk fetch without triggering
# the throttle.
REQUEST_DELAY_SEC = 1.0

# Default window pulled per ticker. 1y covers current-year P&L; extend
# to "3y" on demand for multi-year analysis.
DEFAULT_PERIOD = "1y"


# ---------------------------------------------------------------------------
# Default fetcher — directly hits yfinance
# ---------------------------------------------------------------------------

def _default_fetcher(ticker: str, period: str):
    """Single-ticker yfinance call. Returns a pandas DataFrame or None.

    Isolated so tests can monkeypatch it without pulling yfinance into
    every test module.
    """
    try:
        import yfinance as yf
        df = yf.download(
            ticker, period=period, progress=False,
            auto_adjust=True, threads=False,
        )
        return df
    except Exception as exc:
        logger.debug("yfinance %s failed: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Ticker discovery — which symbols do we actually need prices for?
# ---------------------------------------------------------------------------

def tickers_from_db(
    conn: sqlite3.Connection,
    year: Optional[int] = None,
    chamber: Optional[str] = None,
) -> List[str]:
    """Return distinct tickers in the trades table, optionally filtered
    by year and chamber. Only returns non-null tickers.
    """
    clauses = ["ticker IS NOT NULL"]
    args: List = []
    if year is not None:
        clauses.append("transaction_date >= ? AND transaction_date < ?")
        args.append(f"{year}-01-01")
        args.append(f"{year + 1}-01-01")
    if chamber:
        clauses.append("chamber = ?")
        args.append(chamber)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT DISTINCT ticker FROM trades WHERE {where} ORDER BY ticker",
        args,
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def cache_path_for(ticker: str, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / f"{ticker}.csv"


def is_cached(ticker: str, cache_dir: Path = CACHE_DIR) -> bool:
    p = cache_path_for(ticker, cache_dir)
    return p.exists() and p.stat().st_size > 0


# ---------------------------------------------------------------------------
# Fetch orchestrator
# ---------------------------------------------------------------------------

def refresh_prices(
    tickers: List[str],
    period: str = DEFAULT_PERIOD,
    cache_dir: Path = CACHE_DIR,
    delay: float = REQUEST_DELAY_SEC,
    force: bool = False,
    fetcher: Callable = _default_fetcher,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, int]:
    """Refresh price CSVs for the given tickers.

    Returns stats dict: fetched / cached / errors / empty.

    Args:
      tickers      Symbols to fetch
      period       yfinance period string ('1y', '3y', 'max', ...)
      cache_dir    Directory for CSVs (created if missing)
      delay        Seconds between consecutive network fetches (politeness)
      force        If True, re-fetch even if cached
      fetcher      Callable(ticker, period) → DataFrame-or-None.
                   Tests inject a mock here to avoid hitting Yahoo.
      on_progress  Optional callback(n_done, n_total) for UI updates.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    import pandas as pd

    stats = {"fetched": 0, "cached": 0, "errors": 0, "empty": 0}
    total = len(tickers)

    for i, ticker in enumerate(tickers, 1):
        if on_progress:
            on_progress(i, total)

        cache_path = cache_path_for(ticker, cache_dir)
        if cache_path.exists() and not force:
            stats["cached"] += 1
            continue

        # Polite delay before hitting network
        if delay > 0:
            time.sleep(delay)

        df = fetcher(ticker, period)
        if df is None:
            stats["errors"] += 1
            continue

        try:
            if df.empty:
                # Still write an empty CSV so next run doesn't re-fetch
                empty = pd.DataFrame({"Close": []})
                empty.to_csv(cache_path, index_label="Date")
                stats["empty"] += 1
                continue

            # yfinance with threads=False returns a flat DataFrame when
            # given a single ticker. Normalize: handle MultiIndex too.
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            if "Close" not in df.columns:
                stats["errors"] += 1
                continue
            df[["Close"]].dropna().to_csv(cache_path, index_label="Date")
            stats["fetched"] += 1
        except Exception as exc:
            logger.debug("Cache write for %s failed: %s", ticker, exc)
            stats["errors"] += 1

    return stats
