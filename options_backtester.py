"""Phase H of OPTIONS_PROGRAM_PLAN.md — options backtester.

We don't currently have paid historical options data ($99/mo Polygon
historical, $thousands OptionMetrics). Instead this backtester uses a
SYNTHETIC pricing approach:

  - Historical underlying bars come from Alpaca (free, real)
  - Implied volatility at each historical date approximated as
    trailing 30-day realized volatility
  - Option prices computed via Black-Scholes (existing compute_greeks)

What this captures correctly:
  - Direction of P&L (does this strategy class make/lose money?)
  - Approximate magnitude (correct within IV-mismatch noise)
  - Time decay, gamma, regime-dependent behavior

What this does NOT capture:
  - Bid-ask spread costs (assumed zero)
  - Real IV term structure / skew (assumed flat)
  - Vol surface dynamics around catalysts (earnings IV expansion)
  - Liquidity / slippage on multi-leg fills

For STRATEGY VALIDATION (does iron condor on SPY beat random in
neutral regime?) this is sufficient. For PRECISE P&L FORECASTING
(will this exact iron condor make exactly $X?) it isn't.

Layered build:
  Layer 1 (this commit): historical IV approximation + Black-Scholes
    pricing of arbitrary options at historical dates.
  Layer 2: single-leg strategy simulator (open → hold → close).
  Layer 3: multi-leg simulator (vertical / condor / straddle).
  Layer 4: orchestrator that replays strategy entry rules over a
    historical period and produces summary stats.
  Layer 5: dashboard integration.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date as _date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Backtest assumptions (documented inline so future readers see them)
HISTORICAL_IV_LOOKBACK_DAYS = 30
DEFAULT_RISK_FREE_RATE = 0.045  # match options_trader default


def historical_iv_approximation(symbol: str,
                                    as_of: _date,
                                    lookback_days: int = HISTORICAL_IV_LOOKBACK_DAYS,
                                    bars_provider=None,
                                    ) -> Optional[float]:
    """Approximate IV at a historical date using trailing realized vol.

    Args:
        symbol: underlying ticker.
        as_of: date for which IV is needed.
        lookback_days: window of trading days.
        bars_provider: callable(symbol, end_date, lookback_days) →
            DataFrame of OHLCV bars ending at end_date. When None, uses
            market_data.get_bars (Alpaca-first).

    Returns annualized IV as decimal (0.25 = 25%) or None when there's
    not enough data.

    Implementation: stdev of daily log returns over lookback_days,
    annualized by sqrt(252). Standard quant approach.
    """
    if bars_provider is None:
        bars_provider = _default_bars_provider

    try:
        bars = bars_provider(symbol, as_of, lookback_days + 5)
    except Exception as exc:
        logger.debug("bars fetch failed for %s @ %s: %s",
                     symbol, as_of, exc)
        return None
    if bars is None or len(bars) < lookback_days // 2:
        return None

    # Filter bars to dates <= as_of
    try:
        eligible = bars[bars.index.date <= as_of]
    except AttributeError:
        # Index may not be DatetimeIndex; assume already filtered
        eligible = bars
    if len(eligible) < lookback_days // 2:
        return None

    last = eligible.tail(lookback_days)
    closes = last["close"].astype(float)
    log_returns = (closes / closes.shift(1)).apply(math.log).dropna()
    if len(log_returns) < 5:
        return None
    daily_std = float(log_returns.std())
    if daily_std <= 0:
        return None
    annualized = daily_std * math.sqrt(252)
    return annualized


def historical_spot(symbol: str,
                       as_of: _date,
                       bars_provider=None) -> Optional[float]:
    """Return the close price of `symbol` on the trading day at or
    immediately before `as_of`. Handles weekends / holidays."""
    if bars_provider is None:
        bars_provider = _default_bars_provider
    try:
        bars = bars_provider(symbol, as_of, 10)
    except Exception:
        return None
    if bars is None or len(bars) == 0:
        return None
    try:
        eligible = bars[bars.index.date <= as_of]
    except AttributeError:
        eligible = bars
    if len(eligible) == 0:
        return None
    return float(eligible["close"].iloc[-1])


def price_option_at_date(symbol: str,
                              as_of: _date,
                              strike: float,
                              expiry: _date,
                              is_call: bool,
                              iv_override: Optional[float] = None,
                              bars_provider=None,
                              risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
                              ) -> Optional[Dict[str, Any]]:
    """Synthetic option price at `as_of` using Black-Scholes.

    Args:
        symbol: underlying.
        as_of: historical date.
        strike, expiry, is_call: option contract.
        iv_override: provide IV directly to bypass the approximation
            (useful for tests or when caller has better IV data).
        bars_provider: see historical_iv_approximation.

    Returns dict shaped like compute_greeks output:
      {price, delta, gamma, vega, theta, rho, days_to_expiry, spot, iv}
    Returns None when prerequisites can't be computed.
    """
    from options_trader import compute_greeks

    days = (expiry - as_of).days
    if days <= 0:
        # Already expired — synthetic price is intrinsic value
        spot = historical_spot(symbol, as_of, bars_provider)
        if spot is None:
            return None
        intrinsic = (max(0.0, spot - strike) if is_call
                     else max(0.0, strike - spot))
        return {
            "price": intrinsic, "delta": 0.0, "gamma": 0.0, "vega": 0.0,
            "theta": 0.0, "rho": 0.0, "days_to_expiry": 0,
            "spot": spot, "iv": 0.0,
            "intrinsic_only": True,
        }

    spot = historical_spot(symbol, as_of, bars_provider)
    if spot is None or spot <= 0:
        return None

    iv = iv_override
    if iv is None:
        iv = historical_iv_approximation(symbol, as_of,
                                            bars_provider=bars_provider)
    if iv is None or iv <= 0:
        return None

    g = compute_greeks(
        spot=spot, strike=strike, days_to_expiry=days,
        iv=iv, is_call=is_call, risk_free_rate=risk_free_rate,
    )
    if g is None:
        return None
    g["spot"] = spot
    g["iv"] = iv
    g["days_to_expiry"] = days
    return g


def _default_bars_provider(symbol: str, as_of: _date,
                              lookback_days: int):
    """Default bars provider — Alpaca-first via market_data.get_bars.

    Since get_bars returns a trailing window from "now," we ask for
    enough bars to comfortably include the as_of date. Caller filters
    by as_of inside historical_iv_approximation / historical_spot.
    """
    from market_data import get_bars
    today = _date.today()
    days_back = max((today - as_of).days, 0) + lookback_days + 10
    return get_bars(symbol, limit=days_back)
