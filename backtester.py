"""Backtesting engine for evaluating strategies on historical data."""

import math
import random
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from market_data import get_bars_daterange, add_indicators

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level caches for yfinance downloads (24-hour TTL)
# ---------------------------------------------------------------------------
_data_cache: Dict[str, dict] = {}
_CACHE_TTL = 86400  # 24 hours in seconds

# Per-symbol cache shared by _fetch_yf_history. Keyed on symbol only; we
# always fetch the maximum-needed window and slice down for shorter requests.
# This is critical for validation runs where one strategy triggers 7
# backtests with different `days` arguments — without this cache each
# backtest re-downloads every symbol from yfinance.
_symbol_cache: Dict[str, dict] = {}
_SYMBOL_CACHE_MAX_DAYS = 720   # always fetch this much so any gate is served


@dataclass
class BacktestResult:
    """Container for backtest output."""
    trades: List[Dict]
    equity_curve: pd.Series
    metrics: Dict
    config: Dict


def backtest(
    symbol: str,
    strategy_fn: Callable,
    days: int = 365,
    initial_capital: float = 100_000,
    position_size_pct: float = 0.05,
) -> BacktestResult:
    """
    Walk-forward backtest: fetch historical bars, iterate day by day,
    call strategy_fn with a df parameter containing only data up to that
    point, and track entries/exits/equity.

    Args:
        symbol: Ticker symbol to backtest.
        strategy_fn: Strategy function with signature fn(symbol, ..., df=None).
        days: Number of calendar days of history to test over.
        initial_capital: Starting cash balance.
        position_size_pct: Fraction of equity to risk per trade.

    Returns:
        BacktestResult with trades, equity curve, metrics, and config.
    """
    end_date = datetime.now()
    # Fetch extra history so indicators have a warm-up window
    warmup_days = 100
    start_date = end_date - timedelta(days=days + warmup_days)

    df = get_bars_daterange(
        symbol,
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
    )

    if df.empty:
        raise ValueError(f"No historical data returned for {symbol}")

    # Minimum rows needed before we start generating signals
    min_rows = warmup_days

    cash = initial_capital
    position_qty = 0
    position_entry_price = 0.0
    position_entry_date = None

    trades: List[Dict] = []
    equity_values: List[float] = []
    equity_dates: List = []

    for i in range(min_rows, len(df)):
        # Slice: only data up to and including the current bar
        window = df.iloc[: i + 1].copy()
        current_bar = df.iloc[i]
        current_price = float(current_bar["close"])
        current_date = df.index[i]

        # Call the strategy with the windowed DataFrame
        signal = strategy_fn(symbol, df=window)

        signal_action = signal.get("signal", "HOLD")

        # Determine effective action
        should_buy = signal_action in ("BUY", "STRONG_BUY", "WEAK_BUY")
        should_sell = signal_action in ("SELL", "STRONG_SELL", "WEAK_SELL")

        # Execute trades
        if should_buy and position_qty == 0:
            # Open a long position
            equity = cash  # no position yet
            trade_value = equity * position_size_pct
            shares = int(trade_value // current_price)
            if shares > 0:
                cost = shares * current_price
                cash -= cost
                position_qty = shares
                position_entry_price = current_price
                position_entry_date = current_date

        elif should_sell and position_qty > 0:
            # Close the position
            proceeds = position_qty * current_price
            pnl = proceeds - (position_qty * position_entry_price)
            cash += proceeds

            holding_days = 0
            if position_entry_date is not None:
                holding_days = (current_date - position_entry_date).days

            trades.append({
                "symbol": symbol,
                "entry_date": position_entry_date,
                "exit_date": current_date,
                "entry_price": position_entry_price,
                "exit_price": current_price,
                "qty": position_qty,
                "pnl": pnl,
                "pnl_pct": pnl / (position_qty * position_entry_price) * 100,
                "holding_days": holding_days,
                "signal": signal_action,
                "reason": signal.get("reason", ""),
            })

            position_qty = 0
            position_entry_price = 0.0
            position_entry_date = None

        # Record equity (cash + market value of open position)
        mark_to_market = position_qty * current_price
        equity_values.append(cash + mark_to_market)
        equity_dates.append(current_date)

    # If still holding a position at end, close it for accounting
    if position_qty > 0:
        final_price = float(df.iloc[-1]["close"])
        proceeds = position_qty * final_price
        pnl = proceeds - (position_qty * position_entry_price)
        cash += proceeds

        holding_days = 0
        if position_entry_date is not None:
            holding_days = (df.index[-1] - position_entry_date).days

        trades.append({
            "symbol": symbol,
            "entry_date": position_entry_date,
            "exit_date": df.index[-1],
            "entry_price": position_entry_price,
            "exit_price": final_price,
            "qty": position_qty,
            "pnl": pnl,
            "pnl_pct": pnl / (position_qty * position_entry_price) * 100,
            "holding_days": holding_days,
            "signal": "CLOSE (end of backtest)",
            "reason": "Position closed at end of backtest period",
        })

        position_qty = 0
        # Update final equity
        if equity_values:
            equity_values[-1] = cash

    equity_curve = pd.Series(equity_values, index=equity_dates, name="equity")
    metrics = calculate_metrics(trades, equity_curve, initial_capital)

    config = {
        "symbol": symbol,
        "strategy": strategy_fn.__name__,
        "days": days,
        "initial_capital": initial_capital,
        "position_size_pct": position_size_pct,
        "start_date": equity_dates[0] if equity_dates else None,
        "end_date": equity_dates[-1] if equity_dates else None,
    }

    return BacktestResult(
        trades=trades,
        equity_curve=equity_curve,
        metrics=metrics,
        config=config,
    )


def calculate_metrics(
    trades: List[Dict],
    equity_curve: pd.Series,
    initial_capital: float,
) -> Dict:
    """
    Compute performance metrics from backtest results.

    Returns dict with: total_return, annualized_return, sharpe_ratio,
    sortino_ratio, max_drawdown, win_rate, profit_factor, avg_trade_pnl,
    num_trades, avg_holding_days.
    """
    # --- Return metrics ---
    final_equity = equity_curve.iloc[-1] if len(equity_curve) > 0 else initial_capital
    total_return = (final_equity - initial_capital) / initial_capital * 100

    trading_days = len(equity_curve)
    years = trading_days / 252 if trading_days > 0 else 1
    annualized_return = ((final_equity / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0.0

    # --- Daily returns for Sharpe / Sortino ---
    daily_returns = equity_curve.pct_change().dropna()

    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe_ratio = (daily_returns.mean() / daily_returns.std()) * math.sqrt(252)
    else:
        sharpe_ratio = 0.0

    downside_returns = daily_returns[daily_returns < 0]
    if len(downside_returns) > 1 and downside_returns.std() > 0:
        sortino_ratio = (daily_returns.mean() / downside_returns.std()) * math.sqrt(252)
    else:
        sortino_ratio = 0.0

    # --- Max drawdown ---
    if len(equity_curve) > 0:
        cumulative_max = equity_curve.cummax()
        drawdowns = (equity_curve - cumulative_max) / cumulative_max * 100
        max_drawdown = float(drawdowns.min())
    else:
        max_drawdown = 0.0

    # --- Trade-level metrics ---
    num_trades = len(trades)
    if num_trades > 0:
        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate = len(wins) / num_trades * 100
        avg_trade_pnl = sum(pnls) / num_trades

        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        avg_holding_days = sum(t.get("holding_days", 0) for t in trades) / num_trades
    else:
        win_rate = 0.0
        avg_trade_pnl = 0.0
        profit_factor = 0.0
        avg_holding_days = 0.0

    return {
        "total_return": round(total_return, 2),
        "annualized_return": round(annualized_return, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "sortino_ratio": round(sortino_ratio, 2),
        "max_drawdown": round(max_drawdown, 2),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_trade_pnl": round(avg_trade_pnl, 2),
        "num_trades": num_trades,
        "avg_holding_days": round(avg_holding_days, 1),
    }


def print_backtest_report(result: BacktestResult) -> None:
    """Pretty-print backtest metrics and summary to the terminal."""
    cfg = result.config
    m = result.metrics

    print("\n" + "=" * 60)
    print("  BACKTEST REPORT")
    print("=" * 60)

    print(f"\n  Symbol:           {cfg.get('symbol')}")
    print(f"  Strategy:         {cfg.get('strategy')}")
    print(f"  Period:           {cfg.get('start_date')} -> {cfg.get('end_date')}")
    print(f"  Initial Capital:  ${cfg.get('initial_capital', 0):,.2f}")
    print(f"  Position Size:    {cfg.get('position_size_pct', 0) * 100:.1f}%")

    print("\n" + "-" * 60)
    print("  PERFORMANCE METRICS")
    print("-" * 60)

    print(f"  Total Return:       {m['total_return']:+.2f}%")
    print(f"  Annualized Return:  {m['annualized_return']:+.2f}%")
    print(f"  Sharpe Ratio:       {m['sharpe_ratio']:.2f}")
    print(f"  Sortino Ratio:      {m['sortino_ratio']:.2f}")
    print(f"  Max Drawdown:       {m['max_drawdown']:.2f}%")

    print("\n" + "-" * 60)
    print("  TRADE STATISTICS")
    print("-" * 60)

    print(f"  Number of Trades:   {m['num_trades']}")
    print(f"  Win Rate:           {m['win_rate']:.1f}%")
    print(f"  Profit Factor:      {m['profit_factor']:.2f}")
    print(f"  Avg Trade P&L:      ${m['avg_trade_pnl']:+,.2f}")
    print(f"  Avg Holding Days:   {m['avg_holding_days']:.1f}")

    if result.trades:
        print("\n" + "-" * 60)
        print("  RECENT TRADES (last 10)")
        print("-" * 60)
        for trade in result.trades[-10:]:
            pnl_str = f"${trade['pnl']:+,.2f}"
            pct_str = f"({trade['pnl_pct']:+.1f}%)"
            entry = str(trade.get("entry_date", ""))[:10]
            exit_ = str(trade.get("exit_date", ""))[:10]
            print(f"  {entry} -> {exit_}  |  {trade['qty']} shares  |  "
                  f"${trade['entry_price']:.2f} -> ${trade['exit_price']:.2f}  |  "
                  f"{pnl_str} {pct_str}")

    print("\n" + "=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Strategy-router backtest (Feature 6)
# ---------------------------------------------------------------------------

def _calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate the latest ATR from a DataFrame with high/low/close columns."""
    if len(df) < period + 1:
        return 0.0

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean()
    latest = atr.iloc[-1]
    return float(latest) if pd.notna(latest) else 0.0


def _fetch_yf_history(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Fetch historical daily bars from yfinance, with per-symbol caching.

    We always download the maximum window (_SYMBOL_CACHE_MAX_DAYS) and slice
    down to the caller's requested `days`. This means a single validation run
    triggering 7 backtests with different `days` values only downloads each
    symbol ONCE, not 7 times. 24-hour TTL on cache entries.

    Returns DataFrame with columns: open, high, low, close, volume,
    indexed by datetime. Returns None on failure.
    """
    now = time.time()
    cached = _symbol_cache.get(symbol)
    if cached and (now - cached["ts"]) < _CACHE_TTL and cached["data"] is not None:
        full_df = cached["data"]
    else:
        full_df = _download_symbol(symbol, _SYMBOL_CACHE_MAX_DAYS)
        _symbol_cache[symbol] = {"data": full_df, "ts": now}

    if full_df is None or full_df.empty:
        return None

    # Slice to requested window (latest `days` rows after normalization)
    needed_rows = days + 60   # buffer for weekends/warmup (caller handles)
    if len(full_df) > needed_rows:
        return full_df.iloc[-needed_rows:].copy()
    return full_df.copy()


def _download_symbol(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Download historical bars via Alpaca (primary) or yfinance (crypto fallback)."""
    try:
        from market_data import get_bars_daterange
        end = datetime.now()
        start = end - timedelta(days=days + 10)
        df = get_bars_daterange(
            symbol,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
        )
        if df is None or df.empty:
            return None

        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                return None

        return df

    except Exception as exc:
        logger.debug("Bar fetch failed for %s: %s", symbol, exc)
        return None


def backtest_strategy(
    market_type: str,
    days: int = 180,
    initial_capital: float = 10_000,
    sample_size: int = 30,
    atr_sl_mult: float = 2.0,
    atr_tp_mult: float = 3.0,
    symbols: Optional[List[str]] = None,
    signal_fn: Optional[Callable] = None,
) -> Dict:
    """Backtest a market-specific strategy against historical data.

    Uses the strategy_router to run the correct strategy engine for the
    given market type, then simulates entries and ATR-based exits.

    Args:
        market_type: "micro", "small", "midcap", "largecap", "crypto"
        days: Number of trading days to test.
        initial_capital: Starting capital.
        sample_size: Number of symbols to sample from the universe.
        atr_sl_mult: ATR multiplier for stop-loss (default 2x).
        atr_tp_mult: ATR multiplier for take-profit (default 3x).
        symbols: Optional explicit symbol list. When provided, overrides the
            random sampling — used by validate_strategy() so every gate in a
            single validation run tests the SAME symbols. This lets the
            per-symbol cache serve repeat requests instantly.

    Returns:
        dict with: total_return_pct, win_rate, max_drawdown_pct, sharpe_ratio,
        num_trades, avg_hold_days, best_trade, worst_trade, trades, config.
    """
    from segments import get_segment
    from strategy_router import run_strategy

    segment = get_segment(market_type)
    universe = segment.get("universe", [])

    if not universe:
        return {"error": f"No universe found for market type: {market_type}"}

    if symbols is not None:
        # Respect caller's symbol list (used by validation runs for cache
        # consistency across gates)
        pass
    elif len(universe) > sample_size:
        symbols = random.sample(universe, sample_size)
    else:
        symbols = list(universe)

    print(f"\nBacktesting {market_type} strategy on {len(symbols)} symbols over {days} days...")
    print(f"  ATR stops: SL={atr_sl_mult}x, TP={atr_tp_mult}x")
    print(f"  Initial capital: ${initial_capital:,.2f}\n")

    # Warmup: extra days for indicators
    warmup_days = 50
    total_fetch_days = days + warmup_days + 30  # buffer for weekends/holidays

    all_trades: List[Dict] = []
    equity = initial_capital
    daily_equity: List[float] = [initial_capital]
    symbols_tested = 0
    symbols_skipped = 0

    for idx, symbol in enumerate(symbols):
        print(f"  [{idx + 1}/{len(symbols)}] {symbol}...", end=" ", flush=True)

        df = _fetch_yf_history(symbol, total_fetch_days)
        if df is None or len(df) < warmup_days + 20:
            print("skipped (insufficient data)")
            symbols_skipped += 1
            continue

        # OPTIMIZATION: precompute all indicators once for the full history.
        # Strategy engines check `if "rsi" not in df.columns` and skip
        # add_indicators() when indicators are already present, so computing
        # once up-front avoids redoing ~30 indicators on every day's window.
        # This is the single biggest speedup in the backtest loop.
        try:
            from market_data import add_indicators
            df = add_indicators(df)
        except Exception as exc:
            logger.warning("Failed to precompute indicators for %s: %s", symbol, exc)

        symbols_tested += 1

        # Walk forward day by day
        position_open = False
        entry_price = 0.0
        entry_date = None
        stop_loss = 0.0
        take_profit = 0.0
        symbol_trades = 0

        for i in range(warmup_days, len(df)):
            window = df.iloc[:i + 1]   # view, not copy — strategy doesn't mutate
            current_bar = df.iloc[i]
            current_price = float(current_bar["close"])
            current_high = float(current_bar["high"])
            current_low = float(current_bar["low"])
            current_date = df.index[i]

            if position_open:
                # Check stops using high/low of the bar
                hit_sl = current_low <= stop_loss
                hit_tp = current_high >= take_profit

                if hit_sl or hit_tp:
                    if hit_sl:
                        exit_price = stop_loss
                        exit_reason = "stop_loss"
                    else:
                        exit_price = take_profit
                        exit_reason = "take_profit"

                    pnl = exit_price - entry_price
                    pnl_pct = (pnl / entry_price) * 100
                    hold_days = (current_date - entry_date).days if entry_date else 0

                    all_trades.append({
                        "symbol": symbol,
                        "entry_date": str(entry_date)[:10] if entry_date else "",
                        "exit_date": str(current_date)[:10],
                        "entry_price": round(entry_price, 4),
                        "exit_price": round(exit_price, 4),
                        "pnl": round(pnl, 4),
                        "pnl_pct": round(pnl_pct, 2),
                        "holding_days": hold_days,
                        "exit_reason": exit_reason,
                    })

                    equity += pnl * (initial_capital * 0.10 / entry_price)  # ~10% position
                    daily_equity.append(equity)
                    position_open = False
                    symbol_trades += 1
                    continue

            if not position_open:
                # Run strategy to check for entry signal
                try:
                    if signal_fn is not None:
                        signal = signal_fn(symbol, df=window) or {"signal": "HOLD"}
                    else:
                        signal = run_strategy(symbol, market_type, df=window, params=None)
                    action = signal.get("signal", "HOLD")

                    if action in ("BUY", "STRONG_BUY"):
                        entry_price = current_price
                        entry_date = current_date
                        position_open = True

                        # Calculate ATR-based stops
                        atr = _calculate_atr(window, period=14)
                        if atr > 0:
                            stop_loss = entry_price - (atr * atr_sl_mult)
                            take_profit = entry_price + (atr * atr_tp_mult)
                        else:
                            # Fallback: fixed 3%/6% stops
                            stop_loss = entry_price * 0.97
                            take_profit = entry_price * 1.06
                except Exception:
                    pass  # Strategy error -- skip this bar

            daily_equity.append(equity)

        # Close any open position at the end
        if position_open:
            final_price = float(df.iloc[-1]["close"])
            pnl = final_price - entry_price
            pnl_pct = (pnl / entry_price) * 100
            hold_days = (df.index[-1] - entry_date).days if entry_date else 0

            all_trades.append({
                "symbol": symbol,
                "entry_date": str(entry_date)[:10] if entry_date else "",
                "exit_date": str(df.index[-1])[:10],
                "entry_price": round(entry_price, 4),
                "exit_price": round(final_price, 4),
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 2),
                "holding_days": hold_days,
                "exit_reason": "end_of_backtest",
            })

            equity += pnl * (initial_capital * 0.10 / entry_price)
            daily_equity.append(equity)

        print(f"{symbol_trades} trades")

    # --- Calculate aggregate metrics ---
    total_return_pct = ((equity - initial_capital) / initial_capital) * 100

    num_trades = len(all_trades)
    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    win_rate = (len(wins) / num_trades * 100) if num_trades > 0 else 0.0

    # Max drawdown from equity curve
    eq_series = pd.Series(daily_equity)
    peak = eq_series.cummax()
    drawdown = (eq_series - peak) / peak * 100
    max_drawdown_pct = float(drawdown.min()) if len(drawdown) > 0 else 0.0

    # Sharpe ratio from daily equity changes
    eq_returns = eq_series.pct_change().dropna()
    if len(eq_returns) > 1 and eq_returns.std() > 0:
        sharpe_ratio = (eq_returns.mean() / eq_returns.std()) * math.sqrt(252)
    else:
        sharpe_ratio = 0.0

    # Average holding days
    avg_hold_days = (
        sum(t["holding_days"] for t in all_trades) / num_trades
        if num_trades > 0 else 0.0
    )

    # Best and worst trades
    best_trade = max(all_trades, key=lambda t: t["pnl_pct"]) if all_trades else None
    worst_trade = min(all_trades, key=lambda t: t["pnl_pct"]) if all_trades else None

    return {
        "market_type": market_type,
        "days": days,
        "initial_capital": initial_capital,
        "final_equity": round(equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "win_rate": round(win_rate, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "num_trades": num_trades,
        "avg_hold_days": round(avg_hold_days, 1),
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "symbols_tested": symbols_tested,
        "symbols_skipped": symbols_skipped,
        "trades": all_trades,
    }


def print_strategy_backtest_report(results: Dict) -> None:
    """Print a formatted backtest report for a strategy backtest."""
    print("\n" + "=" * 65)
    print("  STRATEGY BACKTEST REPORT")
    print("=" * 65)

    print(f"\n  Market Type:        {results.get('market_type', '?')}")
    print(f"  Period:             {results.get('days', '?')} trading days")
    print(f"  Symbols Tested:     {results.get('symbols_tested', 0)} "
          f"(skipped {results.get('symbols_skipped', 0)})")
    print(f"  Initial Capital:    ${results.get('initial_capital', 0):,.2f}")
    print(f"  Final Equity:       ${results.get('final_equity', 0):,.2f}")

    print("\n" + "-" * 65)
    print("  PERFORMANCE")
    print("-" * 65)

    total_ret = results.get("total_return_pct", 0)
    ret_color = "+" if total_ret >= 0 else ""
    print(f"  Total Return:       {ret_color}{total_ret:.2f}%")
    print(f"  Sharpe Ratio:       {results.get('sharpe_ratio', 0):.2f}")
    print(f"  Max Drawdown:       {results.get('max_drawdown_pct', 0):.2f}%")

    print("\n" + "-" * 65)
    print("  TRADE STATISTICS")
    print("-" * 65)

    print(f"  Total Trades:       {results.get('num_trades', 0)}")
    print(f"  Win Rate:           {results.get('win_rate', 0):.1f}%")
    print(f"  Avg Holding Days:   {results.get('avg_hold_days', 0):.1f}")

    best = results.get("best_trade")
    worst = results.get("worst_trade")
    if best:
        print(f"\n  Best Trade:         {best['symbol']} {best['pnl_pct']:+.2f}% "
              f"({best.get('entry_date', '')} -> {best.get('exit_date', '')})")
    if worst:
        print(f"  Worst Trade:        {worst['symbol']} {worst['pnl_pct']:+.2f}% "
              f"({worst.get('entry_date', '')} -> {worst.get('exit_date', '')})")

    # Exit reason breakdown
    trades = results.get("trades", [])
    if trades:
        sl_count = sum(1 for t in trades if t.get("exit_reason") == "stop_loss")
        tp_count = sum(1 for t in trades if t.get("exit_reason") == "take_profit")
        eob_count = sum(1 for t in trades if t.get("exit_reason") == "end_of_backtest")
        print(f"\n  Exit Breakdown:")
        print(f"    Stop-Loss:        {sl_count}")
        print(f"    Take-Profit:      {tp_count}")
        print(f"    End of Backtest:  {eob_count}")

    # Show last 10 trades
    if trades:
        print("\n" + "-" * 65)
        print("  RECENT TRADES (last 10)")
        print("-" * 65)
        for t in trades[-10:]:
            pnl_str = f"{t['pnl_pct']:+.1f}%"
            print(f"  {t.get('symbol', '?'):8s} | "
                  f"{t.get('entry_date', ''):10s} -> {t.get('exit_date', ''):10s} | "
                  f"${t.get('entry_price', 0):>8.2f} -> ${t.get('exit_price', 0):>8.2f} | "
                  f"{pnl_str:>7s} | {t.get('exit_reason', '')}")

    print("\n" + "=" * 65 + "\n")


# ---------------------------------------------------------------------------
# "What If" parameterized backtester (Settings page)
# ---------------------------------------------------------------------------

def _fetch_universe_batch(market_type: str, days: int) -> Optional[pd.DataFrame]:
    """Fetch historical data for the full universe of a market type.

    Uses Alpaca via market_data.get_bars per symbol with module-level
    caching (24-hour TTL). Constructs a MultiIndex DataFrame matching the
    shape that downstream _extract_symbol_df expects.

    Returns:
        MultiIndex DataFrame grouped by ticker, or None on failure.
    """
    from market_data import get_bars
    from segments import get_segment

    cache_key = f"{market_type}_{days}"
    now = time.time()

    # Check cache
    if cache_key in _data_cache:
        entry = _data_cache[cache_key]
        if now - entry["ts"] < _CACHE_TTL and entry["data"] is not None:
            logger.debug("Cache hit for %s", cache_key)
            return entry["data"]

    segment = get_segment(market_type)
    universe = segment.get("universe", [])
    if not universe:
        return None

    limit = days + 60  # buffer for warmup + weekends/holidays

    try:
        logger.info("Fetching %d symbols for %s via Alpaca...",
                     len(universe), market_type)
        frames = {}
        for sym in universe:
            try:
                df = get_bars(sym, limit=limit)
                if df is not None and not df.empty:
                    # Capitalize columns for MultiIndex compat with _extract_symbol_df
                    df.columns = [c.capitalize() for c in df.columns]
                    frames[sym] = df
            except Exception:
                continue

        if not frames:
            return None

        # Build MultiIndex DataFrame: columns = (ticker, OHLCV)
        data = pd.concat(frames, axis=1)

        # Cache it
        _data_cache[cache_key] = {"data": data, "ts": now}
        return data

    except Exception as exc:
        logger.warning("Universe fetch failed for %s: %s", market_type, exc)
        return None


def _extract_symbol_df(batch_data: pd.DataFrame, symbol: str,
                       universe_size: int) -> Optional[pd.DataFrame]:
    """Extract a single symbol's OHLCV DataFrame from a batch download result.

    Handles both MultiIndex (multi-ticker) and single-level (single-ticker)
    column layouts returned by yfinance.
    """
    try:
        if universe_size == 1:
            # Single ticker: columns are just Price levels (Open, High, ...)
            df = batch_data.copy()
        else:
            # Multi-ticker: columns are (Ticker, Price)
            level0 = batch_data.columns.get_level_values(0)
            if symbol in level0:
                df = batch_data[symbol].copy()
            else:
                # Fallback: try yfinance-style symbol (BTC/USD -> BTC-USD)
                yf_sym = symbol.replace("/", "-")
                if yf_sym not in level0:
                    return None
                df = batch_data[yf_sym].copy()

        # Normalize column names to lowercase
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[-1].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
        else:
            df.columns = [str(c).lower() for c in df.columns]

        # Keep only OHLCV columns to avoid conflicts
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        if len(keep) < 5:
            return None
        df = df[keep].copy()

        # Drop rows with NaN close
        df = df.dropna(subset=["close"])

        # Ensure numeric types
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        if len(df) < 20:
            return None

        return df

    except Exception:
        return None


def backtest_with_params(market_type: str, params: dict, days: int = 90,
                         progress_callback: Optional[Callable] = None) -> dict:
    """Backtest a strategy with specific parameter overrides.

    Args:
        market_type: "micro", "small", "midcap", "largecap", "crypto"
        params: dict of all configurable parameters matching UserContext fields:
            stop_loss_pct, take_profit_pct, max_position_pct,
            use_atr_stops, atr_multiplier_sl, atr_multiplier_tp,
            ai_confidence_threshold, strategy_momentum_breakout,
            strategy_volume_spike, strategy_mean_reversion, strategy_gap_and_go,
            rsi_oversold, rsi_overbought, volume_surge_multiplier, etc.
        days: Number of trading days to backtest
        progress_callback: Optional callable(msg) to report progress updates.

    Returns:
        dict with: total_return_pct, win_rate, max_drawdown_pct, sharpe_ratio,
        num_trades, avg_hold_days, best_trade_pct, worst_trade_pct, trades list,
        symbols_tested
    """
    def _progress(msg):
        if progress_callback:
            progress_callback(msg)
    from segments import get_segment
    from strategy_router import run_strategy

    segment = get_segment(market_type)
    universe = segment.get("universe", [])

    if not universe:
        return {"error": f"No universe found for market type: {market_type}"}

    # Batch download the full universe
    _progress("Downloading historical data...")
    batch_data = _fetch_universe_batch(market_type, days)
    if batch_data is None:
        return {"error": "Failed to download market data"}

    # Extract params
    use_atr_stops = bool(params.get("use_atr_stops", True))
    atr_sl_mult = float(params.get("atr_multiplier_sl", 2.0))
    atr_tp_mult = float(params.get("atr_multiplier_tp", 3.0))
    fixed_sl_pct = float(params.get("stop_loss_pct", 0.03))
    fixed_tp_pct = float(params.get("take_profit_pct", 0.10))
    use_trailing = bool(params.get("use_trailing_stops", True))
    trailing_mult = float(params.get("trailing_atr_multiplier", 1.5))

    warmup_days = 50
    initial_capital = 10_000
    equity = initial_capital
    daily_equity: List[float] = [initial_capital]
    all_trades: List[Dict] = []
    symbols_tested = 0

    for sym_idx, symbol in enumerate(universe):
        _progress(f"Testing symbol {sym_idx + 1}/{len(universe)} ({symbol})...")
        df = _extract_symbol_df(batch_data, symbol, len(universe))
        if df is None or len(df) < warmup_days + 20:
            continue

        symbols_tested += 1

        # Pre-compute indicators ONCE for the full dataframe
        df_full = add_indicators(df.copy())

        # Walk forward day by day
        position_open = False
        entry_price = 0.0
        entry_date = None
        stop_loss = 0.0
        take_profit = 0.0
        trailing_high = 0.0

        for i in range(warmup_days, len(df_full)):
            window = df_full.iloc[:i + 1]
            current_bar = df_full.iloc[i]
            current_price = float(current_bar["close"])
            current_high = float(current_bar["high"])
            current_low = float(current_bar["low"])
            current_date = df_full.index[i]

            if position_open:
                # Update trailing high watermark
                if current_high > trailing_high:
                    trailing_high = current_high

                # Check ATR/fixed stops using high/low of the bar
                hit_sl = current_low <= stop_loss
                hit_tp = current_high >= take_profit

                # Trailing stop check
                hit_trailing = False
                if use_trailing and trailing_high > entry_price:
                    atr_val = _calculate_atr(window, period=14)
                    if atr_val > 0:
                        trail_stop = trailing_high - (atr_val * trailing_mult)
                        if current_low <= trail_stop and trail_stop > stop_loss:
                            hit_trailing = True
                            exit_price = trail_stop
                            exit_reason = "trailing_stop"

                if hit_sl or hit_tp or hit_trailing:
                    if not hit_trailing:
                        if hit_sl:
                            exit_price = stop_loss
                            exit_reason = "stop_loss"
                        else:
                            exit_price = take_profit
                            exit_reason = "take_profit"

                    # Apply slippage on exit (realistic fill)
                    exit_price = exit_price * (1 - 0.002)  # 0.2% slippage on sell

                    pnl = exit_price - entry_price
                    pnl_pct = (pnl / entry_price) * 100
                    hold_days = (current_date - entry_date).days if entry_date else 0

                    all_trades.append({
                        "symbol": symbol,
                        "entry_date": str(entry_date)[:10] if entry_date else "",
                        "exit_date": str(current_date)[:10],
                        "entry_price": round(entry_price, 4),
                        "exit_price": round(exit_price, 4),
                        "pnl": round(pnl, 4),
                        "pnl_pct": round(pnl_pct, 2),
                        "holding_days": hold_days,
                        "exit_reason": exit_reason,
                    })

                    equity += pnl * (initial_capital * 0.10 / entry_price)
                    daily_equity.append(equity)
                    position_open = False
                    continue

            if not position_open:
                # Run strategy to check for entry signal
                try:
                    signal = run_strategy(symbol, market_type, df=window, params=params)
                    action = signal.get("signal", "HOLD")

                    if action in ("BUY", "STRONG_BUY"):
                        # Apply slippage on entry (realistic fill price)
                        slippage_pct = 0.002  # 0.2% slippage
                        entry_price = current_price * (1 + slippage_pct)
                        entry_date = current_date
                        position_open = True
                        trailing_high = current_high

                        # Calculate stops
                        if use_atr_stops:
                            atr = _calculate_atr(window, period=14)
                            if atr > 0:
                                stop_loss = entry_price - (atr * atr_sl_mult)
                                take_profit = entry_price + (atr * atr_tp_mult)
                            else:
                                stop_loss = entry_price * (1 - fixed_sl_pct)
                                take_profit = entry_price * (1 + fixed_tp_pct)
                        else:
                            stop_loss = entry_price * (1 - fixed_sl_pct)
                            take_profit = entry_price * (1 + fixed_tp_pct)
                except Exception:
                    pass

                daily_equity.append(equity)

        # Close any open position at the end
        if position_open:
            final_price = float(df_full.iloc[-1]["close"])
            pnl = final_price - entry_price
            pnl_pct = (pnl / entry_price) * 100
            hold_days = (df_full.index[-1] - entry_date).days if entry_date else 0

            all_trades.append({
                "symbol": symbol,
                "entry_date": str(entry_date)[:10] if entry_date else "",
                "exit_date": str(df_full.index[-1])[:10],
                "entry_price": round(entry_price, 4),
                "exit_price": round(final_price, 4),
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 2),
                "holding_days": hold_days,
                "exit_reason": "end_of_backtest",
            })

            equity += pnl * (initial_capital * 0.10 / entry_price)
            daily_equity.append(equity)

    _progress("Calculating metrics...")

    # --- Calculate aggregate metrics ---
    total_return_pct = ((equity - initial_capital) / initial_capital) * 100

    num_trades = len(all_trades)
    wins = [t for t in all_trades if t["pnl"] > 0]
    win_rate = (len(wins) / num_trades * 100) if num_trades > 0 else 0.0

    # Max drawdown from equity curve
    eq_series = pd.Series(daily_equity)
    peak = eq_series.cummax()
    drawdown = (eq_series - peak) / peak * 100
    max_drawdown_pct = float(drawdown.min()) if len(drawdown) > 0 else 0.0

    # Sharpe ratio from daily equity changes
    eq_returns = eq_series.pct_change().dropna()
    if len(eq_returns) > 1 and eq_returns.std() > 0:
        sharpe_ratio = (eq_returns.mean() / eq_returns.std()) * math.sqrt(252)
    else:
        sharpe_ratio = 0.0

    # Average holding days
    avg_hold_days = (
        sum(t["holding_days"] for t in all_trades) / num_trades
        if num_trades > 0 else 0.0
    )

    # Best and worst trades by pct
    best_trade_pct = max((t["pnl_pct"] for t in all_trades), default=0.0)
    worst_trade_pct = min((t["pnl_pct"] for t in all_trades), default=0.0)

    return {
        "total_return_pct": round(total_return_pct, 2),
        "win_rate": round(win_rate, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "num_trades": num_trades,
        "avg_hold_days": round(avg_hold_days, 1),
        "best_trade_pct": round(best_trade_pct, 2),
        "worst_trade_pct": round(worst_trade_pct, 2),
        "symbols_tested": symbols_tested,
        "trades": all_trades,
    }


def backtest_comparison(market_type: str, current_params: dict,
                        new_params: dict, days: int = 90,
                        progress_callback: Optional[Callable] = None) -> dict:
    """Run two backtests and return comparison.

    Returns dict with current_results, new_results, and diff for each metric.
    """
    def _progress(msg):
        if progress_callback:
            progress_callback(msg)

    _progress("Running backtest with current settings...")
    current_results = backtest_with_params(market_type, current_params, days=days,
                                           progress_callback=progress_callback)
    _progress("Running backtest with proposed settings...")
    new_results = backtest_with_params(market_type, new_params, days=days,
                                       progress_callback=progress_callback)

    if "error" in current_results or "error" in new_results:
        return {
            "error": current_results.get("error") or new_results.get("error"),
        }

    # Calculate diffs for numeric metrics
    diff_keys = [
        "total_return_pct", "win_rate", "max_drawdown_pct",
        "sharpe_ratio", "num_trades", "best_trade_pct", "worst_trade_pct",
    ]
    diff = {}
    for key in diff_keys:
        c = current_results.get(key, 0) or 0
        n = new_results.get(key, 0) or 0
        diff[key] = round(n - c, 2)

    # Strip trade lists from response to keep payload small
    current_summary = {k: v for k, v in current_results.items() if k != "trades"}
    new_summary = {k: v for k, v in new_results.items() if k != "trades"}

    return {
        "current": current_summary,
        "proposed": new_summary,
        "diff": diff,
        "days": days,
        "symbols_tested": new_results.get("symbols_tested", 0),
    }
