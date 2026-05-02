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


# ---------------------------------------------------------------------------
# Layer 3 — multi-leg strategy simulator
# ---------------------------------------------------------------------------

@dataclass
class MultiLegBacktestTrade:
    """Result of simulating a multi-leg strategy open → close."""
    symbol: str
    strategy_name: str       # 'bull_call_spread' / 'iron_condor' / etc.
    entry_date: _date
    exit_date: _date
    expiry: _date
    qty: int                 # number of spreads
    legs: List[Dict[str, Any]]   # per-leg entry + exit details
    net_entry_premium: float     # signed: +debit / -credit (per spread)
    net_exit_value: float        # signed (per spread)
    pnl_dollars: float           # total signed P&L
    exit_reason: str
    days_held: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy_name": self.strategy_name,
            "entry_date": self.entry_date.isoformat(),
            "exit_date": self.exit_date.isoformat(),
            "expiry": self.expiry.isoformat(),
            "qty": self.qty,
            "net_entry_premium": self.net_entry_premium,
            "net_exit_value": self.net_exit_value,
            "pnl_dollars": self.pnl_dollars,
            "exit_reason": self.exit_reason,
            "days_held": self.days_held,
            "legs": self.legs,
        }


def simulate_multileg_strategy(
    strategy,                    # OptionStrategy from options_multileg
    entry_date: _date,
    profit_target_pct_of_max: Optional[float] = None,
    stop_loss_pct_of_max: Optional[float] = None,
    time_stop_days_before_expiry: int = 0,
    bars_provider=None,
    iv_override: Optional[float] = None,
) -> Optional[MultiLegBacktestTrade]:
    """Simulate a multi-leg strategy from entry through close.

    Args:
        strategy: an OptionStrategy from options_multileg (built by
            one of the BUILDERS — vertical, condor, butterfly,
            straddle, strangle, calendar, diagonal).
        entry_date: when to open the position.
        profit_target_pct_of_max: exit at this fraction of max profit.
            For credit spreads with max_gain=$X, 0.50 means "exit when
            we've captured 50% of $X."
        stop_loss_pct_of_max: exit at this fraction of max LOSS.
            For credit spreads with max_loss=$Y, 0.50 means "exit
            when we're down 50% of $Y."
        time_stop_days_before_expiry: 0 = hold to expiry; >0 = exit
            this many days before expiry.

    Returns MultiLegBacktestTrade or None on entry-pricing failure.
    """
    if not strategy.legs:
        return None
    # Assumes all legs share the same expiry (vertical / condor /
    # butterfly / straddle / strangle do; calendar / diagonal don't —
    # we use the LATEST expiry in the strategy as the simulation end).
    leg_expiries = [_date.fromisoformat(leg.expiry) for leg in strategy.legs]
    final_expiry = max(leg_expiries)
    underlying = strategy.underlying

    if entry_date >= final_expiry:
        return None

    # 1. Price each leg at entry. Cleaner per-leg accounting:
    #   - For each leg, store entry_premium (per share, unsigned).
    #   - At exit, store exit_value (per share, unsigned).
    #   - Per-leg P&L:
    #       buy:  (exit - entry) * 100 * qty
    #       sell: (entry - exit) * 100 * qty
    #   - Sum across legs = total spread P&L.
    # net_entry_premium_per_spread is kept for reporting only (signed
    # convention: + debit, - credit) but NOT used for P&L math.
    leg_entries: List[Dict[str, Any]] = []
    net_entry_premium_per_spread = 0.0

    for leg in strategy.legs:
        leg_expiry = _date.fromisoformat(leg.expiry)
        leg_pricing = price_option_at_date(
            underlying, entry_date, leg.strike, leg_expiry,
            is_call=(leg.right == "C"),
            iv_override=iv_override, bars_provider=bars_provider,
        )
        if leg_pricing is None or leg_pricing.get("price", 0) <= 0:
            return None
        leg_premium = float(leg_pricing["price"])
        signed_contribution = (leg_premium if leg.side == "buy"
                                else -leg_premium)
        net_entry_premium_per_spread += signed_contribution
        leg_entries.append({
            "leg_index": len(leg_entries),
            "occ_symbol": leg.occ_symbol,
            "strike": leg.strike, "right": leg.right,
            "side": leg.side, "qty": leg.qty,
            "entry_premium": leg_premium,
        })

    # max_gain / max_loss baselines: use the strategy's own values
    # when present, else compute roughly from net premium and width.
    max_gain = strategy.max_gain_per_contract
    max_loss = strategy.max_loss_per_contract
    if max_gain is None or max_loss is None:
        # Defined-risk fallback: approximate from spread width
        if strategy.spread_width_points > 0:
            net_dollars = abs(net_entry_premium_per_spread) * 100
            width_dollars = strategy.spread_width_points * 100
            if strategy.is_credit:
                max_gain = max_gain or net_dollars
                max_loss = max_loss or (width_dollars - net_dollars)
            else:
                max_loss = max_loss or net_dollars
                max_gain = max_gain or (width_dollars - net_dollars)

    # 2. Walk forward day-by-day, valuing the position
    cursor = entry_date + timedelta(days=1)
    final_exit_date = final_expiry
    final_net_exit_value_per_spread = 0.0
    exit_reason = "expiry"

    leg_currents: List[float] = []  # latest per-leg exit values

    def _per_leg_pnl_per_share(leg, entry_p, exit_v):
        """Per-leg P&L per share. 'buy': exit - entry; 'sell': entry - exit."""
        return (exit_v - entry_p) if leg.side == "buy" else (entry_p - exit_v)

    while cursor <= final_expiry:
        days_to_expiry = (final_expiry - cursor).days

        # Price each leg at this cursor
        all_legs_priceable = True
        cur_leg_values: List[float] = []
        for leg in strategy.legs:
            leg_expiry = _date.fromisoformat(leg.expiry)
            if cursor >= leg_expiry:
                spot = historical_spot(underlying, cursor, bars_provider)
                if spot is None:
                    all_legs_priceable = False
                    break
                intrinsic = (max(0.0, spot - leg.strike) if leg.right == "C"
                             else max(0.0, leg.strike - spot))
                leg_value = intrinsic
            else:
                leg_pricing = price_option_at_date(
                    underlying, cursor, leg.strike, leg_expiry,
                    is_call=(leg.right == "C"),
                    iv_override=iv_override, bars_provider=bars_provider,
                )
                if leg_pricing is None:
                    all_legs_priceable = False
                    break
                leg_value = float(leg_pricing["price"])
            cur_leg_values.append(leg_value)

        if not all_legs_priceable:
            cursor += timedelta(days=1)
            continue

        # Per-leg P&L sum (per share, then *100 for dollars per spread)
        pnl_per_share = sum(
            _per_leg_pnl_per_share(strategy.legs[i],
                                       leg_entries[i]["entry_premium"],
                                       cur_leg_values[i])
            for i in range(len(strategy.legs))
        )
        pnl_per_spread = pnl_per_share * 100

        # Time-stop check
        if (time_stop_days_before_expiry > 0
                and days_to_expiry <= time_stop_days_before_expiry):
            final_exit_date = cursor
            leg_currents = cur_leg_values
            exit_reason = "time_stop"
            break

        if (profit_target_pct_of_max is not None
                and max_gain is not None and max_gain > 0):
            if pnl_per_spread >= profit_target_pct_of_max * max_gain:
                final_exit_date = cursor
                leg_currents = cur_leg_values
                exit_reason = "profit_target"
                break

        if (stop_loss_pct_of_max is not None
                and max_loss is not None and max_loss > 0):
            if pnl_per_spread <= -stop_loss_pct_of_max * max_loss:
                final_exit_date = cursor
                leg_currents = cur_leg_values
                exit_reason = "stop_loss"
                break

        if days_to_expiry <= 0:
            final_exit_date = cursor
            leg_currents = cur_leg_values
            exit_reason = ("expiry_profit" if pnl_per_spread > 0
                            else "expiry_loss")
            break

        cursor += timedelta(days=1)

    # If loop never broke (shouldn't happen since expiry check fires),
    # use the last priceable values.
    if not leg_currents:
        leg_currents = cur_leg_values if 'cur_leg_values' in locals() else \
            [le["entry_premium"] for le in leg_entries]
        final_exit_date = final_expiry

    # Final P&L
    pnl_per_share_final = sum(
        _per_leg_pnl_per_share(strategy.legs[i],
                                   leg_entries[i]["entry_premium"],
                                   leg_currents[i])
        for i in range(len(strategy.legs))
    )
    pnl_per_spread = pnl_per_share_final * 100
    total_pnl = pnl_per_spread * strategy.qty
    # net_exit_value: signed per-spread net (for reporting; matches the
    # sign convention of net_entry_premium_per_spread)
    final_net_exit_value_per_spread = sum(
        v if leg.side == "buy" else -v
        for leg, v in zip(strategy.legs, leg_currents)
    )

    days_held = max((final_exit_date - entry_date).days, 0)

    # Augment legs with exit details
    for i, leg in enumerate(strategy.legs):
        leg_currents_i = leg_currents[i] if i < len(leg_currents) else 0.0
        leg_entries[i]["exit_value"] = leg_currents_i

    return MultiLegBacktestTrade(
        symbol=underlying,
        strategy_name=strategy.name,
        entry_date=entry_date,
        exit_date=final_exit_date,
        expiry=final_expiry,
        qty=strategy.qty,
        legs=leg_entries,
        net_entry_premium=round(net_entry_premium_per_spread, 4),
        net_exit_value=round(final_net_exit_value_per_spread, 4),
        pnl_dollars=round(total_pnl, 2),
        exit_reason=exit_reason,
        days_held=days_held,
    )


# ---------------------------------------------------------------------------
# Layer 4 — strategy orchestrator (replay entry rules across a period)
# ---------------------------------------------------------------------------

@dataclass
class BacktestSummary:
    """Summary stats for a strategy backtest over a historical period."""
    strategy_name: str
    symbol: str
    period_start: _date
    period_end: _date
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate_pct: float
    total_pnl_dollars: float
    avg_pnl_dollars: float
    best_trade_pnl: float
    worst_trade_pnl: float
    avg_days_held: float
    sharpe_proxy: float       # mean / stdev of trade returns
    trades: List[Dict[str, Any]]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
            "win_rate_pct": self.win_rate_pct,
            "total_pnl_dollars": self.total_pnl_dollars,
            "avg_pnl_dollars": self.avg_pnl_dollars,
            "best_trade_pnl": self.best_trade_pnl,
            "worst_trade_pnl": self.worst_trade_pnl,
            "avg_days_held": self.avg_days_held,
            "sharpe_proxy": self.sharpe_proxy,
            "trades": self.trades,
        }


def backtest_strategy_over_period(
    strategy_factory,
    symbol: str,
    period_start: _date,
    period_end: _date,
    entry_rule,
    cycle_days: int = 7,
    profit_target_pct_of_max: Optional[float] = None,
    stop_loss_pct_of_max: Optional[float] = None,
    time_stop_days_before_expiry: int = 0,
    bars_provider=None,
    iv_override: Optional[float] = None,
) -> BacktestSummary:
    """Replay entry rules across a historical period.

    Walks the period day by day at `cycle_days` cadence. At each
    decision point, calls `entry_rule(symbol, as_of)` → True/False to
    decide whether to open a trade. When True, calls
    `strategy_factory(as_of)` → an OptionStrategy, then simulates
    via simulate_multileg_strategy.

    Args:
        strategy_factory: callable(as_of) → OptionStrategy. Lets the
            caller decide strikes/expiry based on date (e.g. always
            ~5% OTM, ~30 days out).
        entry_rule: callable(symbol, as_of) → bool. Examples:
            "fire every cycle" — lambda s, d: True
            "only when IV rich" — uses historical_iv_approximation
            "only when stock above SMA50" — etc.
        cycle_days: how often to re-evaluate the entry rule. Daily =
            1; weekly = 7. Real options programs evaluate
            weekly/monthly, not daily.
        profit_target_pct_of_max / stop_loss_pct_of_max /
        time_stop_days_before_expiry: pass-through to
            simulate_multileg_strategy.
        bars_provider, iv_override: pass-through.

    Returns BacktestSummary with per-trade detail + aggregate stats.
    """
    import math as _math

    trades: List[Dict[str, Any]] = []
    cursor = period_start
    while cursor <= period_end:
        try:
            should_enter = entry_rule(symbol, cursor)
        except Exception as exc:
            logger.debug("entry_rule failed on %s: %s", cursor, exc)
            should_enter = False

        if should_enter:
            try:
                spec = strategy_factory(cursor)
            except Exception as exc:
                logger.debug("strategy_factory failed on %s: %s",
                             cursor, exc)
                spec = None
            if spec is not None:
                trade = simulate_multileg_strategy(
                    spec, cursor,
                    profit_target_pct_of_max=profit_target_pct_of_max,
                    stop_loss_pct_of_max=stop_loss_pct_of_max,
                    time_stop_days_before_expiry=time_stop_days_before_expiry,
                    bars_provider=bars_provider,
                    iv_override=iv_override,
                )
                if trade is not None:
                    trades.append(trade.as_dict())
        cursor += timedelta(days=cycle_days)

    # Aggregate stats
    n_trades = len(trades)
    if n_trades == 0:
        return BacktestSummary(
            strategy_name="(no trades)", symbol=symbol,
            period_start=period_start, period_end=period_end,
            n_trades=0, n_wins=0, n_losses=0, win_rate_pct=0.0,
            total_pnl_dollars=0.0, avg_pnl_dollars=0.0,
            best_trade_pnl=0.0, worst_trade_pnl=0.0,
            avg_days_held=0.0, sharpe_proxy=0.0, trades=[],
        )

    pnls = [t["pnl_dollars"] for t in trades]
    n_wins = sum(1 for p in pnls if p > 0)
    n_losses = sum(1 for p in pnls if p < 0)
    win_rate = (n_wins / n_trades * 100) if n_trades else 0.0
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / n_trades

    days_held_list = [t.get("days_held", 0) for t in trades]
    avg_days = sum(days_held_list) / n_trades if days_held_list else 0.0

    # Sharpe proxy: mean / stdev of per-trade $ P&L (trades-as-units,
    # not annualized)
    if n_trades >= 2:
        mean_pnl = avg_pnl
        var_pnl = sum((p - mean_pnl) ** 2 for p in pnls) / (n_trades - 1)
        stdev = _math.sqrt(var_pnl)
        sharpe = (mean_pnl / stdev) if stdev > 0 else 0.0
    else:
        sharpe = 0.0

    strategy_name = trades[0].get("strategy_name") or trades[0].get("strategy", "?")

    return BacktestSummary(
        strategy_name=strategy_name, symbol=symbol,
        period_start=period_start, period_end=period_end,
        n_trades=n_trades, n_wins=n_wins, n_losses=n_losses,
        win_rate_pct=round(win_rate, 1),
        total_pnl_dollars=round(total_pnl, 2),
        avg_pnl_dollars=round(avg_pnl, 2),
        best_trade_pnl=round(max(pnls), 2),
        worst_trade_pnl=round(min(pnls), 2),
        avg_days_held=round(avg_days, 1),
        sharpe_proxy=round(sharpe, 3),
        trades=trades,
    )
