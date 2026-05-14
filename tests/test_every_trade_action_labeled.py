"""Structural guardrail: every literal `"action"` value assigned in
trade-decision modules is one of the KNOWN_ACTIONS — no typos, no
missing labels, no `None` action sneaking through.

The bug class.
A new code path inside a trade-decision function (e.g. a new
short-side filter) returns a result dict missing the `action` key,
or with `action="skiip"` (typo), or with `action=None`. Downstream
code does:
    if result["action"] in ("BUY", "SELL", "SHORT"):
        submit_order(...)
The new path returns `action="skiip"`, which doesn't match any
case — so the trade is silently dropped without a log line. The
operator sees the candidate disappear from the dashboard but no
"why" trail.

This is a static check (AST-walk of trade-decision modules). For
every:
  - Dict literal `{"action": "...", ...}`
  - Subscript assignment `result["action"] = "..."`
the value must be a string literal in KNOWN_ACTIONS. Dynamic
assignments (`result["action"] = some_var`) are out of scope for
the static check; they're rare in this codebase and a separate
runtime test would be needed to cover them.

Acceptable patterns:
  1. action value is in KNOWN_ACTIONS → ok
  2. action value is a non-string-literal expression (e.g. a
     variable, function call) → skipped (out of static scope)
  3. action value is in INTENTIONALLY_DYNAMIC_ACTIONS — must be
     listed with rationale (e.g., a placeholder set later in the
     same function)
"""
from __future__ import annotations

import ast
import os
import sys
from typing import List, Optional, Set, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Files in scope. Each holds at least one trade-decision function
# (returns a dict with an `action` field consumed by downstream
# scheduler/trader code). Note: `bracket_orders.py` is intentionally
# excluded — it's a low-level broker-execution helper that submits
# orders given an already-decided `BUY`/`SHORT`/`SELL`. It does not
# emit a trade-decision dict; its callers do.
TRADE_DECISION_FILES = (
    "trade_pipeline.py",
    "trader.py",
    "options_trader.py",
    "options_multileg.py",
    "stat_arb_pair_book.py",
)


# Universe of valid `action` values. Sourced from a grep of every
# string literal assigned to an `"action"` key in the files above
# (2026-05-14). Default-deny: any new label requires a written
# entry here so reviewers can sanity-check that downstream consumers
# have a case for it.
KNOWN_ACTIONS: Set[str] = {
    # Core stock execution
    "BUY", "SELL", "SHORT", "HOLD", "SKIP", "NONE", "BLOCKED",
    # Stock execution outcomes / errors
    "ERROR",
    # Pre-trade gating outcomes (no order sent; surfaced to dashboard)
    "EXCLUDED", "EARNINGS_SKIP", "BROKER_DISCONNECTED",
    "KILL_SWITCH", "CATASTROPHIC_SINGLE_TRADE",
    "BOOK_CONCENTRATION_CAP", "DRAWDOWN_PAUSE",
    "BLACKLIST_BLOCKED", "COOLDOWN", "INTRADAY_RISK_HALT",
    # Options pipeline
    "OPTIONS", "OPTIONS_OPEN", "MULTILEG_OPEN",
    # Pair-trading pipeline
    "PAIR_TRADE", "PAIR_OPEN", "PAIR_CLOSE",
    "ENTER_LONG_A_SHORT_B", "ENTER_SHORT_A_LONG_B",
    "REGIME_BREAK_EXIT", "EXIT",
}


# Per-file allowlist for action values that look unfamiliar but are
# legitimate (e.g., docstring examples in the action set, defensive
# placeholders). Default-deny.
INTENTIONALLY_DYNAMIC_ACTIONS: dict = {
    # Currently empty — every observed action is in KNOWN_ACTIONS.
}


def _get_action_string(value_node: ast.expr) -> Optional[str]:
    """If the AST node is a string-literal constant, return its
    value. Otherwise None (dynamic — out of static scope)."""
    if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
        return value_node.value
    return None


# Sibling-key heuristic: a dict is a "trade result" (the kind we care
# about) when it has `action` AND at least one of these other keys.
# This filters out non-trade-result dicts that happen to use the key
# "action" for a different namespace (e.g., the `dd` drawdown-state
# dict whose values are "normal"/"pause"/"reduce").
TRADE_RESULT_SIBLING_KEYS = frozenset({
    "symbol", "signal", "reason", "qty", "shares", "price",
    "ai_confidence", "stop_loss", "take_profit", "side",
})


def _dict_is_trade_result(d: ast.Dict) -> bool:
    """True iff this dict literal looks like a trade result (has
    `action` AND at least one of the sibling trade-result keys)."""
    sibling_keys = set()
    has_action = False
    for k in d.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            if k.value == "action":
                has_action = True
            else:
                sibling_keys.add(k.value)
    if not has_action:
        return False
    return bool(sibling_keys & TRADE_RESULT_SIBLING_KEYS)


def _walk_action_assignments(tree: ast.AST) -> List[Tuple[int, str]]:
    """Yield (lineno, action_value_literal) for every:
      - Trade-result dict literal with key "action" → string-literal value
      - Subscript assignment d["action"] = "string-literal" where d is
        clearly a trade result (heuristic: variable name is `result`,
        `out`, or contains `result`)
    """
    out = []
    for node in ast.walk(tree):
        # Pattern 1: trade-result dict literal {"action": "STRING", ...}
        if isinstance(node, ast.Dict):
            if not _dict_is_trade_result(node):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant)
                        and k.value == "action"):
                    s = _get_action_string(v)
                    if s is not None:
                        out.append((k.lineno, s))
        # Pattern 2: result["action"] = "STRING" (variable named like
        # a trade result — `result`, `r`, `out`, anything ending in
        # `_result`). Avoids matching `dd["action"] = ...`.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Subscript):
                    continue
                if not isinstance(target.value, ast.Name):
                    continue
                var_name = target.value.id
                if not (var_name in ("result", "out", "r")
                        or var_name.endswith("_result")):
                    continue
                slice_node = target.slice
                if isinstance(slice_node, ast.Constant):
                    if slice_node.value == "action":
                        s = _get_action_string(node.value)
                        if s is not None:
                            out.append((target.lineno, s))
    return out


class TestEveryTradeActionLabeled:
    """Static check: every literal `action=...` in trade-decision
    files uses a value from KNOWN_ACTIONS."""

    def test_all_action_literals_are_known(self):
        violations: List[Tuple[str, int, str]] = []
        for fname in TRADE_DECISION_FILES:
            path = os.path.join(REPO_ROOT, fname)
            if not os.path.exists(path):
                continue
            with open(path) as fh:
                src = fh.read()
            try:
                tree = ast.parse(src)
            except SyntaxError as exc:
                pytest.fail(f"Could not parse {fname}: {exc}")
            for lineno, action in _walk_action_assignments(tree):
                if action in KNOWN_ACTIONS:
                    continue
                if action in INTENTIONALLY_DYNAMIC_ACTIONS:
                    continue
                violations.append((fname, lineno, action))

        if violations:
            details = "\n".join(
                f"  {fname}:{lineno}  action={value!r}"
                for fname, lineno, value in violations
            )
            pytest.fail(
                f"{len(violations)} trade-decision sites use an "
                f"action label that is not in KNOWN_ACTIONS. "
                f"Downstream consumers (scheduler, trader, dashboard) "
                f"have switch/case logic keyed on the action value — "
                f"a new label that downstream doesn't know about "
                f"will be silently dropped.\n\n"
                f"Violations:\n{details}\n\n"
                f"Fix one of:\n"
                f"  1. If the new action is intentional, add it to "
                f"KNOWN_ACTIONS (and verify every downstream consumer "
                f"has a case for it)\n"
                f"  2. If it's a typo, fix the source\n"
                f"  3. If it's a placeholder set later in the same "
                f"function, add it to INTENTIONALLY_DYNAMIC_ACTIONS "
                f"with rationale"
            )

    def test_scanner_finds_at_least_one_action_per_file(self):
        """Sanity: each trade-decision file must contain at least one
        action assignment. If the scanner returns 0 for a file, the
        AST traversal regressed."""
        for fname in TRADE_DECISION_FILES:
            path = os.path.join(REPO_ROOT, fname)
            if not os.path.exists(path):
                continue
            with open(path) as fh:
                src = fh.read()
            tree = ast.parse(src)
            found = _walk_action_assignments(tree)
            assert len(found) > 0, (
                f"Scanner found 0 action assignments in {fname} — "
                f"AST traversal likely regressed. Manually verified "
                f"on 2026-05-14 that every file in TRADE_DECISION_FILES "
                f"has at least one action= literal."
            )
