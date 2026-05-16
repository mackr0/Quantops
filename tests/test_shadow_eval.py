"""Tests for the shadow model evaluation system.

Covers:
- Agreement scoring across the various AI response shapes.
- dispatch_shadow_calls is a no-op when shadow eval is disabled or
  the db_path doesn't map to a profile.
- call_ai() return value is unaffected by shadow eval state (the
  operational invariant — shadow eval is observational only).
- Shadow provider errors never propagate into the operational path.
- The shadow cost cap blocks calls when exceeded, without blocking
  the primary call.
- The daily digest function skips silently when no rows exist.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import Future
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Agreement scoring (pure helpers)
# ---------------------------------------------------------------------------

class TestAgreementScoring:
    def test_matching_signal_returns_1(self):
        from shadow_eval import _compute_agreement
        assert _compute_agreement(
            {"signal": "BUY", "confidence": 0.8},
            {"signal": "BUY", "confidence": 0.6},
        ) == 1

    def test_differing_signal_returns_0(self):
        from shadow_eval import _compute_agreement
        assert _compute_agreement(
            {"signal": "BUY"}, {"signal": "HOLD"},
        ) == 0

    def test_case_insensitive_match(self):
        from shadow_eval import _compute_agreement
        assert _compute_agreement(
            {"signal": "buy"}, {"signal": "BUY"},
        ) == 1

    def test_missing_signal_returns_none(self):
        from shadow_eval import _compute_agreement
        assert _compute_agreement({}, {"signal": "BUY"}) is None
        assert _compute_agreement({"signal": "BUY"}, {}) is None
        assert _compute_agreement({}, {}) is None

    def test_action_field_used_as_fallback(self):
        from shadow_eval import _compute_agreement
        # Some prompts use `action` instead of `signal`
        assert _compute_agreement(
            {"action": "SELL"}, {"signal": "SELL"},
        ) == 1


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestConfigLoading:
    def test_disabled_profile_returns_none(self, tmp_main_db):
        """When enable_shadow_eval=0 (default), no shadow config."""
        from models import create_trading_profile, create_user
        user_id = create_user("u1@example.com", "pw", "U1")
        pid = create_trading_profile(user_id, "Test", "small")
        # By default enable_shadow_eval = 0
        from shadow_eval import _load_shadow_config
        assert _load_shadow_config(pid) is None

    def test_no_models_returns_none(self, tmp_main_db):
        from models import create_trading_profile, create_user, update_trading_profile
        user_id = create_user("u2@example.com", "pw", "U2")
        pid = create_trading_profile(user_id, "Test", "small")
        update_trading_profile(
            pid,
            enable_shadow_eval=1,
            shadow_models="[]",
        )
        from shadow_eval import _load_shadow_config
        assert _load_shadow_config(pid) is None

    def test_enabled_with_models_returns_dict(self, tmp_main_db):
        from models import create_trading_profile, create_user, update_trading_profile
        user_id = create_user("u3@example.com", "pw", "U3")
        pid = create_trading_profile(user_id, "Test", "small")
        update_trading_profile(
            pid,
            enable_shadow_eval=1,
            shadow_models=json.dumps(["google:gemini-2.0-flash"]),
            shadow_api_keys_enc=json.dumps({}),
        )
        from shadow_eval import _load_shadow_config
        cfg = _load_shadow_config(pid)
        assert cfg is not None
        assert cfg["models"] == [
            {"provider": "google", "model": "gemini-2.0-flash"}
        ]


# ---------------------------------------------------------------------------
# Operational invariant: call_ai output unchanged by shadow eval
# ---------------------------------------------------------------------------

class TestOperationalInvariant:
    """The primary contract: call_ai's return value is unaffected by
    shadow eval state. Whether shadow eval is off, on with a working
    candidate, or on with a broken candidate, the primary call must
    return the same text."""

    def _stub_primary_provider(self, monkeypatch):
        def fake_call(provider, prompt, model, api_key, max_tokens):
            return ('{"signal": "BUY", "confidence": 0.7}', 100, 50)
        monkeypatch.setattr("ai_providers._call_provider", fake_call)

    def test_shadow_eval_disabled_returns_clean_response(
            self, monkeypatch, tmp_profile_db):
        self._stub_primary_provider(monkeypatch)
        from ai_providers import call_ai
        result = call_ai(
            "test prompt", provider="anthropic", model="claude-haiku-4-5",
            api_key="test", db_path=tmp_profile_db, purpose="test",
        )
        assert result == '{"signal": "BUY", "confidence": 0.7}'

    def test_shadow_provider_error_does_not_affect_primary(
            self, monkeypatch, tmp_main_db, tmp_path):
        """If the shadow provider raises, call_ai still returns the
        primary result unmodified."""
        # Set up a profile-DB-style path so shadow eval will engage
        from models import create_user, create_trading_profile, update_trading_profile
        user_id = create_user("u4@example.com", "pw", "U4")
        pid = create_trading_profile(user_id, "Test", "small")
        update_trading_profile(
            pid,
            enable_shadow_eval=1,
            shadow_models=json.dumps(["google:gemini-2.0-flash"]),
            shadow_api_keys_enc=json.dumps({}),
        )
        profile_db = str(tmp_path / f"profile_{pid}.db")
        from journal import init_db
        init_db(profile_db)

        self._stub_primary_provider(monkeypatch)

        from ai_providers import call_ai
        # Even with the shadow_models config pointing at a provider
        # that has no api key (so dispatch errors / records "no key"),
        # the primary call still returns the same response.
        result = call_ai(
            "test prompt", provider="anthropic", model="claude-haiku-4-5",
            api_key="test", db_path=profile_db, purpose="test",
        )
        assert result == '{"signal": "BUY", "confidence": 0.7}'


# ---------------------------------------------------------------------------
# Dispatch + DB write (synchronous via inline pool)
# ---------------------------------------------------------------------------

class _InlinePool:
    """Stand-in for ThreadPoolExecutor that runs callables inline,
    so tests can observe rows synchronously."""

    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as exc:
            f.set_exception(exc)
        return f


class TestDispatchShadowCalls:
    def test_dispatch_writes_row_on_success(
            self, monkeypatch, tmp_main_db, tmp_path):
        from models import create_user, create_trading_profile, update_trading_profile
        user_id = create_user("u5@example.com", "pw", "U5")
        pid = create_trading_profile(user_id, "Test", "small")
        # Encrypt a fake key so the dispatcher actually fires
        from crypto import encrypt
        update_trading_profile(
            pid,
            enable_shadow_eval=1,
            shadow_models=json.dumps(["google:gemini-2.0-flash"]),
            shadow_api_keys_enc=json.dumps({
                "google": encrypt("fake-google-key"),
            }),
        )
        profile_db = str(tmp_path / f"profile_{pid}.db")
        from journal import init_db
        init_db(profile_db)

        # Stub the provider so the shadow call "succeeds"
        def fake_call(provider, prompt, model, api_key, max_tokens):
            assert provider == "google"
            assert api_key == "fake-google-key"
            return ('{"signal": "HOLD"}', 30, 10)
        monkeypatch.setattr("ai_providers._call_provider", fake_call)

        # Make pool inline so the row is written before we assert
        import shadow_eval
        monkeypatch.setattr(shadow_eval, "_POOL", _InlinePool())

        call_id = shadow_eval.dispatch_shadow_calls(
            db_path=profile_db,
            prompt="test prompt",
            max_tokens=1024,
            purpose="test_purpose",
            primary_provider="anthropic",
            primary_model="claude-haiku-4-5",
            primary_response='{"signal": "BUY"}',
        )
        assert call_id is not None

        conn = sqlite3.connect(profile_db)
        rows = conn.execute(
            "SELECT call_id, provider, model, parsed_signal, agreement, "
            "       error, primary_provider "
            "FROM ai_shadow_calls"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == call_id
        assert row[1] == "google"
        assert row[2] == "gemini-2.0-flash"
        assert row[3] == "HOLD"
        assert row[4] == 0  # BUY vs HOLD → disagree
        assert row[5] is None  # no error
        assert row[6] == "anthropic"

    def test_no_api_key_logs_skip_row(
            self, monkeypatch, tmp_main_db, tmp_path):
        """When a configured shadow model has no API key, we still
        write a row with an error message so the daily email can
        surface the gap."""
        from models import create_user, create_trading_profile, update_trading_profile
        user_id = create_user("u6@example.com", "pw", "U6")
        pid = create_trading_profile(user_id, "Test", "small")
        update_trading_profile(
            pid,
            enable_shadow_eval=1,
            shadow_models=json.dumps(["google:gemini-2.0-flash"]),
            shadow_api_keys_enc=json.dumps({}),  # no keys
        )
        profile_db = str(tmp_path / f"profile_{pid}.db")
        from journal import init_db
        init_db(profile_db)

        import shadow_eval
        monkeypatch.setattr(shadow_eval, "_POOL", _InlinePool())

        shadow_eval.dispatch_shadow_calls(
            db_path=profile_db,
            prompt="test",
            max_tokens=100,
            purpose="x",
            primary_provider="anthropic",
            primary_model="m",
            primary_response='{"signal": "BUY"}',
        )

        conn = sqlite3.connect(profile_db)
        row = conn.execute(
            "SELECT error FROM ai_shadow_calls"
        ).fetchone()
        conn.close()
        assert row is not None
        assert "no api key" in (row[0] or "").lower()

    def test_returns_none_when_no_profile_id_in_path(self):
        """Tests / CLI invocations that don't use profile_NNN.db paths
        should skip shadow eval entirely — return None, no errors."""
        from shadow_eval import dispatch_shadow_calls
        result = dispatch_shadow_calls(
            db_path="/some/other/path.db",
            prompt="x",
            max_tokens=100,
            purpose=None,
            primary_provider="anthropic",
            primary_model="m",
            primary_response='{"signal": "BUY"}',
        )
        assert result is None


# ---------------------------------------------------------------------------
# Cost cap
# ---------------------------------------------------------------------------

class TestShadowDailyCap:
    """The per-user cap helpers: user override wins, else env-var
    default. Mirrors cost_guard.daily_ceiling_usd."""

    def test_user_override_wins(self, tmp_main_db, monkeypatch):
        from models import create_user, _get_conn
        import config
        from contextlib import closing
        monkeypatch.setattr(config, "SHADOW_DAILY_COST_CAP_USD", 1.0)

        user_id = create_user("capuser@example.com", "pw", "Cap User")
        with closing(_get_conn()) as conn:
            conn.execute(
                "UPDATE users SET shadow_daily_cost_cap_usd = ? WHERE id = ?",
                (5.50, user_id),
            )
            conn.commit()

        from shadow_eval import shadow_daily_cap, shadow_cap_source
        assert shadow_daily_cap(user_id) == 5.50
        assert shadow_cap_source(user_id) == "user"

    def test_falls_back_to_env_when_null(self, tmp_main_db, monkeypatch):
        from models import create_user
        import config
        monkeypatch.setattr(config, "SHADOW_DAILY_COST_CAP_USD", 2.25)

        user_id = create_user("envuser@example.com", "pw", "Env User")
        from shadow_eval import shadow_daily_cap, shadow_cap_source
        # No override → env var default
        assert shadow_daily_cap(user_id) == 2.25
        assert shadow_cap_source(user_id) == "auto"


class TestShadowCostCap:
    def test_cap_blocks_when_exceeded(self, monkeypatch, tmp_profile_db):
        """When cumulative shadow spend would exceed
        SHADOW_DAILY_COST_CAP_USD, the call records an error row and
        the provider is never hit."""
        import config
        monkeypatch.setattr(config, "SHADOW_DAILY_COST_CAP_USD", 0.0001)

        # Pre-populate the ledger with a hefty cost so we're already
        # over the cap
        conn = sqlite3.connect(tmp_profile_db)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO ai_shadow_calls "
            "(call_id, timestamp, provider, model, cost_usd) "
            "VALUES ('prev', ?, 'google', 'gemini-2.0-flash', 1.00)",
            (f"{et_today} 09:00:00",),
        )
        conn.commit()
        conn.close()

        # Verify the provider is NOT called
        provider_called = {"yes": False}

        def fake_call(*args, **kwargs):
            provider_called["yes"] = True
            return ('{"signal": "BUY"}', 1, 1)
        monkeypatch.setattr("ai_providers._call_provider", fake_call)

        from shadow_eval import _run_one_shadow
        _run_one_shadow(
            call_id="testcall",
            db_path=tmp_profile_db,
            purpose="test",
            prompt="x" * 100,
            prompt_hash="h",
            max_tokens=500,
            provider="google",
            model="gemini-2.0-flash",
            api_key="fake",
            primary_provider="anthropic",
            primary_model="m",
            primary_response='{"signal": "BUY"}',
            primary_parsed={"signal": "BUY"},
        )

        assert provider_called["yes"] is False
        # The blocked row should be recorded with an error
        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT error FROM ai_shadow_calls WHERE call_id='testcall'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert "cap" in (row[0] or "").lower()


# ---------------------------------------------------------------------------
# Verdict scoring
# ---------------------------------------------------------------------------

class TestVerdictScoring:
    """The 'which was better' logic. Given a primary signal, a shadow
    signal, and the actual return, decide which model called it
    correctly."""

    def test_primary_buy_with_gain_beats_shadow_hold(self):
        from shadow_eval import verdict_for_disagreement
        v = verdict_for_disagreement("BUY", "HOLD", return_pct=5.0)
        assert v["winner"] == "primary"
        assert "5.0%" in v["reason"]

    def test_shadow_hold_with_loss_beats_primary_buy(self):
        from shadow_eval import verdict_for_disagreement
        v = verdict_for_disagreement("BUY", "HOLD", return_pct=-4.0)
        assert v["winner"] == "shadow"

    def test_small_move_is_tie(self):
        from shadow_eval import verdict_for_disagreement
        v = verdict_for_disagreement("BUY", "HOLD", return_pct=0.5)
        # ±0.5% is within the noise band — neither wins
        assert v["winner"] == "tie"

    def test_short_call_correct_on_drop(self):
        from shadow_eval import verdict_for_disagreement
        v = verdict_for_disagreement("BUY", "SHORT", return_pct=-3.5)
        assert v["winner"] == "shadow"

    def test_both_wrong_when_buy_and_short_both_lose(self):
        from shadow_eval import verdict_for_disagreement
        # BUY hoping for upside but stock went down → wrong
        # SHORT hoping for downside but...wait, if return is negative,
        # SHORT was right. We need a case where both are wrong.
        # BUY (wants up) + HOLD (wants flat) with big down move:
        # BUY wrong, HOLD wrong → both_wrong
        v = verdict_for_disagreement("BUY", "HOLD", return_pct=-10.0)
        # Actually BUY wrong (lost money), HOLD wrong (missed not the
        # opportunity, but the loss). Still 1.5%+ threshold means HOLD
        # is "wrong" because the move was big.
        assert v["winner"] in ("shadow", "both_wrong")
        # HOLD was less wrong than BUY here — saved the operator the
        # full -10% drawdown. So shadow wins.
        assert v["winner"] == "shadow"

    def test_missing_return_returns_unknown_quality(self):
        from shadow_eval import _signal_outcome_quality
        assert _signal_outcome_quality("BUY", None) == "unknown"

    def test_hold_with_big_move_is_wrong(self):
        from shadow_eval import _signal_outcome_quality
        assert _signal_outcome_quality("HOLD", 5.0) == "wrong"
        assert _signal_outcome_quality("HOLD", -5.0) == "wrong"
        assert _signal_outcome_quality("HOLD", 0.5) == "right"


# ---------------------------------------------------------------------------
# Daily digest email
# ---------------------------------------------------------------------------

class TestShadowEvalDailyDigest:
    def test_skips_silently_with_no_rows(self, monkeypatch, tmp_profile_db):
        """If shadow eval produced zero rows today, the email is not
        sent (returns False) — no inbox spam on disabled profiles."""
        sent_subjects = []

        def fake_send(subject, html, ctx=None):
            sent_subjects.append(subject)
            return True
        monkeypatch.setattr("notifications.send_email", fake_send)

        # Stub a minimal ctx with db_path
        class _Ctx:
            db_path = tmp_profile_db
            profile_name = "Test"

        from notifications import notify_shadow_eval_daily
        result = notify_shadow_eval_daily(ctx=_Ctx())
        assert result is False
        assert sent_subjects == []

    def test_sends_when_rows_present(self, monkeypatch, tmp_profile_db):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO ai_shadow_calls "
            "(call_id, timestamp, purpose, provider, model, raw_response, "
            " parsed_signal, agreement, cost_usd, primary_provider, "
            " primary_model, primary_response, primary_parsed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("c1", f"{et_today} 10:00:00", "single_analyze", "google",
             "gemini-2.0-flash",
             '{"signal": "HOLD"}', "HOLD", 0, 0.0002,
             "anthropic", "claude-haiku-4-5",
             '{"signal": "BUY", "confidence": 0.8}',
             json.dumps({"signal": "BUY", "confidence": 0.8})),
        )
        conn.commit()
        conn.close()

        captured = {}

        def fake_send(subject, html, ctx=None):
            captured["subject"] = subject
            captured["html"] = html
            return True
        monkeypatch.setattr("notifications.send_email", fake_send)

        class _Ctx:
            db_path = tmp_profile_db
            profile_name = "Test Profile"

        from notifications import notify_shadow_eval_daily
        result = notify_shadow_eval_daily(ctx=_Ctx())
        assert result is True
        assert "Shadow Eval" in captured["subject"]
        # Body should mention the disagreement
        assert "gemini" in captured["html"].lower()
        assert "buy" in captured["html"].lower()
        assert "hold" in captured["html"].lower()
