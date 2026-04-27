"""Structural guardrail: no per-symbol Alpaca bar calls on the web path.

History: on 2026-04-27 the dashboard was timing out at 120s and getting
gunicorn workers SIGKILL'd because `client._make_price_fetcher` called
`market_data.get_bars(symbol, limit=1)` once per held symbol. With 10
virtual profiles × several positions × parallel workers, this hammered
Alpaca's rate limit and triggered 3-second-sleep retries until each
worker timed out.

The fix: a process-wide TTL price cache populated by ONE batched
`get_snapshots(symbols)` call per render. This file enforces that the
fix doesn't regress.
"""

import ast
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(path):
    with open(os.path.join(REPO_ROOT, path), "r") as fh:
        return fh.read()


def _calls_in_function(source, function_name):
    """Return the set of call-target names (e.g. 'get_bars', 'foo.bar')
    appearing inside the named function body. Used for an AST-level
    'function X must not call Y' assertion."""
    tree = ast.parse(source)
    targets = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    func = inner.func
                    if isinstance(func, ast.Name):
                        targets.add(func.id)
                    elif isinstance(func, ast.Attribute):
                        targets.add(func.attr)
                        if isinstance(func.value, ast.Name):
                            targets.add(f"{func.value.id}.{func.attr}")
    return targets


def test_price_fetcher_does_not_call_get_bars():
    """The dashboard price path must NEVER call single-symbol get_bars.

    This is the exact bug that timed out gunicorn workers on 2026-04-27.
    Single-symbol bar fetches × N positions × M virtual profiles ×
    parallel workers = Alpaca rate-limit storm = 120s worker timeout.

    The fix uses Alpaca's batched `get_snapshots(symbols)` instead.
    """
    source = _source("client.py")
    targets = _calls_in_function(source, "_make_price_fetcher")
    # The inner fetch() function lives inside _make_price_fetcher, so
    # walk-from-the-outer captures both. Same for _prefetch_prices's
    # use of get_snapshots — that's allowed and won't appear here.
    assert "get_bars" not in targets, (
        "REGRESSION: client._make_price_fetcher (or its inner fetch) "
        "now calls get_bars(). This causes a per-symbol Alpaca bar API "
        "call for every held position on every dashboard render — exactly "
        "the rate-limit storm that SIGKILL'd gunicorn workers on "
        "2026-04-27. Use _prefetch_prices() with batched get_snapshots() "
        "instead. Found call to: " + str(targets)
    )


def test_prefetch_prices_uses_batched_snapshots():
    """The replacement path must use get_snapshots, not per-symbol bars."""
    source = _source("client.py")
    targets = _calls_in_function(source, "_prefetch_prices")
    assert "get_snapshots" in targets, (
        "_prefetch_prices must call get_snapshots() — that's the whole "
        "point of the batched price-fetch fix. Found: " + str(targets)
    )
    assert "get_bars" not in targets, (
        "_prefetch_prices must NOT call get_bars(). Use batched "
        "get_snapshots() — that's the entire point of this function. "
        "Found: " + str(targets)
    )


def test_price_fetcher_has_process_wide_cache():
    """The price cache must be module-level (shared across workers in
    the same process), not per-fetcher-instance.

    Per-instance caches don't help: every dashboard render rebuilds
    the fetcher, every profile rebuilds it, so each request ate the
    cold-cache path. The 2026-04-27 fix moved the cache to module
    scope.
    """
    source = _source("client.py")
    # Match against the module-level binding (anchored at start of line)
    assert re.search(r"^_price_cache\s*[:=]", source, re.MULTILINE), (
        "client.py must declare a module-level `_price_cache` dict so "
        "all gunicorn workers in the process share the same TTL cache. "
        "Per-fetcher caches do not survive across requests."
    )
    assert re.search(r"^_PRICE_CACHE_TTL\s*=", source, re.MULTILINE), (
        "client.py must define `_PRICE_CACHE_TTL` at module scope — "
        "without a TTL the cache stays fresh forever and shows stale "
        "prices. Recommended value: 30 seconds."
    )
    assert "_price_cache_lock" in source, (
        "_price_cache must be guarded by a `threading.Lock()` — "
        "gunicorn workers may run threads that read/write concurrently."
    )


def test_dashboard_view_does_not_call_get_bars():
    """The dashboard route handler and its helpers must not invoke
    market_data.get_bars (which is single-symbol). All web-path price
    fetches go through the cached/batched path.
    """
    source = _source("views.py")
    # Hard-grep: any reference to get_bars in views.py is suspect. We
    # scan the whole file because the dashboard route invokes many
    # helpers; rather than transitively walk the call graph, we simply
    # forbid the symbol from appearing.
    forbidden = re.findall(r"\bget_bars\s*\(", source)
    assert not forbidden, (
        "views.py must not call get_bars() directly — it's a per-symbol "
        "Alpaca bar fetch and will trigger the same rate-limit storm "
        "that SIGKILL'd gunicorn workers on 2026-04-27. If you need a "
        "current price on the web path, use client._prefetch_prices() "
        "+ the cached fetcher. Occurrences: " + str(len(forbidden))
    )


def test_held_symbols_helper_exists():
    """The web-path prefetch needs the held-symbol list ahead of time —
    that's what enables the single batched snapshots() call."""
    source = _source("client.py")
    assert "_held_symbols_from_journal" in source, (
        "client.py must expose `_held_symbols_from_journal(db_path)` "
        "so virtual-profile entry points can batch-prefetch prices "
        "before invoking the journal helper that calls price_fetcher "
        "per symbol."
    )
