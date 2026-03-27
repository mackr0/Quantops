#!/usr/bin/env python3
"""
Quantops — AI-powered paper trading system.

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
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
