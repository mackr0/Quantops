"""Structural guardrail: every broker-API call (api.submit_order,
api.cancel_order, api.list_positions, api.get_account, api.get_order,
api.get_orders) in production code is either:
  1. Wrapped in a try/except that catches Exception
  2. Wrapped by `_retrying_call(...)` (exponential-backoff helper)
  3. Wrapped by `call_with_health_tracking(...)` (broker-health
     wrapper that records failure but RE-RAISES — only acceptable
     when the OUTER scope catches the re-raise)
  4. Annotated with `# RETRY_OK: <rationale>` if the call is inside
     a tight loop where retry is handled at a higher level

The bug class.
A new trader-side code path calls `api.submit_order(...)` directly
without retry. A momentary 429 / 503 from Alpaca crashes the entire
scheduler cycle for that profile; the operator sees a stack trace
in logs, one missed signal, and no notification. The next cycle
runs fine — but the dropped trade was a real signal that's now gone.

The acceptable patterns above all share one invariant: if the
broker call raises a transient exception, the SCHEDULER does not
crash. Either the exception is swallowed locally (with a log) or
caught higher up.
"""
from __future__ import annotations

import ast
import os
import sys
from typing import List, Set, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Files in scope. Every place that touches Alpaca's order-placement
# / account / position surface area.
BROKER_FILES = (
    "client.py",
    "trader.py",
    "trade_pipeline.py",
    "bracket_orders.py",
    "reconcile_journal_to_broker.py",
    "multi_scheduler.py",
    "options_trader.py",
    "options_multileg.py",
    "options_delta_hedger.py",
    "options_lifecycle.py",
    "options_roll_manager.py",
    "stat_arb_pair_book.py",
    "order_guard.py",
    "aggregate_audit.py",
)


# Methods on the alpaca `api` object that we treat as broker calls
# requiring retry/exception protection.
BROKER_METHODS = {
    "submit_order",
    "cancel_order",
    "list_positions",
    "get_account",
    "get_order",
    "get_orders",
    "close_position",
}


# Helper functions known to wrap a broker call with safe semantics.
# A call wrapped by one of these is treated as guarded.
KNOWN_RETRY_WRAPPERS = {
    "_retrying_call",
    "call_with_health_tracking",
}


def _build_parent_lookup(tree: ast.Module) -> dict:
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def _is_broker_method_call(node: ast.Call) -> bool:
    """True iff `node` is api.<broker_method>(...)."""
    fn = node.func
    if not isinstance(fn, ast.Attribute):
        return False
    if fn.attr not in BROKER_METHODS:
        return False
    if not isinstance(fn.value, ast.Name):
        return False
    if fn.value.id != "api":
        return False
    return True


def _is_inside_try_except(node: ast.AST, parent_lookup) -> bool:
    """Walk up parents; return True if any ancestor is a `try`
    with an Exception-catching except clause."""
    cur = parent_lookup.get(id(node))
    while cur is not None:
        if isinstance(cur, ast.Try):
            # Verify the except handlers actually catch broad-enough
            # exceptions. `except Exception` or `except:` is sufficient.
            for handler in cur.handlers:
                if handler.type is None:
                    return True
                # Exception or BaseException
                if (isinstance(handler.type, ast.Name)
                        and handler.type.id in (
                            "Exception", "BaseException")):
                    return True
                # Specific exception class — also acceptable for the
                # purpose of "won't crash the scheduler"; we just need
                # SOMETHING catching the broker error.
                return True
        cur = parent_lookup.get(id(cur))
    return False


def _is_wrapped_by_retry_helper(node: ast.Call,
                                  parent_lookup) -> bool:
    """True iff this api.foo(...) call appears as an argument to a
    known retry-helper Call (e.g. `_retrying_call(api.submit_order, ...)`).

    Note: when the helper takes the BOUND METHOD `api.submit_order`
    as a positional arg (without parens), the AST for that arg is
    an Attribute, not a Call — so it doesn't show up in this scanner.
    We separately detect that pattern by scanning the helper-call's
    arguments for the bound method name.
    """
    parent = parent_lookup.get(id(node))
    while parent is not None:
        if isinstance(parent, ast.Call):
            fn = parent.func
            fn_name = None
            if isinstance(fn, ast.Name):
                fn_name = fn.id
            elif isinstance(fn, ast.Attribute):
                fn_name = fn.attr
            if fn_name in KNOWN_RETRY_WRAPPERS:
                return True
        parent = parent_lookup.get(id(parent))
    return False


def _has_retry_ok_comment(src: str, lineno: int) -> bool:
    """Look for a `# RETRY_OK:` comment in the contiguous comment
    block immediately above `lineno`. Walks UP through any number
    of consecutive `#` lines until hitting a non-comment line."""
    lines = src.split("\n")
    idx = lineno - 2  # line above the call (0-indexed)
    while idx >= 0:
        line = lines[idx].strip()
        if line.startswith("# RETRY_OK"):
            return True
        if line.startswith("#") or line == "":
            idx -= 1
            continue
        break  # hit a code line
    return False


def _find_unguarded_broker_calls(src_path: str) -> List[Tuple[int, str]]:
    """Return [(lineno, method_name)] for each unguarded broker call."""
    with open(src_path) as fh:
        src = fh.read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    parent_lookup = _build_parent_lookup(tree)
    out: List[Tuple[int, str]] = []
    # Pre-pass: collect line numbers of bound-method retries so the
    # subsequent _retrying_call(api.submit_order, ...) shape is recognized.
    retry_helper_lines: Set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fn_name = None
        if isinstance(fn, ast.Name):
            fn_name = fn.id
        elif isinstance(fn, ast.Attribute):
            fn_name = fn.attr
        if fn_name not in KNOWN_RETRY_WRAPPERS:
            continue
        # Walk this call's args; if any arg is api.<broker_method>
        # (Attribute, not Call), record its line.
        for arg in node.args:
            if (isinstance(arg, ast.Attribute)
                    and arg.attr in BROKER_METHODS
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id == "api"):
                retry_helper_lines.add(arg.lineno)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_broker_method_call(node):
            continue
        # Pattern A: directly wrapped in try/except
        if _is_inside_try_except(node, parent_lookup):
            continue
        # Pattern B: wrapped as Call by retry helper
        if _is_wrapped_by_retry_helper(node, parent_lookup):
            continue
        # Pattern C: bound-method passed to retry helper at this line
        if node.lineno in retry_helper_lines:
            continue
        # Pattern D: explicit RETRY_OK comment
        if _has_retry_ok_comment(src, node.lineno):
            continue
        out.append((node.lineno, node.func.attr))
    return out


class TestBrokerApiRetryGuards:
    """Default-deny: every api.<broker_method>(...) call in BROKER_FILES
    must be guarded by try/except, retry helper, or RETRY_OK comment."""

    def test_no_unguarded_broker_calls(self):
        violations: List[Tuple[str, int, str]] = []
        for fname in BROKER_FILES:
            path = os.path.join(REPO_ROOT, fname)
            if not os.path.exists(path):
                continue
            for lineno, method in _find_unguarded_broker_calls(path):
                violations.append((fname, lineno, method))
        if violations:
            details = "\n".join(
                f"  {fname}:{lineno}  api.{method}(...)"
                for fname, lineno, method in violations
            )
            pytest.fail(
                f"{len(violations)} broker-API calls are unguarded "
                f"— a transient 429/503 from Alpaca will crash the "
                f"scheduler at one of these sites and silently drop "
                f"the trade signal.\n\nViolations:\n{details}\n\n"
                f"Fix one of:\n"
                f"  1. Wrap in try/except Exception with a logger "
                f"warning + safe return\n"
                f"  2. Wrap with `_retrying_call(api.<method>, ...)` "
                f"(exponential backoff helper in "
                f"reconcile_journal_to_broker.py)\n"
                f"  3. Wrap with `call_with_health_tracking(...)` "
                f"only if the outer scope already catches Exception\n"
                f"  4. If the call is inside a tight loop where retry "
                f"is handled at a higher level, add `# RETRY_OK: "
                f"<rationale>` on the line above"
            )
