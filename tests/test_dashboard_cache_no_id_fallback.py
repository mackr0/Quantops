"""Pin the fix for `_safe_positions` / `_safe_account_info` cache
key safety.

Caught 2026-05-10 via a rare flake in
`test_enriched_positions::test_short_position_gets_sell_side`. Root
cause: cache key was `f"positions_{getattr(ctx, 'db_path', id(ctx))}"`.
When ctx had no db_path (test fixtures with SimpleNamespace ctx),
the key fell back to `id(ctx)`. CPython reuses object IDs after GC,
so over a long test run a fresh ctx could land at a recently-freed
address within the 30s TTL window, causing cache hit on a different
test's positions.

Fix: skip caching entirely when ctx has no db_path. Production ctx
always has db_path (built via `build_user_context_from_profile`);
the fallback only existed for defensive coding and tripped on
tests.

This test pins:
1. `_safe_positions(ctx_without_db_path)` does NOT populate the
   shared cache (so two such calls can't collide).
2. `_safe_positions(ctx_with_db_path)` DOES populate the cache
   (production behavior preserved).
3. Same invariants for `_safe_account_info`.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def _clear_dashboard_cache():
    """Snapshot + restore the module-level dashboard cache so this
    file's tests don't see entries from prior tests and don't leak
    entries forward."""
    import views
    snapshot = dict(views._dashboard_cache)
    views._dashboard_cache.clear()
    yield
    views._dashboard_cache.clear()
    views._dashboard_cache.update(snapshot)


def _ctx_without_db_path():
    """Mirrors `test_enriched_positions._ctx_with_positions`'s shape.
    SimpleNamespace, no db_path attr — exactly the shape that tripped
    the id(ctx) fallback."""
    return SimpleNamespace(
        get_alpaca_api=lambda: MagicMock(),
        display_name="Test", segment="small",
    )


def _ctx_with_db_path(path="/tmp/profile_999.db"):
    return SimpleNamespace(
        get_alpaca_api=lambda: MagicMock(),
        display_name="Test", segment="small",
        db_path=path,
    )


class TestSafePositionsCacheKeySafety:
    def test_no_db_path_skips_cache_entirely(self):
        """When ctx has no db_path, the fetch must NOT populate the
        cache. Otherwise GC + id-reuse can cause cross-test
        pollution within the 30s TTL window."""
        from views import _safe_positions, _dashboard_cache

        ctx = _ctx_without_db_path()
        with patch("client.get_positions",
                   return_value=[{"symbol": "AAPL", "qty": 5}]):
            _safe_positions(ctx)

        # Cache must contain ZERO entries — neither under id(ctx)
        # nor under any other key derived from the ctx.
        assert not _dashboard_cache, (
            f"Cache should be empty when ctx has no db_path, got: "
            f"{dict(_dashboard_cache)}"
        )

    def test_db_path_populates_cache_under_db_path_key(self):
        """Production behavior preserved: when ctx has db_path, the
        result is cached under the db_path-derived key."""
        from views import _safe_positions, _dashboard_cache

        ctx = _ctx_with_db_path("/tmp/profile_42.db")
        positions = [{"symbol": "MSFT", "qty": 10}]
        with patch("client.get_positions", return_value=positions):
            _safe_positions(ctx)

        assert "positions_/tmp/profile_42.db" in _dashboard_cache
        ts, cached_positions = _dashboard_cache[
            "positions_/tmp/profile_42.db"
        ]
        assert cached_positions == positions

    def test_two_no_db_path_ctxs_do_not_share_cache(self):
        """The exact bug shape: two SimpleNamespace ctxs with
        DIFFERENT positions must each get their own positions back,
        not the other's. Pre-fix, with id() collision, the second
        call would have returned the first call's cached positions."""
        from views import _safe_positions

        ctx1 = _ctx_without_db_path()
        ctx2 = _ctx_without_db_path()

        with patch("client.get_positions",
                   return_value=[{"symbol": "AAPL", "qty": 5}]):
            r1 = _safe_positions(ctx1)
        with patch("client.get_positions",
                   return_value=[{"symbol": "TSLA", "qty": -3}]):
            r2 = _safe_positions(ctx2)

        assert r1 == [{"symbol": "AAPL", "qty": 5}]
        assert r2 == [{"symbol": "TSLA", "qty": -3}]


class TestSafeAccountInfoCacheKeySafety:
    def test_no_db_path_skips_cache(self):
        from views import _safe_account_info, _dashboard_cache

        ctx = _ctx_without_db_path()
        with patch("client.get_account_info",
                   return_value={"equity": 100000}):
            _safe_account_info(ctx)

        assert not _dashboard_cache, (
            f"Cache should be empty when ctx has no db_path, got: "
            f"{dict(_dashboard_cache)}"
        )

    def test_db_path_populates_cache_under_db_path_key(self):
        from views import _safe_account_info, _dashboard_cache

        ctx = _ctx_with_db_path("/tmp/profile_77.db")
        with patch("client.get_account_info",
                   return_value={"equity": 50000}):
            _safe_account_info(ctx)

        assert "account_/tmp/profile_77.db" in _dashboard_cache
