"""2026-07-22 — the rate-limit death spiral. The protective sweep
re-polled every dead bracket parent every cycle for every profile, the
fill-updater re-polled every unfilled row, and the combined volume
blew Alpaca's rate limit. The 429s rejected legitimate orders (the
GOOG option-spread closes) AND starved `_task_update_fills` — the
machinery that voids journaled-but-never-filled phantom rows — so
phantoms accumulated (NFLX journal +482 vs broker +359, GOOG −9 vs
−1), the account decomposition broke, and the integrity kill switch
halted the fleet.

Pins:
  1. Immutable orders (terminal status, terminal/absent legs) are
     cached FOREVER — one API call per process lifetime.
  2. A terminal parent with a LIVE leg is NOT immutable (a bracket
     child can still be canceled later) — TTL cache only.
  3. Non-terminal orders re-fetch after TTL.
  4. A 429 opens the breaker; during cooldown stale cache is served
     and uncached lookups raise WITHOUT touching the API.
  5. Wiring: the sweep's parent check, pending-row check,
     _is_order_active, and the fill-updater all use the cache; the
     sweep defers placements while the breaker is open; the guarded
     submit door reports submit-side 429s to the breaker.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _fresh_cache():
    import order_status_cache as osc
    osc.clear_cache()
    yield
    osc.clear_cache()


def _order(status, legs=None):
    o = MagicMock()
    o.status = status
    o.legs = legs
    return o


class TestCache:

    def test_immutable_cached_forever(self):
        from order_status_cache import get_order_cached
        api = MagicMock()
        api.get_order.return_value = _order(
            "filled", legs=[_order("canceled")])
        for _ in range(5):
            get_order_cached(api, "oid1", nested=True)
        assert api.get_order.call_count == 1

    def test_terminal_parent_with_live_leg_not_forever(self, monkeypatch):
        import order_status_cache as osc
        api = MagicMock()
        api.get_order.return_value = _order("filled", legs=[_order("new")])
        osc.get_order_cached(api, "oid2", nested=True)
        # expire the TTL
        key = ("oid2", True)
        fetched_at, order, forever = osc._cache[key]
        assert forever is False
        osc._cache[key] = (fetched_at - osc.NONTERMINAL_TTL_S - 1,
                           order, forever)
        osc.get_order_cached(api, "oid2", nested=True)
        assert api.get_order.call_count == 2

    def test_nonterminal_refetches_after_ttl(self):
        import order_status_cache as osc
        api = MagicMock()
        api.get_order.return_value = _order("new")
        osc.get_order_cached(api, "oid3")
        osc.get_order_cached(api, "oid3")          # within TTL — cached
        assert api.get_order.call_count == 1
        key = ("oid3", False)
        fetched_at, order, forever = osc._cache[key]
        osc._cache[key] = (fetched_at - osc.NONTERMINAL_TTL_S - 1,
                           order, forever)
        osc.get_order_cached(api, "oid3")
        assert api.get_order.call_count == 2


class TestBreaker:

    def test_429_opens_breaker_and_reraises(self):
        import order_status_cache as osc
        api = MagicMock()
        api.get_order.side_effect = RuntimeError(
            'Alpaca order rejected (429): {"message":"rate limit exceeded"}')
        with pytest.raises(RuntimeError):
            osc.get_order_cached(api, "oid4")
        assert osc.rate_limited() is True

    def test_cooldown_serves_stale_and_blocks_uncached(self):
        import order_status_cache as osc
        api = MagicMock()
        api.get_order.return_value = _order("new")
        osc.get_order_cached(api, "cached-oid")
        osc.note_rate_limit("test")
        # stale-allowed for the cached one, no new API hit
        key = ("cached-oid", False)
        fetched_at, order, forever = osc._cache[key]
        osc._cache[key] = (fetched_at - osc.NONTERMINAL_TTL_S - 1,
                           order, forever)
        assert osc.get_order_cached(api, "cached-oid") is order
        assert api.get_order.call_count == 1
        # uncached raises without touching the API
        with pytest.raises(osc.RateLimitCooldown):
            osc.get_order_cached(api, "never-seen")
        assert api.get_order.call_count == 1

    def test_non_429_errors_do_not_open_breaker(self):
        import order_status_cache as osc
        api = MagicMock()
        api.get_order.side_effect = RuntimeError("connection reset")
        with pytest.raises(RuntimeError):
            osc.get_order_cached(api, "oid5")
        assert osc.rate_limited() is False


class TestWiring:

    def _src(self, name):
        return open(os.path.join(REPO, name)).read()

    def test_sweep_parent_check_uses_cache(self):
        src = self._src("bracket_orders.py")
        idx = src.index("nested=True,\n                        )")
        window = src[idx - 700:idx]
        assert "get_order_cached" in window

    def test_sweep_pending_row_check_uses_cache(self):
        src = self._src("bracket_orders.py")
        idx = src.index('_existing_oid = _pending_row["order_id"]')
        assert "get_order_cached" in src[idx:idx + 300]

    def test_is_order_active_uses_cache(self):
        src = self._src("bracket_orders.py")
        idx = src.index("def _is_order_active")
        assert "get_order_cached" in src[idx:idx + 700]

    def test_fill_updater_uses_cache(self):
        src = self._src("multi_scheduler.py")
        idx = src.index("for trade in unfilled:")
        assert "get_order_cached" in src[idx:idx + 900]

    def test_sweep_defers_placement_during_cooldown(self):
        src = self._src("bracket_orders.py")
        idx = src.index("deferring protective placement")
        window = src[idx - 600:idx]
        assert "rate_limited()" in window

    def test_submit_door_reports_429(self):
        src = self._src("order_guard.py")
        idx = src.index("result = api.submit_order(**kwargs)")
        window = src[idx:idx + 800]
        assert "note_rate_limit" in window
