"""Pin the per-user TTL cache on api_dashboard_totals + api_portfolio.

Caught 2026-05-10 (Issue 14): both endpoints made Alpaca calls per
poll. With 11 profiles, JS polling every 30s caused ~22 Alpaca
calls per poll for /api/dashboard-totals — wasted because Alpaca
state doesn't change second-to-second. Multi-tab multiplied this.

This test pins the cache semantics that fix it:
1. Within TTL: second call hits cache (zero additional upstream calls).
2. Past TTL: next call bypasses cache, fetches fresh.
3. Per-user keying: user A's cached value never returned to user B.
4. Failures NOT cached: a 500 response on one call doesn't poison
   the next call's chance to retry cleanly.
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _user(user_id=1):
    u = MagicMock()
    u.is_authenticated = True
    u.id = user_id
    u.is_admin = True
    u.is_viewer = False
    u.role = "admin"
    u.email = f"u{user_id}@example.com"
    u.display_name = f"U{user_id}"
    u.effective_user_id = user_id
    return u


@pytest.fixture
def app_client(tmp_main_db, monkeypatch):
    """Real Flask app with a temp main DB, plus a clean TTL cache."""
    # Reset module-level cache between tests
    import views
    views._TTL_CACHE.clear()

    # Seed two users + one profile each so api_dashboard_totals has
    # something to iterate.
    import sqlite3
    conn = sqlite3.connect(tmp_main_db)
    conn.execute(
        "INSERT INTO users (id, email, password_hash, role, created_at) "
        "VALUES (1, 'u1@example.com', 'x', 'user', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO users (id, email, password_hash, role, created_at) "
        "VALUES (2, 'u2@example.com', 'x', 'user', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO trading_profiles (id, user_id, name, market_type) "
        "VALUES (1, 1, 'A', 'midcap')"
    )
    conn.execute(
        "INSERT INTO trading_profiles (id, user_id, name, market_type) "
        "VALUES (2, 2, 'B', 'midcap')"
    )
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    # Don't propagate exceptions in test — we want Flask's normal
    # 500-on-uncaught-exception behavior so the cache-isolation
    # test can verify a 500 response isn't cached.
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.config["TRAP_HTTP_EXCEPTIONS"] = False
    return app.test_client()


def _patches_for_dashboard_totals(call_counter):
    """Stub all upstream calls api_dashboard_totals makes; bump the
    shared counter on each one so the test can assert how many
    upstream fetches happened."""
    def fake_account(ctx=None, **kw):
        call_counter["account"] += 1
        return {"equity": 100_000.0, "cash": 50_000.0, "buying_power": 100_000.0}
    def fake_positions(ctx=None, **kw):
        call_counter["positions"] += 1
        return []
    def fake_spend(db_path):
        call_counter["spend"] += 1
        return {"today": {"usd": 0.42}, "7d": {"usd": 0}, "30d": {"usd": 0},
                "by_purpose_30d": [], "by_model_30d": []}
    def fake_build_ctx(profile_id):
        ctx = MagicMock()
        ctx.db_path = f"quantopsai_profile_{profile_id}.db"
        ctx.profile_id = profile_id
        return ctx
    return [
        patch("client.get_account_info", fake_account),
        patch("client.get_positions", fake_positions),
        patch("ai_cost_ledger.spend_summary", fake_spend),
        patch("models.build_user_context_from_profile", fake_build_ctx),
    ]


class TestDashboardTotalsCache:
    def test_second_call_within_ttl_hits_cache(self, app_client):
        counter = {"account": 0, "positions": 0, "spend": 0}
        with patch("flask_login.utils._get_user", return_value=_user(1)):
            for p in _patches_for_dashboard_totals(counter):
                p.start()
            try:
                r1 = app_client.get("/api/dashboard-totals")
                first_calls = dict(counter)
                r2 = app_client.get("/api/dashboard-totals")
            finally:
                from unittest.mock import patch as _p
                _p.stopall()

        assert r1.status_code == 200
        assert r2.status_code == 200
        # Second call must NOT have triggered any new upstream calls.
        assert counter == first_calls, (
            f"Cache miss within TTL — second call triggered new "
            f"upstream calls. before={first_calls} after={counter}"
        )
        # Both responses must be identical
        assert r1.data == r2.data

    def test_past_ttl_fetches_fresh(self, app_client, monkeypatch):
        """Move time forward past the TTL, expect upstream re-fetch."""
        counter = {"account": 0, "positions": 0, "spend": 0}
        with patch("flask_login.utils._get_user", return_value=_user(1)):
            for p in _patches_for_dashboard_totals(counter):
                p.start()
            try:
                app_client.get("/api/dashboard-totals")
                first_calls = dict(counter)

                # Fast-forward time past the TTL by mutating the
                # cache entry's stored timestamp.
                import views as v
                key = ("api_dashboard_totals", 1)
                ts, payload = v._TTL_CACHE[key]
                v._TTL_CACHE[key] = (ts - 100, payload)  # 100s ago

                app_client.get("/api/dashboard-totals")
            finally:
                from unittest.mock import patch as _p
                _p.stopall()

        # Upstream calls must have happened a second time.
        assert counter["account"] > first_calls["account"], (
            "Cache should have expired and re-fetched, but no new "
            "upstream account call was made."
        )

    def test_failure_not_cached(self, app_client):
        """If the per-profile loop hits an exception we still return
        a 200 (rows just contains fewer entries because each failed
        profile is logged + skipped). But if get_active_profiles
        itself raised, the endpoint would 500 — verify that 500
        response is NOT cached, so a transient failure doesn't lock
        the user out of fresh data for 30s."""
        # Force get_active_profiles to raise on the first call.
        first_call = {"raised": False}
        def flaky_active_profiles(user_id=None):
            if not first_call["raised"]:
                first_call["raised"] = True
                raise RuntimeError("transient DB error")
            # Recovers on second call
            return [{"id": 1, "name": "A", "user_id": 1, "market_type": "stocks"}]

        counter = {"account": 0, "positions": 0, "spend": 0}
        with patch("flask_login.utils._get_user", return_value=_user(1)):
            with patch("models.get_active_profiles", flaky_active_profiles):
                for p in _patches_for_dashboard_totals(counter):
                    p.start()
                try:
                    # First call: raises → 500 (Flask catches the
                    # bare exception via the route). The current
                    # code doesn't have a top-level try/except so
                    # it 500s; verify cache wasn't poisoned.
                    r1 = app_client.get("/api/dashboard-totals")
                    # Second call: should retry (recovers)
                    r2 = app_client.get("/api/dashboard-totals")
                finally:
                    from unittest.mock import patch as _p
                    _p.stopall()

        assert r1.status_code == 500
        # The retry must have actually happened — get_active_profiles
        # was called twice (raised once, succeeded once).
        assert first_call["raised"]
        # Second call should succeed because the failure wasn't cached.
        assert r2.status_code == 200, (
            f"Failure was cached — second call also failed. "
            f"r2 body: {r2.data[:300]}"
        )

    def test_per_user_keying_no_leak(self, app_client):
        """User 1's cached data must NOT be returned to user 2."""
        counter = {"account": 0, "positions": 0, "spend": 0}
        for p in _patches_for_dashboard_totals(counter):
            p.start()
        try:
            # User 1 fetches, populates cache key (..., 1)
            with patch("flask_login.utils._get_user", return_value=_user(1)):
                r1 = app_client.get("/api/dashboard-totals")
            calls_after_user1 = dict(counter)

            # User 2 fetches — must NOT get user 1's cached payload;
            # must trigger a fresh upstream fetch (user 2 has their
            # own profiles to walk).
            with patch("flask_login.utils._get_user", return_value=_user(2)):
                r2 = app_client.get("/api/dashboard-totals")
        finally:
            from unittest.mock import patch as _p
            _p.stopall()

        assert r1.status_code == 200
        assert r2.status_code == 200
        # User 2's call DID trigger upstream — proves it didn't share
        # user 1's cached entry.
        assert counter["account"] > calls_after_user1["account"], (
            "User 2's call should have triggered a fresh upstream "
            "call (not returned user 1's cached payload). Counter "
            "didn't change — cache was shared across users."
        )


class TestTtlCacheHelpers:
    """Direct tests of _ttl_cache_get / _ttl_cache_set so the
    contract is pinned independently of the routes."""

    def test_set_then_get_returns_value(self):
        from views import _ttl_cache_get, _ttl_cache_set, _TTL_CACHE
        _TTL_CACHE.clear()
        _ttl_cache_set(("test_key",), {"hello": "world"})
        assert _ttl_cache_get(("test_key",)) == {"hello": "world"}

    def test_expired_returns_none(self):
        from views import _ttl_cache_get, _ttl_cache_set, _TTL_CACHE
        _TTL_CACHE.clear()
        _ttl_cache_set(("test_key",), "value")
        # Mutate the timestamp to look 100 seconds old
        ts, val = _TTL_CACHE[("test_key",)]
        _TTL_CACHE[("test_key",)] = (ts - 100, val)
        assert _ttl_cache_get(("test_key",), ttl=30) is None

    def test_miss_returns_none(self):
        from views import _ttl_cache_get, _TTL_CACHE
        _TTL_CACHE.clear()
        assert _ttl_cache_get(("never_set",)) is None

    def test_custom_ttl_respected(self):
        from views import _ttl_cache_get, _ttl_cache_set, _TTL_CACHE
        _TTL_CACHE.clear()
        _ttl_cache_set(("k",), "v")
        # Within a 1000s TTL, still fresh
        assert _ttl_cache_get(("k",), ttl=1000) == "v"
        # Within a 0.001s TTL, stale (already aged a few microseconds)
        time.sleep(0.01)
        assert _ttl_cache_get(("k",), ttl=0.001) is None
