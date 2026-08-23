"""Vendor-fair structured output + model registry refresh + shadow scope.

docs/25 (Model Selection & Learning Plan) items 1.1, 1.2, decision D5.

Before 2026-08-23 only the Anthropic path enforced a JSON schema
(forced tool_use); OpenAI and Gemini got a plain prompt and a text
parser, so any model comparison measured parsers as much as models.
Now every vendor is held to the same schema through its own native
structured-output mode, via ONE call path (call_ai) that carries cost
capping, retries, failover, the cost ledger, and shadow dispatch for
all of them identically.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import ai_pricing  # noqa: E402
import ai_providers as ap  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["BUY", "HOLD"]},
                    "confidence": {"type": "number",
                                   "minimum": 0, "maximum": 100},
                    "reasoning": {"type": "string"},
                },
                "required": ["symbol", "verdict", "confidence"],
            },
        },
    },
    "required": ["verdicts"],
}


# ---------------------------------------------------------------------------
# 1.1 — registry and pricing
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_every_registered_model_is_priced(self):
        """A model the picker offers must never fall to FALLBACK_PRICING
        — that would misreport spend by up to 100x."""
        for provider, info in ap.PROVIDERS.items():
            for mid in info["models"]:
                assert ai_pricing.price_for(mid) is not None, (
                    f"{provider}:{mid} has no ai_pricing entry")

    def test_defaults_are_registered_and_current(self):
        for provider, mid in ap._DEFAULT_MODELS.items():
            assert mid in ap.PROVIDERS[provider]["models"]
            assert "legacy" not in ap.PROVIDERS[provider]["models"][mid]

    def test_prices_carry_a_verification_date(self):
        d = _dt.date.fromisoformat(ai_pricing.PRICES_VERIFIED_ON)
        assert d >= _dt.date(2026, 8, 23)

    def test_experiment_arms_are_registered(self):
        """The Phase-1 arms (docs/25 D1) must be selectable."""
        assert "gpt-5.6-luna" in ap.PROVIDERS["openai"]["models"]
        assert "gemini-3.5-flash-lite" in ap.PROVIDERS["google"]["models"]
        assert "gemini-3.7-flash" in ap.PROVIDERS["google"]["models"]

    def test_opus_4_6_price_corrected(self):
        p = ai_pricing.price_for("claude-opus-4-6")
        assert (p["input"], p["output"]) == (5.00, 25.00)


# ---------------------------------------------------------------------------
# 1.2 — one structured contract on every vendor
# ---------------------------------------------------------------------------

class TestStrictSchemaRewrite:
    def test_openai_strict_form(self):
        strict = ap._openai_strict_schema(SCHEMA)
        item = strict["properties"]["verdicts"]["items"]
        assert strict["additionalProperties"] is False
        assert item["additionalProperties"] is False
        assert sorted(item["required"]) == sorted(item["properties"])
        assert "minimum" not in item["properties"]["confidence"]
        assert "maximum" not in item["properties"]["confidence"]
        # input untouched
        assert SCHEMA["properties"]["verdicts"]["items"]["required"] == [
            "symbol", "verdict", "confidence"]


class _FakeResp:
    def __init__(self, text):
        self.choices = [types.SimpleNamespace(
            message=types.SimpleNamespace(content=text))]
        self.usage = types.SimpleNamespace(prompt_tokens=10,
                                           completion_tokens=5)


class TestOpenAIPath:
    def test_schema_becomes_strict_json_schema_response_format(self, monkeypatch):
        captured = {}

        class _Completions:
            def create(self, **kw):
                captured.update(kw)
                return _FakeResp('{"verdicts": []}')

        class _Client:
            def __init__(self, api_key=None):
                self.chat = types.SimpleNamespace(completions=_Completions())

        monkeypatch.setattr("openai.OpenAI", _Client)
        text, _i, _o = ap._call_openai("p", "gpt-5.6-luna", "k", 512,
                                       schema=SCHEMA)
        assert json.loads(text) == {"verdicts": []}
        rf = captured["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["schema"]["additionalProperties"] is False
        # GPT-5.x rejects the legacy max_tokens parameter.
        assert "max_tokens" not in captured
        assert captured["max_completion_tokens"] == 512
        assert captured["reasoning_effort"] == "low"

    def test_legacy_model_gets_no_reasoning_effort(self, monkeypatch):
        captured = {}

        class _Completions:
            def create(self, **kw):
                captured.update(kw)
                return _FakeResp("ok")

        class _Client:
            def __init__(self, api_key=None):
                self.chat = types.SimpleNamespace(completions=_Completions())

        monkeypatch.setattr("openai.OpenAI", _Client)
        ap._call_openai("p", "gpt-4.1-nano", "k", 64)
        assert "reasoning_effort" not in captured
        assert "response_format" not in captured

    def test_reasoning_effort_rejection_retries_without_it(self, monkeypatch):
        calls = []

        class _Completions:
            def create(self, **kw):
                calls.append(dict(kw))
                if "reasoning_effort" in kw:
                    raise RuntimeError("Unsupported parameter: reasoning_effort")
                return _FakeResp("ok")

        class _Client:
            def __init__(self, api_key=None):
                self.chat = types.SimpleNamespace(completions=_Completions())

        monkeypatch.setattr("openai.OpenAI", _Client)
        text, _i, _o = ap._call_openai("p", "gpt-5-nano", "k", 64)
        assert text == "ok"
        assert len(calls) == 2 and "reasoning_effort" not in calls[1]


class TestGooglePath:
    def test_schema_goes_to_response_json_schema(self, monkeypatch):
        captured = {}

        class _Models:
            def generate_content(self, *, model, contents, config):
                captured["model"] = model
                captured["config"] = config
                return types.SimpleNamespace(
                    text='{"verdicts": []}',
                    usage_metadata=types.SimpleNamespace(
                        prompt_token_count=1, candidates_token_count=1,
                        cached_content_token_count=0))

        class _Client:
            def __init__(self, api_key=None):
                self.models = _Models()

        fake_genai = types.SimpleNamespace(Client=_Client)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        import google as _g
        monkeypatch.setattr(_g, "genai", fake_genai, raising=False)
        text, _i, _o, _c = ap._call_google("p", "gemini-3.7-flash", "k", 256,
                                           schema=SCHEMA)
        assert json.loads(text) == {"verdicts": []}
        assert captured["config"]["response_json_schema"] == SCHEMA
        assert captured["config"]["response_mime_type"] == "application/json"


class TestAnthropicPath:
    def test_schema_forces_tool_use_and_returns_json_text(self, monkeypatch):
        captured = {}

        class _Messages:
            def create(self, **kw):
                captured.update(kw)
                block = types.SimpleNamespace(type="tool_use",
                                              input={"verdicts": []})
                return types.SimpleNamespace(
                    content=[block],
                    usage=types.SimpleNamespace(input_tokens=1,
                                                output_tokens=1))

        class _Client:
            def __init__(self, api_key=None):
                self.messages = _Messages()

        monkeypatch.setattr("anthropic.Anthropic", _Client)
        text, _i, _o = ap._call_anthropic("p", "claude-sonnet-5", "k", 256,
                                          schema=SCHEMA)
        assert json.loads(text) == {"verdicts": []}
        assert captured["tool_choice"] == {"type": "tool", "name": "emit"}
        assert captured["tools"][0]["input_schema"] == SCHEMA


class TestOneCallPath:
    def test_call_ai_structured_routes_every_provider_through_call_ai(
            self, monkeypatch):
        """The ledger, cost cap, failover and shadow dispatch live in
        call_ai — every vendor must pass through it WITH the schema."""
        seen = []

        def fake_call_ai(prompt, **kw):
            seen.append(kw)
            return '{"verdicts": [{"symbol": "A", "verdict": "BUY", "confidence": 1}]}'

        monkeypatch.setattr(ap, "call_ai", fake_call_ai)
        for provider in ("anthropic", "openai", "google"):
            out = ap.call_ai_structured("p", SCHEMA, provider=provider,
                                        model="m", api_key="k",
                                        purpose="ensemble:x")
            assert out["verdicts"][0]["symbol"] == "A"
        assert len(seen) == 3
        assert all(kw["schema"] == SCHEMA for kw in seen)
        assert all(kw["purpose"] == "ensemble:x" for kw in seen)

    def test_non_object_or_garbage_returns_none(self, monkeypatch):
        monkeypatch.setattr(ap, "call_ai", lambda *a, **k: "not json")
        assert ap.call_ai_structured("p", SCHEMA, provider="openai",
                                     model="m", api_key="k") is None
        monkeypatch.setattr(ap, "call_ai", lambda *a, **k: "[1,2]")
        assert ap.call_ai_structured("p", SCHEMA, provider="openai",
                                     model="m", api_key="k") is None

    def test_call_ai_threads_schema_to_provider_and_shadows(self, monkeypatch):
        got = {}

        def fake_provider(provider, prompt, model, key, max_tokens, **kw):
            got["provider_kw"] = kw
            return ('{"verdicts": []}', 1, 1, 0)

        def fake_dispatch(**kw):
            got["shadow_kw"] = kw
            return None

        monkeypatch.setattr(ap, "_call_provider", fake_provider)
        monkeypatch.setattr(ap, "_enforce_cost_cap", lambda *a, **k: None)
        import shadow_eval
        monkeypatch.setattr(shadow_eval, "dispatch_shadow_calls", fake_dispatch)
        ap.call_ai("p", provider="openai", model="gpt-5.6-luna",
                   api_key="k", schema=SCHEMA, purpose="ensemble:x")
        assert got["provider_kw"] == {"schema": SCHEMA}
        assert got["shadow_kw"]["schema"] == SCHEMA

    def test_call_ai_without_schema_keeps_legacy_call_shape(self, monkeypatch):
        """Existing 5-positional patchers of _call_provider must keep
        working — the schema kwarg is only passed when set."""
        def fake_provider(provider, prompt, model, key, max_tokens):
            return ("ok", 1, 1, 0)

        monkeypatch.setattr(ap, "_call_provider", fake_provider)
        monkeypatch.setattr(ap, "_enforce_cost_cap", lambda *a, **k: None)
        assert ap.call_ai("p", provider="openai", model="m", api_key="k") == "ok"


class TestEnsembleHasNoVendorFork:
    def test_no_anthropic_only_structured_branch(self):
        import inspect
        import ensemble
        src = inspect.getsource(ensemble)
        assert 'use_tools = (ai_provider == "anthropic")' not in src
        assert "spec.parse_response(raw)" not in src, (
            "the plain-prompt text-parser path must not return")


# ---------------------------------------------------------------------------
# D5 — shadow purpose scope
# ---------------------------------------------------------------------------

class TestShadowPurposeScope:
    def test_default_scope_is_specialists_only(self, monkeypatch):
        import config
        import shadow_eval
        monkeypatch.setattr(config, "SHADOW_PURPOSES", ("ensemble:",))
        assert shadow_eval.purpose_is_shadowed("ensemble:risk_assessor")
        assert not shadow_eval.purpose_is_shadowed("batch_select")
        assert not shadow_eval.purpose_is_shadowed(None)

    def test_empty_scope_shadows_everything(self, monkeypatch):
        import config
        import shadow_eval
        monkeypatch.setattr(config, "SHADOW_PURPOSES", ())
        assert shadow_eval.purpose_is_shadowed("batch_select")
        assert shadow_eval.purpose_is_shadowed(None)

    def test_dispatch_skips_out_of_scope_purpose_before_any_work(
            self, monkeypatch):
        import config
        import shadow_eval
        monkeypatch.setattr(config, "SHADOW_PURPOSES", ("ensemble:",))
        monkeypatch.setattr(
            shadow_eval, "_load_shadow_config",
            lambda pid: (_ for _ in ()).throw(AssertionError("must not load")))
        out = shadow_eval.dispatch_shadow_calls(
            db_path="/x/quantopsai_profile_999.db", prompt="p",
            max_tokens=10, purpose="batch_select",
            primary_provider="google", primary_model="m",
            primary_response="{}")
        assert out is None

    def test_config_default_is_specialists(self):
        import config
        assert config.SHADOW_PURPOSES == ("ensemble:",)
