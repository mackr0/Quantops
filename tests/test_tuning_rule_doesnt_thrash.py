"""Structural guardrail: every `_optimize_*` self-tuning rule
must have an anti-thrash mechanism (cooldown, neutral band, or
documented exception).

The bug class.
A tuning rule fires every cycle. The signal it reads is noisy.
Without a cooldown or neutral band, the rule flips a parameter
back and forth: ON, OFF, ON, OFF, ON. Each flip looks like a
"successful tuning decision" in the audit log but the system is
just oscillating. The operator's parameter changes get reverted
every night.

Acceptable patterns:
  1. `_safe_change_guarded(profile_id, param)` cooldown gate
     (the canonical pattern — checks last_change recency)
  2. `_get_recent_adjustment(...)` cooldown variant
  3. A "neutral band" where the rule explicitly returns None
     for signal values in some range (e.g., 5-30% actionable
     ratio, 40-60% win rate)
  4. `# THRASH_OK: <rationale>` comment at the function start
     for rules that intentionally re-evaluate every cycle
     (e.g., crisis-state checks)

This catches the design defect where someone writes a tuning
rule with no anti-thrash guard.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from typing import List

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Functions intentionally without a thrash guard, with rationale
KNOWN_THRASH_OK = {
    # Functions that genuinely need to re-evaluate every cycle
    "apply_parameter_adjustments":
        "Helper that applies pre-computed changes from the "
        "pipeline tuner — the calling rule is the one that "
        "should have the guard, not this writer.",
}


# Patterns that count as anti-thrash protection.
ANTI_THRASH_PATTERNS = (
    "_safe_change_guarded",
    "_get_recent_adjustment",
    "_was_adjustment_effective",
    # Neutral-band patterns — comments that mark a no-change zone
    "neutral band",
    "no change",
    "no_op",
)


def _find_optimizer_functions(src_path: str) -> List[ast.FunctionDef]:
    with open(src_path) as fh:
        tree = ast.parse(fh.read())
    return [
        n for n in ast.walk(tree)
        if (isinstance(n, ast.FunctionDef)
            and (n.name.startswith("_optimize_")
                 or n.name == "apply_parameter_adjustments"))
    ]


def _function_writes_to_profile(func: ast.FunctionDef) -> bool:
    """True iff this function calls update_trading_profile.
    Functions that don't write don't need anti-thrash."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (
            target.id if isinstance(target, ast.Name)
            else target.attr if isinstance(target, ast.Attribute)
            else None
        )
        if name in ("update_trading_profile", "set_override",
                    "set_signal_weight", "deprecate_strategy",
                    "add_to_blacklist"):
            return True
    return False


def _function_has_anti_thrash(func: ast.FunctionDef,
                                 src: str) -> bool:
    """True iff the function body has a recognized anti-thrash
    pattern."""
    # Get the function source text (lines [func.lineno-1 :
    # func.end_lineno])
    lines = src.split("\n")
    body_text = "\n".join(lines[func.lineno - 1:
                                  (func.end_lineno or len(lines))])
    for pattern in ANTI_THRASH_PATTERNS:
        if pattern in body_text:
            return True
    # THRASH_OK comment as escape hatch
    if "# THRASH_OK" in body_text:
        return True
    return False


class TestTuningRuleDoesntThrash:
    SCAN_FILES = [
        "self_tuning.py",
        "pipelines/tuning_writer.py",
    ]

    def test_every_writing_optimizer_has_anti_thrash(self):
        violations = []
        scanned_count = 0
        for rel in self.SCAN_FILES:
            path = os.path.join(REPO_ROOT, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path) as fh:
                    src = fh.read()
            except Exception:
                continue
            for func in _find_optimizer_functions(path):
                scanned_count += 1
                if not _function_writes_to_profile(func):
                    continue
                if func.name in KNOWN_THRASH_OK:
                    continue
                if _function_has_anti_thrash(func, src):
                    continue
                violations.append((rel, func.lineno, func.name))
        assert scanned_count >= 5, (
            f"Scanner found only {scanned_count} optimizer "
            f"functions — likely broken; investigate."
        )
        if violations:
            details = "\n".join(
                f"  {rel}:{lineno}  def {fname}(...)"
                for rel, lineno, fname in violations
            )
            pytest.fail(
                f"{len(violations)} tuning rule(s) write parameters "
                f"WITHOUT an anti-thrash guard. Will oscillate on "
                f"noisy signals.\n\nFix one of:\n"
                f"  1. Wrap with _safe_change_guarded(profile_id, "
                f"<param>) at the start\n"
                f"  2. Add an explicit neutral band where the rule "
                f"returns None on borderline signals\n"
                f"  3. Add a `# THRASH_OK: <rationale>` comment "
                f"explaining why this rule needs to re-evaluate "
                f"every cycle\n\nSites:\n{details}"
            )

    def test_known_thrash_ok_entries_match_existing(self):
        all_func_names = set()
        for rel in self.SCAN_FILES:
            path = os.path.join(REPO_ROOT, rel)
            if not os.path.exists(path):
                continue
            for func in _find_optimizer_functions(path):
                all_func_names.add(func.name)
        stale = set(KNOWN_THRASH_OK) - all_func_names
        if stale:
            pytest.fail(
                "KNOWN_THRASH_OK contains entries for functions "
                "that don't exist:\n  "
                + "\n  ".join(sorted(stale))
                + "\n\nRemove these — they protect nothing."
            )
