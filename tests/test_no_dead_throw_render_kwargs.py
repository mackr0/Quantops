"""Cross-cutting guardrail: no `render_template(..., X=[])` where the
same-named variable was populated earlier in the function.

Caught 2026-05-09: `views.api_performance` (line 2593) called
`render_template("performance.html", ..., tuning_history=[],
tuning_status=[], learned_patterns=[], sec_alerts=[], ...)`. All four
variables had real per-profile DB-query work earlier in the same
function (lines 2208-2235, 2240-2277, 2509-2536). Net result: 4
datasets recomputed on every page load, then thrown away. Plus the
template doesn't even consume them, so they couldn't render anyway.

This test catches the exact "compute then throw away" shape:
- A `render_template(...)` call with a kwarg whose value is a literal
  `[]` or `{}` AND
- A variable of the same name was assigned a non-trivial value in
  the same function body BEFORE the render_template call.

Pure literal `[]` / `{}` defaults that aren't shadowed by an earlier
computation are legit — this targets the dead-throw shape only.
"""

import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


VIEWS_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "views.py",
)


# Allowlist for kwargs that are intentionally always-empty (e.g. a
# render_template that takes a list arg but populates it via JS only).
# Format: (function_name, kwarg_name) → "rationale + date".
ALLOWLIST: dict = {
    # Empty by design — examples to add only when intentional:
    # ("render_some_view", "deferred_blob"): "Computed client-side; 2026-MM-DD",
}


def _is_empty_literal(node):
    """True iff `node` is `[]` or `{}` (or a Constant tuple/set
    that's empty)."""
    if isinstance(node, ast.List) and not node.elts:
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    if isinstance(node, ast.Tuple) and not node.elts:
        return True
    if isinstance(node, ast.Set) and not getattr(node, "elts", []):
        return True
    return False


def _name_assigned_to_nontrivial_in(body_nodes, target_name):
    """Walk `body_nodes` (statements) looking for any assignment to
    `target_name` whose RHS is something other than `[]`/`{}`/`None`.
    Returns line number of the first non-trivial assignment, or None."""
    for node in body_nodes:
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            for tgt in sub.targets:
                if isinstance(tgt, ast.Name) and tgt.id == target_name:
                    # Trivial RHS shapes — empty list/dict/None. We
                    # only flag dead-throws when the variable was
                    # actually populated.
                    rhs = sub.value
                    if _is_empty_literal(rhs):
                        continue
                    if isinstance(rhs, ast.Constant) and rhs.value is None:
                        continue
                    return getattr(sub, "lineno", None)
            # Augmented (`x += [...]`) / list.append patterns also
            # count as populating the variable.
        for sub in ast.walk(node):
            if isinstance(sub, ast.AugAssign):
                tgt = sub.target
                if isinstance(tgt, ast.Name) and tgt.id == target_name:
                    return getattr(sub, "lineno", None)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                # `target_name.append(...)` or `.extend(...)`
                obj = sub.func.value
                if (isinstance(obj, ast.Name)
                        and obj.id == target_name
                        and sub.func.attr in ("append", "extend")):
                    return getattr(sub, "lineno", None)
    return None


def test_no_render_template_kwarg_throws_away_computed_var():
    with open(VIEWS_PATH) as f:
        src = f.read()
    tree = ast.parse(src)

    leaks = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Find render_template calls inside this function
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else None)
            if name != "render_template":
                continue
            for kw in call.keywords:
                if kw.arg is None:  # **kwargs splat
                    continue
                if not _is_empty_literal(kw.value):
                    continue
                if (fn.name, kw.arg) in ALLOWLIST:
                    continue
                # Look for a same-named variable assigned non-trivially
                # before this render_template call.
                # Restrict the search to statements lexically before
                # this call.
                call_lineno = getattr(call, "lineno", 10**9)
                preceding = [s for s in fn.body
                             if getattr(s, "lineno", 0) < call_lineno]
                first_pop = _name_assigned_to_nontrivial_in(
                    preceding, kw.arg,
                )
                if first_pop is not None:
                    leaks.append(
                        f"  views.py:{call_lineno} in {fn.name}() — "
                        f"render_template kwarg `{kw.arg}=[]` (or `{{}}`) "
                        f"throws away the variable populated at "
                        f"line {first_pop}. Either pass the variable "
                        f"or delete the dead computation."
                    )

    assert not leaks, (
        "Found render_template() kwargs hardcoded as empty literals "
        "while the same-named variable was populated earlier in the "
        "function. This is the 2026-05-09 Issue 8 shape — compute "
        "then throw away. Either pass the variable or delete the "
        "dead computation.\n\n" + "\n".join(leaks)
    )
