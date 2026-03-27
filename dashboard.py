"""Rich terminal dashboard for Quantops.

All display functions gracefully fall back to plain print() if the rich
library is not installed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
    from rich import box

    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    console = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _color_for_signal(signal: str) -> str:
    """Map a signal string to a rich color name."""
    signal_upper = signal.upper()
    if "BUY" in signal_upper:
        return "green"
    if "SELL" in signal_upper:
        return "red"
    return "yellow"


def _color_for_pnl(value: float) -> str:
    if value > 0:
        return "green"
    if value < 0:
        return "red"
    return "white"


def _plain_table(headers: List[str], rows: List[List[str]], title: str = "") -> None:
    """Plain-text table fallback when rich is absent."""
    if title:
        print(f"\n--- {title} ---")
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    print(fmt.format(*headers))
    print("-" * (sum(col_widths) + 2 * (len(col_widths) - 1)))
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))
    print()


# ---------------------------------------------------------------------------
# 1. Portfolio Dashboard
# ---------------------------------------------------------------------------

def show_portfolio_dashboard(
    account_info: Dict[str, Any],
    positions: List[Dict[str, Any]],
    risk_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """Display account info, positions with P&L coloring, and optional risk summary."""

    if not RICH_AVAILABLE:
        print("\n=== PORTFOLIO DASHBOARD ===")
        print(f"  Equity:         ${account_info.get('equity', 0):>12,.2f}")
        print(f"  Buying Power:   ${account_info.get('buying_power', 0):>12,.2f}")
        print(f"  Cash:           ${account_info.get('cash', 0):>12,.2f}")
        print(f"  Portfolio Value: ${account_info.get('portfolio_value', 0):>12,.2f}")
        print(f"  Status:          {account_info.get('status', 'N/A')}")

        if positions:
            headers = ["Symbol", "Qty", "Avg Entry", "Current", "Mkt Value", "Unrealized P/L", "P/L %"]
            rows = []
            for p in positions:
                rows.append([
                    p.get("symbol", ""),
                    f"{p.get('qty', 0):.0f}",
                    f"${p.get('avg_entry_price', 0):.2f}",
                    f"${p.get('current_price', 0):.2f}",
                    f"${p.get('market_value', 0):,.2f}",
                    f"${p.get('unrealized_pl', 0):+,.2f}",
                    f"{p.get('unrealized_plpc', 0) * 100:+.2f}%",
                ])
            _plain_table(headers, rows, title="Positions")
        else:
            print("  No open positions.\n")

        if risk_summary:
            print("--- Risk Summary ---")
            for k, v in risk_summary.items():
                print(f"  {k}: {v}")
            print()
        return

    # --- Rich version ---
    acct_table = Table(title="Account", box=box.SIMPLE_HEAVY, show_header=False)
    acct_table.add_column("Field", style="bold cyan")
    acct_table.add_column("Value", justify="right")
    acct_table.add_row("Equity", f"${account_info.get('equity', 0):,.2f}")
    acct_table.add_row("Buying Power", f"${account_info.get('buying_power', 0):,.2f}")
    acct_table.add_row("Cash", f"${account_info.get('cash', 0):,.2f}")
    acct_table.add_row("Portfolio Value", f"${account_info.get('portfolio_value', 0):,.2f}")
    acct_table.add_row("Status", account_info.get("status", "N/A"))

    pos_table = Table(title="Positions", box=box.ROUNDED)
    pos_table.add_column("Symbol", style="bold")
    pos_table.add_column("Qty", justify="right")
    pos_table.add_column("Avg Entry", justify="right")
    pos_table.add_column("Current", justify="right")
    pos_table.add_column("Mkt Value", justify="right")
    pos_table.add_column("Unrealized P/L", justify="right")
    pos_table.add_column("P/L %", justify="right")

    if positions:
        for p in positions:
            pnl = p.get("unrealized_pl", 0)
            pnl_color = _color_for_pnl(pnl)
            pnl_pct = p.get("unrealized_plpc", 0) * 100
            pos_table.add_row(
                p.get("symbol", ""),
                f"{p.get('qty', 0):.0f}",
                f"${p.get('avg_entry_price', 0):.2f}",
                f"${p.get('current_price', 0):.2f}",
                f"${p.get('market_value', 0):,.2f}",
                f"[{pnl_color}]${pnl:+,.2f}[/{pnl_color}]",
                f"[{pnl_color}]{pnl_pct:+.2f}%[/{pnl_color}]",
            )
    else:
        pos_table.add_row("No open positions", "", "", "", "", "", "")

    panels = [Panel(acct_table, title="Account Overview", border_style="blue")]
    panels.append(Panel(pos_table, title="Open Positions", border_style="green"))

    if risk_summary:
        risk_table = Table(box=box.SIMPLE, show_header=False)
        risk_table.add_column("Metric", style="bold")
        risk_table.add_column("Value", justify="right")
        for k, v in risk_summary.items():
            risk_table.add_row(str(k), str(v))
        panels.append(Panel(risk_table, title="Risk Summary", border_style="red"))

    console.print()
    for p in panels:
        console.print(p)


# ---------------------------------------------------------------------------
# 2. Signals Dashboard
# ---------------------------------------------------------------------------

def show_signals_dashboard(signals: List[Dict[str, Any]]) -> None:
    """Display strategy signals with color coding."""

    if not RICH_AVAILABLE:
        headers = ["Symbol", "Signal", "Price", "Reason"]
        rows = []
        for s in signals:
            rows.append([
                s.get("symbol", ""),
                s.get("signal", "HOLD"),
                f"${s.get('price', 0):.2f}" if s.get("price") else "N/A",
                s.get("reason", ""),
            ])
        _plain_table(headers, rows, title="Strategy Signals")
        return

    table = Table(title="Strategy Signals", box=box.ROUNDED)
    table.add_column("Symbol", style="bold")
    table.add_column("Signal", justify="center")
    table.add_column("Price", justify="right")
    table.add_column("Reason")

    for s in signals:
        sig = s.get("signal", "HOLD")
        color = _color_for_signal(sig)
        price = f"${s.get('price', 0):.2f}" if s.get("price") else "N/A"
        table.add_row(
            s.get("symbol", ""),
            f"[bold {color}]{sig}[/bold {color}]",
            price,
            s.get("reason", ""),
        )

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# 3. Trade History
# ---------------------------------------------------------------------------

def show_trade_history(trades: List[Dict[str, Any]]) -> None:
    """Display a formatted trade history table."""

    if not RICH_AVAILABLE:
        if not trades:
            print("No trades to display.")
            return
        headers = ["Entry Date", "Exit Date", "Symbol", "Qty", "Entry $", "Exit $", "P&L", "P&L %", "Days"]
        rows = []
        for t in trades:
            rows.append([
                str(t.get("entry_date", ""))[:10],
                str(t.get("exit_date", ""))[:10],
                t.get("symbol", ""),
                str(t.get("qty", 0)),
                f"${t.get('entry_price', 0):.2f}",
                f"${t.get('exit_price', 0):.2f}",
                f"${t.get('pnl', 0):+,.2f}",
                f"{t.get('pnl_pct', 0):+.1f}%",
                str(t.get("holding_days", 0)),
            ])
        _plain_table(headers, rows, title="Trade History")
        return

    if not trades:
        console.print("[dim]No trades to display.[/dim]")
        return

    table = Table(title="Trade History", box=box.ROUNDED)
    table.add_column("Entry Date", style="dim")
    table.add_column("Exit Date", style="dim")
    table.add_column("Symbol", style="bold")
    table.add_column("Qty", justify="right")
    table.add_column("Entry $", justify="right")
    table.add_column("Exit $", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("P&L %", justify="right")
    table.add_column("Days", justify="right")

    for t in trades:
        pnl = t.get("pnl", 0)
        color = _color_for_pnl(pnl)
        table.add_row(
            str(t.get("entry_date", ""))[:10],
            str(t.get("exit_date", ""))[:10],
            t.get("symbol", ""),
            str(t.get("qty", 0)),
            f"${t.get('entry_price', 0):.2f}",
            f"${t.get('exit_price', 0):.2f}",
            f"[{color}]${pnl:+,.2f}[/{color}]",
            f"[{color}]{t.get('pnl_pct', 0):+.1f}%[/{color}]",
            str(t.get("holding_days", 0)),
        )

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# 4. Backtest Results
# ---------------------------------------------------------------------------

def show_backtest_results(result: Any) -> None:
    """Display backtest metrics from a BacktestResult object."""

    m = result.metrics
    cfg = result.config

    if not RICH_AVAILABLE:
        print("\n=== BACKTEST RESULTS ===")
        print(f"  Symbol:           {cfg.get('symbol')}")
        print(f"  Strategy:         {cfg.get('strategy')}")
        print(f"  Period:           {cfg.get('start_date')} -> {cfg.get('end_date')}")
        print(f"  Initial Capital:  ${cfg.get('initial_capital', 0):,.2f}")
        print()
        for k, v in m.items():
            label = k.replace("_", " ").title()
            print(f"  {label:<22} {v}")
        print()
        return

    # Config panel
    config_table = Table(box=box.SIMPLE, show_header=False)
    config_table.add_column("Field", style="bold cyan")
    config_table.add_column("Value")
    config_table.add_row("Symbol", str(cfg.get("symbol")))
    config_table.add_row("Strategy", str(cfg.get("strategy")))
    config_table.add_row("Period", f"{cfg.get('start_date')} -> {cfg.get('end_date')}")
    config_table.add_row("Initial Capital", f"${cfg.get('initial_capital', 0):,.2f}")
    config_table.add_row("Position Size", f"{cfg.get('position_size_pct', 0) * 100:.1f}%")

    # Metrics table
    metrics_table = Table(box=box.SIMPLE_HEAVY, title="Performance Metrics")
    metrics_table.add_column("Metric", style="bold")
    metrics_table.add_column("Value", justify="right")

    ret_color = _color_for_pnl(m.get("total_return", 0))
    metrics_table.add_row("Total Return", f"[{ret_color}]{m.get('total_return', 0):+.2f}%[/{ret_color}]")
    metrics_table.add_row("Annualized Return", f"[{ret_color}]{m.get('annualized_return', 0):+.2f}%[/{ret_color}]")
    metrics_table.add_row("Sharpe Ratio", f"{m.get('sharpe_ratio', 0):.2f}")
    metrics_table.add_row("Sortino Ratio", f"{m.get('sortino_ratio', 0):.2f}")
    dd_color = "red" if m.get("max_drawdown", 0) < -5 else "yellow"
    metrics_table.add_row("Max Drawdown", f"[{dd_color}]{m.get('max_drawdown', 0):.2f}%[/{dd_color}]")
    metrics_table.add_row("Win Rate", f"{m.get('win_rate', 0):.1f}%")
    metrics_table.add_row("Profit Factor", f"{m.get('profit_factor', 0):.2f}")
    pnl_color = _color_for_pnl(m.get("avg_trade_pnl", 0))
    metrics_table.add_row("Avg Trade P&L", f"[{pnl_color}]${m.get('avg_trade_pnl', 0):+,.2f}[/{pnl_color}]")
    metrics_table.add_row("Number of Trades", str(m.get("num_trades", 0)))
    metrics_table.add_row("Avg Holding Days", f"{m.get('avg_holding_days', 0):.1f}")

    console.print()
    console.print(Panel(config_table, title="Backtest Configuration", border_style="blue"))
    console.print(Panel(metrics_table, title="Results", border_style="green"))

    # Also show trade history if trades exist
    if result.trades:
        show_trade_history(result.trades[-10:])


# ---------------------------------------------------------------------------
# 5. AI Analysis
# ---------------------------------------------------------------------------

def show_ai_analysis(
    ai_result: Dict[str, Any],
    sentiment_result: Optional[Dict[str, Any]] = None,
) -> None:
    """Display Claude AI analysis with reasoning, confidence, and risk factors."""

    if not RICH_AVAILABLE:
        print("\n=== AI ANALYSIS ===")
        print(f"  Recommendation: {ai_result.get('recommendation', 'N/A')}")
        print(f"  Confidence:     {ai_result.get('confidence', 'N/A')}")
        print(f"  Reasoning:      {ai_result.get('reasoning', 'N/A')}")
        risk_factors = ai_result.get("risk_factors", [])
        if risk_factors:
            print("  Risk Factors:")
            for rf in risk_factors:
                print(f"    - {rf}")
        if sentiment_result:
            print(f"\n  Sentiment Score: {sentiment_result.get('score', 'N/A')}")
            print(f"  Sentiment:       {sentiment_result.get('sentiment', 'N/A')}")
            summary = sentiment_result.get("summary", "")
            if summary:
                print(f"  Summary:         {summary}")
        print()
        return

    # AI reasoning panel
    rec = ai_result.get("recommendation", "N/A")
    rec_color = _color_for_signal(rec)
    confidence = ai_result.get("confidence", "N/A")
    reasoning = ai_result.get("reasoning", "No reasoning provided.")

    ai_text = Text()
    ai_text.append("Recommendation: ", style="bold")
    ai_text.append(f"{rec}\n", style=f"bold {rec_color}")
    ai_text.append("Confidence: ", style="bold")
    ai_text.append(f"{confidence}\n\n", style="bold")
    ai_text.append("Reasoning:\n", style="bold underline")
    ai_text.append(f"{reasoning}\n")

    risk_factors = ai_result.get("risk_factors", [])
    if risk_factors:
        ai_text.append("\nRisk Factors:\n", style="bold red")
        for rf in risk_factors:
            ai_text.append(f"  - {rf}\n", style="red")

    console.print()
    console.print(Panel(ai_text, title="AI Analysis", border_style="magenta"))

    if sentiment_result:
        sent_text = Text()
        score = sentiment_result.get("score", "N/A")
        sentiment = sentiment_result.get("sentiment", "N/A")
        sent_color = "green" if str(sentiment).lower() == "positive" else (
            "red" if str(sentiment).lower() == "negative" else "yellow"
        )

        sent_text.append("Score: ", style="bold")
        sent_text.append(f"{score}\n", style=f"bold {sent_color}")
        sent_text.append("Sentiment: ", style="bold")
        sent_text.append(f"{sentiment}\n", style=f"bold {sent_color}")
        summary = sentiment_result.get("summary", "")
        if summary:
            sent_text.append("\nSummary:\n", style="bold underline")
            sent_text.append(f"{summary}\n")

        console.print(Panel(sent_text, title="Market Sentiment", border_style="cyan"))


# ---------------------------------------------------------------------------
# 6. Scan Results
# ---------------------------------------------------------------------------

def show_scan_results(scan_results: List[Dict[str, Any]]) -> None:
    """Display enhanced colored scan output for symbol screening."""

    if not RICH_AVAILABLE:
        if not scan_results:
            print("No scan results.")
            return
        headers = ["Symbol", "Signal", "Price", "Score", "Reason"]
        rows = []
        for r in scan_results:
            rows.append([
                r.get("symbol", ""),
                r.get("signal", ""),
                f"${r.get('price', 0):.2f}" if r.get("price") else "N/A",
                str(r.get("score", "")),
                r.get("reason", ""),
            ])
        _plain_table(headers, rows, title="Scan Results")
        return

    if not scan_results:
        console.print("[dim]No scan results.[/dim]")
        return

    table = Table(title="Market Scan Results", box=box.ROUNDED)
    table.add_column("Symbol", style="bold")
    table.add_column("Signal", justify="center")
    table.add_column("Price", justify="right")
    table.add_column("Score", justify="center")
    table.add_column("Reason")

    for r in scan_results:
        sig = r.get("signal", "")
        color = _color_for_signal(sig)
        price = f"${r.get('price', 0):.2f}" if r.get("price") else "N/A"
        score = r.get("score", "")
        score_str = str(score)
        if isinstance(score, (int, float)):
            score_color = "green" if score >= 7 else ("yellow" if score >= 4 else "red")
            score_str = f"[{score_color}]{score}[/{score_color}]"

        table.add_row(
            r.get("symbol", ""),
            f"[bold {color}]{sig}[/bold {color}]",
            price,
            score_str,
            r.get("reason", ""),
        )

    console.print()
    console.print(table)
