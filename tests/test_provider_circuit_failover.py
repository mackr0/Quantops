"""AI provider circuit-breaker + failover tests.

Doomsday gap closed: when Anthropic returns 529 / 5xx / timeout
repeatedly, the circuit breaker opens and traffic auto-routes to
OpenAI (or Google) instead of stalling every profile's scan.

Tests cover both the standalone circuit semantics in
`provider_circuit.py` and the integrated failover in
`ai_providers.call_ai`.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Standalone circuit semantics
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_circuits():
    from provider_circuit import reset
    reset()
    yield
    reset()


def test_circuit_starts_closed():
    from provider_circuit import is_open
    assert is_open("anthropic") is False


def test_one_failure_does_not_open_circuit():
    from provider_circuit import record_failure, is_open
    record_failure("anthropic", Exception("503 service unavailable"))
    assert is_open("anthropic") is False


def test_three_failures_open_circuit():
    from provider_circuit import record_failure, is_open
    for _ in range(3):
        record_failure("anthropic", Exception("529 overloaded"))
    assert is_open("anthropic") is True


def test_success_resets_failure_count():
    from provider_circuit import record_failure, record_success, is_open
    record_failure("anthropic", Exception("529"))
    record_failure("anthropic", Exception("529"))
    record_success("anthropic")
    record_failure("anthropic", Exception("529"))
    record_failure("anthropic", Exception("529"))
    # Only 2 in a row → circuit stays closed
    assert is_open("anthropic") is False


def test_circuits_are_independent_per_provider():
    from provider_circuit import record_failure, is_open
    for _ in range(3):
        record_failure("anthropic", Exception("529"))
    assert is_open("anthropic") is True
    assert is_open("openai") is False


def test_status_reports_state():
    from provider_circuit import record_failure, status
    for _ in range(3):
        record_failure("anthropic", Exception("529"))
    s = status()
    assert s["anthropic"]["state"] == "open"
    assert s["anthropic"]["consecutive_failures"] >= 3


# ---------------------------------------------------------------------------
# Failover in call_ai
# ---------------------------------------------------------------------------

def test_call_ai_failover_falls_back_when_anthropic_521s():
    """Primary 529s three times → circuit opens. Fallback (openai)
    succeeds → call returns the openai response."""
    from provider_circuit import record_failure
    import config

    # Pre-open the anthropic circuit
    for _ in range(3):
        record_failure("anthropic", Exception("529 overloaded"))

    fake_response = ("openai-response-text", 100, 50)
    with patch.object(config, "OPENAI_API_KEY", "sk-test"), \
         patch.object(config, "GEMINI_API_KEY", None), \
         patch.object(config, "ANTHROPIC_API_KEY", "anthropic-test"), \
         patch("ai_providers._call_openai", return_value=fake_response) as openai_mock, \
         patch("ai_providers._call_anthropic") as anthropic_mock:
        from ai_providers import call_ai
        out = call_ai(
            "hello", provider="anthropic", model="claude-haiku-4-5",
            api_key="anthropic-test",
        )
    assert "openai-response-text" in out
    # Anthropic should NOT have been called — circuit was open
    anthropic_mock.assert_not_called()
    openai_mock.assert_called_once()


def test_call_ai_propagates_through_fallback_on_inline_failure():
    """Anthropic was healthy; first call raises a transient error
    → circuit records failure but doesn't open yet (only 1 failure).
    The same call should immediately try the fallback."""
    import config
    fake_openai_response = ("from-openai", 50, 10)
    with patch.object(config, "OPENAI_API_KEY", "sk-test"), \
         patch.object(config, "GEMINI_API_KEY", None), \
         patch("ai_providers._call_anthropic",
               side_effect=Exception("529 overloaded")), \
         patch("ai_providers._call_openai",
               return_value=fake_openai_response):
        from ai_providers import call_ai
        out = call_ai(
            "hello", provider="anthropic", model="claude",
            api_key="anthropic-test",
        )
    assert "from-openai" in out


def test_non_transient_errors_do_not_trip_circuit():
    """A 401 auth error should NOT open the circuit (we'd just stay
    failed forever). It should propagate to the caller as-is."""
    import config
    from provider_circuit import is_open
    with patch.object(config, "OPENAI_API_KEY", None), \
         patch.object(config, "GEMINI_API_KEY", None), \
         patch("ai_providers._call_anthropic",
               side_effect=Exception("401 invalid api key")):
        from ai_providers import call_ai
        with pytest.raises(Exception, match="401"):
            call_ai("hello", provider="anthropic", model="claude",
                    api_key="bad-key")
    assert is_open("anthropic") is False


def test_call_ai_no_fallback_configured_raises_when_primary_open():
    """If only anthropic is configured AND its circuit is open, the
    call should raise (no fallback to use)."""
    from provider_circuit import record_failure
    import config
    for _ in range(3):
        record_failure("anthropic", Exception("529"))

    with patch.object(config, "OPENAI_API_KEY", None), \
         patch.object(config, "GEMINI_API_KEY", None):
        from ai_providers import call_ai
        with pytest.raises(RuntimeError, match="exhausted"):
            call_ai("hello", provider="anthropic", model="claude",
                    api_key="anthropic-test")


def test_successful_primary_does_not_invoke_fallback():
    import config
    fake = ("primary-ok", 10, 5)
    with patch.object(config, "OPENAI_API_KEY", "sk-test"), \
         patch.object(config, "GEMINI_API_KEY", None), \
         patch("ai_providers._call_anthropic", return_value=fake) as a_mock, \
         patch("ai_providers._call_openai") as o_mock:
        from ai_providers import call_ai
        out = call_ai("hello", provider="anthropic", model="claude",
                      api_key="anthropic-test")
    assert "primary-ok" in out
    a_mock.assert_called_once()
    o_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Anthropic-fallback suppression — 2026-05-19 incident.
#
# Gemini 503s opened the google circuit; every subsequent AI call
# silently fell back to Anthropic at ~$0.01-$0.02/call. The user's
# profiles are deliberately configured for Gemini (cheap). The
# fallback chain must NOT secretly spend on Claude when the primary
# was a non-Anthropic provider, unless explicitly opted in via
# AI_ALLOW_ANTHROPIC_FALLBACK=1.
# ---------------------------------------------------------------------------

class TestAnthropicFallbackSuppression:
    """The fallback chain must never silently route a Gemini-or-OpenAI
    primary to paid Anthropic. This is policy, not heuristic — the
    behavioral test pins it at the chain-builder level so any future
    refactor that re-introduces the silent path breaks here."""

    def test_chain_excludes_anthropic_by_default_when_primary_is_google(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "anthropic-test")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "google-test")
        monkeypatch.delenv("AI_ALLOW_ANTHROPIC_FALLBACK", raising=False)
        from ai_providers import _build_fallback_chain
        chain = _build_fallback_chain("google")
        providers = [p for p, _, _ in chain]
        assert "anthropic" not in providers, (
            f"anthropic must be excluded from google-primary fallback chain "
            f"by default; got {providers}"
        )
        # OpenAI is fine — it's not the paid escalation we're guarding
        assert "openai" in providers

    def test_chain_excludes_anthropic_by_default_when_primary_is_openai(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "anthropic-test")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "google-test")
        monkeypatch.delenv("AI_ALLOW_ANTHROPIC_FALLBACK", raising=False)
        from ai_providers import _build_fallback_chain
        chain = _build_fallback_chain("openai")
        providers = [p for p, _, _ in chain]
        assert "anthropic" not in providers

    def test_chain_includes_anthropic_when_opt_in_flag_set(self, monkeypatch):
        """Operator can opt back in via env var if they explicitly
        want paid fallback. Documented escape hatch."""
        import config
        monkeypatch.setattr(config, "OPENAI_API_KEY", None)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "anthropic-test")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "google-test")
        monkeypatch.setenv("AI_ALLOW_ANTHROPIC_FALLBACK", "1")
        from ai_providers import _build_fallback_chain
        chain = _build_fallback_chain("google")
        providers = [p for p, _, _ in chain]
        assert "anthropic" in providers

    def test_chain_includes_anthropic_when_primary_is_anthropic(self, monkeypatch):
        """Gate only affects FALLBACK to anthropic. When anthropic IS
        the primary, the chain-builder filters it as 'primary == fallback'
        anyway, so this test pins the obvious case — no regression where
        my gate accidentally affects primary calls."""
        import config
        monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "anthropic-test")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "google-test")
        monkeypatch.delenv("AI_ALLOW_ANTHROPIC_FALLBACK", raising=False)
        from ai_providers import _build_fallback_chain
        chain = _build_fallback_chain("anthropic")
        providers = [p for p, _, _ in chain]
        # primary is anthropic → it's NOT in the chain (it's already
        # at position 0 in `attempts`). Other providers fall through.
        assert "anthropic" not in providers
        assert "openai" in providers
        assert "google" in providers

    def test_end_to_end_gemini_outage_does_not_invoke_anthropic(
        self, monkeypatch,
    ):
        """Behavioral guarantee: when primary=google fails transient,
        anthropic._call_anthropic must NOT be called even though an
        Anthropic key is present in config. The cycle should raise
        RuntimeError 'exhausted' instead (no eligible fallback)."""
        from provider_circuit import reset, record_failure
        reset()
        # Pre-open the google circuit so call_ai immediately moves to
        # the fallback chain
        for _ in range(3):
            record_failure("google", Exception("503 high demand"))

        import config
        monkeypatch.setattr(config, "OPENAI_API_KEY", None)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "anthropic-test")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "google-test")
        monkeypatch.delenv("AI_ALLOW_ANTHROPIC_FALLBACK", raising=False)

        with patch("ai_providers._call_anthropic") as anthropic_mock, \
             patch("ai_providers._call_google") as google_mock:
            from ai_providers import call_ai
            with pytest.raises(RuntimeError, match="exhausted"):
                call_ai("hello", provider="google",
                        model="gemini-2.5-flash-lite",
                        api_key="google-test")
        # Critical assertion: even though Anthropic is configured AND
        # would normally be the only available fallback, the gate
        # blocks the call from reaching it.
        anthropic_mock.assert_not_called()
        google_mock.assert_not_called()  # google circuit is open
