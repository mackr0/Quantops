"""Guardrail: dashboard JS must render BLOCKED / CANCELED badges
on AI picks whose `execution_outcome` says they didn't make it to
the broker.

Caught 2026-05-18: P16 NoAltData's AI selected 3 trades — BUY CRM,
BUY CSCO, SHORT AVGO — but only the BUYs executed. The SHORT was
filtered by the "short-on-bounce-days" pre-trade gate (AVGO was
down -0.2% so the rule rejected shorting into the move). The
`api_cycle_data` enrichment correctly stamped
`execution_outcome='no_fill'` on the SHORT AVGO entry, but the
dashboard JS only had branches for `'rejected'` and
`'converted_to_close'` — the 'no_fill' badge silently never
rendered. Operator saw 3 trades in the AI Brain widget and went
hunting for an AVGO short that never existed.

This test pins the structural invariant: every documented
execution_outcome value that means "didn't execute" must have a
render branch.
"""
from __future__ import annotations


def _read_dashboard() -> str:
    with open("templates/dashboard.html", encoding="utf-8") as f:
        return f.read()


def test_no_fill_badge_branch_exists():
    """JS must check `t.execution_outcome === 'no_fill'` and render
    a badge. Without this, AI's blocked-by-pre-trade-gate picks
    silently disappear from the dashboard ticker."""
    src = _read_dashboard()
    assert "execution_outcome === 'no_fill'" in src, (
        "Dashboard JS missing branch for execution_outcome='no_fill'. "
        "AI picks that the pre-trade gate skipped (short-on-bounce, "
        "already-positioned dedup, meta-model suppression) will "
        "disappear silently."
    )
    assert "BLOCKED" in src, (
        "BLOCKED badge label missing from dashboard.html"
    )


def test_canceled_badge_branch_exists():
    """JS must also handle the 'canceled' outcome (limit orders that
    didn't fill within the stale-order window)."""
    src = _read_dashboard()
    assert "execution_outcome === 'canceled'" in src, (
        "Dashboard JS missing branch for execution_outcome='canceled'."
    )


def test_didnt_execute_styling_includes_all_three_outcomes():
    """The strikethrough + muted color must apply for ALL of the
    'didn't execute' outcomes — rejected, no_fill, canceled.
    Pre-2026-05-18 it only applied to rejected, leaving no_fill
    picks rendering in bright green like they had executed."""
    src = _read_dashboard()
    # The `didntExecute = (...rejected ... no_fill ... canceled...)`
    # combined check must exist.
    needles = [
        "execution_outcome === 'rejected'",
        "execution_outcome === 'no_fill'",
        "execution_outcome === 'canceled'",
    ]
    for n in needles:
        assert n in src, (
            f"Missing styling guard for {n}. The TRADES SELECTED "
            f"row will render bright-green when this outcome fires, "
            f"misleading the operator into thinking the trade fired."
        )


def test_blockbadge_concatenated_into_html_output():
    """The blockBadge variable must actually be concatenated into the
    final `html +=` statement; defining it without using it is the
    classic UX gap that lets the bug ship."""
    src = _read_dashboard()
    assert "blockBadge" in src
    # Look for blockBadge appearing in an html concat (presence after
    # `+ badge` / `+ convertBadge`)
    assert ("+ blockBadge" in src or "blockBadge +" in src), (
        "blockBadge defined but never concatenated into html output. "
        "Badge won't render on the dashboard."
    )


def test_simple_strategies_logs_activity_for_baseline_trades():
    """buy_hold and random trades must call log_activity so they
    show up in the dashboard's Strategy Activity ticker alongside
    AI-pipeline trades. Original code silently wrote to per-profile
    trades table only — ticker reads from master activity_log."""
    with open("simple_strategies.py", encoding="utf-8") as f:
        src = f.read()
    assert "log_activity" in src, (
        "simple_strategies._submit_and_log must call log_activity "
        "so baseline (random / buy_hold) trades surface in the "
        "dashboard ticker."
    )
    assert "trade_executed" in src, (
        "log_activity call must use activity_type='trade_executed' "
        "(matching the AI path so the ticker filters cleanly)."
    )
