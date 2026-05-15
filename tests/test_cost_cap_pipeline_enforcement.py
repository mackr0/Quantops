"""Structural guardrail: the daily cost cap must be enforced at the
ai_providers boundary so every AI call (batch_select, ensembles,
sentiment, transcript scoring, etc.) is gated — not just self-tuner
actions.

The bug class (2026-05-15).
The cost cap was advisory only: `can_afford_action()` was called
from 3 sites in self_tuning.py and nowhere else. The trade pipeline
ran uncapped — batch_select alone is ~73% of daily AI spend and it
ignored the cap entirely. Setting `daily_cost_ceiling_usd = 5` did
NOT cap actual spend at $5; it only capped self-tuner actions at $5.

The structural fix: gate INSIDE call_ai / call_ai_structured so
every AI call route flows through the check. New callers added in
the future inherit enforcement automatically — no risk of "forgot
to add can_afford_action to the new path."

This test pins the contract:
  - call_ai with a db_path that resolves to a user must check the cap
  - When over cap, call_ai must raise CostCapExceeded BEFORE hitting
    the provider (no provider call, no token spend, no ledger write)
  - call_ai_structured must enforce the same gate
  - The activity_log must record cap-block events so they're visible
    on the dashboard
  - When db_path can't be attributed to a user the call must NOT
    block (fall-open — no way to enforce per-user cap without user)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def fake_user_and_profile(tmp_path, monkeypatch):
    """Create a master DB with one user (id=42, ceiling $1.00) and
    one trading_profile (id=99) owned by that user. Return the
    profile DB path. The narrow ceiling makes "over cap" trivial to
    reproduce without spending real money."""
    master_db = str(tmp_path / "master.db")
    monkeypatch.setattr("config.DB_PATH", master_db, raising=False)
    monkeypatch.setenv("DB_PATH", master_db)

    # `_get_conn` reads config.DB_PATH at call time; init_user_db()
    # uses _get_conn(), so the patch above redirects the master DB
    # for all model writes during the test.
    from models import init_user_db, _get_conn
    init_user_db()
    with closing(_get_conn()) as conn:
        # users table requires email, password_hash, daily_cost_ceiling_usd.
        # Insert with a password_hash placeholder so NOT NULL passes.
        conn.execute(
            "INSERT INTO users (id, email, password_hash, "
            " daily_cost_ceiling_usd) VALUES (42, 'test@x', 'x', 1.0)",
        )
        conn.execute(
            "INSERT INTO trading_profiles (id, user_id, name, market_type) "
            "VALUES (99, 42, 'Test', 'small')",
        )
        conn.commit()
    profile_db = str(tmp_path / "quantopsai_profile_99.db")
    from journal import init_db as init_profile_db
    init_profile_db(profile_db)
    return profile_db


class TestCostCapEnforcedAtProviderBoundary:
    def test_user_id_for_db_path_resolves_correctly(
        self, fake_user_and_profile,
    ):
        """The path → user_id mapping is the foundation. If this
        breaks, EVERY downstream cap check falls open."""
        from cost_guard import user_id_for_db_path
        assert user_id_for_db_path(fake_user_and_profile) == 42

    def test_user_id_for_unknown_path_returns_none(self, tmp_path):
        """Paths that don't carry a profile_id (or refer to a profile
        that doesn't exist) return None — the cap-enforcement path
        treats None as 'fall open' so unaffiliated AI calls aren't
        blocked by a missing mapping."""
        from cost_guard import user_id_for_db_path
        assert user_id_for_db_path("") is None
        assert user_id_for_db_path("not_a_profile.db") is None
        assert user_id_for_db_path("/tmp/random.db") is None

    def test_call_ai_blocks_when_over_cap(self, fake_user_and_profile,
                                            monkeypatch):
        """The headline test. With ceiling $1, a fake "today_spend"
        of $0.99 means even a tiny AI call exceeds the cap. call_ai
        MUST raise CostCapExceeded WITHOUT invoking _call_provider."""
        # Fake "today_spend = $0.99" so any non-trivial call goes over.
        # Spend already AT the ceiling — any positive call cost
        # pushes over. This is the "second cycle of the day after
        # the user-set $1 cap was hit by the first" shape.
        monkeypatch.setattr(
            "cost_guard.today_spend", lambda user_id: 1.0,
        )
        # Ensure _call_provider is NOT invoked — that's the "no
        # provider call, no token spend" half of the contract.
        provider_invocations = []

        def _spy_provider(*args, **kwargs):
            provider_invocations.append((args, kwargs))
            return ("STUB", 100, 100)

        monkeypatch.setattr(
            "ai_providers._call_provider", _spy_provider,
        )

        from ai_providers import call_ai
        from cost_guard import CostCapExceeded
        with pytest.raises(CostCapExceeded) as exc_info:
            call_ai(
                "x" * 100, provider="anthropic",
                model="claude-haiku-4-5-20251001",
                api_key="fake-key", max_tokens=1024,
                db_path=fake_user_and_profile,
                purpose="test_call",
            )
        # Provider was NOT called.
        assert provider_invocations == [], (
            "CostCapExceeded must fire BEFORE the provider call; "
            f"provider was invoked {len(provider_invocations)} times"
        )
        # Recommendation message carries the action context.
        assert "test_call" in str(exc_info.value)

    def test_call_ai_proceeds_when_under_cap(self, fake_user_and_profile,
                                               monkeypatch):
        """Inverse test: the gate must NOT block legitimate calls.
        With $0 spent so far against a $1 ceiling, a tiny call
        proceeds normally."""
        monkeypatch.setattr(
            "cost_guard.today_spend", lambda user_id: 0.0,
        )
        # Stub the provider so the test doesn't hit real APIs.
        monkeypatch.setattr(
            "ai_providers._call_provider",
            lambda *a, **k: ("ok-response", 50, 50),
        )
        # Stub circuit-breaker checks
        monkeypatch.setattr(
            "provider_circuit.is_open", lambda _provider: False,
        )
        monkeypatch.setattr(
            "provider_circuit.record_success", lambda _provider: None,
        )

        from ai_providers import call_ai
        result = call_ai(
            "tiny prompt", provider="anthropic",
            model="claude-haiku-4-5-20251001",
            api_key="fake-key", max_tokens=128,
            db_path=fake_user_and_profile,
            purpose="test_call",
        )
        assert result == "ok-response"

    def test_call_ai_falls_open_when_db_path_unattributable(
        self, monkeypatch,
    ):
        """Calls without a db_path (or with an unrecognized one) MUST
        proceed — there's no user to attribute spend to. Falling
        closed would block legitimate startup/admin AI calls that
        don't run inside a profile context."""
        monkeypatch.setattr(
            "ai_providers._call_provider",
            lambda *a, **k: ("ok", 1, 1),
        )
        monkeypatch.setattr(
            "provider_circuit.is_open", lambda _provider: False,
        )
        monkeypatch.setattr(
            "provider_circuit.record_success", lambda _provider: None,
        )
        from ai_providers import call_ai
        # No db_path at all — must not raise CostCapExceeded.
        result = call_ai(
            "tiny", provider="anthropic",
            model="claude-haiku-4-5-20251001",
            api_key="fake-key", max_tokens=10,
        )
        assert result == "ok"

    def test_cost_cap_writes_to_activity_log(
        self, fake_user_and_profile, monkeypatch,
    ):
        """When the cap fires, an activity_log row must be written so
        the dashboard / activity feed can surface 'why no new trades.'
        Silent failures are explicitly forbidden by the project's
        feedback rules."""
        # Spend already AT the ceiling — any positive call cost
        # pushes over. This is the "second cycle of the day after
        # the user-set $1 cap was hit by the first" shape.
        monkeypatch.setattr(
            "cost_guard.today_spend", lambda user_id: 1.0,
        )
        monkeypatch.setattr(
            "ai_providers._call_provider",
            lambda *a, **k: ("STUB", 100, 100),
        )
        from ai_providers import call_ai
        from cost_guard import CostCapExceeded
        with pytest.raises(CostCapExceeded):
            call_ai(
                "x" * 100, provider="anthropic",
                model="claude-haiku-4-5-20251001",
                api_key="fake-key", max_tokens=1024,
                db_path=fake_user_and_profile,
                purpose="batch_select",
            )
        # Verify activity_log got the entry.
        from models import _get_conn
        with closing(_get_conn()) as conn:
            rows = conn.execute(
                "SELECT activity_type, title, detail FROM activity_log "
                "WHERE user_id = 42 AND activity_type = 'cost_cap_blocked'",
            ).fetchall()
        assert rows, (
            "Cost cap fired but no activity_log entry written — "
            "dashboard would show 'no new trades' with no explanation"
        )
        assert "batch_select" in rows[0][2]


class TestStructuralEnforcementCoverage:
    """Class-level guard: catches the BUG CLASS, not just the instance.

    Per the project's 'test for the class, not the instance' rule, this
    test enforces that every public AI-call entry point in ai_providers
    invokes _enforce_cost_cap. If a future refactor adds a new call
    path that forgets the gate, this test fails."""

    def test_every_public_call_function_invokes_cost_cap(self):
        """Read ai_providers.py and verify every function whose name
        starts with 'call_' contains a call to _enforce_cost_cap.
        Catches the next 'someone added a new entry point and forgot
        the gate' class of regression."""
        import ast
        import inspect
        import ai_providers

        src = inspect.getsource(ai_providers)
        tree = ast.parse(src)
        offending = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("call_"):
                continue
            # Look for any Call node whose .func attribute references
            # _enforce_cost_cap.
            invokes_gate = any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "_enforce_cost_cap"
                for sub in ast.walk(node)
            )
            if not invokes_gate:
                offending.append(node.name)
        assert not offending, (
            f"These public AI-call entry points do NOT invoke "
            f"_enforce_cost_cap: {offending}. "
            f"Every entry point must gate on the daily cost cap."
        )
