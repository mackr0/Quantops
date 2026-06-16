"""2026-06-16 — STRUCTURAL guardrails for per-profile order isolation.

These tests pin the universal contracts so the bug CLASS cannot
silently return on a new code path (per feedback_fix_class_not_instance).
They are deliberately AST/source-based: they fail the moment someone
re-introduces an account-wide cancel or a fuzzy cross-profile fill
match anywhere in the live loop — not just at the specific lines we
fixed today.

Contracts:

  C1. Any function in the live trade/scheduler loop that cancels a
      broker order (`api.cancel_order`) after an account-wide
      `api.list_orders` MUST gate the cancel on
      `own_broker_order_ids` — so it can only touch THIS profile's
      own orders, never a sibling's on the shared Alpaca account.

  C2. The fuzzy symbol/qty/time fill matcher (`_find_matching_exit_fill`)
      stays DELETED. It attributed siblings' fills to the wrong
      profile on a shared account.

  C3. Reconcile fill attribution is own-order-id-only: neither
      `_detect_protective_fill` nor the phantom classifiers may
      search broker order history (`list_orders`) to "find" an exit.
      A close is recognized solely via THIS profile's own
      protective_*_order_id (+ the replace-chain walk on those ids);
      anything else becomes `orphan_close` and HALTS.

See PROFILE_ORDER_ISOLATION.md.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _calls_attr(node: ast.AST, attr: str) -> bool:
    """True if the AST subtree contains any call `*.<attr>(...)`."""
    for n in ast.walk(node):
        if (isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr):
            return True
    return False


def _function_defs(src: str):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


# ---------------------------------------------------------------------------
# C1 — no account-wide cancel without an own-id gate
# ---------------------------------------------------------------------------

# Functions allowed to cancel without referencing own_broker_order_ids
# because they cancel ONLY ids drawn from THIS profile's own journal
# (protective_*_order_id columns) via cancel_for_symbol / explicit id.
_C1_ALLOWLIST = {
    # (module, function) pairs that are own-journal-scoped by construction
}


@pytest.mark.parametrize("module", ["trader.py", "multi_scheduler.py"])
def test_no_accountwide_cancel_without_own_id_gate(module):
    src = (REPO / module).read_text()
    offenders = []
    for fn in _function_defs(src):
        fn_src = ast.get_source_segment(src, fn) or ""
        cancels = _calls_attr(fn, "cancel_order")
        lists = _calls_attr(fn, "list_orders")
        if cancels and lists:
            if (module, fn.name) in _C1_ALLOWLIST:
                continue
            if "own_broker_order_ids" not in fn_src:
                offenders.append(fn.name)
    assert not offenders, (
        f"{module}: function(s) {offenders} cancel a broker order after "
        f"an account-wide list_orders WITHOUT gating on "
        f"own_broker_order_ids. On a shared Alpaca account this can "
        f"cancel a SIBLING profile's order (the SPCX/SOUN class). "
        f"Intersect the open orders with own_broker_order_ids(db_path) "
        f"before cancelling. See PROFILE_ORDER_ISOLATION.md."
    )


def test_own_broker_order_ids_primitive_exists():
    """The load-bearing primitive must exist and read both the trades
    order-id columns and the long_vol_hedges table."""
    src = (REPO / "order_guard.py").read_text()
    assert "def own_broker_order_ids(" in src
    for col in ("order_id", "protective_stop_order_id",
                "protective_tp_order_id", "protective_trailing_order_id"):
        assert col in src, f"own_broker_order_ids must read {col}"


# ---------------------------------------------------------------------------
# C2 — the fuzzy matcher stays deleted
# ---------------------------------------------------------------------------


def test_fuzzy_exit_matcher_is_gone():
    src = (REPO / "reconcile_journal_to_broker.py").read_text()
    tree = ast.parse(src)
    fn_names = {n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_find_matching_exit_fill" not in fn_names, (
        "_find_matching_exit_fill was re-introduced. It searches the "
        "SHARED account's order history by symbol/qty/time and "
        "attributes siblings' fills to the wrong profile (BATL/PPCB "
        "oversells, SOUN drift). Attribution must be own-order-id-only."
    )
    # Also ensure no live call survived (e.g. via a renamed helper that
    # still calls it).
    assert "_find_matching_exit_fill(" not in src.replace(
        "# ", ""  # allow it only inside comments
    ) or src.count("_find_matching_exit_fill(") == 0


# ---------------------------------------------------------------------------
# C3 — own-order-id-only attribution in reconcile
# ---------------------------------------------------------------------------


def _func_by_name(src, name):
    for fn in _function_defs(src):
        if fn.name == name:
            return fn
    return None


@pytest.mark.parametrize("fn_name", [
    "_classify_long_phantom",
    "_classify_short_phantom",
])
def test_phantom_classifiers_do_not_search_broker_history(fn_name):
    """The phantom classifiers must NOT call list_orders to fuzzy-find
    an exit. They may only inspect the entry order (get_order) and
    return orphan_close when no OWN order explains the close."""
    src = (REPO / "reconcile_journal_to_broker.py").read_text()
    fn = _func_by_name(src, fn_name)
    assert fn is not None, f"{fn_name} missing"
    assert not _calls_attr(fn, "list_orders"), (
        f"{fn_name} calls list_orders — that is a fuzzy broker-history "
        f"search and the cross-profile theft vector. It must return "
        f"'orphan_close' for unexplained closes instead."
    )
    fn_src = ast.get_source_segment(src, fn) or ""
    assert "orphan_close" in fn_src, (
        f"{fn_name} must return 'orphan_close' for an unexplained "
        f"broker-flat close so the safety net HALTs (never silently "
        f"diverges, never fuzzy-claims a sibling's fill)."
    )


def test_orphan_close_counts_toward_halt():
    """An unexplained close must HALT — it may not sit silently in a
    non-halting bucket (feedback_never_silent_ok)."""
    src = (REPO / "reconcile_journal_to_broker.py").read_text()
    # The synthesis_actions tally (which drives halt_and_alert) must
    # include orphan_close.
    idx = src.index("synthesis_actions = (")
    window = src[idx:idx + 500]
    assert 'actions["orphan_close"]' in window, (
        "orphan_close is not counted toward the reconciler halt — an "
        "unexplained broker-flat close would silently persist as a "
        "phantom open position. It must HALT."
    )


def test_detect_protective_fill_has_no_fuzzy_fallback():
    """_detect_protective_fill must end with the own-order-id contract:
    no list_orders fuzzy fallback after the protective_*_order_id walk."""
    src = (REPO / "reconcile_journal_to_broker.py").read_text()
    fn = _func_by_name(src, "_detect_protective_fill")
    assert fn is not None
    assert not _calls_attr(fn, "list_orders"), (
        "_detect_protective_fill regained a list_orders fuzzy fallback "
        "— attribution must be own-order-id-only (protective_*_order_id "
        "+ replace-chain walk)."
    )
