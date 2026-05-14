"""Structural guardrail: every action type the AI can propose must
be eligible for a `trade_executed` activity-log entry so it appears
in the dashboard ticker. The previous filter
`if action in ("BUY", "SELL", "SHORT")` silently dropped MULTILEG_OPEN,
OPTIONS, MULTILEG_CLOSE, and PAIR_TRADE — those trades landed in
the trades table but never appeared in the operator-facing ticker.

The bug class.
A new action type is added (MULTILEG_OPEN was added 2026-05-06; the
shipped scheduler still only logged BUY/SELL/SHORT). Any place that
enumerates "the actions we care about" without referencing the
canonical list silently drops the new action.

This test pins the canonical EXECUTED_ACTIONS set in
multi_scheduler.py against the actions the AI prompt can return.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Every action type that the AI can return as an executed trade.
# Sourced from ai_analyst.py prompt + trade_pipeline action handling.
# HOLD/SKIP are intentionally excluded — they're non-actions and
# don't need ticker entries.
CANONICAL_EXECUTED_ACTIONS = {
    "BUY", "STRONG_BUY", "SELL", "STRONG_SELL",
    "SHORT", "COVER",
    "OPTIONS", "MULTILEG_OPEN", "MULTILEG_CLOSE",
    "PAIR_TRADE",
}


class TestAllActionTypesLoggedToTicker:
    def test_multi_scheduler_logs_every_executed_action(self):
        """The activity-log filter in multi_scheduler.py must include
        every executed action type. Catches the 2026-05-14 bug where
        MULTILEG_OPEN trades landed in the trades table but were
        silently filtered out of the ticker because the filter was
        `if action in ("BUY", "SELL", "SHORT")`."""
        path = os.path.join(REPO_ROOT, "multi_scheduler.py")
        with open(path) as f:
            src = f.read()

        # Find the EXECUTED_ACTIONS set definition.
        m = re.search(
            r"EXECUTED_ACTIONS\s*=\s*\{([^}]+)\}",
            src,
            re.DOTALL,
        )
        assert m, (
            "multi_scheduler.py is missing the EXECUTED_ACTIONS set "
            "that gates trade-executed activity logging. Without "
            "this canonical set, a future PR can add a new action "
            "(MULTILEG_OPEN happened 2026-05-06) and silently drop "
            "it from the ticker."
        )
        contents = m.group(1)
        present_actions = set(re.findall(r'"([A-Z_]+)"', contents))

        missing = CANONICAL_EXECUTED_ACTIONS - present_actions
        assert not missing, (
            f"multi_scheduler.py EXECUTED_ACTIONS is missing: "
            f"{sorted(missing)}. Trades with these actions land in "
            f"the trades table but never appear in the dashboard "
            f"ticker. Add them to the set or document the omission."
        )

    def test_no_hardcoded_buy_sell_short_filter(self):
        """Catch the specific bug pattern: a hardcoded action list
        of just (BUY, SELL, SHORT) used to gate trade activity. Any
        such filter silently drops every other action type."""
        path = os.path.join(REPO_ROOT, "multi_scheduler.py")
        with open(path) as f:
            src = f.read()
        # The exact bug-causing pattern (the `in (...)` form with
        # only the three stock actions) must not exist anywhere.
        bug_patterns = [
            r"action.*\)\s*in\s*\(\s*['\"]BUY['\"]\s*,\s*['\"]SELL['\"]\s*,\s*['\"]SHORT['\"]\s*\)",
            r"action.*\)\s*in\s*\[\s*['\"]BUY['\"]\s*,\s*['\"]SELL['\"]\s*,\s*['\"]SHORT['\"]\s*\]",
        ]
        for pat in bug_patterns:
            assert not re.search(pat, src), (
                f"multi_scheduler.py contains a hardcoded "
                f"`action in ('BUY','SELL','SHORT')` filter — this "
                f"is the exact pattern that silently dropped "
                f"MULTILEG_OPEN trades from the ticker. Use the "
                f"canonical EXECUTED_ACTIONS set instead."
            )
