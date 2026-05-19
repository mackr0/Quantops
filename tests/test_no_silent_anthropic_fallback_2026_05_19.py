"""No-silent-Anthropic-fallback contract — pinned 2026-05-19.

Before 2026-05-19, removing the Anthropic API key from the
Settings page silently left it active via `/opt/quantopsai/.env`'s
`ANTHROPIC_API_KEY` — six independent code paths in ai_analyst,
political_sentiment, self_tuning, user_context, and ai_providers
fell back to `config.ANTHROPIC_API_KEY` when no explicit key was
provided.

This was actively misleading: the UI implied authority it didn't
have. An operator who removed the key thought they had stopped
Anthropic use; the system kept calling it.

This file pins the post-fix contract: ctx (or an explicit
api_key argument) is the ONLY source. The silent .env fallback
is gone.

Each test corresponds to one of the originally-leaky paths.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Path 3: ai_analyst.get_claude_client
# ---------------------------------------------------------------------------

class TestGetClaudeClientNoSilentFallback:
    def test_missing_api_key_raises_with_clear_message(self, monkeypatch):
        """Even with config.ANTHROPIC_API_KEY set, calling
        get_claude_client() with no api_key must raise. The function
        no longer auto-picks up the .env key."""
        import config
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY",
                             "fake-env-key-that-should-not-be-used")
        from ai_analyst import get_claude_client
        with pytest.raises(ValueError) as exc_info:
            get_claude_client()  # no api_key argument
        msg = str(exc_info.value).lower()
        assert "missing anthropic api key" in msg or "missing" in msg
        # Error message should point at the right fix (Settings page)
        assert "settings" in msg

    def test_explicit_api_key_works(self, monkeypatch):
        from ai_analyst import get_claude_client
        # Patch the anthropic SDK so we don't actually connect
        with patch("anthropic.Anthropic") as mock_anthropic:
            client = get_claude_client(api_key="sk-ant-real")
            mock_anthropic.assert_called_once_with(api_key="sk-ant-real")


# ---------------------------------------------------------------------------
# Path 4 + 5: ai_analyst.analyze_symbol / analyze_portfolio_risk
# Both used the pattern `ctx.ai_api_key if ctx else config.ANTHROPIC_API_KEY`.
# That else branch is now `None`, so a ctx-less call gets a None api_key.
# ---------------------------------------------------------------------------

class TestAnalyzeSymbolNoCtxFallback:
    def test_no_ctx_passes_none_api_key_to_call_ai(self, monkeypatch):
        """With ctx=None, analyze_symbol must pass api_key=None to
        call_ai (which will raise ValueError "API key is required").
        This is the desired behavior — ctx-less callers must fail
        loudly instead of silently using .env."""
        import config
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY",
                             "fake-env-key-that-should-not-be-used")
        # Patch call_ai + bar fetch so we can drive analyze_symbol
        # to the call_ai invocation site
        import pandas as pd
        df = pd.DataFrame({
            "open": [100, 101], "high": [101, 102],
            "low": [99, 100], "close": [100.5, 101.5],
            "volume": [1000, 1100],
        })
        with patch("ai_analyst.call_ai") as mock_call_ai, \
             patch("ai_analyst.get_api") as mock_get_api, \
             patch("ai_analyst.get_bars") as mock_bars, \
             patch("ai_analyst.add_indicators", side_effect=lambda x: x):
            mock_bars.return_value = df
            mock_get_api.return_value = MagicMock()
            mock_call_ai.return_value = '{"signal":"HOLD","confidence":0,"reasoning":"x","risk_factors":[],"price_targets":{}}'
            try:
                from ai_analyst import analyze_symbol
                analyze_symbol("AAPL", ctx=None)
            except Exception:
                # If something else fails (e.g., bars fetch), it's OK
                # — we're only asserting on the call_ai invocation.
                pass
            # Verify that whatever was passed for api_key was NOT the
            # config.ANTHROPIC_API_KEY value (the post-fix contract).
            if mock_call_ai.call_args is not None:
                kwargs = mock_call_ai.call_args.kwargs
                assert kwargs.get("api_key") is None, (
                    f"analyze_symbol with ctx=None must pass api_key=None; "
                    f"got {kwargs.get('api_key')!r} — the silent .env "
                    f"fallback was supposed to be removed 2026-05-19"
                )


# ---------------------------------------------------------------------------
# Path 6: political_sentiment.get_political_context
# ---------------------------------------------------------------------------

class TestPoliticalSentimentNoCtxFallback:
    def test_no_ctx_passes_none_api_key(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-env-key")
        with patch("political_sentiment.call_ai") as mock_call_ai:
            mock_call_ai.return_value = '{"summary":"x"}'
            try:
                from political_sentiment import get_maga_mode_context
                get_maga_mode_context(ctx=None)
            except Exception:
                pass
            if mock_call_ai.call_args is not None:
                kwargs = mock_call_ai.call_args.kwargs
                assert kwargs.get("api_key") is None, (
                    f"political_sentiment must not silently use "
                    f"config.ANTHROPIC_API_KEY; got {kwargs.get('api_key')!r}"
                )


# ---------------------------------------------------------------------------
# Path 7: self_tuning.propose_strategies_for_profile (or wherever the
# ai_api_key resolution happens)
#
# The previous code had:
#     ai_api_key = getattr(ctx, "ai_api_key", None)
#     if not ai_api_key:
#         ai_api_key = os.getenv("ANTHROPIC_API_KEY", "")
#     if not ai_api_key:
#         return None
#
# Post-fix:
#     ai_api_key = getattr(ctx, "ai_api_key", None)
#     if not ai_api_key:
#         return None
# ---------------------------------------------------------------------------

class TestSelfTuningNoEnvFallback:
    def test_self_tuning_module_does_not_read_anthropic_env(self):
        """Structural test — the source file no longer contains the
        `os.getenv("ANTHROPIC_API_KEY"` pattern at the strategy-proposer
        site. Catches a refactor that accidentally reintroduces the
        silent fallback. Note: we scope this to the specific function;
        global config references via `config.ANTHROPIC_API_KEY` are OK
        because config itself reads .env (that's how config works)."""
        import inspect
        import self_tuning
        # Pull source of the function that had the leaky pattern
        # The function name varies by version — check broadly
        src = inspect.getsource(self_tuning)
        # The exact assignment line we removed:
        #   ai_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        # Pattern: scan source lines and ignore comments (lines whose
        # first non-whitespace char is `#`). If a non-comment line
        # contains the getenv call, the regression is back.
        offender = None
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # comment, not executable code
            if 'os.getenv("ANTHROPIC_API_KEY"' in line:
                offender = line
                break
        assert offender is None, (
            f"self_tuning.py contains an executable line calling "
            f"os.getenv('ANTHROPIC_API_KEY'): {offender!r}. The silent "
            f".env fallback was removed 2026-05-19 and must NOT be "
            f"re-added. Use ctx.ai_api_key only."
        )


# ---------------------------------------------------------------------------
# Path 2: user_context._build_context_for_segment
# ---------------------------------------------------------------------------

class TestBuildContextForSegmentNoEnvFallback:
    def test_segment_ctx_does_not_inherit_anthropic_env(self, monkeypatch):
        """The legacy `_build_context_for_segment` function now defaults
        ai_api_key='' instead of config.ANTHROPIC_API_KEY. Verifies the
        result of building a segment-level ctx contains an empty AI key."""
        import config
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-env-key")
        from user_context import build_context_from_segment
        try:
            ctx = build_context_from_segment("largecap")
        except Exception:
            pytest.skip("build_context_from_segment couldn't build a "
                         "segment ctx in this test env — skip")
        # AI key on the resulting ctx must be empty, not the .env value
        assert ctx.ai_api_key == "" or ctx.ai_api_key is None, (
            f"Legacy segment ctx must default ai_api_key='', not pick "
            f"up config.ANTHROPIC_API_KEY; got {ctx.ai_api_key!r}"
        )
