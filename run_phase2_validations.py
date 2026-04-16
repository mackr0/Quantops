#!/usr/bin/env python3
"""Run Phase 2 validation gates against all 5 live strategy engines.

Persists results to strategy_validations.db and prints a summary.

Usage:
    python run_phase2_validations.py
"""

import json
import sys

from rigorous_backtest import validate_strategy, save_validation


STRATEGIES = [
    ("micro_combined", "micro"),
    ("small_combined", "small"),
    ("mid_combined", "midcap"),
    ("large_combined", "largecap"),
    ("crypto_combined", "crypto"),
]


def run_all():
    print("=" * 70)
    print("  QuantOpsAI Phase 2 — Validating all 5 live strategy engines")
    print("=" * 70)
    print()

    results = []
    for strategy_name, market_type in STRATEGIES:
        print(f"→ Validating {strategy_name} on {market_type}...")

        # strategy_fn is a no-op placeholder — backtest_strategy() routes to
        # the correct engine via market_type. See rigorous_backtest.py.
        result = validate_strategy(
            strategy_fn=lambda sym, df=None: None,
            market_type=market_type,
            history_days=180,      # 6 months is what our data source supports well
            sample_size=20,        # reasonable breadth, reasonable speed
            monte_carlo_iterations=500,
        )

        save_validation(strategy_name, result)
        results.append((strategy_name, market_type, result))

        verdict = result["verdict"]
        score = result["score"]
        passed = len(result["passed_gates"])
        total = passed + len(result["failed_gates"])

        mark = "✓" if verdict == "PASS" else "✗"
        print(f"  {mark} {verdict}  score={score:.1f}  gates={passed}/{total}  elapsed={result['elapsed_sec']:.1f}s")

        if result["failed_gates"]:
            for f in result["failed_gates"]:
                print(f"    ✗ {f['gate']}: {f['reason']} (actual: {f['actual']}, threshold: {f['threshold']})")
        print()

    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    passes = sum(1 for _, _, r in results if r["verdict"] == "PASS")
    print(f"  {passes} / {len(results)} strategies PASSED")
    print()
    for name, market, r in results:
        print(f"  {name:20} ({market:8}) — {r['verdict']:4} score={r['score']:5.1f}")

    # Non-zero exit code if any failed (useful for CI)
    if passes < len(results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
