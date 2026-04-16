#!/usr/bin/env python3
"""Run backtests against all 5 strategy engines to validate they work
with real market data and produce reasonable results.

Usage:
    python run_backtest_validation.py
"""

import sys
import time

from backtester import backtest_strategy

MARKET_TYPES = ["micro", "small", "midcap", "largecap", "crypto"]
DAYS = 90
CAPITAL = 10_000
SAMPLE = 15


def main():
    print("=" * 60)
    print("  QuantOpsAI Strategy Backtest Validation")
    print(f"  Period: {DAYS} days | Capital: ${CAPITAL:,} | Sample: {SAMPLE} symbols")
    print("=" * 60)

    results = {}
    failures = []

    for mt in MARKET_TYPES:
        print(f"\n--- {mt.upper()} ---")
        start = time.time()
        try:
            result = backtest_strategy(
                mt, days=DAYS, initial_capital=CAPITAL, sample_size=SAMPLE
            )
            elapsed = time.time() - start
            results[mt] = result

            print(f"  Symbols tested: {result.get('symbols_tested', SAMPLE)}")
            print(f"  Total Return:   {result['total_return_pct']:+.2f}%")
            print(f"  Win Rate:       {result['win_rate']:.1f}%")
            print(f"  Max Drawdown:   {result['max_drawdown_pct']:.1f}%")
            print(f"  Sharpe Ratio:   {result['sharpe_ratio']:.2f}")
            print(f"  Num Trades:     {result['num_trades']}")
            print(f"  Avg Hold Days:  {result['avg_hold_days']:.1f}")
            best = result.get("best_trade")
            worst = result.get("worst_trade")
            if isinstance(best, (int, float)) and best is not None:
                print(f"  Best Trade:     {best:+.2f}%")
            else:
                print(f"  Best Trade:     N/A")
            if isinstance(worst, (int, float)) and worst is not None:
                print(f"  Worst Trade:    {worst:+.2f}%")
            else:
                print(f"  Worst Trade:    N/A")
            print(f"  Time:           {elapsed:.1f}s")

        except Exception as e:
            elapsed = time.time() - start
            failures.append((mt, str(e)))
            print(f"  FAILED ({elapsed:.1f}s): {e}")

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)

    if results:
        print(f"\n  {'Engine':<12} {'Return':>8} {'Win%':>6} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>7}")
        print(f"  {'-'*12} {'-'*8} {'-'*6} {'-'*7} {'-'*7} {'-'*7}")
        for mt, r in results.items():
            print(
                f"  {mt:<12} {r['total_return_pct']:>+7.1f}% "
                f"{r['win_rate']:>5.1f}% "
                f"{r['max_drawdown_pct']:>6.1f}% "
                f"{r['sharpe_ratio']:>7.2f} "
                f"{r['num_trades']:>7d}"
            )

    if failures:
        print(f"\n  FAILURES ({len(failures)}):")
        for mt, err in failures:
            print(f"    {mt}: {err}")
        sys.exit(1)
    else:
        print(f"\n  All {len(results)} strategy engines validated successfully.")


if __name__ == "__main__":
    main()
