"""Structural guardrail: every "expensive" operation (LLM call,
yfinance ticker fetch) inside a `for` loop is either behind a
cache, behind a rate limiter / explicit budget guard, or annotated
with a `# COSTLY_OK:` comment naming the bound.

The bug class.
An inner `for symbol in candidates:` loop that calls
`anthropic.Anthropic(...).messages.create(...)` per symbol can
balloon: 200 candidates × ~$0.02/call = $4 / cycle. A 15-minute
cadence over a single market day = ~$100/day per profile. Over
4 profiles = ~$400/day silent burn. The cost surfaces in next
month's bill, not in any per-cycle log.

Equivalent shapes for yfinance: `yf.Ticker(symbol).history(...)`
inside a per-symbol loop is rate-limited by Yahoo (HTTP 429 after
~50 calls in 60s); the loop runs to ~50 then quietly fails for
the rest, so half the candidates are missing data with no error.

Acceptable patterns:
  1. Cache lookup (`shared_ai_cache.get(...)` or `_get_cached(...)`)
     guards the loop OR is INSIDE it (cache-hits short-circuit).
  2. Rate limiter inside the loop (`time.sleep(...)`, RateLimiter,
     or yf_lock.thread_safe_download which serializes).
  3. Explicit budget guard (`cost_guard.can_afford_action(...)`
     or `_within_budget()` check).
  4. Explicit `# COSTLY_OK: <rationale>` comment naming the bound
     (e.g. "loop is bounded to <= 10 candidates").
  5. Loop is inside a function whose caller batches/caches
     (these need explicit COSTLY_OK with the caller name).

Default-deny: any new costly-call-in-a-loop without one of the
above fails the test.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from typing import Iterator, List, Optional, Set, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Files where loop-based expensive calls would be most damaging.
# Targeted scope: not every file in the repo, just ones that
# orchestrate per-symbol or per-candidate loops touching paid APIs.
COSTLY_FILES = (
    "ai_analyst.py",
    "ai_providers.py",
    "ensemble.py",
    "news_sentiment.py",
    "trade_pipeline.py",
    "multi_scheduler.py",
    "alternative_data.py",
    "market_data.py",
    "macro_data.py",
    "factor_data.py",
    "sec_filings.py",
    "earnings_calendar.py",
    "sector_classifier.py",
    "screener.py",
    "options_oracle.py",
    "options_strategy_advisor.py",
    "post_mortem.py",
)


# Call shapes that are EXPENSIVE per invocation.
# Format: (callee_attr, value_id_or_None) — None means match any caller.
COSTLY_CALL_PATTERNS = (
    # LLM provider calls
    ("create", "messages"),       # client.messages.create(...)
    ("create", "completions"),    # client.completions.create(...)
    # Yahoo Finance per-symbol fetches
    ("Ticker", "yf"),             # yf.Ticker(...)
    ("Ticker", "yfinance"),       # yfinance.Ticker(...)
    ("download", "yf"),           # yf.download(...) — bulk but rate-limited
    ("download", "yfinance"),
)


# Names whose presence inside the same scope as the costly call
# indicates a cache lookup is in play.
CACHE_NAME_TOKENS = (
    "shared_ai_cache", "_get_cached", "lru_cache", "cached",
    "cache_get", "get_from_cache", "_ensemble_cache",
    "_political_cache", "yf_lock",
)

# Names indicating a rate limit / sleep / budget guard.
RATE_LIMIT_TOKENS = (
    "time.sleep", "ratelimit", "RateLimiter", "_within_budget",
    "can_afford_action", "cost_guard", "throttle",
)


def _build_parent_lookup(tree: ast.Module) -> dict:
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def _is_costly_call(node: ast.Call) -> bool:
    """Match against COSTLY_CALL_PATTERNS."""
    fn = node.func
    if not isinstance(fn, ast.Attribute):
        return False
    attr = fn.attr
    # Walk inward to get base value name(s)
    base_chain: List[str] = []
    cur = fn.value
    while isinstance(cur, ast.Attribute):
        base_chain.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        base_chain.append(cur.id)
    base_chain.reverse()
    base_name_last = base_chain[-1] if base_chain else None
    base_name_first = base_chain[0] if base_chain else None
    for c_attr, c_base in COSTLY_CALL_PATTERNS:
        if attr != c_attr:
            continue
        if c_base is None:
            return True
        # Match either the immediate base attr (client.messages.create →
        # last is "messages") or the leftmost base ident
        if base_name_last == c_base or base_name_first == c_base:
            return True
    return False


def _enclosing_for_loop(node: ast.AST,
                          parent_lookup) -> Optional[ast.For]:
    """Return the innermost enclosing For loop (or None)."""
    cur = parent_lookup.get(id(node))
    while cur is not None:
        if isinstance(cur, ast.For):
            return cur
        cur = parent_lookup.get(id(cur))
    return None


def _enclosing_function(node: ast.AST,
                          parent_lookup) -> Optional[ast.FunctionDef]:
    cur = parent_lookup.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parent_lookup.get(id(cur))
    return None


def _scope_contains_token(node: ast.AST, tokens: Tuple[str, ...]) -> bool:
    """Render `node` to source and check for any of `tokens`."""
    try:
        src = ast.unparse(node)
    except Exception:
        return False
    return any(tok in src for tok in tokens)


def _has_costly_ok_comment(src: str, lineno: int) -> bool:
    """Walk UP through contiguous comment lines from lineno-1 looking
    for `# COSTLY_OK:`."""
    lines = src.split("\n")
    idx = lineno - 2
    while idx >= 0:
        line = lines[idx].strip()
        if line.startswith("# COSTLY_OK"):
            return True
        if line.startswith("#") or line == "":
            idx -= 1
            continue
        break
    return False


def _find_unguarded_costly_calls_in_loops(
        src_path: str) -> List[Tuple[int, str]]:
    with open(src_path) as fh:
        src = fh.read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    parent_lookup = _build_parent_lookup(tree)
    out: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_costly_call(node):
            continue
        # In a for loop?
        loop = _enclosing_for_loop(node, parent_lookup)
        if loop is None:
            continue
        # Loop body OR enclosing function contains a cache / rate
        # limiter / budget guard?
        if _scope_contains_token(loop, CACHE_NAME_TOKENS):
            continue
        if _scope_contains_token(loop, RATE_LIMIT_TOKENS):
            continue
        # Check enclosing function as well — many caches live one level
        # up (function-level cache lookup before the loop).
        fn_node = _enclosing_function(node, parent_lookup)
        if fn_node is not None:
            if _scope_contains_token(fn_node, CACHE_NAME_TOKENS):
                continue
            if _scope_contains_token(fn_node, RATE_LIMIT_TOKENS):
                continue
        # Explicit COSTLY_OK?
        if _has_costly_ok_comment(src, node.lineno):
            continue
        # Render the costly call for the failure message
        try:
            call_src = ast.unparse(node).split("\n")[0][:80]
        except Exception:
            call_src = "<call>"
        out.append((node.lineno, call_src))
    return out


class TestExpensiveOperationsThrottled:
    """For each costly file, every LLM-or-yfinance call inside a
    `for` loop must be guarded (cache/throttle/budget/COSTLY_OK)."""

    def test_no_unthrottled_costly_loop_calls(self):
        violations: List[Tuple[str, int, str]] = []
        for fname in COSTLY_FILES:
            path = os.path.join(REPO_ROOT, fname)
            if not os.path.exists(path):
                continue
            for lineno, snippet in _find_unguarded_costly_calls_in_loops(path):
                violations.append((fname, lineno, snippet))
        if violations:
            details = "\n".join(
                f"  {fname}:{lineno}  {snippet}"
                for fname, lineno, snippet in violations
            )
            pytest.fail(
                f"{len(violations)} costly call(s) inside a `for` "
                f"loop without a cache/throttle/budget guard. The "
                f"per-cycle cost can balloon silently when shortlist "
                f"size grows.\n\n"
                f"Violations:\n{details}\n\nFix one of:\n"
                f"  1. Cache lookup (shared_ai_cache, lru_cache, "
                f"_get_cached, etc.) inside the loop\n"
                f"  2. Rate limiter (time.sleep with rationale, "
                f"yf_lock.thread_safe_download)\n"
                f"  3. Budget guard (cost_guard.can_afford_action(...))"
                f"\n  4. `# COSTLY_OK: <rationale>` comment above the "
                f"call naming the bound (e.g. loop bounded to <=10 "
                f"items, or caller batches)"
            )
