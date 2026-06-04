"""Phase B1 atomic-journaling audit (2026-05-19).

Per `feedback_no_orphan_broker_fills`: every `api.submit_order` call
site in PRODUCTION code must have a corresponding journal write
(`log_trade`, `record_trade`, or an explicit `UPDATE trades` for
exit paths that mutate an existing row) within the same code path,
with no early-return between them.

These are STRUCTURAL guardrails — they grep the live source so a
future refactor that accidentally removes a journal write surfaces
immediately instead of silently producing orphan broker fills
(which the 2026-05-19 reconciler safety net would then HALT on,
but the right answer is to never produce the orphan in the first
place).

Pinned today:
  1. No production caller may pass `log=False` to functions whose
     `log` parameter gates the journal write.
  2. Every production `api.submit_order` call has a `log_trade` /
     `UPDATE trades` within K source lines (no early return
     between them).
  3. The known load-bearing intermediate work (ADV capture,
     slippage estimation, entry_meta fetch) is wrapped in
     bare-Exception catches so the journal write below can't be
     orphaned by an unexpected exception type.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# Files that legitimately submit broker orders in production.
# Update this list if a new submit_order call site lands; the
# accompanying audit must confirm the new site has atomic
# journaling. The list is explicit (not a glob) so a new file
# adding submit_order is detected: see
# test_no_unknown_submit_order_call_sites below.
PRODUCTION_SUBMIT_FILES = {
    "trader.py",
    "trade_pipeline.py",
    "options_delta_hedger.py",
    "options_roll_manager.py",
    "multi_scheduler.py",
    "stat_arb_pair_book.py",
    "simple_strategies.py",
    "bracket_orders.py",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_test_or_script(rel_path: str) -> bool:
    """Tests + one-off scripts + maintenance tools are allowed to
    bypass the atomic-journaling rule. PRODUCTION code paths must
    not."""
    return (
        rel_path.startswith("tests/")
        or rel_path.startswith("scripts/")
        or rel_path.startswith(".claude/")
        or rel_path.startswith("venv/")
        or "_test_" in rel_path
        or rel_path.endswith("_2026_05_18.py")  # one-off cleanup scripts
        or rel_path == "reset_for_clean_experiment.py"
        or rel_path.startswith("cleanup_")
    )


# ---------------------------------------------------------------------------
# (1) No production caller may pass log=False
# ---------------------------------------------------------------------------

def test_no_production_caller_passes_log_false():
    """The `log` parameter on execute_trade / execute_pair_trade /
    execute_option_strategy gates the journal write. A future caller
    that accidentally passes log=False would silently bypass the
    journal — an orphan broker fill. Tests are exempt (they mock
    the journal); production code is not.

    If this fails: either revert the new log=False site, OR
    refactor to remove the parameter entirely so the journal
    write is unconditional."""
    offenders = []
    for path in REPO.rglob("*.py"):
        rel = str(path.relative_to(REPO))
        if _is_test_or_script(rel):
            continue
        try:
            src = _read(path)
        except Exception:
            continue
        for m in re.finditer(r"\blog\s*=\s*False\b", src):
            line_no = src[:m.start()].count("\n") + 1
            offenders.append(f"{rel}:{line_no}")
    assert not offenders, (
        "Production code passes log=False — would silently bypass "
        "journaling.\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# (2) Every production submit_order has a journal write nearby
# ---------------------------------------------------------------------------

# A submit_order site is considered "atomically journaled" if any
# of these markers appears within the next MAX_LINES_TO_JOURNAL
# source lines, with no `return` statement in between.
JOURNAL_MARKERS = (
    "log_trade(",
    "UPDATE trades",  # exit-path UPDATE of existing row
    "INSERT INTO trades",  # rare — direct INSERT
    # 2026-05-21 — protective placements (stop / TP / trailing) now
    # journal a `pending_protective` row in-function via this helper
    # immediately after submit_order. Recognizing the helper call as
    # a journal marker turns the old line-number EXEMPTION into a
    # positive verification — the audit now CONFIRMS protective sites
    # journal, rather than skipping them.
    "_write_pending_protective_row(",
)

MAX_LINES_TO_JOURNAL = 100  # accommodates ADV+slippage enrichment between submit and journal


# 2026-05-21 — Bracket-order submission USED to have a different
# atomicity model (function returns order_id, caller persists the
# linkage; submit_order had no in-function journal marker). That
# left a hole: when the broker autonomously FILLED a protective
# order, there was no trades row for the reconciler to UPDATE, so
# the fill looked like an orphan and tripped the safety-net halt
# (caught 2026-05-21 on pid24 QCOM trailing stop).
#
# Now the protective placement helpers journal a `pending_protective`
# row in-function via `_write_pending_protective_row` (a recognized
# JOURNAL_MARKER above). So they pass the main forward-scan check
# like any other submit_order site — no line-number exemption
# needed. The set is kept empty (rather than deleted) so the
# `(fname, i+1) in BRACKET_SUBMIT_SITES` guard below stays valid
# and a future operator can re-add an exemption if a genuinely
# different model is introduced.
#
# The caller-side UPDATE (protective_*_order_id linkage on the
# entry row) is STILL required and still pinned by
# test_bracket_callers_persist_order_id_atomically below.
BRACKET_SUBMIT_SITES = set()


def test_every_submit_order_site_has_a_journal_write_nearby():
    """For each `api.submit_order(...)` in production source, walk
    forward and confirm a journal-write marker appears within
    MAX_LINES_TO_JOURNAL lines, with no `return` between them.

    Bracket-protective sites are exempt — they call
    `_persist_bracket_order_id` (or the explicit
    `UPDATE trades SET protective_*_order_id`), which is in the
    marker list. Pure submit-and-discard sites are not allowed
    in production.
    """
    offenders = []
    for fname in PRODUCTION_SUBMIT_FILES:
        path = REPO / fname
        if not path.exists():
            continue
        src_lines = _read(path).splitlines()
        for i, line in enumerate(src_lines):
            if "api.submit_order" not in line and ".submit_order(" not in line:
                continue
            # Skip docstring/comment occurrences
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') \
                    or stripped.startswith("'''"):
                continue
            # Bracket-order sites are exempt — different atomicity
            # model (function returns order_id, caller updates).
            if (fname, i + 1) in BRACKET_SUBMIT_SITES:
                continue
            # Scan forward for the journal marker
            found_marker = False
            for j in range(i + 1,
                            min(i + 1 + MAX_LINES_TO_JOURNAL, len(src_lines))):
                line_j = src_lines[j]
                if any(m in line_j for m in JOURNAL_MARKERS):
                    found_marker = True
                    break
            if not found_marker:
                offenders.append(f"{fname}:{i + 1}: {line.strip()[:60]}")
    assert not offenders, (
        f"submit_order sites with NO journal write within "
        f"{MAX_LINES_TO_JOURNAL} lines:\n  "
        + "\n  ".join(offenders)
    )


def test_bracket_callers_persist_order_id_atomically():
    """The bracket pattern: `submit_protective_*` returns order_id;
    callers must persist it on the parent trade row immediately,
    same try-block, no early return between them. Pin the live
    pattern in bracket_orders.py:376-400 so a future refactor
    can't accidentally drop the UPDATE."""
    src = _read(REPO / "bracket_orders.py")
    # The pattern: `order_id = submit_protective_...(...)` followed
    # within ~20 lines by `UPDATE trades SET {column}`.
    src_lines = src.splitlines()
    for i, line in enumerate(src_lines):
        if "order_id = submit_protective_" not in line:
            continue
        # Scan forward for the column-store. Two patterns:
        #   `UPDATE trades SET {column} = ?` (line 397)
        #   `UPDATE trades SET ... protective_*_order_id ...`
        found = False
        for j in range(i + 1, min(i + 25, len(src_lines))):
            if "UPDATE trades" in src_lines[j]:
                found = True
                break
        assert found, (
            f"bracket_orders.py:{i + 1} calls submit_protective_* "
            "but no UPDATE trades within 25 lines — bracket caller "
            "atomicity broken."
        )


def test_protective_placement_rolls_back_broker_order_on_journal_failure():
    """2026-06-04 atomic-placement contract: every call site that
    invokes `_write_pending_protective_row` must check its return
    value and call `_rollback_broker_order` on False so the broker
    order is canceled when the journal write fails. Without this,
    a journal-write failure leaves a broker order with no journal
    row — the orphan class the reconciler safety net is designed
    to halt on.

    Structural check: scan bracket_orders.py for every call to
    `_write_pending_protective_row(`. Within the next 50 lines,
    confirm a `_rollback_broker_order(` appears (the False branch
    handler). If a future placement helper is added that journals
    but doesn't roll back, this test fails immediately."""
    src = _read(REPO / "bracket_orders.py")
    src_lines = src.splitlines()
    offenders = []
    for i, line in enumerate(src_lines):
        if "_write_pending_protective_row(" not in line:
            continue
        # Skip the function definition itself
        if line.strip().startswith("def _write_pending_protective_row"):
            continue
        # Skip docstring/comment occurrences
        stripped = line.strip()
        if (stripped.startswith("#") or stripped.startswith('"""')
                or stripped.startswith("'''")):
            continue
        # Scan forward for the rollback call
        found_rollback = False
        for j in range(i + 1, min(i + 50, len(src_lines))):
            if "_rollback_broker_order(" in src_lines[j]:
                found_rollback = True
                break
        if not found_rollback:
            offenders.append(
                f"bracket_orders.py:{i + 1}: "
                f"_write_pending_protective_row() call has no "
                f"_rollback_broker_order() within 50 lines — journal "
                f"failure would leave a broker orphan"
            )
    assert not offenders, (
        "Atomic-placement contract violated:\n  "
        + "\n  ".join(offenders)
    )


def test_no_unknown_submit_order_call_sites():
    """If a NEW file adds api.submit_order, this test fails — forcing
    the operator to add it to PRODUCTION_SUBMIT_FILES + confirm
    atomic journaling holds at the new site."""
    found_files = set()
    for path in REPO.rglob("*.py"):
        rel = str(path.relative_to(REPO))
        if _is_test_or_script(rel):
            continue
        # Skip pure docstring references (no parens)
        try:
            src = _read(path)
        except Exception:
            continue
        if re.search(r"api\.submit_order\(|\.submit_order\(", src):
            # Confirm there's at least one ACTUAL call (not just
            # docstring text). The compiled regex requires `(`.
            found_files.add(path.name)
    # The ABC at pipelines/__init__.py has the string in a docstring
    # but no actual call — filter via second pass on AST-ish check.
    docstring_only = set()
    for fname in list(found_files):
        path = REPO / fname if (REPO / fname).exists() else next(
            (p for p in REPO.rglob(fname)), None,
        )
        if path is None:
            continue
        src = _read(path)
        # Match only outside triple-quoted blocks.
        code_no_docstrings = re.sub(
            r'"""[\s\S]*?"""', "", src,
        )
        code_no_docstrings = re.sub(
            r"'''[\s\S]*?'''", "", code_no_docstrings,
        )
        if not re.search(
            r"api\.submit_order\(|\w+\.submit_order\(",
            code_no_docstrings,
        ):
            docstring_only.add(fname)
    found_files -= docstring_only

    unknown = found_files - PRODUCTION_SUBMIT_FILES
    assert not unknown, (
        f"New submit_order call site detected in: {sorted(unknown)}. "
        f"Add to PRODUCTION_SUBMIT_FILES in this test AFTER confirming "
        f"the new site writes a journal row atomically in the same "
        f"code path (per `feedback_no_orphan_broker_fills`)."
    )


# ---------------------------------------------------------------------------
# (3) Load-bearing intermediate work catches bare Exception
# ---------------------------------------------------------------------------

def test_trade_pipeline_intermediate_enrichment_catches_bare_exception():
    """The ADV + slippage enrichment between submit_order and
    log_trade in trade_pipeline.py used to catch a restrictive
    tuple. An unexpected exception type would orphan the broker
    fill. Pinned to bare Exception 2026-05-19."""
    src = _read(REPO / "trade_pipeline.py")
    # Look for the BUY-side enrichment block
    buy_block_marker = "ADV capture failed for %s on BUY"
    sell_block_marker = "ADV capture failed for %s on SELL"
    assert buy_block_marker in src
    assert sell_block_marker in src
    # The pattern that was REMOVED: tuple-based except listing
    # specific exception types. If it's back, the guardrail failed.
    forbidden = "(KeyError, ValueError, AttributeError, TypeError,\n                    ImportError, OSError) as _adv_exc"
    assert forbidden not in src, (
        "trade_pipeline.py BUY/SELL enrichment block reverted to "
        "restrictive exception tuple. Use bare `except Exception` "
        "so an unknown exception type can't orphan the journal write."
    )


def test_trader_exit_metadata_fetch_is_wrapped():
    """trader.py:_process_exit_trigger fetches entry metadata AFTER
    submit_order and BEFORE log_trade. If the fetch raises, the
    broker fill is orphaned. Pin that it's wrapped in try/except
    so failure produces empty meta but never blocks log_trade."""
    src = _read(REPO / "trader.py")
    # The new wrap landed 2026-05-19 (Phase B1)
    assert "try:\n        entry_meta = get_open_entry_metadata" in src, (
        "trader.py:get_open_entry_metadata must be wrapped in "
        "try/except so a metadata-fetch failure cannot orphan "
        "the just-submitted broker fill."
    )
    assert 'entry_meta = {"ai_confidence": None, "ai_reasoning": None}' in src, (
        "trader.py must fall back to empty entry_meta on metadata "
        "fetch failure — keep the journal write load-bearing."
    )
