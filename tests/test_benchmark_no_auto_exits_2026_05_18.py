"""Guardrail: buy_hold and random `strategy_type` profiles must NOT
inherit the AI-driven auto-exit / risk-control tasks. Otherwise the
benchmarks aren't pure nulls and the AI-vs-baseline comparison the
experiment is designed to measure is contaminated.

Caught 2026-05-18 14:53 ET when EXP-A1-RandomA's SNPS position
trailing-stop-exited mid-day (high $517.17 − 1.5×ATR $12.11 = $499
stop, price fell to $492.73). Then random re-bought SNPS on the
next cycle → trailing-stop fired again. Same churn on P14's AMD.

Per docs/15_EXPERIMENT_DESIGN_2026_05_17.md:
  - buy_hold: "Never sells voluntarily. Re-trades only when SPY
    weight drifts > 5% from 100%."
  - random: "Closes any position not in today's pick, opens new
    picks equal-weighted from available cash. Zero AI involvement."

Neither should have trailing stops, ATR-based exits, position-runaway
alerts, AI-consistency floors, or kill-switch triggers.

This test pins the structural invariant: the auto-exit task
registration must be gated on `_is_baseline = strategy_type in
(buy_hold, random)`.
"""
from __future__ import annotations

import re


def test_check_exits_gated_on_baseline_flag():
    """`Check Exits` registration must be inside a `not _is_baseline`
    branch (or equivalent). The function name appears in the lambda
    that wraps the task; the surrounding code must guard it."""
    with open("multi_scheduler.py", encoding="utf-8") as f:
        src = f.read()
    # Locate the Check Exits run_task block + its surrounding 200 chars
    # of context. The guard must mention `_is_baseline` or
    # `strategy_type` somewhere ahead of it.
    m = re.search(
        r"([\s\S]{0,400})run_task\(\s*"
        r"f?\"\[\{seg_label\}\] Check Exits\"",
        src,
    )
    assert m, "Could not find `Check Exits` run_task registration"
    context = m.group(0)
    assert "_is_baseline" in context or "strategy_type" in context, (
        "Check Exits is not gated on a baseline / strategy_type check. "
        "Benchmark profiles (random, buy_hold) will inherit the "
        "trailing-stop auto-exit — contaminates the experiment. "
        f"Context: {context[-300:]!r}"
    )


def test_stop_coverage_gated_on_baseline_flag():
    """`Stop Coverage` auto-attaches protective stops to open longs.
    Random + buy_hold shouldn't have protective stops at all."""
    with open("multi_scheduler.py", encoding="utf-8") as f:
        src = f.read()
    m = re.search(
        r"([\s\S]{0,400})run_task\(\s*"
        r"f?\"\[\{seg_label\}\] Stop Coverage\"",
        src,
    )
    assert m, "Could not find `Stop Coverage` run_task registration"
    context = m.group(0)
    assert "_is_baseline" in context or "strategy_type" in context, (
        "Stop Coverage is not gated. Adds protective stops to "
        "benchmark profiles → trailing-stop exits → contamination."
    )


def test_baseline_flag_definition_present():
    """The `_is_baseline` flag must be defined from `strategy_type`
    before the auto-exit task block. Without this, the gates above
    would NameError at runtime."""
    with open("multi_scheduler.py", encoding="utf-8") as f:
        src = f.read()
    # Must have a line like `_is_baseline = ... strategy_type ... in ...
    # ("buy_hold", "random")` or similar
    pat = re.compile(
        r"_is_baseline\s*=.*strategy_type[\s\S]{0,200}"
        r"buy_hold[\s\S]{0,100}random",
        re.IGNORECASE,
    )
    assert pat.search(src), (
        "Missing `_is_baseline = ... strategy_type ... buy_hold / "
        "random` definition in multi_scheduler.py"
    )


def test_buy_hold_and_random_in_dispatcher():
    """Sanity: simple_strategies.dispatch DOES handle buy_hold and
    random, so when we gate the AI-exit tasks the baseline profiles
    still get their own strategy logic via the scan_and_trade →
    simple_strategies dispatch path."""
    with open("simple_strategies.py", encoding="utf-8") as f:
        src = f.read()
    assert 'st == "buy_hold"' in src or "'buy_hold'" in src, (
        "simple_strategies.dispatch missing buy_hold handler"
    )
    assert 'st == "random"' in src or "'random'" in src, (
        "simple_strategies.dispatch missing random handler"
    )
