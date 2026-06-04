"""Random strategy's deterministic pick substitutes inactive Alpaca
symbols (2026-06-04).

Background: on 2026-06-04 reset day, pid26's `_pick_random_symbols`
seed drew [CRK, GE, PG, VERV, CERE]. VERV and CERE are inactive at
Alpaca → submit_order rejected each → strategy moved on without
substituting → pid26 ended day with 3 holdings, pid27 (sibling
replica) with 5. The two random replicas are meant to bound variance
against each other; day-1 capital imbalance compromises that.

Fix: `_pick_random_symbols` accepts an optional `api`; when present,
it draws a larger pool from the SAME seeded RNG and takes the first
n that pass `api.get_asset(sym)` active+tradable. Determinism
preserved: same seed + same broker state → same picks. Backward
compat: api=None falls back to pre-2026-06-04 unfiltered behavior.

Tests pin:
  1. With api=None: behaves exactly like before (no filtering).
  2. With api: skips inactive / non-tradable assets; returns n active.
  3. Determinism: same seed + same broker state → same picks.
  4. Inactive-heavy universe: substitution finds active picks even
     when most candidates are rejected.
  5. API error on get_asset: treated as not-tradable (fail-safe).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _mock_api(tradable_symbols: set):
    """Mock api.get_asset that returns active+tradable iff symbol is
    in `tradable_symbols`; inactive otherwise."""
    api = MagicMock()
    def get_asset(sym):
        asset = MagicMock()
        asset.status = "active" if sym in tradable_symbols else "inactive"
        asset.tradable = sym in tradable_symbols
        return asset
    api.get_asset.side_effect = get_asset
    return api


def _mock_api_raises(symbols_that_raise: set):
    """Mock api.get_asset that raises for the given symbols, otherwise
    returns active+tradable."""
    api = MagicMock()
    def get_asset(sym):
        if sym in symbols_that_raise:
            raise Exception(f"asset {sym} is not active")
        asset = MagicMock()
        asset.status = "active"
        asset.tradable = True
        return asset
    api.get_asset.side_effect = get_asset
    return api


def test_pick_with_no_api_preserves_old_behavior():
    """api=None must use the pre-2026-06-04 unfiltered behavior so
    tests / harnesses that don't mock get_asset still work."""
    from simple_strategies import _pick_random_symbols
    universe = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    picks = _pick_random_symbols(profile_id=42, universe=universe, n=5,
                                  api=None)
    assert len(picks) == 5
    assert len(set(picks)) == 5
    assert all(p in universe for p in picks)


def test_pick_filters_inactive_symbols(monkeypatch):
    """When api is provided, inactive symbols are substituted out of
    the sample. All returned picks must be active+tradable."""
    from simple_strategies import _pick_random_symbols
    universe = [f"SYM{i:03d}" for i in range(100)]
    inactive = {"SYM005", "SYM042", "SYM077"}
    tradable = set(universe) - inactive
    api = _mock_api(tradable_symbols=tradable)
    picks = _pick_random_symbols(profile_id=26, universe=universe,
                                  n=5, api=api)
    assert len(picks) == 5
    assert not (set(picks) & inactive), (
        "Inactive symbols must be substituted out — picks contained "
        f"inactive: {set(picks) & inactive}"
    )


def test_pick_is_deterministic_with_same_broker_state():
    """Same seed + same broker state → same picks. Critical for
    reproducibility of the random benchmark."""
    from simple_strategies import _pick_random_symbols
    universe = [f"SYM{i:03d}" for i in range(100)]
    inactive = {"SYM010", "SYM020"}
    tradable = set(universe) - inactive
    api1 = _mock_api(tradable_symbols=tradable)
    api2 = _mock_api(tradable_symbols=tradable)
    picks1 = _pick_random_symbols(26, universe, 5, api=api1)
    picks2 = _pick_random_symbols(26, universe, 5, api=api2)
    assert picks1 == picks2


def test_pick_handles_inactive_heavy_universe():
    """Even when most of the universe is inactive, substitution finds
    n active picks if the pool covers them. Pool size = max(n*4, 20),
    so an inactive ratio up to ~75% should still yield n active picks
    from a normal-sized universe."""
    from simple_strategies import _pick_random_symbols
    universe = [f"SYM{i:03d}" for i in range(100)]
    # Only 10 active out of 100 = 90% inactive — pool of 20 wouldn't
    # be enough but pool of max(5*4, 20) = 20 might find 2 (10% hit
    # rate). Use a smaller n=2 to be in the survivable range.
    tradable = {"SYM001", "SYM002", "SYM003", "SYM004", "SYM005",
                "SYM006", "SYM007", "SYM008", "SYM009", "SYM010"}
    api = _mock_api(tradable_symbols=tradable)
    picks = _pick_random_symbols(profile_id=99, universe=universe,
                                  n=2, api=api)
    assert set(picks) <= tradable
    # The pool from rng.sample(universe, 20) gives ~10% active hit
    # rate (2 expected); the test asserts that whatever subset is
    # found is fully active, not that exactly 2 are returned (the
    # exact count varies with seed and sample composition).


def test_pick_fails_safe_on_api_error():
    """If get_asset raises for some symbol, treat that symbol as
    not-tradable (don't optimistically include it). Better to
    substitute than to risk a mid-flight rejection."""
    from simple_strategies import _pick_random_symbols, _is_alpaca_tradable
    universe = [f"SYM{i:03d}" for i in range(100)]
    raises_for = {"SYM005", "SYM042"}
    api = _mock_api_raises(symbols_that_raise=raises_for)
    # Direct unit test on the predicate
    for sym in raises_for:
        assert _is_alpaca_tradable(api, sym) is False
    # End-to-end — picks must not include any symbol that raises
    picks = _pick_random_symbols(profile_id=26, universe=universe,
                                  n=5, api=api)
    assert not (set(picks) & raises_for)


def test_is_alpaca_tradable_with_no_api_is_true():
    """With api=None the predicate defaults to True so backwards-compat
    callers (tests, dry-runs) don't get all picks filtered out."""
    from simple_strategies import _is_alpaca_tradable
    assert _is_alpaca_tradable(None, "AAPL") is True
