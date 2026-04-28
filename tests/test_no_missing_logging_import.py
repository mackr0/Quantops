"""Guardrail: any module that uses `logging.*` must `import logging`.

History: on 2026-04-28 a `NameError: name 'logging' is not defined`
fired every Check Exits cycle for the Large Cap Limit Orders
profile because two `logging.info`/`logging.debug` calls were
added to `trader.py` (short-borrow accrual + MFE updater on
2026-04-27, commit `e2c040d`) without adding `import logging` at
the top.

The cycle's exit task silently failed for ~24 hours before the
user spotted it in the dashboard's Scan Failures panel.

This test AST-walks every .py file in the repo. For each file:
- Find all `Name(id='logging')` references in the function bodies
  (i.e., `logging.info(...)`, `logging.warning(...)`, etc.)
- Find all `Import(name='logging')` statements at module scope
- If the file uses `logging.X` but doesn't import logging → fail.

Skips test files (they often mock logging) and venv/cache dirs.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Skip these — they're test files / generated / vendored.
_SKIP_PATTERNS = (
    "tests/",
    "/venv/",
    "/.venv/",
    "/__pycache__/",
    "/.git/",
    "exports/",
)


def _python_files():
    """Iterate every .py file under the repo root."""
    for path in REPO_ROOT.rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT))
        if any(p in rel for p in _SKIP_PATTERNS):
            continue
        # Skip test files at top level too (e.g., test_*.py outside tests/)
        if rel.startswith("test_") or rel == "tests":
            continue
        yield path


def _imports_logging(tree: ast.AST) -> bool:
    """True if the module has `import logging` or `from logging import X`
    at any scope (module / function-local). We allow nested imports."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "logging":
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "logging":
                return True
    return False


def _uses_logging_attr(tree: ast.AST) -> bool:
    """True if any `Attribute` access has `logging` as its base —
    i.e., the file calls `logging.info(...)`, `logging.warning(...)`,
    or any other `logging.X`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "logging":
                return True
    return False


def test_every_logging_user_imports_logging():
    """For each repo .py file, if it uses `logging.X` at any depth,
    it must also import logging at any scope."""
    offenders = []
    for path in _python_files():
        try:
            src = path.read_text()
        except Exception:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        if _uses_logging_attr(tree) and not _imports_logging(tree):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    if offenders:
        details = "\n".join(f"  - {f}" for f in sorted(offenders))
        pytest.fail(
            "The following module(s) use `logging.X` (e.g.,\n"
            "`logging.info(...)`, `logging.warning(...)`) but don't\n"
            "`import logging`. NameError fires every time the call site\n"
            "executes — a silent regression that only surfaces when the\n"
            "code path runs in production. See the 2026-04-28 incident\n"
            "in CHANGELOG: trader.py's check_exits failed every cycle\n"
            "for ~24 hours before being caught.\n"
            f"\n{details}"
        )
