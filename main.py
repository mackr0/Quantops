#!/usr/bin/env python3
"""
QuantOpsAI — AI-powered paper trading system.

Usage:
    python main.py account              Show account info
    python main.py positions            Show current positions
    python main.py dashboard            Full portfolio dashboard
    python main.py analyze AAPL         Technical analysis for a symbol
    python main.py ai-analyze AAPL      AI-powered analysis using Claude
    python main.py sentiment AAPL       News sentiment analysis
    python main.py scan                 Scan watchlist (technical)
    python main.py ai-scan              Scan watchlist with AI + sentiment
    python main.py trade AAPL           Execute trade based on combined strategy
    python main.py trade-scan           Scan and trade all signals
    python main.py check-exits          Check stop-loss / take-profit triggers
    python main.py backtest AAPL        Backtest combined strategy on a symbol
    python main.py journal              Show trade history
    python main.py performance          Show performance summary
    python main.py snapshot             Save daily portfolio snapshot

  Aggressive Small-Cap Commands:
    python main.py screen               Screen for small/micro-cap candidates
    python main.py aggro-scan           Aggressive scan on screened stocks
    python main.py aggro-trade          Screen, scan, and auto-trade aggressively
    python main.py aggro-analyze SYM    Aggressive analysis on a specific symbol

  AI Performance Tracking:
    python main.py ai-report            Show AI prediction accuracy report
    python main.py ai-resolve           Resolve pending AI predictions vs actual prices
"""

import sys
import json
from config import WATCHLIST
from client import get_api, get_account_info, get_positions
from strategies import sma_crossover_strategy, rsi_strategy, combined_strategy
from trader import execute_trade, check_exits
from journal import init_db, log_daily_snapshot, get_trade_history, get_performance_summary
from portfolio_manager import get_risk_summary


def print_json(data):
    print(json.dumps(data, indent=2, default=str))


# ── Account & Portfolio ──────────────────────────────────────────────

def cmd_account():
    print("=== Account Info ===")
    print_json(get_account_info())


def cmd_positions():
    print("=== Current Positions ===")
    positions = get_positions()
    if not positions:
        print("No open positions.")
    else:
        print_json(positions)


def cmd_dashboard():
    from dashboard import show_portfolio_dashboard
    api = get_api()
    account = get_account_info(api)
    positions = get_positions(api)
    risk = get_risk_summary(account, positions)
    show_portfolio_dashboard(account, positions, risk)


# ── Analysis ─────────────────────────────────────────────────────────

def cmd_analyze(symbol):
    print(f"=== Technical Analysis: {symbol} ===")
    print("\n--- SMA Crossover ---")
    print_json(sma_crossover_strategy(symbol))
    print("\n--- RSI ---")
    print_json(rsi_strategy(symbol))
    print("\n--- Combined Signal ---")
    print_json(combined_strategy(symbol))


def cmd_ai_analyze(symbol):
    from ai_analyst import analyze_symbol
    from dashboard import show_ai_analysis
    print(f"=== AI Analysis: {symbol} ===")
    result = analyze_symbol(symbol)
    try:
        show_ai_analysis(result)
    except Exception:
        print_json(result)


def cmd_sentiment(symbol):
    from news_sentiment import get_sentiment_signal, fetch_news, analyze_sentiment
    from client import get_api
    print(f"=== Sentiment Analysis: {symbol} ===")
    api = get_api()
    news = fetch_news(symbol, api=api)
    if not news:
        print("No recent news found.")
        return
    print(f"Found {len(news)} news items\n")
    result = analyze_sentiment(symbol, news)
    print_json(result)


# ── Scanning ─────────────────────────────────────────────────────────

def cmd_scan():
    from dashboard import show_scan_results
    print("=== Watchlist Scan ===")
    results = []
    for symbol in WATCHLIST:
        try:
            result = combined_strategy(symbol)
            results.append(result)
        except Exception as e:
            results.append({"symbol": symbol, "signal": "ERROR", "reason": str(e)})
    try:
        show_scan_results(results)
    except Exception:
        for r in results:
            print(f"  {r['symbol']:6s} | {r['signal']:12s} | {r.get('reason', '')}")


def cmd_ai_scan():
    from ai_analyst import analyze_symbol, compare_signals
    from news_sentiment import get_sentiment_signal
    from dashboard import show_scan_results
    print("=== AI-Enhanced Watchlist Scan ===\n")
    results = []
    for symbol in WATCHLIST:
        try:
            print(f"  Analyzing {symbol}...", end=" ", flush=True)
            tech_signal = combined_strategy(symbol)
            ai_signal = analyze_symbol(symbol)
            merged = compare_signals(tech_signal, ai_signal)

            # Add sentiment
            try:
                sentiment = get_sentiment_signal(symbol)
                merged["sentiment"] = sentiment.get("reason", "N/A")
            except Exception:
                merged["sentiment"] = "unavailable"

            results.append(merged)
            print(f"{merged['signal']} (confidence: {merged.get('confidence', 'N/A')})")
        except Exception as e:
            print(f"ERROR: {e}")
            results.append({"symbol": symbol, "signal": "ERROR", "reason": str(e)})

    print()
    try:
        show_scan_results(results)
    except Exception:
        for r in results:
            print(f"  {r['symbol']:6s} | {r['signal']:12s} | {r.get('reason', '')}")


# ── Trading ──────────────────────────────────────────────────────────

def cmd_trade(symbol):
    print(f"=== Trade: {symbol} ===")
    signal = combined_strategy(symbol)
    print(f"Signal: {signal['signal']} — {signal['reason']}")
    result = execute_trade(symbol, signal)
    print_json(result)


def cmd_trade_scan():
    print("=== Trade Scan ===")
    for symbol in WATCHLIST:
        try:
            signal = combined_strategy(symbol)
            if signal["signal"] != "HOLD":
                print(f"\n--- {symbol}: {signal['signal']} ---")
                result = execute_trade(symbol, signal)
                print_json(result)
            else:
                print(f"  {symbol}: HOLD — skipping")
        except Exception as e:
            print(f"  {symbol}: ERROR — {e}")


def cmd_check_exits():
    print("=== Checking Stop-Loss / Take-Profit ===")
    results = check_exits()
    if not results:
        print("No positions triggered.")
    else:
        for r in results:
            print(f"  {r['symbol']}: {r['trigger'].upper()} — sold {r['qty']} shares")
            print(f"    Reason: {r['reason']}")


# ── Backtesting ──────────────────────────────────────────────────────

def cmd_backtest(symbol, days=365):
    from backtester import backtest, print_backtest_report
    from dashboard import show_backtest_results
    print(f"=== Backtest: {symbol} ({days} days) ===\n")
    result = backtest(symbol, combined_strategy, days=days)
    try:
        show_backtest_results(result)
    except Exception:
        print_backtest_report(result)


# ── Journal & Performance ────────────────────────────────────────────

def cmd_journal(symbol=None):
    from dashboard import show_trade_history
    init_db()
    trades = get_trade_history(symbol=symbol)
    if not trades:
        print("No trades in journal yet.")
        return
    try:
        show_trade_history(trades)
    except Exception:
        print_json(trades)


def cmd_performance():
    init_db()
    print("=== Performance Summary ===")
    summary = get_performance_summary()
    print_json(summary)


def cmd_snapshot():
    init_db()
    api = get_api()
    account = get_account_info(api)
    positions = get_positions(api)
    log_daily_snapshot(
        equity=account["equity"],
        cash=account["cash"],
        portfolio_value=account["portfolio_value"],
        num_positions=len(positions),
    )
    print(f"Snapshot saved: equity=${account['equity']:,.2f}, "
          f"positions={len(positions)}, cash=${account['cash']:,.2f}")


# ── Aggressive Small-Cap ──────────────────────────────────────────────

def cmd_screen():
    from screener import run_full_screen
    print("=== Small/Micro-Cap Stock Screener ===\n")
    print("Scanning for stocks $1-$20 with 500K+ avg volume...\n")
    results = run_full_screen()

    summary = results.get("summary", {})
    print(f"\n{'='*60}")
    print(f"Candidates found: {summary.get('total_candidates', 0)}")
    print(f"Volume surges:    {summary.get('volume_surges', 0)}")
    print(f"Momentum stocks:  {summary.get('momentum_stocks', 0)}")
    print(f"Breakouts:        {summary.get('breakouts', 0)}")

    for category in ("volume_surges", "momentum", "breakouts"):
        stocks = results.get(category, [])
        if stocks:
            print(f"\n--- {category.replace('_', ' ').title()} ---")
            for s in stocks[:10]:
                print(f"  {s['symbol']:6s} | ${s.get('price', 0):>8.2f} | {s.get('reason', '')}")


def cmd_aggro_scan():
    from screener import run_full_screen
    from aggressive_strategy import aggressive_combined_strategy
    from dashboard import show_scan_results

    print("=== Aggressive Small-Cap Scan ===\n")
    print("Step 1: Screening for candidates...\n")
    screen = run_full_screen()

    # Collect unique symbols from all categories
    symbols = set()
    for cat in ("volume_surges", "momentum", "breakouts", "candidates"):
        for s in screen.get(cat, []):
            symbols.add(s["symbol"])

    symbols = list(symbols)[:30]  # Cap at 30 to keep it manageable
    print(f"\nStep 2: Running aggressive analysis on {len(symbols)} stocks...\n")

    results = []
    for sym in symbols:
        try:
            result = aggressive_combined_strategy(sym)
            results.append(result)
            signal = result["signal"]
            if signal != "HOLD":
                print(f"  {sym:6s} -> {signal}")
        except Exception as e:
            print(f"  {sym:6s} -> ERROR: {e}")

    print()
    actionable = [r for r in results if r["signal"] != "HOLD"]
    if actionable:
        try:
            show_scan_results(actionable)
        except Exception:
            for r in actionable:
                print(f"  {r['symbol']:6s} | {r['signal']:12s} | {r.get('reason', '')}")
    else:
        print("No actionable signals found in this scan.")


def cmd_aggro_trade():
    from screener import run_full_screen
    from aggressive_trader import run_aggressive_scan_and_trade

    print("=== Aggressive Auto-Trade (AI-Reviewed) ===\n")
    print("Step 1: Screening for small-cap candidates...\n")
    screen = run_full_screen()

    symbols = set()
    for cat in ("volume_surges", "momentum", "breakouts", "candidates"):
        for s in screen.get(cat, []):
            symbols.add(s["symbol"])

    symbols = list(symbols)[:30]
    print(f"\nStep 2: Analyzing with AI review before trading ({len(symbols)} stocks)...\n")

    summary = run_aggressive_scan_and_trade(symbols)

    print(f"\n{'='*60}")
    print(f"  Stocks scanned:  {summary.get('total', 0)}")
    print(f"  Buys executed:   {summary.get('buys', 0)}")
    print(f"  Sells executed:  {summary.get('sells', 0)}")
    print(f"  AI vetoed:       {summary.get('ai_vetoed', 0)}")
    print(f"  Holds:           {summary.get('holds', 0)}")
    print(f"  Skipped:         {summary.get('skips', 0)}")
    print(f"  Errors:          {summary.get('errors', 0)}")
    print(f"{'='*60}")

    # Show executed trades
    executed = [d for d in summary.get("details", []) if d.get("action") in ("BUY", "SELL")]
    if executed:
        print(f"\n--- Executed Trades ---")
        for d in executed:
            ai_info = f"AI: {d.get('ai_signal', '?')} ({d.get('ai_confidence', '?')}%)" if d.get('ai_signal') else ""
            print(f"  {d['action']:4s} {d['symbol']:6s} | qty: {d.get('qty', 'N/A'):>6} | ~${d.get('estimated_cost', 0):>10,.2f} | {ai_info}")

    # Show AI vetoes
    vetoed = [d for d in summary.get("details", []) if d.get("action") == "AI_VETOED"]
    if vetoed:
        print(f"\n--- AI Vetoed (saved you from these) ---")
        for d in vetoed:
            print(f"  {d['symbol']:6s} | Technical: {d.get('signal', '?')} | AI: {d.get('ai_signal', '?')} ({d.get('ai_confidence', '?')}%) | {d.get('reason', '')[:80]}")


def cmd_aggro_analyze(symbol):
    from aggressive_strategy import aggressive_combined_strategy
    from dashboard import show_ai_analysis
    print(f"=== Aggressive Analysis: {symbol} ===\n")
    result = aggressive_combined_strategy(symbol)
    print_json(result)


# ── AI Performance Tracking ───────────────────────────────────────────

def cmd_ai_report():
    from ai_tracker import init_tracker_db, print_ai_report
    init_tracker_db()
    print_ai_report()


def cmd_ai_resolve():
    from ai_tracker import init_tracker_db, resolve_predictions
    init_tracker_db()
    count = resolve_predictions()
    print(f"Resolved {count} predictions. Run 'ai-report' to see updated accuracy.")


# ── CLI Router ───────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1].lower()

    if command == "account":
        cmd_account()
    elif command == "positions":
        cmd_positions()
    elif command == "dashboard":
        cmd_dashboard()
    elif command == "analyze" and len(sys.argv) >= 3:
        cmd_analyze(sys.argv[2].upper())
    elif command == "ai-analyze" and len(sys.argv) >= 3:
        cmd_ai_analyze(sys.argv[2].upper())
    elif command == "sentiment" and len(sys.argv) >= 3:
        cmd_sentiment(sys.argv[2].upper())
    elif command == "scan":
        cmd_scan()
    elif command == "ai-scan":
        cmd_ai_scan()
    elif command == "trade" and len(sys.argv) >= 3:
        cmd_trade(sys.argv[2].upper())
    elif command == "trade-scan":
        cmd_trade_scan()
    elif command == "check-exits":
        cmd_check_exits()
    elif command == "backtest" and len(sys.argv) >= 3:
        days = int(sys.argv[3]) if len(sys.argv) >= 4 else 365
        cmd_backtest(sys.argv[2].upper(), days)
    elif command == "journal":
        sym = sys.argv[2].upper() if len(sys.argv) >= 3 else None
        cmd_journal(sym)
    elif command == "performance":
        cmd_performance()
    elif command == "snapshot":
        cmd_snapshot()
    elif command == "screen":
        cmd_screen()
    elif command == "aggro-scan":
        cmd_aggro_scan()
    elif command == "aggro-trade":
        cmd_aggro_trade()
    elif command == "aggro-analyze" and len(sys.argv) >= 3:
        cmd_aggro_analyze(sys.argv[2].upper())
    elif command == "ai-report":
        cmd_ai_report()
    elif command == "ai-resolve":
        cmd_ai_resolve()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
