#!/usr/bin/env python3
"""
Alpaca AI Trader — Paper trading bot with technical analysis strategies.

Usage:
    python main.py account          — Show account info
    python main.py positions        — Show current positions
    python main.py analyze AAPL     — Analyze a symbol using combined strategy
    python main.py scan             — Scan a watchlist for signals
    python main.py trade AAPL       — Analyze and execute trade for a symbol
    python main.py trade-scan       — Scan watchlist and execute trades
"""

import sys
import json
from client import get_account_info, get_positions
from strategies import sma_crossover_strategy, rsi_strategy, combined_strategy
from trader import execute_trade

# Default watchlist — edit as you like
WATCHLIST = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "SPY", "QQQ"]


def print_json(data):
    print(json.dumps(data, indent=2, default=str))


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


def cmd_analyze(symbol):
    print(f"=== Analysis: {symbol} ===")
    print("\n--- SMA Crossover ---")
    print_json(sma_crossover_strategy(symbol))
    print("\n--- RSI ---")
    print_json(rsi_strategy(symbol))
    print("\n--- Combined Signal ---")
    print_json(combined_strategy(symbol))


def cmd_scan():
    print("=== Watchlist Scan ===")
    for symbol in WATCHLIST:
        try:
            result = combined_strategy(symbol)
            emoji = {"STRONG_BUY": ">>", "BUY": ">", "WEAK_BUY": ">?",
                     "STRONG_SELL": "<<", "SELL": "<", "WEAK_SELL": "<?",
                     "HOLD": "--"}
            indicator = emoji.get(result["signal"], "??")
            print(f"  [{indicator}] {symbol:6s} | {result['signal']:12s} | RSI: {result.get('rsi', 'N/A'):>6} | ${result.get('price', 0):.2f} | {result['reason']}")
        except Exception as e:
            print(f"  [!!] {symbol:6s} | ERROR: {e}")


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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1].lower()

    if command == "account":
        cmd_account()
    elif command == "positions":
        cmd_positions()
    elif command == "analyze" and len(sys.argv) >= 3:
        cmd_analyze(sys.argv[2].upper())
    elif command == "scan":
        cmd_scan()
    elif command == "trade" and len(sys.argv) >= 3:
        cmd_trade(sys.argv[2].upper())
    elif command == "trade-scan":
        cmd_trade_scan()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
