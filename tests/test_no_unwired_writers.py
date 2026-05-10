"""Cross-cutting guardrail: no unwired CRUD writers.

Caught 2026-05-09: `models.log_decision()` had been defined on day 1
of the multi-user platform (commit 4647854 — "Add multi-user web
platform with Flask UI"), wired into a UI panel, given a reader
function (`get_decisions`), and SHIPPED. But it was NEVER called
from production code. The `decision_log` table had zero rows in its
entire lifetime. The Recent Activity dashboard panel and the
trades.html decision audit table were silently broken since day 1
because `{% if decisions %}` hid the empty UI from view.

Six weeks later (commit b59b48d, 2026-03-28), the parallel
`activity_log` table + Strategy Activity Ticker was added and wired
correctly. Nobody noticed the original was dead.

This test catches the meta-pattern: any function defined in
`models.py` whose body contains an `INSERT INTO ...` statement MUST
have at least one caller in production code (excluding `models.py`
itself, tests, and the venv). If you ship a writer with no consumer,
this test fails — surfaces the half-built feature BEFORE the
{% if %} masks it for months.
"""

import ast
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


MODELS_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "models.py",
)
REPO_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)


def _writer_functions_in_models():
    """Return list of (function_name, line_no) for every top-level
    function in models.py whose body contains an `INSERT INTO ...`
    or `INSERT OR REPLACE INTO ...` statement."""
    with open(MODELS_PATH) as f:
        src = f.read()
    tree = ast.parse(src)

    INSERT_RE = re.compile(
        r"INSERT\s+(?:OR\s+(?:REPLACE|IGNORE)\s+)?INTO\s+\w+",
        re.IGNORECASE,
    )

    writers = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Concat every string literal in the body so we catch SQL
        # split across adjacent string literals.
        body_strings = []
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                body_strings.append(child.value)
        body_blob = " ".join(body_strings)
        if INSERT_RE.search(body_blob):
            writers.append((node.name, node.lineno))
    return writers


def _has_caller_in_production(fn_name):
    """grep entire repo for `<fn_name>(` calls in production code
    (excluding venv, .git, __pycache__, tests, node_modules).

    A call site counts if it's NOT the function's own def line.
    Internal models.py calls count — if `cache_symbol_names()` is
    called by `get_cached_names()` inside models.py, and
    `get_cached_names()` is reachable from outside, then
    `cache_symbol_names` is reachable too. The original 2026-05-09
    `log_decision` bug had zero call sites of ANY kind — that's the
    shape this catches.
    """
    r = subprocess.run(
        ["grep", "-rEn", "--include=*.py",
         "--exclude-dir=venv", "--exclude-dir=.git",
         "--exclude-dir=__pycache__", "--exclude-dir=tests",
         "--exclude-dir=node_modules",
         r"\b" + re.escape(fn_name) + r"\(",
         REPO_ROOT],
        capture_output=True, text=True,
    )
    def_re = re.compile(r":\s*\d+:\s*(?:async\s+)?def\s+"
                         + re.escape(fn_name) + r"\b")
    for line in r.stdout.splitlines():
        if def_re.search(line):
            continue
        return True
    return False


# --- Allowlist for writers that legitimately have no caller yet ---
# Add a name here ONLY if you're shipping scaffolding intentionally
# AND have a tracked TODO with a date. Never silently allowlist.
ALLOWLIST: dict = {
    # Example format:
    # "log_some_future_thing": "Wired up by Phase-3 work, OPEN_ITEMS #42, by 2026-06-01",
}


def test_no_unwired_writer_functions_in_models():
    """Every function in models.py that INSERTs into a table must
    have at least one production-code caller. The 2026-05-09
    log_decision incident is the prototype: a writer shipped in
    commit 4647854, never called, dead for the entire repo history,
    silently broken UI that {% if %} hid.

    Failure means: you shipped scaffolding. Either wire it up or
    delete it; don't leave it as a trap for the next person.

    To intentionally ship scaffolding, add the name to ALLOWLIST
    above with a comment explaining when/why it'll be wired up.
    """
    writers = _writer_functions_in_models()
    unwired = []
    for name, line in writers:
        if name.startswith("_"):
            # Private helpers don't need external callers — they're
            # invoked from sibling functions inside models.py.
            continue
        if name in ALLOWLIST:
            continue
        if not _has_caller_in_production(name):
            unwired.append(f"  models.py:{line} — {name}() has zero "
                            "callers outside models.py / tests / venv. "
                            "Either wire it up or delete it.")
    assert not unwired, (
        "Found CRUD writer function(s) in models.py with no production "
        "caller. This is the 2026-05-09 log_decision shape — DOA "
        "scaffolding that gets hidden by `{% if %}` and rots for "
        "months. Delete or wire up.\n\n"
        + "\n".join(unwired)
    )
