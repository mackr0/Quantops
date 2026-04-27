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
        """Walks EVERY function in self_tuning.py — not just
        `_optimize_*` — and EVERY string literal those functions
        produce, including ones passed to .append() / log calls /
        f-string args. The previous version of this test only
        walked Return statements inside `_optimize_*` functions and
        missed the orchestrator-level string at line 1331 that put
        `max_position_pct 0.08->0.092` directly into the activity
        ticker on 2026-04-27.

        Coverage is now: every string literal anywhere in the
        module that's NOT a safe argument to `_label()` /
        `display_name()` / `format_param_value()` is checked for
        embedded PARAM_BOUNDS keys.
        """
        src = _self_tuning_source()
        tree = ast.parse(src)
        parent_map = _build_parent_map(tree)
        param_names = _bounded_param_names()

        offenders = []  # list of (function_name, lineno, offending_substring, full_string)

        # Module-level docstring — the very first statement of the
        # module if it's a string Constant. Skip it from the scan.
        module_docstring_id = None
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)):
            module_docstring_id = id(tree.body[0].value)

        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Skip the function's own docstring (first statement,
            # string Constant) so doc text doesn't trip the guard.
            func_docstring_id = None
            if (func.body and isinstance(func.body[0], ast.Expr)
                    and isinstance(func.body[0].value, ast.Constant)
                    and isinstance(func.body[0].value.value, str)):
                func_docstring_id = id(func.body[0].value)

            for child in ast.walk(func):
                # Each string Constant under this function. _walk_strings_in
                # already filters to non-safe args; we just need to
                # additionally exclude docstrings.
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    if id(child) == func_docstring_id:
                        continue
                    if id(child) == module_docstring_id:
                        continue
                    if _is_safe_constant_arg(child, parent_map):
                        continue
                    s = child.value
                    # Standalone string equal to the param name is an
                    # INTERNAL identifier (DB column name, kwargs key,
                    # log_tuning_change action_type, dict key). The
                    # user never reads these. We only care about
                    # param names embedded INSIDE a longer string —
                    # that's where they leak to user-facing surfaces.
                    if s.strip() in param_names:
                        continue
                    for pname in param_names:
                        if pname not in s:
                            continue
                        if pname in ALLOWED_LITERAL_SUBSTRINGS:
                            continue
                        idx = s.find(pname)
                        before = s[idx - 1] if idx > 0 else " "
                        after = (s[idx + len(pname)]
                                 if idx + len(pname) < len(s) else " ")
                        if (before.isalnum() or before == "_") or (
                                after.isalnum() or after == "_"):
                            continue
                        offenders.append((func.name, child.lineno, pname, s.strip()))

        if offenders:
            details = "\n".join(
                f"  {fn}() (line {ln}): "
                f"contains raw '{pname}' in: {text!r}"
                for fn, ln, pname, text in offenders
            )
            pytest.fail(
                "self_tuning.py contains string literal(s) that embed raw\n"
                "snake_case parameter names. These strings flow into\n"
                "user-facing surfaces (activity ticker, weekly digest,\n"
                "tuning-history table, AI prompt 'past adjustments' block).\n"
                "Use `_label(name)` to render the human-readable label and\n"
                "`format_param_value(name, value)` for the value, OR\n"
                "rephrase the message in plain English.\n\n"
                f"Offenders:\n{details}"
            )

    def test_label_helper_exists_and_uses_display_name(self):
        """Sanity: the _label helper that author should be using exists."""
        from self_tuning import _label
        # Must round-trip through the display_names registry.
        assert _label("max_correlation") == "Max Correlation"
        assert _label("atr_multiplier_tp") == "ATR Target Multiplier"


# ---------------------------------------------------------------------------
# Decimal-formatting guard — catches "0.08->0.092" instead of "8.0% → 9.2%"
# ---------------------------------------------------------------------------

class TestNoRawDecimalsForPercentageParams:
    """Percentage-typed parameters (max_position_pct, stop_loss_pct, etc.)
    must be rendered through `format_param_value(name, value)` before
    appearing in user-facing strings. Otherwise the activity ticker shows
    "max_position_pct 0.08->0.092" instead of "Max Position Size 8.0% → 9.2%".

    Caught the same day as the snake_case orchestrator-level leak:
    the original guard only checked `_optimize_*` Returns, missing the
    string at `apply_auto_adjustments`'s past-adjustment review block.
    The strengthened version of the snake_case guard above now walks
    every function. THIS test adds the value-side coverage.

    Heuristic: in any string literal that contains the SUBSTRING
    `{old_v}->` or `{new_v}->` or `{old_val}` or `{new_val}` or similar
    "old/new value" f-string interpolations, we walk back to the
    parent FormattedValue / JoinedStr to confirm the value comes
    from a Name node (not a `format_param_value(...)` call). If the
    surrounding f-string mentions a percentage param name, fail.

    Less heuristic: scan every string literal that contains a percentage
    parameter name. If the same f-string contains a raw `{...}`
    interpolation that's NOT wrapped in `format_param_value`, the test
    flags it as a likely raw-decimal leak.
    """

    def test_percentage_params_are_formatted_through_helper(self):
        from display_names import _PERCENTAGE_PARAMS as PCT_PARAMS
        src = _self_tuning_source()
        tree = ast.parse(src)
        parent_map = _build_parent_map(tree)

        # Map id(call) -> True for every Call to format_param_value /
        # _fmt / similar. Used to confirm an f-string interpolation
        # wraps its value through the formatter.
        SAFE_FORMATTERS = {"format_param_value", "_fmt"}

        offenders = []

        for jstr in ast.walk(tree):
            if not isinstance(jstr, ast.JoinedStr):
                continue

            # Reconstruct the literal-string skeleton of the f-string.
            literal_parts = []
            for piece in jstr.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    literal_parts.append(piece.value)
            literal_text = "".join(literal_parts)

            # Does this f-string mention any percentage param name?
            mentioned_pcts = [p for p in PCT_PARAMS if p in literal_text]
            if not mentioned_pcts:
                continue

            # Does this f-string contain a raw-Name interpolation NOT
            # wrapped through format_param_value / _fmt? Walk each
            # FormattedValue.
            for piece in jstr.values:
                if not isinstance(piece, ast.FormattedValue):
                    continue
                # Inspect what's inside {...}. Acceptable shapes:
                # - Call to format_param_value / _fmt
                # - Constant
                # - Anything that resolves through a safe wrapper
                value_node = piece.value
                if isinstance(value_node, ast.Call):
                    fn = value_node.func
                    fname = (fn.id if isinstance(fn, ast.Name)
                             else fn.attr if isinstance(fn, ast.Attribute)
                             else None)
                    if fname in SAFE_FORMATTERS:
                        continue
                # A raw Name interpolation. Look at variable name —
                # if it suggests an old/new value of a percentage
                # param, this is a raw-decimal leak.
                if isinstance(value_node, ast.Name):
                    var = value_node.id
                    if var in {"old_v", "new_v", "old_val", "new_val",
                               "current", "new_pct", "current_pct"}:
                        offenders.append((
                            jstr.lineno, var, mentioned_pcts,
                            literal_text.strip()[:120],
                        ))

        if offenders:
            details = "\n".join(
                f"  line {ln}: f-string mentions {mentioned!r} but interpolates "
                f"`{var}` raw (not via format_param_value); text: {text!r}"
                for ln, var, mentioned, text in offenders
            )
            pytest.fail(
                "self_tuning.py builds an f-string that mentions a percentage\n"
                "parameter name AND interpolates a raw old/new value. The user\n"
                "sees '0.08->0.092' instead of '8.0% → 9.2%'. Wrap the value\n"
                "through `format_param_value(name, value)` (imported as `_fmt`).\n"
                "\n"
                f"Offenders:\n{details}"
            )
