"""Structural guardrail: every function in the tuning pipeline
that calls `update_trading_profile(profile_id, X=value)` must
also call `log_tuning_change(...)` so the change appears in the
operator-visible tuning history.

The bug class.
A self-tuning rule adjusts a parameter, but the operator sees
nothing in the tuning_history panel. The change "happened" but
there's no audit trail. Symptoms:
  - Operator notices a parameter changed and asks "why?"
  - Reverse engineering through git blame / scheduler logs
  - Trust in the autonomy layer erodes
  - Hard to diagnose performance regressions

This week I almost shipped the wave-9a meta_pregate tuner without
the log call — caught it in review. The structural test makes
"forgot the log" impossible to ship.

Scope: only `_optimize_*` functions (the canonical tuning rules)
and the `apply_parameter_adjustments` helper. Other call sites
of update_trading_profile (the settings page form handler,
manual scripts) intentionally don't log to tuning_history because
they're operator-driven, not AI-driven.
"""
from __future__ import annotations

import ast
import os
import sys
from typing import List

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Functions where update_trading_profile is OK without
# log_tuning_change. Each entry needs a written rationale.
ALLOWLIST_FUNCTIONS = {
    # Operator-driven update paths (settings page form, profile
    # creation) — these are user actions, not AI tuning, so
    # tuning_history is the wrong audit log. The web framework
    # logs them via request log.
    # No entries currently. Operator-driven update sites are
    # in views.py / models.py — not _optimize_* functions, so
    # they're naturally excluded by the discovery scope below.
}


def _find_optimizer_functions(src_path: str) -> List[ast.FunctionDef]:
    """Return AST nodes for every function whose name starts with
    _optimize_ in the file. Plus apply_parameter_adjustments."""
    with open(src_path) as fh:
        tree = ast.parse(fh.read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if (node.name.startswith("_optimize_")
                or node.name == "apply_parameter_adjustments"):
            out.append(node)
    return out


def _function_calls_update_trading_profile(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (
            target.id if isinstance(target, ast.Name)
            else target.attr if isinstance(target, ast.Attribute)
            else None
        )
        if name == "update_trading_profile":
            return True
    return False


def _function_calls_log_tuning_change(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (
            target.id if isinstance(target, ast.Name)
            else target.attr if isinstance(target, ast.Attribute)
            else None
        )
        if name == "log_tuning_change":
            return True
    return False


class TestUpdateTradingProfilePairedWithLogTuningChange:
    SCAN_FILES = [
        "self_tuning.py",
        "pipelines/tuning_writer.py",
        "insight_propagation.py",
    ]

    def test_every_tuner_function_logs_its_change(self):
        violations = []
        scanned_count = 0
        update_count = 0
        for rel in self.SCAN_FILES:
            path = os.path.join(REPO_ROOT, rel)
            if not os.path.exists(path):
                continue
            for func in _find_optimizer_functions(path):
                scanned_count += 1
                if not _function_calls_update_trading_profile(func):
                    continue
                update_count += 1
                if _function_calls_log_tuning_change(func):
                    continue
                if func.name in ALLOWLIST_FUNCTIONS:
                    continue
                violations.append((rel, func.lineno, func.name))
        # Sanity: scanner found tuner functions
        assert scanned_count >= 5, (
            f"Scanner found only {scanned_count} _optimize_ "
            f"functions across {len(self.SCAN_FILES)} files — "
            f"likely broken; investigate."
        )
        if violations:
            details = "\n".join(
                f"  {rel}:{lineno}  def {fname}(...)"
                for rel, lineno, fname in violations
            )
            pytest.fail(
                f"{len(violations)} tuner function(s) call "
                f"update_trading_profile WITHOUT a paired "
                f"log_tuning_change. The change happens but no "
                f"audit-trail entry — operator can't see the "
                f"adjustment in tuning_history.\n\nFix: add a "
                f"log_tuning_change(profile_id, user_id, "
                f"adjustment_type, parameter_name, old_value, "
                f"new_value, reason) call in the same function "
                f"after the update_trading_profile call.\n\n"
                f"Sites:\n{details}"
            )

    def test_known_allowlist_entries_match_existing_functions(self):
        """Stale allowlist entries (function no longer exists)
        should fail this test so rationales stay current."""
        all_func_names = set()
        for rel in self.SCAN_FILES:
            path = os.path.join(REPO_ROOT, rel)
            if not os.path.exists(path):
                continue
            for func in _find_optimizer_functions(path):
                all_func_names.add(func.name)
        stale = set(ALLOWLIST_FUNCTIONS) - all_func_names
        if stale:
            pytest.fail(
                "ALLOWLIST_FUNCTIONS entries reference functions "
                "that don't exist:\n  " + "\n  ".join(sorted(stale))
                + "\n\nRemove these — they protect nothing."
            )
