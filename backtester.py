"""Backtesting engine for evaluating strategies on historical data."""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from market_data import get_bars_daterange


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
