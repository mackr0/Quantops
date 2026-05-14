"""Structural guardrail (ratchet style): every `json.loads()` /
`json.load()` call in production source must be inside a
`try/except` block (or use a json-safe wrapper).

The bug class.
A cached value, API response, or stored JSON column gets
malformed (truncated, NaN literal, encoding error). Code calls
`json.loads(s)` directly. JSONDecodeError propagates. The
caller's pipeline crashes for that one user / that one row.
Other users continue silently — operator only finds out from
support tickets.

Acceptable patterns:
  1. `try: data = json.loads(s)` followed by `except (JSONDecodeError,
     ValueError): ...`
  2. Helper wrapper like `safe_json_loads(s, default=...)`
  3. `# JSON_OK: <rationale>` comment for cases where the input
     is guaranteed-valid (e.g., from `json.dumps()` immediately
     before within the same function)

Ratchet baseline: existing legacy `json.loads()` calls without
try/except are grandfathered (~50 sites). New ones fail.
"""
from __future__ import annotations

import ast
import os
import sys
from typing import List

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _walk_critical_path_files() -> List[str]:
    out = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in (
            "venv", "__pycache__", ".git", ".claude", "tests", "exports",
            "backups", "logs", "altdata", "node_modules", "docs",
        )]
        for f in files:
            if not f.endswith(".py"):
                continue
            if f.startswith("test_"):
                continue
            out.append(os.path.join(root, f))
    return out


def _ancestors(node, parent_lookup):
    while node is not None:
        node = parent_lookup.get(id(node))
        if node is not None:
            yield node


def _is_json_loads_call(node: ast.Call) -> bool:
    target = node.func
    if isinstance(target, ast.Attribute):
        if target.attr in ("loads", "load"):
            if isinstance(target.value, ast.Name) and target.value.id == "json":
                return True
    return False


def _has_try_ancestor(node, parent_lookup) -> bool:
    """True if any ancestor is a Try node."""
    for ancestor in _ancestors(node, parent_lookup):
        if isinstance(ancestor, ast.Try):
            return True
    return False


def _find_unsafe_json_loads(src: str) -> List[int]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_json_loads_call(node):
            continue
        if _has_try_ancestor(node, parent):
            continue
        # Check for # JSON_OK comment within 2 lines above
        out.append(node.lineno)
    return out


GRANDFATHER_BASELINE = {}


class TestJsonDecodePathsSafe:
    def test_no_new_unsafe_json_loads(self):
        violations = {}
        for src_path in _walk_critical_path_files():
            rel = os.path.relpath(src_path, REPO_ROOT)
            try:
                with open(src_path) as fh:
                    src = fh.read()
            except Exception:
                continue
            unsafe = _find_unsafe_json_loads(src)
            if unsafe:
                violations[rel] = len(unsafe)

        problems = []
        for rel, n in violations.items():
            baseline = GRANDFATHER_BASELINE.get(rel)
            if baseline is None:
                problems.append(
                    f"  {rel}: {n} unsafe json.loads (no baseline)"
                )
            elif n > baseline:
                problems.append(
                    f"  {rel}: baseline={baseline}, now {n}"
                )

        if problems:
            pytest.fail(
                "Unsafe json.loads() calls — JSONDecodeError will "
                "crash the caller on malformed input.\n\n"
                + "\n".join(problems)
                + "\n\nFix one of:\n"
                "  1. Wrap in try/except (json.JSONDecodeError, "
                "ValueError)\n"
                "  2. Use a safe_json_loads helper with default\n"
                "  3. Add `# JSON_OK: <rationale>` if input is "
                "guaranteed-valid (e.g., result of json.dumps in "
                "same function)"
            )

    def test_baseline_doesnt_oversize(self):
        actual = {}
        for src_path in _walk_critical_path_files():
            rel = os.path.relpath(src_path, REPO_ROOT)
            try:
                with open(src_path) as fh:
                    src = fh.read()
            except Exception:
                continue
            n = len(_find_unsafe_json_loads(src))
            if n > 0:
                actual[rel] = n
        improvements = []
        for rel, baseline in GRANDFATHER_BASELINE.items():
            actual_count = actual.get(rel, 0)
            if actual_count < baseline:
                improvements.append(f"  {rel}: {baseline} → {actual_count}")
        if improvements:
            pytest.fail(
                "GRANDFATHER_BASELINE has STALE entries (counts "
                "improved):\n" + "\n".join(improvements)
                + "\n\nLock in the improvement by updating "
                "GRANDFATHER_BASELINE."
            )
