"""Cross-cutting guardrails for broker order-submission paths.

Born 2026-05-07 after a single audit found 10 bugs of the same shape
that the test suite missed for 5+ days because tests verified
specific files, not invariants.

The invariants enforced here:

1. Every option `api.submit_order(...)` site MUST pass
   `position_intent` (Alpaca async-cancels short opens without it —
   the ARCC root cause).

2. Every executor that submits to Alpaca + writes to journal MUST
   have a duplicate-position guard (the ARCC runaway pattern).

3. No production trade-execution path may use bare `except: pass`
   on a DB write or broker call (silent state drift).

4. Test files that mock `api.get_order(...).filled_avg_price` must
   include AT LEAST ONE assertion exercising the None-immediately-
   after-submit case (paper accounts take 50-500ms to fill — mocks
   that always return a numeric price hide real bugs, as caught
   2026-05-06 multileg + 2026-05-07 audit).
"""

from __future__ import annotations
import ast
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel_path):
    full = os.path.join(ROOT, rel_path)
    with open(full, "r") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Invariant 1: option submit_order calls include position_intent
# ---------------------------------------------------------------------------

# Production files that contain option order submissions. If you add a
# new module that submits options, add it here.
OPTION_SUBMIT_FILES = [
    "options_trader.py",
    "options_multileg.py",
]


def test_every_option_submit_passes_position_intent():
    """Every `api.submit_order(...)` whose `symbol` is an OCC option
    contract must include a `position_intent=` kwarg.

    A grep approximation: scan each option file for submit_order
    calls and assert each call's source window contains
    `position_intent`. False positives are tolerable; false
    negatives (missing intent) would re-introduce the ARCC
    runaway."""
    failures = []
    for rel in OPTION_SUBMIT_FILES:
        src = _read(rel)
        # Find every `api.submit_order(` call's start, then inspect
        # both the call site AND the surrounding ~2000 chars before
        # (kwargs may be assembled into a dict above and passed via
        # **kwargs, e.g. options_multileg's combo path).
        for m in re.finditer(r"api\.submit_order\s*\(", src):
            start = m.start()
            depth = 0
            end = start
            for i in range(start, min(len(src), start + 1500)):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            call_window = src[start:end + 1]
            # If the call uses **dict spread, look up to 2000 chars
            # back for either:
            #  - position_intent literally in the kwargs dict
            #    construction, OR
            #  - _alpaca_leg_dict(...) which is the Alpaca-leg-dict
            #    builder that always includes intent inside the leg
            #    array (combo path).
            uses_spread = re.search(r"\*\*\w+", call_window) is not None
            preamble = src[max(0, start - 2000):start]
            in_preamble = uses_spread and (
                "position_intent" in preamble
                or "_alpaca_leg_dict" in preamble
            )
            if "position_intent" not in call_window and not in_preamble:
                line_no = src.count("\n", 0, start) + 1
                failures.append(f"{rel}:{line_no}\n{call_window}")
    assert not failures, (
        "Found option submit_order call(s) without position_intent.\n"
        "Alpaca async-cancels option opens without intent (ARCC runaway).\n\n"
        + "\n\n".join(failures)
    )


# ---------------------------------------------------------------------------
# Invariant 2: every executor with submit_order + log_trade has a dup guard
# ---------------------------------------------------------------------------

# Functions that are entry executors — they submit orders to the
# broker and write a journal row. Each MUST have a duplicate
# position guard. Format: (file, function_name, dup_guard_marker)
# Each tuple lists ANY of the marker strings that prove the guard is
# present (different executors phrase it differently — multileg has
# "Refusing to duplicate", single-leg + pair use "already exists").
DUP_GUARDED_EXECUTORS = [
    ("options_multileg.py", "execute_multileg_strategy",
        ["Refusing to duplicate", "already exists"]),
    ("options_trader.py", "execute_option_strategy",
        ["Refusing to duplicate", "already exists"]),
    ("stat_arb_pair_book.py", "execute_pair_trade",
        ["Refusing to duplicate", "already exists"]),
]


def test_every_entry_executor_has_dup_guard():
    """Each entry executor must contain a dup-guard marker string.
    The marker proves the source still has the journal-query
    pattern (we don't enforce the exact code, just that it didn't
    silently get removed by a future refactor)."""
    failures = []
    for rel, func_name, markers in DUP_GUARDED_EXECUTORS:
        src = _read(rel)
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            failures.append(f"{rel}: parse error: {exc}")
            continue
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                target = node
                break
        if target is None:
            failures.append(f"{rel}: function {func_name} not found")
            continue
        body_src = ast.get_source_segment(src, target) or ""
        # Case-insensitive marker match — different executors phrase
        # the same idea with different capitalization.
        body_lower = body_src.lower()
        if not any(m.lower() in body_lower for m in markers):
            failures.append(
                f"{rel}::{func_name} missing dup-guard marker "
                f"(any of: {markers}). Without a guard the executor "
                f"can re-fire on every cycle (the ARCC runaway pattern)."
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Invariant 3: no bare except: pass on DB writes or broker calls in
# trade-execution paths
# ---------------------------------------------------------------------------

# Production files where silent failures = silent data drift. NOT
# data-fetcher modules (alternative_data, news_sentiment, etc.) —
# those have legit best-effort patterns documented separately.
TRADE_EXECUTION_FILES = [
    "trade_pipeline.py",
    "trader.py",
    "options_trader.py",
    "options_multileg.py",
    "stat_arb_pair_book.py",
    "bracket_orders.py",
]


def test_no_bare_except_pass_on_db_or_broker_calls():
    """Walk each file's AST. For every `try: ... except: pass`
    block, check whether the try-body contains a DB write
    (`UPDATE trades`, `conn.execute`) or a broker call
    (`api.submit_order`, `api.cancel_order`). If yes, flag.

    `except: pass` is allowed elsewhere — best-effort cache, etc.
    The narrow rule: trade-execution mutations cannot be silent."""
    risky_calls = re.compile(
        r"(?:api\.(?:submit_order|cancel_order|get_order|list_orders)|"
        r"UPDATE\s+trades|conn\.execute|cancel_for_symbol)",
        re.IGNORECASE,
    )
    failures = []
    for rel in TRADE_EXECUTION_FILES:
        src = _read(rel)
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            failures.append(f"{rel}: parse error: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            try_body_src = "\n".join(
                ast.get_source_segment(src, n) or "" for n in node.body
            )
            if not risky_calls.search(try_body_src):
                continue
            for handler in node.handlers:
                # Bare `except:` or `except Exception:` with body
                # that's exactly `pass` (or an unused variable assign
                # then nothing) is the silent swallow we ban.
                if len(handler.body) == 1 and isinstance(
                    handler.body[0], ast.Pass,
                ):
                    line = handler.lineno
                    failures.append(
                        f"{rel}:{line} bare `except: pass` on a try-block "
                        f"that contains DB or broker call. This is the "
                        f"silent-swallow pattern that hid the ARCC runaway."
                    )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Invariant 4: tests that mock filled_avg_price include the None case
# ---------------------------------------------------------------------------

def test_filled_avg_price_mocks_include_none_case():
    """Tests that mock `api.get_order(...).filled_avg_price`
    returning a numeric value as the immediate-after-submit reply
    are unrealistic — Alpaca paper takes 50-500ms to fill. A test
    that ONLY asserts the eventual happy path will not catch
    code that mishandles the None-at-submit case (caught
    2026-05-06: 28 multileg legs shipped to prod with NULL
    fill_price for days because the test mocked `0.45`
    immediately).

    Rule: any test file that mentions `filled_avg_price` must
    ALSO mention either `None`, `pending`, `not_filled`, or `race`
    somewhere — proving SOMEONE thought about the unfilled case.
    """
    tests_dir = os.path.join(ROOT, "tests")
    failures = []
    for fname in os.listdir(tests_dir):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(tests_dir, fname)
        with open(path, "r") as f:
            content = f.read()
        if "filled_avg_price" not in content:
            continue
        # Allowlist: this guardrail file itself documents the rule
        if fname == "test_broker_submit_invariants.py":
            continue
        # Look for any of the unfilled-state markers
        if not re.search(
            r"\bNone\b|\bpending\b|not_filled|\brace\b|filled_avg_price\s*=\s*None",
            content,
        ):
            failures.append(
                f"tests/{fname}: mocks filled_avg_price but never "
                f"exercises the None / pending case. Add at least one "
                f"assertion for the immediate-after-submit None reply."
            )
    assert not failures, (
        "Unrealistic mocks hide real bugs (caught 2026-05-06).\n"
        + "\n".join(failures)
    )
