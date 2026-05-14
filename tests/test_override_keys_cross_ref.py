"""Structural guardrail (2026-05-13): every override-dict key
written by tuning code must be a key the read path actually looks
up.

The bug class.
Override JSON columns (`signal_weights`, `regime_overrides`,
`tod_overrides`, `symbol_overrides`) are KEYED by parameter name.
A tuning rule that writes
    set_override(pid, "typo_param_name", ...)
silently no-ops because `parse_overrides` filters out keys not in
`PARAM_BOUNDS` on the read path. Result: tuning rule "ran",
tuning_history shows the change, dashboards display it — but
NOTHING applies it. The system thinks it's tuning; it's not.

This test scans tuning-writer modules for `set_override` /
`set_signal_weight` / equivalent calls. Where the param_name
argument is a string literal (hard-coded), validate it against
the corresponding registry:
  - regime/tod/symbol overrides → PARAM_BOUNDS
  - signal_weights → signal_weights.WEIGHTABLE_SIGNALS

Runtime-variable param names (loop iteration, dict-key dispatch)
can't be statically validated and are skipped — but the
hard-coded typo class IS catchable and IS dangerous.

Why not also runtime-validate?
Running each tuning function with controlled inputs would catch
runtime-variable typos too but requires heavyweight per-rule
fixtures. The hard-coded literal scan is the highest-ROI 80%
of the value with 20% of the test infrastructure.
"""
from __future__ import annotations

import ast
import os
import sys
from typing import Iterable, List, Set, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_param_bounds() -> Set[str]:
    from param_bounds import PARAM_BOUNDS
    return set(PARAM_BOUNDS.keys())


def _load_weightable_signals() -> Set[str]:
    try:
        from signal_weights import WEIGHTABLE_SIGNALS
        # WEIGHTABLE_SIGNALS may be a list, set, or dict
        if isinstance(WEIGHTABLE_SIGNALS, dict):
            return set(WEIGHTABLE_SIGNALS.keys())
        return set(WEIGHTABLE_SIGNALS)
    except Exception:
        return set()


# Map (callable_name, arg_position_of_param_name) → registry-set
# loader. The arg position is 0-indexed in the *positional* args
# (not counting profile_id which is always position 0).
#
# E.g. `set_override(profile_id, param_name, ...)` → param_name
# is at positional index 1.
WRITER_PATTERNS = [
    # regime_overrides / tod_overrides / symbol_overrides
    # all share the same shape: set_override(pid, param, ...)
    # All three resolve param against PARAM_BOUNDS via parse_overrides.
    ("set_override", 1, "PARAM_BOUNDS"),
    # signal_weights: set_signal_weight(pid, signal_name, weight)
    ("set_signal_weight", 1, "WEIGHTABLE_SIGNALS"),
]


def _extract_string_args(
    src_path: str,
    pattern_name: str,
    arg_index: int,
) -> List[Tuple[int, str]]:
    """Walk the AST of `src_path`, find calls to functions named
    `pattern_name`, and return (lineno, string_arg) pairs where the
    requested arg position holds a string literal.

    Skips calls whose target arg is a Name/Attribute/etc. (runtime
    variable) — those need different validation.
    """
    try:
        with open(src_path, "r") as fh:
            src = fh.read()
    except Exception:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match by callable name (handles both bare set_override
        # and module.set_override after `from X import set_override`)
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name != pattern_name:
            continue
        if len(node.args) <= arg_index:
            continue
        target = node.args[arg_index]
        # Only catch string-literal hard-codes.
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            out.append((node.lineno, target.value))
    return out


def _scan_files() -> Iterable[str]:
    """Files to audit. Tuning-writers live primarily in self_tuning.py
    plus the per-pipeline tuners and view-handler tuning helpers."""
    candidates = [
        "self_tuning.py",
        "pipelines/option.py",
        "pipelines/stock.py",
        "pipelines/tuning_writer.py",
        "insight_propagation.py",
    ]
    for rel in candidates:
        path = os.path.join(REPO_ROOT, rel)
        if os.path.exists(path):
            yield path


class TestOverrideKeysCrossRef:
    def test_set_override_param_names_are_valid(self):
        """Every hard-coded `set_override(pid, '<param>', ...)`
        must use a param_name that's in PARAM_BOUNDS — otherwise
        the override is silently dropped on read."""
        param_bounds = _load_param_bounds()
        if not param_bounds:
            pytest.skip("PARAM_BOUNDS not loadable — skipping")

        violations = []
        for src_path in _scan_files():
            rel = os.path.relpath(src_path, REPO_ROOT)
            for lineno, val in _extract_string_args(
                    src_path, "set_override", 1):
                if val not in param_bounds:
                    violations.append((rel, lineno, val))
        if violations:
            details = "\n".join(
                f"  {rel}:{lineno}  set_override(..., '{val}', ...)  "
                f"— '{val}' not in PARAM_BOUNDS"
                for rel, lineno, val in violations
            )
            pytest.fail(
                "Tuning code writes override keys that the read "
                "path silently drops:\n\n"
                + details
                + "\n\nBug class: parse_overrides filters out "
                "param_names not in PARAM_BOUNDS, so the override "
                "stores in JSON but never gets applied to a trade. "
                "tuning_history shows the change but the system "
                "behaves as if it never happened. Either:\n"
                "  1. Fix the typo so it matches PARAM_BOUNDS\n"
                "  2. Add the param_name to param_bounds.PARAM_BOUNDS"
            )

    def test_set_signal_weight_signal_names_are_valid(self):
        """Same shape, signal_weights registry."""
        signals = _load_weightable_signals()
        if not signals:
            pytest.skip("WEIGHTABLE_SIGNALS not loadable — skipping")

        violations = []
        for src_path in _scan_files():
            rel = os.path.relpath(src_path, REPO_ROOT)
            for lineno, val in _extract_string_args(
                    src_path, "set_signal_weight", 1):
                if val not in signals:
                    violations.append((rel, lineno, val))
        if violations:
            details = "\n".join(
                f"  {rel}:{lineno}  set_signal_weight(..., '{val}', "
                f"...)  — '{val}' not in WEIGHTABLE_SIGNALS"
                for rel, lineno, val in violations
            )
            pytest.fail(
                "Tuning code writes signal weights that the read "
                "path silently drops:\n\n" + details
                + "\n\nFix typo or add the signal name to "
                "signal_weights.WEIGHTABLE_SIGNALS."
            )

    def test_ast_scanner_works_on_known_sample(self):
        """Sanity: confirm the AST walker correctly extracts
        string-literal args. This tests the SCANNER, not production
        code — production today calls set_override(pid, param, ...)
        where `param` is a loop variable (AST Name, not Constant),
        so the walker has nothing to validate. That's actually
        good news (no hard-coded typos), but means the previous
        two tests are no-ops on current code. The walker still
        defends against future drift (anyone adding a hard-coded
        call gets caught). This test verifies the walker itself
        isn't broken."""
        # Write a temp file with a known-shape call, run the walker,
        # confirm it extracts the literal.
        import tempfile
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False) as fh:
            fh.write(
                "def x():\n"
                "    set_override(profile_id, 'stop_loss_pct', 0.04)\n"
                "    set_signal_weight(profile_id, 'breakout', 0.7)\n"
            )
            tmp = fh.name
        try:
            so_hits = _extract_string_args(tmp, "set_override", 1)
            ssw_hits = _extract_string_args(tmp, "set_signal_weight", 1)
        finally:
            os.unlink(tmp)
        assert so_hits == [(2, "stop_loss_pct")], so_hits
        assert ssw_hits == [(3, "breakout")], ssw_hits
