"""Guardrail: optimizer return strings (which flow into user-facing
surfaces — activity ticker, weekly digest, dashboard tuning history)
must not contain raw snake_case parameter names. Use _label(name) or
display_name(name) when interpolating a parameter into a return
string.

Why this exists: on 2026-04-25 the user saw "atr_multiplier_tp" leak
to the dashboard ticker because every _optimize_* function in the
W1/W2/W3 batch was returning strings like "Tightened
atr_multiplier_tp from 3.00 to 2.75 (...)" — embedding the raw column
name directly in user-facing text. The display_name registry was
correct; the bug was that the registry was never consulted in those
return strings.

This test AST-walks self_tuning.py, finds every _optimize_* function,
extracts every string literal (including f-string parts) it could
return, and fails if any contains a raw parameter name from
PARAM_BOUNDS. Forces the author to use the _label() helper or rephrase
the message in plain English ("Reduced max concurrent positions"
instead of "Reduced max_total_positions")."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Strings allowed even though they look like snake_case parameter
# names — these are part of error/log messages OR are intentionally
# referenced as column names (e.g., docstring code samples). Keep this
# list small.
ALLOWED_LITERAL_SUBSTRINGS = {
    # Allow nothing — every parameter mention must go through _label().
}


def _self_tuning_source() -> str:
    return (Path(__file__).resolve().parent.parent / "self_tuning.py").read_text()


_LABEL_HELPERS = {"_label", "display_name"}


def _is_safe_constant_arg(node, parent_map):
    """A string Constant is "safe" if it's a direct positional argument to
    a Call whose function is _label() or display_name() — that's the
    correct way to interpolate a parameter name into user-facing text."""
    parent = parent_map.get(id(node))
    if not isinstance(parent, ast.Call):
        return False
    if node not in parent.args:
        return False
    fn = parent.func
    name = None
    if isinstance(fn, ast.Name):
        name = fn.id
    elif isinstance(fn, ast.Attribute):
        name = fn.attr
    return name in _LABEL_HELPERS


def _walk_strings_in(node, parent_map):
    """Yield string-literal `value` for every Constant under `node` that is
    NOT a safe argument to _label() / display_name(). Includes f-string
    Constant parts."""
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if not _is_safe_constant_arg(child, parent_map):
                yield child.value
        elif isinstance(child, ast.JoinedStr):
            for piece in child.values:
                if (isinstance(piece, ast.Constant)
                        and isinstance(piece.value, str)
                        and not _is_safe_constant_arg(piece, parent_map)):
                    yield piece.value


def _build_parent_map(tree):
    """Map id(node) -> parent_node for every node in the tree. Used by
    _is_safe_constant_arg to look up containing Call expressions."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _bounded_param_names():
    """Return the set of every column name that has bounds defined —
    the universe of parameter identifiers the tuner manipulates."""
    from param_bounds import PARAM_BOUNDS
    return set(PARAM_BOUNDS.keys())


class TestNoSnakeCaseInOptimizerStrings:
    def test_optimizer_return_strings_do_not_embed_param_names(self):
        src = _self_tuning_source()
        tree = ast.parse(src)
        parent_map = _build_parent_map(tree)
        param_names = _bounded_param_names()

        offenders = []  # list of (function_name, lineno, offending_substring, full_string)

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("_optimize_"):
                continue
            # For each Return statement in this function, scan all
            # string literals it could produce.
            for ret in (n for n in ast.walk(node) if isinstance(n, ast.Return)):
                if ret.value is None:
                    continue
                for s in _walk_strings_in(ret.value, parent_map):
                    for pname in param_names:
                        # Match parameter name as a whole "word" (snake_case
                        # tokens are surrounded by spaces, punctuation, or
                        # string boundaries — not by other letters/digits).
                        # Cheap check: must contain underscore-bearing pname
                        # AND not be already inside a longer identifier.
                        if pname in s and pname not in ALLOWED_LITERAL_SUBSTRINGS:
                            # Filter false positives where pname is a
                            # substring of a longer word (e.g., "max_price"
                            # matching inside "max_priced_in"). Boundary check:
                            idx = s.find(pname)
                            before = s[idx - 1] if idx > 0 else " "
                            after = (s[idx + len(pname)]
                                     if idx + len(pname) < len(s) else " ")
                            if (before.isalnum() or before == "_") or (
                                    after.isalnum() or after == "_"):
                                continue
                            offenders.append((node.name, ret.lineno, pname, s.strip()))

        if offenders:
            details = "\n".join(
                f"  {fn}() (line {ln}): "
                f"contains raw '{pname}' in: {text!r}"
                for fn, ln, pname, text in offenders
            )
            pytest.fail(
                "Optimizer return string(s) embed raw snake_case parameter\n"
                "names. These strings flow into user-facing surfaces (activity\n"
                "ticker, weekly digest, tuning-history table). Use the\n"
                "`_label(name)` helper inside f-strings to render the\n"
                "human-readable label, OR rephrase the message in plain\n"
                "English (e.g., 'max concurrent positions' instead of\n"
                "'max_total_positions').\n\n"
                f"Offenders:\n{details}"
            )

    def test_label_helper_exists_and_uses_display_name(self):
        """Sanity: the _label helper that author should be using exists."""
        from self_tuning import _label
        # Must round-trip through the display_names registry.
        assert _label("max_correlation") == "Max Correlation"
        assert _label("atr_multiplier_tp") == "ATR Target Multiplier"
