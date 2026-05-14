"""Structural guardrail: every `sqlite3.connect()` call in
production source must be paired with a guaranteed close
(via context manager OR explicit try/finally close).

Mode: strict (post-2026-05-14 audit).
The full ratchet baseline was paid down to zero on 2026-05-14
— every existing site was converted to a safe pattern. From
this point forward ANY unsafe `sqlite3.connect()` call in
production source fails the test on first introduction.

The bug class.
A long-running scheduler process accumulates open file handles
when sqlite3.connect() leaks. After ~1024 leaked connections (OS
default), every subsequent open fails with "Too many open files."
The scheduler crashes hours-to-days after a code path that opens
without closing was added.

This bug is INSIDIOUS — it doesn't manifest in tests (tests open
and close in seconds). It only shows up under sustained
production load. Detection at the source level is the only
reliable defense.

Acceptable patterns:
  1. `with sqlite3.connect(...) as conn:` (context manager auto-closes)
  2. `with closing(sqlite3.connect(...)) as conn:` (contextlib helper)
  3. `conn = sqlite3.connect(...); try: ... finally: conn.close()`
  4. `conn = _get_conn(...); try: ... finally: conn.close()` (helper-wrapped)
  5. Connection-factory pattern: a function that does
     `conn = sqlite3.connect(...); ...; return conn` (e.g.
     `models._get_conn`, `journal._get_conn`). The CALLER manages
     the lifetime; the factory itself is exempt.

Unsafe pattern:
  - `conn = sqlite3.connect(...); ...; conn.close()` (without try/finally
    — exception between connect and close LEAKS the handle)

This test scans for `sqlite3.connect()` calls and verifies each
is in one of the safe patterns. Allowlist: scripts that are
short-lived (one-shot migrations) where leaks don't accumulate.
"""
from __future__ import annotations

import ast
import os
import sys
from typing import List, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Files where leaks are tolerable (short-lived scripts, test-only).
ALLOWLIST_FILES = {
    # Test scripts — pytest tmp_path provides cleanup
    # (already excluded by walk filter)
    # One-shot migrations / backfills run once then exit
    "backfill_multileg_negative_prices.py":
        "One-shot script. Process exits before any leak accumulates.",
    "backup_db.py":
        "Backup operations open many connections briefly; each is "
        "explicitly closed in try/finally inside backup_one().",
    "recover_cycle_data.py":
        "Recovery utility. Short-lived process.",
    "run_phase2_validations.py":
        "One-shot validation script.",
    "run_backtest_validation.py":
        "One-shot backtest runner.",
    "validate_phase1_realdata.py":
        "One-shot validation script.",
    "migrate.py":
        "Migration script.",
    "migrate_activity_log_format.py":
        "Migration script.",
    "cancel_phantom_option_stock_stops.py":
        "One-shot remediation script (not imported by scheduler). "
        "Process exits before any leak accumulates.",
}


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


def _is_safe_connect(call_node: ast.Call, parent_lookup) -> bool:
    """Determine if a sqlite3.connect() call is in a safe pattern.

    Walks the parent chain to look for:
      - Surrounding `with` statement → safe
      - Subsequent .close() call inside try/finally → safe (heuristic)
      - Connection-factory pattern (assign + return) → safe (caller closes)
    """
    parent = parent_lookup.get(id(call_node))
    # Pattern 1: directly inside `with sqlite3.connect(...) as X:`
    while parent is not None:
        if isinstance(parent, ast.withitem):
            return True
        if isinstance(parent, ast.With):
            return True
        if isinstance(parent, ast.Assign):
            # `conn = sqlite3.connect(...)` — need to check
            # surrounding scope for try/finally with close OR
            # for the factory pattern (function returns conn).
            if _has_try_finally_close_in_scope(parent, parent_lookup):
                return True
            return _is_factory_return_pattern(parent, parent_lookup)
        if isinstance(parent, ast.Return):
            # `return sqlite3.connect(...)` — caller is responsible
            # for closing the connection. Connection-factory pattern.
            return True
        parent = parent_lookup.get(id(parent))
    return False


def _is_factory_return_pattern(assign_node: ast.Assign,
                                  parent_lookup) -> bool:
    """`conn = sqlite3.connect(...); ...; return conn` — the function is a
    connection factory; the caller manages the lifetime. Examples:
    `models._get_conn`, `models.open_profile_db`, `journal._get_conn`.

    Rule: in the enclosing function body, the assigned name appears
    in a `return <name>` statement. Heuristic but tight — captures the
    factory pattern without false-allowing assigns that just leak.
    """
    if not assign_node.targets:
        return False
    target = assign_node.targets[0]
    if not isinstance(target, ast.Name):
        return False
    var_name = target.id
    parent = parent_lookup.get(id(assign_node))
    while parent is not None and not isinstance(parent, ast.FunctionDef):
        parent = parent_lookup.get(id(parent))
    if parent is None:
        return False
    for node in ast.walk(parent):
        if not isinstance(node, ast.Return):
            continue
        if (isinstance(node.value, ast.Name)
                and node.value.id == var_name):
            return True
    return False


def _has_try_finally_close_in_scope(assign_node: ast.Assign,
                                      parent_lookup) -> bool:
    """When `conn = sqlite3.connect()`, check the enclosing function
    body for a `try:` block whose `finally` calls `conn.close()`.

    This is a heuristic — won't catch all variants but catches the
    common `try: ... finally: conn.close()` pattern.
    """
    if not assign_node.targets:
        return False
    target = assign_node.targets[0]
    if not isinstance(target, ast.Name):
        return False
    var_name = target.id
    # Find enclosing function
    parent = parent_lookup.get(id(assign_node))
    while parent is not None and not isinstance(parent, ast.FunctionDef):
        parent = parent_lookup.get(id(parent))
    if parent is None:
        return False
    # Walk function body for Try with finally calling var_name.close()
    for node in ast.walk(parent):
        if not isinstance(node, ast.Try):
            continue
        for fin_node in node.finalbody:
            for sub in ast.walk(fin_node):
                if not isinstance(sub, ast.Call):
                    continue
                if (isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "close"
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == var_name):
                    return True
    return False


def _build_parent_lookup(tree: ast.Module) -> dict:
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def _find_unsafe_connects(src: str) -> List[int]:
    """Return line numbers of unsafe sqlite3.connect() calls."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    parent_lookup = _build_parent_lookup(tree)
    unsafe = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        # Match sqlite3.connect or just connect when imported
        is_sqlite_connect = False
        if (isinstance(target, ast.Attribute)
                and target.attr == "connect"
                and isinstance(target.value, ast.Name)
                and target.value.id == "sqlite3"):
            is_sqlite_connect = True
        if not is_sqlite_connect:
            continue
        if not _is_safe_connect(node, parent_lookup):
            unsafe.append(node.lineno)
    return unsafe


# Per-file baseline of existing unsafe connects. Empty as of the
# 2026-05-14 audit — every prior site was converted to a safe
# pattern. From this point on the test runs in strict mode: any
# unsafe `sqlite3.connect()` in a critical-path file (not in
# ALLOWLIST_FILES) fails on first introduction.
GRANDFATHER_BASELINE: dict = {}


class TestEveryDbConnectionIsClosed:
    def test_no_new_unsafe_connect_patterns(self):
        violations_per_file = {}
        for src_path in _walk_critical_path_files():
            rel = os.path.relpath(src_path, REPO_ROOT)
            if os.path.basename(src_path) in ALLOWLIST_FILES:
                continue
            try:
                with open(src_path) as fh:
                    src = fh.read()
            except Exception:
                continue
            unsafe_lines = _find_unsafe_connects(src)
            if unsafe_lines:
                violations_per_file[rel] = len(unsafe_lines)

        # Apply baseline ratchet
        regressions = []
        new_files = []
        for rel, n in violations_per_file.items():
            baseline = GRANDFATHER_BASELINE.get(rel)
            if baseline is None:
                new_files.append((rel, n))
            elif n > baseline:
                regressions.append((rel, baseline, n))

        problems = []
        if regressions:
            problems.append("Files with NEW unsafe sqlite3.connect:")
            for rel, b, n in regressions:
                problems.append(f"  {rel}: baseline={b}, now {n}")
        if new_files:
            problems.append(
                "\nFiles with unsafe sqlite3.connect (no baseline):"
            )
            for rel, n in new_files:
                problems.append(
                    f"  {rel}: {n} unsafe connect(s)"
                )

        if problems:
            pytest.fail(
                "Unsafe sqlite3.connect() patterns detected — "
                "leaks accumulate over weeks of scheduler runtime "
                "and eventually crash with 'too many open files'.\n\n"
                + "\n".join(problems)
                + "\n\nFix one of:\n"
                "  1. Use `with sqlite3.connect(...) as conn:` "
                "context manager\n"
                "  2. Use `with closing(sqlite3.connect(...)) as "
                "conn:` from contextlib\n"
                "  3. `conn = sqlite3.connect(...); try: ... "
                "finally: conn.close()`\n"
                "  4. If the file is a one-shot script that exits "
                "before leaks accumulate, add to ALLOWLIST_FILES "
                "with a written rationale"
            )

    def test_factory_helper_callers_have_try_finally(self):
        """Class-level check: any caller of a known connection-factory
        helper (`_get_conn`, `_open_journal_conn`, `open_profile_db`)
        must close the returned connection in a try/finally block. The
        factory itself is exempt (it returns the conn for the caller to
        manage); the CALLERS are not.

        Why this exists.
        The 2026-05-14 audit converted 93 direct `sqlite3.connect()`
        sites to safe patterns. One conversion used a factory-extraction
        shortcut (`reconcile_journal_to_broker._open_journal_conn`)
        which made the AST scanner happy on the factory itself but
        left a 290-line caller body with `conn.close()` outside any
        try/finally — an exception between connect and close still
        leaks the handle. This check closes that gap by also tracking
        callers of the factory."""
        FACTORY_HELPERS = {
            "_get_conn",
            "_open_journal_conn",
            "open_profile_db",
            "_open_conn",
        }
        violations = []
        for src_path in _walk_critical_path_files():
            rel = os.path.relpath(src_path, REPO_ROOT)
            if os.path.basename(src_path) in ALLOWLIST_FILES:
                continue
            try:
                with open(src_path) as fh:
                    src = fh.read()
            except Exception:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            parent_lookup = _build_parent_lookup(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not isinstance(node.value, ast.Call):
                    continue
                fn = node.value.func
                fn_name = None
                if isinstance(fn, ast.Name):
                    fn_name = fn.id
                elif isinstance(fn, ast.Attribute):
                    fn_name = fn.attr
                if fn_name not in FACTORY_HELPERS:
                    continue
                # Skip the factory itself (it's the function whose body
                # contains the assign+return).
                if _is_factory_return_pattern(node, parent_lookup):
                    continue
                # Skip if inside a `with closing(...)` context.
                parent = parent_lookup.get(id(node))
                in_with = False
                while parent is not None:
                    if isinstance(parent, (ast.With, ast.withitem)):
                        in_with = True
                        break
                    parent = parent_lookup.get(id(parent))
                if in_with:
                    continue
                if _has_try_finally_close_in_scope(node, parent_lookup):
                    continue
                violations.append((rel, node.lineno, fn_name))
        if violations:
            details = "\n".join(
                f"  {rel}:{lineno}  conn = {fn_name}(...)  — "
                f"no surrounding try/finally close"
                for rel, lineno, fn_name in violations
            )
            pytest.fail(
                "Factory-helper conn assignments without try/finally "
                "close:\n\n" + details + "\n\nFix:\n"
                "  conn = _get_conn(...)\n"
                "  try:\n      ...\n"
                "  finally:\n      conn.close()\n"
                "Or use `with closing(_get_conn(...)) as conn:`."
            )

    def test_scanner_correctly_flags_unsafe_pattern(self):
        """Sanity: the AST walker correctly identifies the
        unsafe shape on a synthetic example."""
        unsafe_sample = """
import sqlite3
def bad():
    conn = sqlite3.connect("x.db")
    conn.execute("SELECT 1")
    conn.close()
"""
        unsafe = _find_unsafe_connects(unsafe_sample)
        assert unsafe, (
            "Scanner failed to flag plain connect-without-finally"
        )

    def test_scanner_correctly_recognizes_safe_pattern(self):
        """Sanity: the AST walker recognizes `with` context."""
        safe_sample = """
import sqlite3
def good():
    with sqlite3.connect("x.db") as conn:
        conn.execute("SELECT 1")
"""
        unsafe = _find_unsafe_connects(safe_sample)
        assert not unsafe, (
            "Scanner false-flagged the safe `with` pattern"
        )
