"""Guardrail: every `logger.X(...)` reference must have a `logger`
defined in the module that contains it.

The classic version of this bug surfaced 2026-05-04 when a long-dormant
code path in trade_pipeline.py (lines 1898, 1904 — strategy-weight
lookup) finally executed in production. The file imports `logging` but
never defines `logger = logging.getLogger(__name__)`, so the call
`logger.debug(...)` raised `NameError: name 'logger' is not defined`,
which crashed the Scan & Trade task entirely.

Other modules use `logging.info(...)` directly OR define `logger =
logging.getLogger(__name__)` at module scope. Mixing both styles in
the same file invites this exact bug — a future code path uses
`logger.X` for symmetry with surrounding code, and if the file uses
`logging.X` everywhere else the `logger` name was never bound.

This test scans every production .py at the repo root and fails if any
file uses `logger.X(...)` without defining `logger` (or without
importing it from another module).
"""
from __future__ import annotations

import os
import re
from typing import List


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# Files we don't scan — venv, third-party, the test suite itself.
_SKIP_DIRS = {"venv", ".venv", "tests", ".pytest_cache", "__pycache__",
               "altdata", "exports", "docs", ".cache", "backups"}

# A "logger" is considered defined when one of these patterns appears.
# Two cheap regexes (avoid alternation backtracking on large files):
#   `logger =`              — assignment
#   `import logger` / `as logger` — explicit import
_LOGGER_ASSIGN_RE = re.compile(r"^\s*logger\s*=", re.MULTILINE)
_LOGGER_IMPORT_RE = re.compile(
    r"^\s*from\s+\S+\s+import\s+[^\n]*\blogger\b", re.MULTILINE,
)
# Any `logger.method(` reference anywhere in the file.
_LOGGER_USE_RE = re.compile(r"\blogger\.[a-zA-Z_]+\s*\(")


def _walk_python_files() -> List[str]:
    paths: List[str] = []
    root = _repo_root()
    for entry in os.listdir(root):
        if entry in _SKIP_DIRS:
            continue
        full = os.path.join(root, entry)
        if os.path.isfile(full) and full.endswith(".py"):
            paths.append(full)
    return paths


def test_every_logger_use_has_a_definition():
    failures: List[str] = []
    for path in _walk_python_files():
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if not _LOGGER_USE_RE.search(content):
            continue
        if (_LOGGER_ASSIGN_RE.search(content)
                or _LOGGER_IMPORT_RE.search(content)):
            continue
        # Use found, no def — list line numbers for readability
        lines = []
        for i, line in enumerate(content.split("\n"), 1):
            if _LOGGER_USE_RE.search(line):
                lines.append(f"  {os.path.basename(path)}:{i}  {line.strip()}")
        failures.append(
            f"{os.path.basename(path)} uses `logger.X(...)` without "
            f"defining `logger` at module scope:\n" + "\n".join(lines)
        )
    assert not failures, (
        "Bare `logger.X(...)` references with no `logger` definition — "
        "this is the failure mode that crashed Small Cap Scan & Trade "
        "on 2026-05-04. Either:\n"
        "  - Add `logger = logging.getLogger(__name__)` at the top of "
        "the file, OR\n"
        "  - Replace the bare `logger.X` calls with `logging.X` to "
        "match the rest of the file's style.\n\nOffenders:\n  "
        + "\n  ".join(failures)
    )
