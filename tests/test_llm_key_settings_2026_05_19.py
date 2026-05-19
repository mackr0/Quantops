"""Fallback LLM key — provider-agnostic settings contract.

2026-05-19. Renamed Settings-page "Anthropic API Key" field to
"Fallback LLM Key" + paired provider dropdown. The underlying
column (`users.anthropic_api_key_enc`) name is preserved for now
(rename is a future refactor) but semantically it stores any
provider's key per `users.llm_provider`.

The key is used by CLI helpers and non-pipeline AI calls
(`main.py ai-analyze`, `news_sentiment.analyze_sentiment`, etc.)
that don't have per-profile context.

Tests pin:
  - Migration adds `users.llm_provider` column on existing DBs
  - `update_user_credentials(llm_provider=..., llm_key=...)` writes
    both columns
  - `get_user_llm_settings(user_id)` returns the configured pair
  - Back-compat alias: `update_user_credentials(anthropic_key=...)`
    still works (existing callers don't break)
  - `news_sentiment.analyze_sentiment` honors ctx, falls back to
    user_id, and skips cleanly with no key — no silent .env
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def fresh_master(tmp_path, monkeypatch):
    """Build a fresh master DB and point config.DB_PATH at it."""
    master = str(tmp_path / "quantopsai.db")
    import config
    monkeypatch.setattr(config, "DB_PATH", master)
    from models import init_user_db
    init_user_db(master)
    # Create a user row
    conn = sqlite3.connect(master)
    conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("u@test", "x"),
    )
    conn.commit()
    uid = conn.execute("SELECT id FROM users").fetchone()[0]
    conn.close()
    return {"master": master, "user_id": uid}


class TestMigrationAddsLLMProviderColumn:
    def test_fresh_db_has_llm_provider_column(self, fresh_master):
        conn = sqlite3.connect(fresh_master["master"])
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(users)"
        ).fetchall()]
        conn.close()
        assert "llm_provider" in cols, (
            "users.llm_provider column missing — migration didn't run"
        )

    def test_default_value_is_anthropic(self, fresh_master):
        """Default preserves behavior for users whose stored key
        was originally an Anthropic key (the legacy assumption)."""
        conn = sqlite3.connect(fresh_master["master"])
        row = conn.execute(
            "SELECT llm_provider FROM users WHERE id=?",
            (fresh_master["user_id"],),
        ).fetchone()
        conn.close()
        assert row[0] == "anthropic"

    def test_migration_is_idempotent(self, fresh_master):
        """Re-running init_user_db must not error on an existing column."""
        from models import init_user_db
        init_user_db(fresh_master["master"])
        init_user_db(fresh_master["master"])  # second time
        # If it raised, the test would fail here


class TestUpdateUserCredentialsWritesLLMProvider:
    def test_writes_both_provider_and_key(self, fresh_master):
        from models import update_user_credentials, get_user_llm_settings
        update_user_credentials(
            fresh_master["user_id"],
            alpaca_key="ak", alpaca_secret="as",
            llm_key="google-test-key",
            llm_provider="google",
            notification_email="x@y",
        )
        result = get_user_llm_settings(fresh_master["user_id"])
        assert result["provider"] == "google"
        assert result["api_key"] == "google-test-key"

    def test_back_compat_anthropic_key_alias(self, fresh_master):
        """Old callers that still pass `anthropic_key=` must keep
        working (the key gets stored under the LLM column)."""
        from models import update_user_credentials, get_user_llm_settings
        update_user_credentials(
            fresh_master["user_id"],
            alpaca_key="ak", alpaca_secret="as",
            anthropic_key="sk-ant-legacy",  # legacy keyword
            notification_email="x@y",
        )
        result = get_user_llm_settings(fresh_master["user_id"])
        # Legacy keyword preserves provider (defaults to anthropic)
        # and stores the key correctly
        assert result["api_key"] == "sk-ant-legacy"

    def test_omitting_provider_preserves_existing_value(self, fresh_master):
        """Update without an llm_provider arg must not overwrite the
        existing provider — only changing the key shouldn't reset
        the provider to default."""
        from models import update_user_credentials, get_user_llm_settings
        update_user_credentials(
            fresh_master["user_id"], llm_key="k1", llm_provider="google",
            alpaca_key="", alpaca_secret="", notification_email="",
            resend_key="",
        )
        update_user_credentials(
            fresh_master["user_id"], llm_key="k2",
            # llm_provider omitted intentionally
            alpaca_key="", alpaca_secret="", notification_email="",
            resend_key="",
        )
        result = get_user_llm_settings(fresh_master["user_id"])
        assert result["provider"] == "google", (
            "Provider should be preserved when only key is updated"
        )
        assert result["api_key"] == "k2"


class TestGetUserLLMSettings:
    def test_returns_provider_and_decrypted_key(self, fresh_master):
        from models import update_user_credentials, get_user_llm_settings
        update_user_credentials(
            fresh_master["user_id"], alpaca_key="", alpaca_secret="",
            llm_key="my-key", llm_provider="openai",
            notification_email="", resend_key="",
        )
        result = get_user_llm_settings(fresh_master["user_id"])
        assert result == {"provider": "openai", "api_key": "my-key"}

    def test_missing_user_returns_empty_defaults(self, fresh_master):
        from models import get_user_llm_settings
        result = get_user_llm_settings(99999)  # nonexistent
        assert result["provider"] == "anthropic"
        assert result["api_key"] == ""


# ---------------------------------------------------------------------------
# news_sentiment.analyze_sentiment — must no longer be hardcoded to
# Anthropic; must honor ctx OR user_id OR skip cleanly.
# ---------------------------------------------------------------------------

class TestNewsSentimentRespectsLLMSettings:
    def test_no_news_returns_neutral_without_calling_ai(self):
        from news_sentiment import analyze_sentiment
        result = analyze_sentiment("AAPL", [])  # no news
        assert result["overall_score"] == 0.0
        assert result["label"] == "NEUTRAL"

    def test_no_ctx_no_user_id_returns_neutral_with_error(self):
        """The exact "no silent .env fallback" contract."""
        from news_sentiment import analyze_sentiment
        result = analyze_sentiment(
            "AAPL",
            [{"source": "S", "headline": "h", "summary": ""}],
            ctx=None, user_id=None,
        )
        assert result["overall_score"] == 0.0
        assert "error" in result
        assert "no llm key" in result["error"].lower()

    def test_ctx_provided_uses_ctx_provider_and_key(self):
        from news_sentiment import analyze_sentiment
        ctx = SimpleNamespace(
            ai_provider="google",
            ai_model="gemini-2.5-flash-lite",
            ai_api_key="g-key",
            db_path=None,
        )
        with patch("ai_providers.call_ai") as mock_call_ai:
            mock_call_ai.return_value = (
                '{"overall_score": 0.5, "label": "BULLISH", "items": []}'
            )
            analyze_sentiment(
                "AAPL",
                [{"source": "S", "headline": "h", "summary": ""}],
                ctx=ctx,
            )
            kwargs = mock_call_ai.call_args.kwargs
            assert kwargs["provider"] == "google"
            assert kwargs["api_key"] == "g-key"
            assert kwargs["model"] == "gemini-2.5-flash-lite"

    def test_user_id_falls_back_to_user_llm_settings(self, fresh_master):
        """When ctx is None but user_id is given, settings come from
        get_user_llm_settings (the Settings page Fallback LLM Key)."""
        from models import update_user_credentials
        update_user_credentials(
            fresh_master["user_id"],
            alpaca_key="", alpaca_secret="",
            llm_key="fallback-google-key",
            llm_provider="google",
            notification_email="", resend_key="",
        )
        from news_sentiment import analyze_sentiment
        with patch("ai_providers.call_ai") as mock_call_ai:
            mock_call_ai.return_value = (
                '{"overall_score": 0.0, "label": "NEUTRAL", "items": []}'
            )
            analyze_sentiment(
                "AAPL",
                [{"source": "S", "headline": "h", "summary": ""}],
                user_id=fresh_master["user_id"],
            )
            kwargs = mock_call_ai.call_args.kwargs
            assert kwargs["provider"] == "google"
            assert kwargs["api_key"] == "fallback-google-key"
