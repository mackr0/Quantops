"""Statistical arbitrage pair book — Item 1b of COMPETITIVE_GAP_PLAN.md.

Real long/short funds (Citadel-class) trade hundreds-to-thousands of
cointegrated pairs simultaneously. We had a one-shot pair-trade
primitive (P2.3 of LONG_SHORT_PLAN) that surfaces 1-3 candidate pairs
to the AI per cycle. This module is the foundation for replacing that
with a proper pair book of 50-200 active cointegrated pairs.

What this module provides (foundation; wiring is multi-session):

  1. `engle_granger` — pairwise cointegration test on two price series.
     Returns p-value, hedge ratio, half-life of mean reversion.
  2. `compute_spread_zscore` — current standardized spread for a known
     pair given its hedge ratio.
  3. `find_cointegrated_pairs` — universe scan. Pairwise EG over the
     supplied symbols + price-history fetcher; ranks survivors.
  4. `Pair` dataclass — frozen description of a cointegrated pair.

Out of scope for this commit (separate sessions):
  - Persistent pair book table in journal
  - Daily rebalance task that re-tests cointegration of active pairs
  - Trade entry/exit signal generator
  - Wiring into trade_pipeline (proposing pair-trade actions to the AI)

Math reference: Engle-Granger two-step.
  Step 1: OLS regress price_a on price_b → hedge_ratio (β) + residuals
  Step 2: ADF test on residuals → p-value (cointegrated if p < 0.05).
  Half-life of mean reversion comes from AR(1) on residual differences:
    Δresid_t = γ·resid_{t-1} + ε  →  half-life = -ln(2) / ln(1+γ).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# Default thresholds — tunable; conservative starting points.
COINT_PVALUE_THRESHOLD = 0.05    # require p < this to call a pair cointegrated
MIN_CORRELATION = 0.6             # filter before EG to cut noise
MIN_HALF_LIFE_DAYS = 1.0          # < 1 day is noise, not mean reversion
MAX_HALF_LIFE_DAYS = 30.0         # > 30 days is too slow to trade


@dataclass(frozen=True)
class Pair:
    """One cointegrated pair. Hedge ratio is "shares of B per share of A"
    so that spread = price_a − hedge_ratio × price_b is stationary."""
    symbol_a: str
    symbol_b: str
    hedge_ratio: float
    p_value: float
    half_life_days: float
    correlation: float

    @property
    def label(self) -> str:
        return f"{self.symbol_a}/{self.symbol_b}"


def _ols_hedge_ratio(price_a: np.ndarray, price_b: np.ndarray) -> float:
    """Plain-numpy OLS slope (no intercept). Slope = β in price_a = β·price_b + ε.

    We use the no-intercept form because the constant gets absorbed into
    the spread mean during z-scoring; including it adds noise without
    changing the cointegration test outcome materially for short-window
    equity pair trading.
    """
    if len(price_a) != len(price_b) or len(price_a) < 2:
        raise ValueError("Series must be same length, ≥ 2 observations")
    pa = np.asarray(price_a, dtype=float)
    pb = np.asarray(price_b, dtype=float)
    denom = float(np.dot(pb, pb))
    if denom <= 0:
        raise ValueError("Degenerate B series (sum of squares is zero)")
    return float(np.dot(pa, pb) / denom)


def _half_life(spread: np.ndarray) -> float:
    """Half-life of mean reversion on the spread series.

    Estimate γ from AR(1) on differences:
      Δspread_t = γ·spread_{t-1} + ε
    Then half-life = -ln(2) / ln(1+γ).

    Returns inf when γ ≥ 0 (series isn't mean-reverting in this window).
    """
    s = np.asarray(spread, dtype=float)
    if len(s) < 3:
        return float("inf")
    lagged = s[:-1]
    diff = s[1:] - s[:-1]
    denom = float(np.dot(lagged, lagged))
    if denom <= 0:
        return float("inf")
    gamma = float(np.dot(lagged, diff) / denom)
    if gamma >= 0 or gamma <= -1:
        # Non-stationary or unstable AR(1)
        return float("inf")
    try:
        return float(-math.log(2) / math.log(1 + gamma))
    except (ValueError, ZeroDivisionError):
        return float("inf")


def engle_granger(price_a: Sequence[float],
                    price_b: Sequence[float]) -> Dict[str, float]:
    """Engle-Granger cointegration test on two price series.

    Returns:
      {
        "p_value": float,          # ADF p-value on residuals
        "hedge_ratio": float,      # OLS β (no-intercept)
        "half_life_days": float,   # mean-reversion speed
        "correlation": float,      # pearson on the raw series
        "n_obs": int,              # number of observations used
      }

    Cointegrated if p_value < COINT_PVALUE_THRESHOLD AND half_life is
    bounded (MIN_HALF_LIFE_DAYS ≤ hl ≤ MAX_HALF_LIFE_DAYS).

    Insufficient data or degenerate series → p_value=1.0 (rejected).
    """
    pa = np.asarray(price_a, dtype=float)
    pb = np.asarray(price_b, dtype=float)
    n = len(pa)

    base = {
        "p_value": 1.0, "hedge_ratio": 0.0,
        "half_life_days": float("inf"),
        "correlation": 0.0, "n_obs": n,
    }

    if n < 30 or len(pb) != n:
        # ADF needs ~30+ obs to be meaningful
        return base
    if not (np.all(np.isfinite(pa)) and np.all(np.isfinite(pb))):
        return base

    try:
        corr_matrix = np.corrcoef(pa, pb)
        correlation = float(corr_matrix[0, 1]) if corr_matrix.size >= 4 else 0.0
    except Exception:
        correlation = 0.0
    base["correlation"] = correlation

    try:
        beta = _ols_hedge_ratio(pa, pb)
    except ValueError:
        return base

    spread = pa - beta * pb
    if not np.all(np.isfinite(spread)) or np.std(spread) <= 1e-9:
        # Degenerate spread → not a real pair
        return base

    # ADF test from statsmodels — returns (test_statistic, p_value, ...)
    try:
        from statsmodels.tsa.stattools import adfuller
        adf_result = adfuller(spread, autolag="AIC")
        p_value = float(adf_result[1])
    except Exception as exc:
        logger.warning("ADF failed for pair (n=%d): %s", n, exc)
        return base

    return {
        "p_value": p_value,
        "hedge_ratio": float(beta),
        "half_life_days": _half_life(spread),
        "correlation": correlation,
        "n_obs": n,
    }


def is_pair_tradeable(eg_result: Dict[str, float],
                        pvalue_threshold: float = COINT_PVALUE_THRESHOLD,
                        min_correlation: float = MIN_CORRELATION) -> bool:
    """Apply the standard filters: cointegrated, correlated, mean-reverting
    on a tradeable timescale."""
    if eg_result["p_value"] >= pvalue_threshold:
        return False
    if abs(eg_result["correlation"]) < min_correlation:
        return False
    hl = eg_result["half_life_days"]
    if not (MIN_HALF_LIFE_DAYS <= hl <= MAX_HALF_LIFE_DAYS):
        return False
    return True


def compute_spread_zscore(price_a: Sequence[float],
                            price_b: Sequence[float],
                            hedge_ratio: float,
                            lookback: int = 60) -> Optional[float]:
    """Current z-score of the spread for a known pair.

    Uses the trailing `lookback` bars to compute the spread mean+std,
    then standardizes the most recent spread observation against them.

    Returns None when there isn't enough history.

    Trading interpretation:
      z > +2  → spread unusually wide. SHORT A / LONG B (hedge_ratio shares).
      z < -2  → spread unusually tight. LONG A / SHORT B.
      |z| < 0.5 → exit (mean-reverted).
      |z| > 3 → regime break — exit and re-test cointegration.
    """
    pa = np.asarray(price_a, dtype=float)
    pb = np.asarray(price_b, dtype=float)
    if len(pa) != len(pb) or len(pa) < lookback + 1:
        return None
    spread = pa[-lookback:] - hedge_ratio * pb[-lookback:]
    mean = float(np.mean(spread))
    std = float(np.std(spread))
    if std <= 1e-9:
        return None
    current = float(spread[-1])
    return (current - mean) / std


def find_cointegrated_pairs(
    symbols: List[str],
    price_history: Callable[[str], Optional[Sequence[float]]],
    pvalue_threshold: float = COINT_PVALUE_THRESHOLD,
    min_correlation: float = MIN_CORRELATION,
    max_pairs: int = 50,
) -> List[Pair]:
    """Pairwise scan for cointegrated pairs over a symbol universe.

    Args:
        symbols: list of tickers to scan.
        price_history: callable(symbol) → close-price series (or None
            if unavailable). Caller decides the window (recommend
            60-180 trading days).
        pvalue_threshold / min_correlation: filter knobs.
        max_pairs: cap returned pairs (sorted by p_value × |1 - corr|).

    Returns a list of Pair, sorted by quality (best first).

    Cost: pre-fetches all price series once (caller's price_history is
    expected to cache); then runs N·(N-1)/2 EG tests. For 100 symbols
    that's ~5000 tests. Each ADF on 90 obs is ~5ms → ~25s total. Run
    daily, not per-cycle.
    """
    series_by_sym: Dict[str, np.ndarray] = {}
    for sym in symbols:
        try:
            ph = price_history(sym)
        except Exception as exc:
            logger.debug("price_history(%s) raised: %s", sym, exc)
            continue
        if ph is None:
            continue
        arr = np.asarray(ph, dtype=float)
        if len(arr) >= 30 and np.all(np.isfinite(arr)):
            series_by_sym[sym] = arr

    valid_syms = sorted(series_by_sym.keys())
    pairs: List[Tuple[float, Pair]] = []

    for i, sym_a in enumerate(valid_syms):
        for sym_b in valid_syms[i + 1:]:
            pa = series_by_sym[sym_a]
            pb = series_by_sym[sym_b]
            n = min(len(pa), len(pb))
            if n < 30:
                continue
            try:
                result = engle_granger(pa[-n:], pb[-n:])
            except Exception as exc:
                logger.debug("EG(%s,%s) raised: %s", sym_a, sym_b, exc)
                continue

            if not is_pair_tradeable(result, pvalue_threshold,
                                       min_correlation):
                continue

            pair = Pair(
                symbol_a=sym_a, symbol_b=sym_b,
                hedge_ratio=result["hedge_ratio"],
                p_value=result["p_value"],
                half_life_days=result["half_life_days"],
                correlation=result["correlation"],
            )
            # Quality score: lower p × shorter half-life → higher quality.
            quality = result["p_value"] * result["half_life_days"]
            pairs.append((quality, pair))

    pairs.sort(key=lambda x: x[0])
    return [p for _, p in pairs[:max_pairs]]
