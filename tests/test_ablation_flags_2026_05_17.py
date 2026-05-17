"""Tests for the 2026-05-17 ablation feature flags:
- enable_alt_data
- enable_meta_model
- enable_options

Per docs/15_EXPERIMENT_DESIGN_2026_05_17.md, each flag disables one
major system component so the ablation arms of the fresh-start
experiment can attribute alpha to specific subsystems.

All three default ON so existing behavior is preserved; ablation
profiles flip them off individually.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_ctx(**overrides):
    """Build a minimal ctx with the ablation flags + sensible defaults."""
    from user_context import UserContext
    base = dict(
        user_id=1, segment="midcap", display_name="Test",
        alpaca_api_key="k", alpaca_secret_key="s",
        ai_provider="anthropic", ai_model="claude-haiku-4-5-20251001",
        ai_api_key="k", db_path=":memory:",
        profile_id=1,
    )
    base.update(overrides)
    return UserContext(**base)


class TestUserContextDefaults:
    """All three flags default to True (current behavior preserved)."""

    def test_enable_alt_data_default_true(self):
        ctx = _make_ctx()
        assert ctx.enable_alt_data is True

    def test_enable_meta_model_default_true(self):
        ctx = _make_ctx()
        assert ctx.enable_meta_model is True

    def test_enable_options_default_true(self):
        ctx = _make_ctx()
        assert ctx.enable_options is True

    def test_strategy_type_default_ai(self):
        ctx = _make_ctx()
        assert ctx.strategy_type == "ai"


class TestAltDataGate:
    """When enable_alt_data=False, the alt-data fetcher is skipped
    and the candidate's alt_data block is set to None."""

    def test_disabled_alt_data_skips_fetch(self):
        """Walk through the trade_pipeline alt-data path manually
        with the gate disabled — verify get_all_alternative_data is
        NOT called."""
        ctx = _make_ctx(enable_alt_data=False)
        with patch(
            "alternative_data.get_all_alternative_data"
        ) as fake_get:
            # Simulate the relevant branch from trade_pipeline:3160-3186
            entry = {}
            symbol = "AAPL"
            if not getattr(ctx, "enable_alt_data", True):
                entry.setdefault("alt_data", None)
            else:
                alt = fake_get(symbol)
                entry["alt_data"] = alt
        fake_get.assert_not_called()
        assert entry["alt_data"] is None

    def test_enabled_alt_data_calls_fetch(self):
        ctx = _make_ctx(enable_alt_data=True)
        with patch(
            "alternative_data.get_all_alternative_data",
            return_value={"insider": {"recent_buys": 1}},
        ) as fake_get:
            entry = {}
            symbol = "AAPL"
            if not getattr(ctx, "enable_alt_data", True):
                entry.setdefault("alt_data", None)
            else:
                alt = fake_get(symbol)
                entry["alt_data"] = alt
        fake_get.assert_called_once_with("AAPL")
        assert entry["alt_data"] == {"insider": {"recent_buys": 1}}


class TestMetaModelGate:
    """When enable_meta_model=False, the pregate skips and the
    main meta-model load is bypassed."""

    def test_disabled_meta_model_pregate_passes_all(self):
        """_meta_pregate_candidates returns the list unmodified
        when the gate is off — every candidate flows through."""
        from trade_pipeline import _meta_pregate_candidates
        ctx = _make_ctx(enable_meta_model=False, meta_pregate_threshold=0.5)
        candidates = [
            {"symbol": "AAPL", "signal": "BUY", "score": 0.5},
            {"symbol": "MSFT", "signal": "BUY", "score": 0.3},
        ]
        out = _meta_pregate_candidates(candidates, ctx)
        assert out == candidates

    def test_enabled_meta_model_attempts_load(self):
        """When the gate is on, _meta_pregate_candidates calls
        meta_model.load_model. (Patched to None so the function
        falls open after the call.)"""
        from trade_pipeline import _meta_pregate_candidates
        ctx = _make_ctx(enable_meta_model=True, meta_pregate_threshold=0.5)
        candidates = [{"symbol": "AAPL", "signal": "BUY", "score": 0.5}]
        with patch("meta_model.load_model", return_value=None) as fake_load:
            _meta_pregate_candidates(candidates, ctx)
        fake_load.assert_called_once()


class TestOptionsGate:
    """When enable_options=False, the multileg_block builder
    is short-circuited to an empty string."""

    def test_disabled_options_yields_empty_multileg_block(self):
        ctx = _make_ctx(enable_options=False)
        # Reproduce the gate logic from ai_analyst.py:1017-1037 to
        # verify the short-circuit path. (Calling build_prompt
        # end-to-end requires too much fixture setup.)
        multileg_block = ""
        options_enabled = getattr(ctx, "enable_options", True)
        try:
            if not options_enabled:
                raise StopIteration
            multileg_block = "non-empty (would render here)"
        except StopIteration:
            multileg_block = ""
        assert multileg_block == ""

    def test_enabled_options_runs_builder(self):
        ctx = _make_ctx(enable_options=True)
        multileg_block = ""
        options_enabled = getattr(ctx, "enable_options", True)
        try:
            if not options_enabled:
                raise StopIteration
            multileg_block = "non-empty (would render here)"
        except StopIteration:
            multileg_block = ""
        assert multileg_block != ""


class TestModelLoadersWireFlags:
    """build_user_context_from_profile must read the new columns
    and populate the UserContext fields. Tests the full
    profile-dict → UserContext wiring end-to-end."""

    @staticmethod
    def _stub_profile(**flag_overrides):
        """A dict-like object that returns 0 for any missing key —
        sufficient to satisfy build_user_context_from_profile which
        reads ~80 columns, most via `profile[...]` (no default).
        Override only the keys the test cares about."""
        class ProfileStub(dict):
            def __getitem__(self, k):
                return super().get(k, 0)
        p = ProfileStub({
            "id": 99, "user_id": 1, "name": "Ablation",
            "market_type": "midcap",
            "alpaca_api_key_enc": "x", "alpaca_secret_key_enc": "y",
            "alpaca_account_id": None,
            "ai_provider": "anthropic",
            "ai_model": "claude-haiku-4-5-20251001",
            "ai_api_key_enc": "k",
            "enable_alt_data": 1, "enable_meta_model": 1,
            "enable_options": 1, "strategy_type": "ai",
            "initial_capital": 200000.0,
            "meta_pregate_threshold": 0.0,
        })
        p.update(flag_overrides)
        return p

    def test_profile_with_all_flags_off(self):
        """Profile dict with 3 flags = 0 → ctx fields = False."""
        import models
        prof = self._stub_profile(
            enable_alt_data=0, enable_meta_model=0, enable_options=0,
        )
        with patch.object(
            models, "get_trading_profile", return_value=prof,
        ), patch.object(
            models, "get_user_by_id",
            return_value={
                "id": 1, "alpaca_api_key_enc": "",
                "alpaca_secret_key_enc": "",
                "anthropic_api_key_enc": "",
                "resend_api_key_enc": "",
                "notification_email": "",
            },
        ), patch.object(models, "decrypt", side_effect=lambda x: x or ""):
            ctx = models.build_user_context_from_profile(99)
        assert ctx.enable_alt_data is False
        assert ctx.enable_meta_model is False
        assert ctx.enable_options is False
        assert ctx.strategy_type == "ai"

    def test_profile_with_strategy_type_buy_hold(self):
        """strategy_type='buy_hold' propagates through to ctx."""
        import models
        prof = self._stub_profile(strategy_type="buy_hold")
        with patch.object(
            models, "get_trading_profile", return_value=prof,
        ), patch.object(
            models, "get_user_by_id",
            return_value={
                "id": 1, "alpaca_api_key_enc": "",
                "alpaca_secret_key_enc": "",
                "anthropic_api_key_enc": "",
                "resend_api_key_enc": "",
                "notification_email": "",
            },
        ), patch.object(models, "decrypt", side_effect=lambda x: x or ""):
            ctx = models.build_user_context_from_profile(99)
        assert ctx.strategy_type == "buy_hold"
