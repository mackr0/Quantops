"""RATCHET: NO fail-open returns inside except handlers in
books-critical modules (2026-07-27, operator directive: ALL of them).

The recurring books-drift class — phantom closes, the fake −$481,798
value drift, the 48 fake per-symbol phantoms, the +$335 basis-less
proceeds — is one code shape: a broker/DB read fails and an except
handler returns a fabricated value (a number, or an empty
list/dict/set that reads as "nothing held / nothing pending / no
protection needed"). The operator's invariant holds only if
unverifiable reads REFUSE: return None, raise, or surface an
explicit UNVERIFIABLE marker.

This test bans the shape outright — NO allowlist. The ONE permitted
pattern is the fresh-DB branch: a return inside an
`if "no such table" ...` guard, because a database that has never
created the table is GENUINELY empty (that is a fact, not a
fabrication). Everything else fails the build: teach the caller to
handle refusal.
"""
from __future__ import annotations

import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

MODULES = [
    "aggregate_audit.py",
    "certify_books.py",
    "integrity_audit.py",
    "reconcile_journal_to_broker.py",
    "journal.py",
    "activities_capture.py",
    "auto_close_broker_orphans.py",
    "audit_runner.py",
    "issues_collector.py",
]


def _fabricated_value_tag(v):
    if (isinstance(v, ast.Constant)
            and isinstance(v.value, (int, float))
            and not isinstance(v.value, bool)):
        return f"number {v.value!r}"
    if isinstance(v, (ast.List, ast.Tuple, ast.Set)) and not v.elts:
        return "empty container"
    if isinstance(v, ast.Dict) and not v.keys:
        return "empty dict"
    if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
            and v.func.id in ("dict", "list", "set", "tuple", "frozenset")
            and not v.args and not v.keywords):
        return f"{v.func.id}()"
    return None


def _inside_fresh_db_guard(ret_node, handler):
    """True when the return sits inside an `if` whose test mentions
    the 'no such table' fresh-DB check — the one honest empty."""
    for n in ast.walk(handler):
        if isinstance(n, ast.If) and "no such table" in ast.dump(n.test):
            for sub in ast.walk(n):
                if sub is ret_node:
                    return True
    return False


def _violations(path):
    tree = ast.parse(open(path).read())
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def owner(lineno):
        best = "?"
        for f in funcs:
            if f.lineno <= lineno <= getattr(f, "end_lineno", f.lineno):
                best = f.name
        return best

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and sub.value is not None:
                tag = _fabricated_value_tag(sub.value)
                if tag and not _inside_fresh_db_guard(sub, node):
                    out.append((owner(sub.lineno), sub.lineno, tag))
    return out


def test_no_failopen_returns_anywhere():
    bad = []
    for fname in MODULES:
        path = os.path.join(REPO, fname)
        if not os.path.exists(path):
            continue
        for func, lineno, tag in _violations(path):
            bad.append(f"{fname}:{lineno} ({func}) returns {tag} "
                       "inside an except handler")
    assert not bad, (
        "Fail-open return(s) in books-critical code — a failed read "
        "must REFUSE (None / raise / UNVERIFIABLE), never fabricate a "
        "value a caller will treat as money truth (the phantom-close "
        "/ fake-$481K / 48-fake-phantoms / basis-less-$335 class). "
        "The only permitted empty is inside an explicit "
        "'no such table' fresh-DB guard:\n  " + "\n  ".join(bad)
    )
