"""Correlation management -- prevent overexposure to correlated positions."""

import logging
import time
from typing import List, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache for correlation data
# ---------------------------------------------------------------------------
_correlation_cache: Dict[str, dict] = {}
_CACHE_TTL = 3600  # 60 minutes


def _yf_symbol(symbol: str) -> str:
    """Convert Alpaca symbol to yfinance format (e.g. BTC/USD -> BTC-USD)."""
    return symbol.replace("/", "-")


def _fetch_returns(symbols: List[str], days: int = 20) -> Optional[Dict[str, np.ndarray]]:
    """Fetch daily returns for a list of symbols using yfinance batch download.

    Returns a dict mapping symbol -> numpy array of daily returns,
    or None if the fetch fails entirely.
    """
    try:
        import yfinance as yf
        from datetime import datetime, timedelta

        # Convert symbols for yfinance
        yf_symbols = [_yf_symbol(s) for s in symbols]

        end = datetime.now()
        # Fetch extra days to account for weekends/holidays
        start = end - timedelta(days=days * 2 + 10)

        # Batch download for efficiency
        data = yf.download(
            yf_symbols,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )

        if data.empty:
            return None

        returns = {}
        for orig_sym, yf_sym in zip(symbols, yf_symbols):
            try:
                if len(yf_symbols) == 1:
                    # Single symbol: data["Close"] is a Series
                    close = data["Close"].dropna()
                else:
                    # Multiple symbols: data["Close"] is a DataFrame
                    close = data["Close"][yf_sym].dropna()

                if len(close) < days:
                    # Not enough data, skip this symbol
                    continue

                # Take the last `days` closing prices and compute returns
                close = close.tail(days + 1)
                daily_returns = close.pct_change().dropna().values
                if len(daily_returns) >= days - 2:  # Allow a little slack
                    returns[orig_sym] = daily_returns
            except Exception as exc:
                logger.debug("Could not extract returns for %s: %s", orig_sym, exc)
                continue

        return returns if returns else None

    except ImportError:
        logger.warning("yfinance not installed -- correlation check disabled")
        return None
    except Exception as exc:
        logger.warning("Failed to fetch returns for correlation: %s", exc)
        return None


def check_correlation(
    symbol: str,
    existing_positions: List[Dict],
    max_correlation: float = 0.7,
) -> Dict:
    """Check if a new symbol is too correlated with existing positions.

    Args:
        symbol: The new symbol to check.
        existing_positions: List of position dicts (from get_positions).
        max_correlation: Maximum allowed Pearson correlation (0-1).

    Returns:
        dict with keys: allowed (bool), and optionally correlated_with,
        correlation, reason.
    """
    if not existing_positions:
        return {"allowed": True, "reason": "no existing positions"}

    existing_symbols = [p["symbol"] for p in existing_positions if p["symbol"] != symbol]
    if not existing_symbols:
        return {"allowed": True, "reason": "no other existing positions"}

    # Check cache first
    cache_key = f"{symbol}:{'|'.join(sorted(existing_symbols))}"
    now = time.time()
    if cache_key in _correlation_cache:
        cached = _correlation_cache[cache_key]
        if now - cached["timestamp"] < _CACHE_TTL:
            return cached["result"]

    # Fetch returns for all symbols (new + existing) in one batch
    all_symbols = [symbol] + existing_symbols
    returns = _fetch_returns(all_symbols, days=20)

    if returns is None or symbol not in returns:
        # Can't calculate -- fail open (don't block trades on data errors)
        result = {"allowed": True, "reason": "insufficient data for correlation check"}
        _correlation_cache[cache_key] = {"result": result, "timestamp": now}
        return result

    new_returns = returns[symbol]

    # Check correlation against each existing position
    for pos_sym in existing_symbols:
        if pos_sym not in returns:
            continue

        pos_returns = returns[pos_sym]

        # Align lengths
        min_len = min(len(new_returns), len(pos_returns))
        if min_len < 5:
            continue

        try:
            corr = np.corrcoef(new_returns[:min_len], pos_returns[:min_len])[0, 1]

            if not np.isfinite(corr):
                continue

            if abs(corr) > max_correlation:
                result = {
                    "allowed": False,
                    "correlated_with": pos_sym,
                    "correlation": round(float(corr), 3),
                    "reason": f"Too correlated with {pos_sym} ({corr:.2f})",
                }
                _correlation_cache[cache_key] = {"result": result, "timestamp": now}
                return result
        except Exception as exc:
            logger.debug("Correlation calc failed for %s vs %s: %s", symbol, pos_sym, exc)
            continue

    result = {"allowed": True, "reason": "correlations within limits"}
    _correlation_cache[cache_key] = {"result": result, "timestamp": now}
    return result


def get_position_diversity(positions: List[Dict]) -> Dict:
    """Analyze diversity of current positions.

    Args:
        positions: List of position dicts.

    Returns:
        dict with total_positions, long, short, unique_groups.
    """
    if not positions:
        return {"total_positions": 0, "long": 0, "short": 0, "unique_groups": 0}

    # Import segments to classify symbols into groups
    try:
        from segments import SEGMENTS
    except ImportError:
        SEGMENTS = {}

    # Build a reverse lookup: symbol -> segment name
    symbol_to_group = {}
    for seg_name, seg_def in SEGMENTS.items():
        for sym in seg_def.get("universe", []):
            symbol_to_group[sym] = seg_name

    long_count = 0
    short_count = 0
    groups = set()

    for p in positions:
        qty = float(p.get("qty", 0))
        sym = p.get("symbol", "")

        if qty > 0:
            long_count += 1
        elif qty < 0:
            short_count += 1

        group = symbol_to_group.get(sym, "other")
        groups.add(group)

    return {
        "total_positions": len(positions),
        "long": long_count,
        "short": short_count,
        "unique_groups": len(groups),
    }
