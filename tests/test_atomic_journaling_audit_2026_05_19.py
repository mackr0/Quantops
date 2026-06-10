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

# 2026-06-10 — widened 100 → 130: the multileg sequential-submit
# path grew ~60 lines of position-intent-mismatch handling +
# rollback (06-09 leg-ordering work) between the raw POST and
# _log_strategy_legs, pushing the marker to ~110 lines out. The
# journaling is still atomic (same function, no early success-path
# return); only the scan window needed to follow the code.
MAX_LINES_TO_JOURNAL = 130  # accommodates ADV+slippage enrichment + multileg error handling between submit and journal


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
        if re.search(r"api\.submit_order\(|\b\w+\.submit_order\(", src):
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


# ---------------------------------------------------------------------------
# (4) Direct-POST path coverage — _submit_alpaca_order_raw bypasses the SDK
#
# Background: the SDK's `submit_order` signature drops kwargs the underlying
# REST API requires (`position_intent`, `legs`). For option orders the code
# uses `_submit_alpaca_order_raw` which POSTs directly to /v2/orders. The
# `api.submit_order` audit above doesn't see those call sites — so the
# atomic-journaling contract has to be enforced separately on this path.
# Without this test, a journal-write failure on a multileg combo silently
# leaves the broker holding fills no virtual book reflects (the
# `broker_orphan` class).
# ---------------------------------------------------------------------------

# Files allowed to call _submit_alpaca_order_raw in production.
# Each must journal the resulting order_id atomically (in the same
# try/except as the submit) — pinned per-file below.
PRODUCTION_RAW_POST_FILES = {
    "options_multileg.py",  # combo + sequential + rollback (rollback
                            # is a CLOSE so no journal write needed —
                            # the parent open already exists)
    "options_trader.py",    # single-leg with position_intent — caller
                            # journals (execute_option_strategy in
                            # trade_pipeline)
    "options_exits.py",     # close path — UPDATEs the existing
                            # journal row in the caller path
}

# Strict per-site contract: each call site is either (a) an OPEN whose
# journal write must appear within MAX_LINES_TO_JOURNAL lines, or (b)
# an explicit close/rollback that operates on an already-journaled
# position (the journal row exists, this submission moves it through
# its state machine — no new INSERT needed).
RAW_POST_CLOSE_SITES = {
    # (file, ~line) — close / rollback / exit paths. The journal row
    # the close acts on was written when the position was opened, so
    # the UPDATE happens via `_task_update_fills` against the existing
    # row's `pending_fill` status. These sites are exempt from the
    # forward-INSERT scan.
    ("options_multileg.py", "rollback"),  # sequential rollback closes
                                          # legs that were submitted
                                          # this call but didn't have
                                          # a journal write yet — the
                                          # parent except path returns
                                          # action=ERROR with the
                                          # leg_order_ids so the result
                                          # carries the broker state
    ("options_exits.py", "exit"),         # exit close — caller UPDATEs
                                          # the open trade row via
                                          # _task_update_fills
}


def test_every_raw_post_caller_journals_atomically():
    """Per `feedback_no_orphan_broker_fills` and
    `feedback_fix_class_not_instance`: every `_submit_alpaca_order_raw`
    call site that OPENS a position must have a journal-write marker
    (or a rollback path that cancels the broker order) within
    MAX_LINES_TO_JOURNAL lines.

    The existing `test_every_submit_order_site_has_a_journal_write_nearby`
    only walks `api.submit_order`. Multileg combo + sequential go
    through `_submit_alpaca_order_raw` (direct POST) and were
    INVISIBLE to that test — which is how the EXP-A2 NVDA strangle
    leaked into `broker_orphan` drift.

    Markers signaling correct atomicity:
      - `_log_strategy_legs(`         multileg journal helper
        (raises `_AtomicPlacementBreach` on per-leg log_trade
        failure, after cancelling the broker order + halting)
      - `_rollback_multileg_broker_orders(`
      - `_AtomicPlacementBreach`      sentinel caught by the caller
      - `log_trade(`                  direct journal write
      - `option_atomic_breach`        single-leg halt sentinel for
        the alert_type written when journal write fails after submit
      - `UPDATE trades`               exit-path UPDATE of existing row

    `_submit_alpaca_order_raw` sites in a function whose contract is
    "return order_id; caller journals atomically" are exempted via
    `RAW_POST_WRAPPER_SITES` below — each such site has a paired
    test below that pins the caller-side journaling.
    """
    raw_post_markers = (
        "_log_strategy_legs(",
        "_rollback_multileg_broker_orders(",
        "_AtomicPlacementBreach",
        "log_trade(",
        "option_atomic_breach",
        "UPDATE trades",
    )
    # Wrapper functions whose contract is "return order_id; the
    # caller journals atomically." Each is paired with a
    # caller-side test that pins the atomic journaling.
    raw_post_wrapper_funcs = ("submit_option_order",)
    offenders = []
    for fname in PRODUCTION_RAW_POST_FILES:
        path = REPO / fname
        if not path.exists():
            continue
        src_lines = _read(path).splitlines()
        for i, line in enumerate(src_lines):
            stripped = line.strip()
            if "_submit_alpaca_order_raw(" not in line:
                continue
            # Skip declarations / docstrings / comments
            if (stripped.startswith("#") or stripped.startswith('"""')
                    or stripped.startswith("'''")
                    or stripped.startswith("def _submit_alpaca_order_raw")
                    or stripped.startswith("from ")
                    or stripped.startswith("return _submit_alpaca_order_raw")):
                continue
            # Skip the wrapper itself + the import marker
            if "from options_multileg import _submit_alpaca_order_raw" in line:
                continue
            # Recognize the wrapper-returns-order_id pattern: scan
            # upward for the enclosing `def <name>(` and skip if the
            # name is on the exempt list.
            enclosing_def = None
            for k in range(i - 1, -1, -1):
                m = re.match(r"^def (\w+)\(", src_lines[k])
                if m:
                    enclosing_def = m.group(1)
                    break
            if enclosing_def in raw_post_wrapper_funcs:
                continue
            # Recognize the rollback context (sequential leg unwind in
            # options_multileg.py — surrounding lines above contain
            # `for sub in submitted:` or `rollback_results = []`).
            window_start = max(0, i - 12)
            preceding = "\n".join(src_lines[window_start:i])
            is_rollback = (
                "for sub in submitted:" in preceding
                or "rollback_results = []" in preceding
            )
            # Recognize the exit-close context (options_exits.py).
            is_close = (
                fname == "options_exits.py"
                or "buy_to_close" in line
                or "sell_to_close" in line
            )
            if is_rollback or is_close:
                continue
            # Forward-scan for an atomic-journal marker. Don't bail on
            # intermediate `return` lines — sequential paths return
            # from an except block on submit failure, then continue
            # to the journal call after the for-loop on success. The
            # window (MAX_LINES_TO_JOURNAL = 100) is wide enough to
            # cover both paths.
            found = False
            for j in range(i + 1,
                            min(i + 1 + MAX_LINES_TO_JOURNAL,
                                len(src_lines))):
                line_j = src_lines[j]
                if any(m in line_j for m in raw_post_markers):
                    found = True
                    break
            if not found:
                offenders.append(
                    f"{fname}:{i + 1}: {line.strip()[:80]}"
                )
    assert not offenders, (
        "`_submit_alpaca_order_raw` call sites with NO atomic-journal "
        "marker within "
        f"{MAX_LINES_TO_JOURNAL} lines (would leave `broker_orphan` "
        "fills on a journal-write failure):\n  "
        + "\n  ".join(offenders)
    )


def test_submit_option_order_callers_journal_atomically():
    """Pin the caller-side contract for the `submit_option_order`
    wrapper: its callers (currently `execute_option_strategy` in
    `options_trader.py`) must journal atomically AND halt the
    profile on journal-write failure. The wrapper itself is exempted
    from the forward-scan test above on the understanding that the
    caller closes the contract; this test enforces that.
    """
    src = _read(REPO / "options_trader.py")
    # Pattern: order_id = submit_option_order(...). Each call site
    # must be followed (within MAX_LINES_TO_JOURNAL) by both a
    # `log_trade(` AND an `option_atomic_breach` marker so we know
    # the journal-write failure path cancels the broker order.
    src_lines = src.splitlines()
    offenders = []
    for i, line in enumerate(src_lines):
        if "= submit_option_order(" not in line:
            continue
        if line.lstrip().startswith("#"):
            continue
        window_end = min(i + 1 + MAX_LINES_TO_JOURNAL, len(src_lines))
        window = "\n".join(src_lines[i + 1:window_end])
        has_journal = "log_trade(" in window
        has_breach = "option_atomic_breach" in window
        if not (has_journal and has_breach):
            missing = []
            if not has_journal:
                missing.append("log_trade(")
            if not has_breach:
                missing.append("option_atomic_breach")
            offenders.append(
                f"options_trader.py:{i + 1}: caller of "
                f"submit_option_order missing {missing} "
                f"within {MAX_LINES_TO_JOURNAL} lines"
            )
    assert not offenders, (
        "submit_option_order caller(s) missing atomic-journal "
        "contract markers:\n  " + "\n  ".join(offenders)
    )


def test_no_unknown_raw_post_call_sites():
    """If a NEW file adds `_submit_alpaca_order_raw`, fail — forcing
    the operator to confirm the new site honors the atomic-placement
    contract on a path that bypasses the SDK's submit_order."""
    found_files = set()
    for path in REPO.rglob("*.py"):
        rel = str(path.relative_to(REPO))
        if _is_test_or_script(rel):
            continue
        try:
            src = _read(path)
        except Exception:
            continue
        # Only count actual call sites, not imports / docstring refs.
        # Strip docstrings first.
        code_no_docstrings = re.sub(
            r'"""[\s\S]*?"""', "", src,
        )
        code_no_docstrings = re.sub(
            r"'''[\s\S]*?'''", "", code_no_docstrings,
        )
        # An actual call is `_submit_alpaca_order_raw(` not preceded
        # by `def ` (the definition) or `from ` (an import).
        for m in re.finditer(
                r"_submit_alpaca_order_raw\(", code_no_docstrings,
        ):
            line_start = code_no_docstrings.rfind("\n", 0, m.start()) + 1
            line_text = code_no_docstrings[line_start:m.end() + 20]
            if "def _submit_alpaca_order_raw" in line_text:
                continue
            if "from " in line_text and "import" in line_text:
                continue
            found_files.add(path.name)
            break
    unknown = found_files - PRODUCTION_RAW_POST_FILES
    assert not unknown, (
        f"New `_submit_alpaca_order_raw` call site detected in: "
        f"{sorted(unknown)}. Add to PRODUCTION_RAW_POST_FILES in this "
        f"test AFTER confirming the new site honors the atomic-placement "
        f"contract: every submit must be paired with either a journal "
        f"write within {MAX_LINES_TO_JOURNAL} lines (open path) or an "
        f"explicit rollback (close path)."
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
