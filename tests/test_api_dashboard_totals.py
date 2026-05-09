"""Pin the `/api/dashboard-totals` endpoint's behavior.

Caught 2026-05-09: the endpoint had two broken imports
(`build_context` from user_context, `get_today_total` from
ai_cost_ledger) — neither symbol exists. The first one made the
endpoint 500 every refresh; the JS silently swallowed `d.error`
and never overwrote the dashboard cells. The bug was invisible
because the server-rendered initial values stayed on screen.

This test pins:
1. The endpoint returns 200 (not 500) for an authenticated user.
2. `cost_today` is sourced from `spend_summary(...)["today"]["usd"]`
   so a real spend on a profile shows up in the JSON response.
3. Per-profile rows + totals match expected values.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _admin():
    u = MagicMock()
    u.is_authenticated = True
    u.id = 42
    u.email = "admin@example.com"
    u.is_admin = True
    u.role = "admin"
    u.is_viewer = False
    u.linked_to_user_id = None
    u.effective_user_id = 42
    u.display_name = "Admin"
    return u


def _client():
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


class TestDashboardTotalsEndpoint:
    def test_endpoint_returns_200_with_real_cost_per_profile(self, monkeypatch):
        """The endpoint must return 200 + a real `cost_today` value
        for each profile. Before the 2026-05-09 fix this 500'd
        because `build_context` and `get_today_total` don't exist."""
        from app import create_app

        # Two enabled profiles, each with a different mocked cost.
        profile_costs = {1: 0.42, 3: 1.18}

        def fake_active_profiles(user_id=None):
            return [
                {"id": 1, "name": "Mid Cap", "user_id": 42},
                {"id": 3, "name": "Small Cap", "user_id": 42},
            ]

        def fake_build_ctx(profile_id):
            ctx = MagicMock()
            ctx.db_path = f"quantopsai_profile_{profile_id}.db"
            ctx.profile_id = profile_id
            return ctx

        def fake_account(ctx=None, **kw):
            return {"equity": 100000.0, "cash": 50000.0,
                    "buying_power": 100000.0}

        def fake_positions(ctx=None, **kw):
            return []

        def fake_spend_summary(db_path):
            # Pull profile_id out of the db_path filename
            pid = int(db_path.replace("quantopsai_profile_", "")
                      .replace(".db", ""))
            return {"today": {"usd": profile_costs[pid]}}

        # The endpoint imports each helper inside the function body,
        # so mock at the source modules.
        monkeypatch.setattr(
            "models.get_active_profiles", fake_active_profiles,
        )
        monkeypatch.setattr(
            "models.build_user_context_from_profile", fake_build_ctx,
        )
        monkeypatch.setattr(
            "client.get_account_info", fake_account,
        )
        monkeypatch.setattr(
            "client.get_positions", fake_positions,
        )
        monkeypatch.setattr(
            "ai_cost_ledger.spend_summary", fake_spend_summary,
        )

        client = _client()
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp = client.get("/api/dashboard-totals")

        assert resp.status_code == 200, (
            f"Endpoint returned {resp.status_code} (would 500 before fix). "
            f"Body: {resp.data[:200]}"
        )
        data = json.loads(resp.data)
        assert "profiles" in data
        assert len(data["profiles"]) == 2
        by_id = {p["id"]: p for p in data["profiles"]}
        # Each profile's cost_today comes from spend_summary, NOT 0
        assert by_id[1]["cost_today"] == pytest.approx(0.42)
        assert by_id[3]["cost_today"] == pytest.approx(1.18)
        # Total cost is the sum
        assert data["total_cost"] == pytest.approx(1.60)

    def test_endpoint_handles_spend_summary_failure_gracefully(self, monkeypatch):
        """If spend_summary fails for one profile, the endpoint must
        still return 200 + cost_today=0 for that profile (with a log
        warning), not 500 the whole request."""
        def fake_active_profiles(user_id=None):
            return [{"id": 1, "name": "P1", "user_id": 42}]

        def fake_build_ctx(profile_id):
            ctx = MagicMock()
            ctx.db_path = "quantopsai_profile_1.db"
            return ctx

        monkeypatch.setattr(
            "models.get_active_profiles", fake_active_profiles,
        )
        monkeypatch.setattr(
            "models.build_user_context_from_profile", fake_build_ctx,
        )
        monkeypatch.setattr(
            "client.get_account_info",
            lambda ctx=None, **kw: {"equity": 0, "cash": 0, "buying_power": 0},
        )
        monkeypatch.setattr(
            "client.get_positions", lambda ctx=None, **kw: [],
        )
        monkeypatch.setattr(
            "ai_cost_ledger.spend_summary",
            lambda db_path: (_ for _ in ()).throw(RuntimeError("DB locked")),
        )

        client = _client()
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp = client.get("/api/dashboard-totals")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["profiles"][0]["cost_today"] == 0.0
