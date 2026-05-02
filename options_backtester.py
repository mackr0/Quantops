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


# ---------------------------------------------------------------------------
# Layer 2 — single-leg strategy simulator
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    """Result of simulating one option position open → close."""
    symbol: str
    strategy: str           # 'long_call' | 'long_put' | 'covered_call' | etc.
    entry_date: _date
    exit_date: _date
    strike: float
    expiry: _date
    is_call: bool
    qty: int                # contracts (positive)
    side: str               # 'buy' or 'sell'
    entry_premium: float    # per-share
    exit_value: float       # per-share at exit
    pnl_dollars: float      # signed P&L on the position
    exit_reason: str        # 'expiry_otm' | 'expiry_itm' | 'time_stop' | 'profit_target' | 'stop_loss'
    days_held: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "strategy": self.strategy,
            "entry_date": self.entry_date.isoformat(),
            "exit_date": self.exit_date.isoformat(),
            "strike": self.strike, "expiry": self.expiry.isoformat(),
            "is_call": self.is_call, "qty": self.qty, "side": self.side,
            "entry_premium": self.entry_premium,
            "exit_value": self.exit_value,
            "pnl_dollars": self.pnl_dollars,
            "exit_reason": self.exit_reason,
            "days_held": self.days_held,
        }


def simulate_single_leg(
    symbol: str,
    entry_date: _date,
    strike: float,
    expiry: _date,
    is_call: bool,
    side: str = "buy",
    qty: int = 1,
    profit_target_pct: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    time_stop_days_before_expiry: int = 0,
    bars_provider=None,
    iv_override: Optional[float] = None,
) -> Optional[BacktestTrade]:
    """Simulate one option position from entry through close.

    Walks the historical period day by day. Closes on whichever fires
    first:
      - profit_target_pct hit (e.g., +50% on a long = exit early)
      - stop_loss_pct hit (e.g., -50% on a long)
      - time_stop_days_before_expiry days from expiry
      - expiry day reached (settle at intrinsic)

    Args:
        side: 'buy' (long premium — pay entry, receive exit) or 'sell'
            (short premium — receive entry, pay exit).
        profit_target_pct / stop_loss_pct: as fraction. For LONG (buy),
            +0.50 means "exit at 50% profit"; -0.50 means "exit at 50%
            loss." For SHORT (sell), the math inverts — long_call
            +50% means we want the option to APPRECIATE; short_call
            +50% means we want the option to LOSE 50% of value (we
            sold high, buy back lower).
        time_stop_days_before_expiry: 0 = hold to expiry; 5 = exit
            5 days before expiry to avoid gamma risk.
        iv_override: pin IV across the whole simulation (testing).

    Returns BacktestTrade with full trade lifecycle, or None if entry
    pricing fails (no historical data for entry_date).
    """
    if entry_date >= expiry:
        return None

    # 1. Price entry
    entry_pricing = price_option_at_date(
        symbol, entry_date, strike, expiry, is_call,
        iv_override=iv_override, bars_provider=bars_provider,
    )
    if entry_pricing is None or entry_pricing.get("price", 0) <= 0:
        return None
    entry_premium = float(entry_pricing["price"])

    multiplier = qty * 100  # 100 shares per contract
    # P&L sign convention:
    #   long (buy): pnl = (exit - entry) * multiplier
    #   short (sell): pnl = (entry - exit) * multiplier
    is_long = (side == "buy")

    # 2. Walk forward day-by-day
    cursor = entry_date + timedelta(days=1)
    final_exit_date = expiry
    final_exit_value = 0.0
    exit_reason = "expiry"

    while cursor <= expiry:
        # Compute current option value
        days_to_expiry = (expiry - cursor).days

        if days_to_expiry <= 0:
            # Expiry day — settle at intrinsic
            spot = historical_spot(symbol, cursor, bars_provider)
            if spot is None:
                cursor += timedelta(days=1)
                continue
            intrinsic = (max(0.0, spot - strike) if is_call
                         else max(0.0, strike - spot))
            final_exit_date = cursor
            final_exit_value = intrinsic
            exit_reason = ("expiry_itm" if intrinsic > 0
                           else "expiry_otm")
            break

        # Time-stop check
        if (time_stop_days_before_expiry > 0
                and days_to_expiry <= time_stop_days_before_expiry):
            current_pricing = price_option_at_date(
                symbol, cursor, strike, expiry, is_call,
                iv_override=iv_override, bars_provider=bars_provider,
            )
            if current_pricing:
                final_exit_date = cursor
                final_exit_value = float(current_pricing["price"])
                exit_reason = "time_stop"
                break

        # Profit/stop targets
        if profit_target_pct is not None or stop_loss_pct is not None:
            current_pricing = price_option_at_date(
                symbol, cursor, strike, expiry, is_call,
                iv_override=iv_override, bars_provider=bars_provider,
            )
            if current_pricing:
                cur_price = float(current_pricing["price"])
                if is_long:
                    pnl_pct = ((cur_price - entry_premium) /
                               entry_premium) if entry_premium > 0 else 0
                else:
                    pnl_pct = ((entry_premium - cur_price) /
                               entry_premium) if entry_premium > 0 else 0
                if (profit_target_pct is not None
                        and pnl_pct >= profit_target_pct):
                    final_exit_date = cursor
                    final_exit_value = cur_price
                    exit_reason = "profit_target"
                    break
                if (stop_loss_pct is not None
                        and pnl_pct <= -abs(stop_loss_pct)):
                    final_exit_date = cursor
                    final_exit_value = cur_price
                    exit_reason = "stop_loss"
                    break

        cursor += timedelta(days=1)
    else:
        # Loop completed without break — should be rare since expiry
        # check fires. Set conservative defaults.
        final_exit_date = expiry
        final_exit_value = 0.0
        exit_reason = "expiry"

    # P&L calculation
    if is_long:
        pnl = (final_exit_value - entry_premium) * multiplier
    else:
        pnl = (entry_premium - final_exit_value) * multiplier

    days_held = max((final_exit_date - entry_date).days, 0)

    strategy_name = ("long_call" if is_long and is_call
                      else "long_put" if is_long
                      else "short_call" if is_call
                      else "short_put")

    return BacktestTrade(
        symbol=symbol, strategy=strategy_name,
        entry_date=entry_date, exit_date=final_exit_date,
        strike=strike, expiry=expiry, is_call=is_call,
        qty=qty, side=side,
        entry_premium=entry_premium, exit_value=final_exit_value,
        pnl_dollars=round(pnl, 2),
        exit_reason=exit_reason, days_held=days_held,
    )
