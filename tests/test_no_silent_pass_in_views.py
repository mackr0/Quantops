"""Cross-cutting AST guardrail: no `except [Exception]: pass` in views.py.

Issue 9 (2026-05-09): views.py had 57 silent-pass exception handlers
that swallowed failures from the dashboard, leaving the user with
missing data and no diagnostic trail. Issue 9 commits 1-3 (SQLite
hardening + open_profile_db + per-site replacement) eliminated all
57 by either:
  - Fixing the root cause that would raise (busy_timeout, schema
    migration), making the wrapper unnecessary
  - Replacing with explicit `logger.warning(...)` that names the
    route + feature + context (profile_id / db_path / symbol)
  - Narrowing to specific exception types (json.JSONDecodeError,
    OSError) for predictable failure modes

This test prevents a regression: any new silent-pass in views.py
fails the build. If a future case genuinely needs to swallow an
exception (e.g. cleanup that can't fail meaningfully), add the
function name to ALLOWLIST below with a one-line rationale.
"""

import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


VIEWS_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "views.py",
)


# Functions where `except: pass` is justified — must include a
# rationale comment. Empty by default; adding to it is the
# enforcement point.
ALLOWLIST: dict = {
    # Format:
    # ("function_name", line_no): "Rationale (date if relevant)",
}


def test_no_except_pass_in_views_py():
    """Every `except [Exception]: pass` (pure-pass body) in views.py
    is a silent failure that hides bugs from the user. Replace with
    `logger.warning(...)` naming the route + feature + context, OR
    narrow the exception type and handle it explicitly, OR fix the
    root cause that would raise."""
    with open(VIEWS_PATH) as f:
        src = f.read()
    tree = ast.parse(src)

    leaks = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            for h in node.handlers:
                # Pure-pass body
                if not (h.body and isinstance(h.body[0], ast.Pass)
                        and len(h.body) == 1):
                    continue
                key = (fn.name, h.lineno)
                if key in ALLOWLIST:
                    continue
                # What exception type is being caught?
                if h.type is None:
                    type_str = "bare except"
                elif isinstance(h.type, ast.Name):
                    type_str = h.type.id
                elif isinstance(h.type, ast.Tuple):
                    type_str = "(" + ", ".join(
                        e.id for e in h.type.elts if isinstance(e, ast.Name)
                    ) + ")"
                else:
                    type_str = "?"
                leaks.append(
                    f"  views.py:{h.lineno} in {fn.name}() — "
                    f"`except {type_str}: pass` silently swallows the "
                    "failure. Replace with logger.warning(...) naming "
                    "the route/feature/context, or fix the root cause."
                )

    assert not leaks, (
        "Found silent-pass exception handlers in views.py. "
        "These hide bugs from the user — there's no way to know "
        "from journald what failed or why. Issue 9 (2026-05-09) "
        "removed all 57; new ones are blocked by this guard.\n\n"
        + "\n".join(leaks)
    )
