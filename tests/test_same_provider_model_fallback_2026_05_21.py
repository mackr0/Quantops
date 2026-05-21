"""Same-provider model fallback (2026-05-21).

When Google's `gemini-2.5-flash-lite` tier started 503ing ~40-50% of
calls, the chain had no way to step up to the more-reliable `flash`
tier while keeping the cost savings on the modal call path. The fix:

  1. Circuits are keyed per-(provider, model). One throttled model
     no longer locks out other models on the same provider.
  2. `users.llm_model` is a new column letting the operator pick a
     same-provider fallback model (typically a higher tier).
  3. `_build_fallback_chain` inserts that fallback at the head of the
     chain (BEFORE cross-provider fallbacks) so the same provider's
     key gets tried with the more-reliable model before the chain
     resorts to a different provider entirely.

Tests pin:
  1. Two (provider, model) circuits are independent — one model's
     trip doesn't open the other.
  2. status() exposes composite keys.
  3. _resolve_same_provider_fallback_model returns the configured
     llm_model when llm_provider matches the primary, else None.
  4. _build_fallback_chain inserts the same-provider entry at the
     head when llm_model is configured.
  5. Behavioral: when -lite's circuit is open, the chain falls
     through to -flash on the same provider before any other
     provider is consulted.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# 1. Per-(provider, model) circuit independence
# ---------------------------------------------------------------------------

class TestCircuitsArePerProviderModel:
    def setup_method(self):
        from provider_circuit import reset
        reset()

    def test_two_models_on_same_provider_are_independent(self):
        """Tripping the circuit for google/gemini-flash-lite must NOT
        cause google/gemini-flash to read as open."""
        from provider_circuit import record_failure, is_open
        for _ in range(3):
            record_failure(
                "google", Exception("503"), "gemini-2.5-flash-lite")
        assert is_open("google", "gemini-2.5-flash-lite") is True
        assert is_open("google", "gemini-2.5-flash") is False, (
            "Same-provider fallback model got locked out by the "
            "primary model's circuit — the per-(provider, model) "
            "keying isn't working."
        )

    def test_legacy_no_model_calls_still_work(self):
        """Pre-2026-05-21 callers that pass only the provider should
        continue to function — back-compat. The bare-provider key is
        independent of any composite (provider, model) keys."""
        from provider_circuit import record_failure, is_open
        record_failure("anthropic", Exception("529"))
        record_failure("anthropic", Exception("529"))
        record_failure("anthropic", Exception("529"))
        assert is_open("anthropic") is True
        # Doesn't bleed into composite keys
        assert is_open("anthropic", "claude-haiku-4-5") is False

    def test_status_exposes_composite_keys(self):
        from provider_circuit import record_failure, status
        for _ in range(3):
            record_failure(
                "google", Exception("503"), "gemini-2.5-flash-lite")
        s = status()
        assert "google::gemini-2.5-flash-lite" in s
        assert s["google::gemini-2.5-flash-lite"]["state"] == "open"

    def test_seconds_until_close_takes_model(self):
        from provider_circuit import (
            record_failure, seconds_until_close,
        )
        for _ in range(3):
            record_failure(
                "google", Exception("503"), "gemini-2.5-flash-lite")
        remaining = seconds_until_close(
            "google", "gemini-2.5-flash-lite")
        assert remaining is not None
        assert 0 < remaining <= 300
        # Different model: no cool-down
        assert seconds_until_close(
            "google", "gemini-2.5-flash") is None


# ---------------------------------------------------------------------------
# 2. _resolve_same_provider_fallback_model lookup
# ---------------------------------------------------------------------------

class TestResolveSameProviderFallback:
    """Helper reads users.llm_provider + users.llm_model from the
    master DB to decide whether to inject a same-provider fallback."""

    @pytest.fixture
    def master_db(self, tmp_path, monkeypatch):
        """Build a minimal master DB with a users table the helper
        can read."""
        db_path = tmp_path / "quantopsai.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                llm_provider TEXT,
                llm_model TEXT
            );
        """)
        conn.commit()
        conn.close()
        monkeypatch.chdir(tmp_path)
        return db_path

    def _set_user(self, db_path, provider, model):
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM users")
        conn.execute(
            "INSERT INTO users (id, llm_provider, llm_model) "
            "VALUES (1, ?, ?)",
            (provider, model),
        )
        conn.commit()
        conn.close()

    def test_returns_model_when_provider_matches(self, master_db):
        from ai_providers import _resolve_same_provider_fallback_model
        self._set_user(master_db, "google", "gemini-2.5-flash")
        out = _resolve_same_provider_fallback_model(
            "google", "gemini-2.5-flash-lite")
        assert out == "gemini-2.5-flash"

    def test_returns_none_when_provider_mismatch(self, master_db):
        """User has llm_provider='openai' but caller's primary is
        'google' — don't inject openai's fallback model into google."""
        from ai_providers import _resolve_same_provider_fallback_model
        self._set_user(master_db, "openai", "gpt-4o")
        out = _resolve_same_provider_fallback_model(
            "google", "gemini-2.5-flash-lite")
        assert out is None

    def test_returns_none_when_no_model_set(self, master_db):
        from ai_providers import _resolve_same_provider_fallback_model
        self._set_user(master_db, "google", None)
        out = _resolve_same_provider_fallback_model(
            "google", "gemini-2.5-flash-lite")
        assert out is None

    def test_returns_none_when_fallback_equals_primary(self, master_db):
        """No-op: operator picked the same model for primary and
        fallback. Don't add a redundant chain entry."""
        from ai_providers import _resolve_same_provider_fallback_model
        self._set_user(master_db, "google", "gemini-2.5-flash-lite")
        out = _resolve_same_provider_fallback_model(
            "google", "gemini-2.5-flash-lite")
        assert out is None

    def test_returns_none_when_db_missing(self, tmp_path, monkeypatch):
        """No master DB → helper falls through cleanly. Don't break
        ai_providers in test environments without a populated DB."""
        monkeypatch.chdir(tmp_path)
        from ai_providers import _resolve_same_provider_fallback_model
        out = _resolve_same_provider_fallback_model(
            "google", "gemini-2.5-flash-lite")
        assert out is None


# ---------------------------------------------------------------------------
# 3. _build_fallback_chain inserts the same-provider entry
# ---------------------------------------------------------------------------

class TestFallbackChainInsertion:
    @pytest.fixture
    def master_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "quantopsai.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                llm_provider TEXT,
                llm_model TEXT
            );
            INSERT INTO users (id, llm_provider, llm_model)
                VALUES (1, 'google', 'gemini-2.5-flash');
        """)
        conn.commit()
        conn.close()
        monkeypatch.chdir(tmp_path)
        return db_path

    def test_same_provider_entry_at_chain_head(self, master_db, monkeypatch):
        """When the user has llm_provider=google + llm_model=flash,
        and the primary call is google/flash-lite, the chain's first
        entry should be (google, primary_key, flash) — before any
        cross-provider entries."""
        import config
        monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "g-test")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)

        from ai_providers import _build_fallback_chain
        chain = _build_fallback_chain("google", "gemini-2.5-flash-lite")
        assert len(chain) >= 1
        first = chain[0]
        assert first[0] == "google", (
            f"Same-provider fallback should be FIRST in chain; "
            f"got {first[0]} at position 0"
        )
        assert first[1] == "g-test", (
            "Same-provider fallback should reuse the provider's "
            "primary API key — not require a new credential."
        )
        assert first[2] == "gemini-2.5-flash"

    def test_chain_falls_through_to_cross_provider_when_no_same_provider_model(
        self, tmp_path, monkeypatch,
    ):
        """When user hasn't configured llm_model (None), the chain
        should look exactly like pre-refactor."""
        import config
        # Clean DB — no users row
        db_path = tmp_path / "quantopsai.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, "
            "llm_provider TEXT, llm_model TEXT)"
        )
        conn.commit()
        conn.close()
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "g-test")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)

        from ai_providers import _build_fallback_chain
        chain = _build_fallback_chain("google", "gemini-2.5-flash-lite")
        # Should have openai (cross-provider), but NO google entry
        # (we're the primary; no same-provider fallback configured)
        for provider, _, _ in chain:
            assert provider != "google", (
                "Without an llm_model configured, the chain should "
                "not include a same-provider google entry."
            )


# ---------------------------------------------------------------------------
# 4. Behavioral: same-provider fallback fires before cross-provider
# ---------------------------------------------------------------------------

class TestEndToEndSameProviderFallback:
    """Wire-level: with -lite primary and -flash fallback configured,
    a -lite 503 storm should route to -flash (same provider, same key)
    instead of to OpenAI/Anthropic."""

    @pytest.fixture
    def master_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "quantopsai.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                llm_provider TEXT,
                llm_model TEXT
            );
            INSERT INTO users (id, llm_provider, llm_model)
                VALUES (1, 'google', 'gemini-2.5-flash');
        """)
        conn.commit()
        conn.close()
        monkeypatch.chdir(tmp_path)
        return db_path

    def test_lite_503_routes_to_flash_same_provider(
        self, master_db, monkeypatch,
    ):
        """Primary call -lite 503s on all retries. Same-provider
        fallback -flash is configured. Chain should call _call_google
        with model=flash AND succeed without ever calling openai/
        anthropic."""
        import ai_providers
        from provider_circuit import reset
        reset()
        monkeypatch.setattr(ai_providers, "_RETRY_DELAYS_SECONDS",
                            (0.0, 0.0))

        import config
        monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "g-test")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)

        call_log = []
        def fake_google(prompt, model, key, max_tokens):
            call_log.append(model)
            if model == "gemini-2.5-flash-lite":
                raise Exception("503 high demand")
            # -flash succeeds
            return ("flash-response", 100, 50)

        with patch("ai_providers._call_google",
                   side_effect=fake_google), \
             patch("ai_providers._call_openai") as openai_mock, \
             patch("ai_providers._call_anthropic") as anthropic_mock:
            from ai_providers import call_ai
            out = call_ai(
                "hi", provider="google",
                model="gemini-2.5-flash-lite",
                api_key="g-test",
            )

        assert "flash-response" in out
        # Google was called for both -lite (3 retry attempts) and -flash (1)
        assert call_log.count("gemini-2.5-flash-lite") == 3, (
            f"Expected 3 -lite attempts (1 + 2 retries); got "
            f"{call_log.count('gemini-2.5-flash-lite')}"
        )
        assert call_log.count("gemini-2.5-flash") == 1
        # Cross-provider fallbacks should NOT have fired
        openai_mock.assert_not_called()
        anthropic_mock.assert_not_called()

    def test_flash_also_503s_then_cross_provider_fallback(
        self, master_db, monkeypatch,
    ):
        """When BOTH -lite and -flash 503, the chain should escape
        to cross-provider (OpenAI)."""
        import ai_providers
        from provider_circuit import reset
        reset()
        monkeypatch.setattr(ai_providers, "_RETRY_DELAYS_SECONDS",
                            (0.0, 0.0))

        import config
        monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "g-test")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)

        with patch("ai_providers._call_google",
                   side_effect=Exception("503 high demand")), \
             patch("ai_providers._call_openai",
                   return_value=("from-openai", 0, 0)) as openai_mock:
            from ai_providers import call_ai
            out = call_ai(
                "hi", provider="google",
                model="gemini-2.5-flash-lite",
                api_key="g-test",
            )
        assert "from-openai" in out
        openai_mock.assert_called_once()
