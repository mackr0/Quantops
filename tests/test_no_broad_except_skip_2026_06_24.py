"""Structural guard: no test may swallow a broad exception into a skip.

2026-06-24 — `test_no_silent_anthropic_fallback`'s segment-ctx security
check had silently stopped running: it called
`build_context_from_segment("largecap")`, but segments were restructured
to `['stocks','crypto']`, so `get_segment` raised `KeyError` — which a
`except Exception: pytest.skip(...)` handler swallowed. The check had not
executed for weeks, reported only as an innocuous "1 skipped".

The anti-pattern: wrapping real test logic in a broad `except`
(`Exception` / `BaseException` / bare) and turning ANY failure into
`pytest.skip`. A genuine regression then masquerades as a skipped test
instead of a red failure — the exact silent-failure class this codebase
forbids.

This test pins the contract structurally (AST), so the pattern can't
return. Legitimate conditional skips remain allowed — just guard them on
an explicit precondition (`if not _in_git_repo(): pytest.skip(...)`) or
catch a NARROW, specific exception type (e.g.
`except subprocess.CalledProcessError`) so unexpected errors still surface.
"""
from __future__ import annotations

import ast
import glob
import os

import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))

# Exception types broad enough that catching them and skipping would
# swallow a genuine bug. Narrow types (KeyError, FileNotFoundError,
# subprocess.CalledProcessError, ImportError, ...) are fine — they name
# a specific, expected non-run condition.
_BROAD_NAMES = {"Exception", "BaseException"}


def _handler_is_broad(handler: ast.ExceptHandler) -> bool:
    """A bare `except:` or `except Exception/BaseException:` (alone or in
    a tuple) is broad."""
    t = handler.type
    if t is None:
        return True  # bare except
    candidates = t.elts if isinstance(t, ast.Tuple) else [t]
    for node in candidates:
        # Match `Exception`, `BaseException`, and dotted forms like
        # `builtins.Exception`.
        if isinstance(node, ast.Name) and node.id in _BROAD_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _BROAD_NAMES:
            return True
    return False


def _calls_pytest_skip(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "skip"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "pytest"
        ):
            return True
    return False


def _scan_file(path: str):
    """Return list of (lineno,) violations in one test file."""
    with open(path, encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=path)
        except SyntaxError:
            return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _handler_is_broad(node):
            # Only the handler body counts — a skip in the `try` body is
            # an unconditional skip, caught by a different review, not this
            # swallow-the-error pattern.
            if any(_calls_pytest_skip(stmt) for stmt in node.body):
                out.append(node.lineno)
    return out


def test_no_test_swallows_broad_exception_into_skip():
    violations = []
    for path in sorted(glob.glob(os.path.join(TESTS_DIR, "*.py"))):
        for lineno in _scan_file(path):
            rel = os.path.relpath(path, os.path.dirname(TESTS_DIR))
            violations.append(f"  {rel}:{lineno}")

    assert not violations, (
        "Test(s) swallow a broad exception into pytest.skip — a genuine "
        "regression would silently masquerade as a skip instead of "
        "failing red (this is exactly how a security test stopped running "
        "for weeks, 2026-06-24):\n\n"
        + "\n".join(violations)
        + "\n\nFix: guard the skip on an explicit precondition, or catch a "
        "NARROW exception type (KeyError / FileNotFoundError / "
        "subprocess.CalledProcessError / ...) so unexpected errors surface."
    )


def test_guard_detects_a_broad_except_skip():
    """Self-check: the detector actually fires on the anti-pattern."""
    sample = (
        "import pytest\n"
        "def test_x():\n"
        "    try:\n"
        "        do_real_work()\n"
        "    except Exception:\n"
        "        pytest.skip('masked')\n"
    )
    tree = ast.parse(sample)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _handler_is_broad(node):
            if any(_calls_pytest_skip(s) for s in node.body):
                hits.append(node.lineno)
    assert hits, "Detector failed to flag a broad-except-into-skip sample"


def test_guard_allows_narrow_except_skip():
    """Self-check: a NARROW except → skip is permitted."""
    sample = (
        "import pytest, subprocess\n"
        "def test_y():\n"
        "    try:\n"
        "        do_real_work()\n"
        "    except subprocess.CalledProcessError:\n"
        "        pytest.skip('not a repo')\n"
    )
    tree = ast.parse(sample)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            assert not _handler_is_broad(node), (
                "Narrow except (CalledProcessError) wrongly flagged as broad"
            )
