"""Model picker shows available models + their cost (2026-06-24).

The Settings AI-model picker was a hardcoded list with no cost and stale
entries: it offered the deprecated `gemini-2.0-flash` (live generateContent
404s) and the dated `claude-sonnet-4-20250514` / `claude-opus-4-20250514`
(gone from Anthropic's /v1/models, and unpriced → blank cost), while omitting
the cheap `gemini-2.5-flash` standard tier. The operator (cost-constrained,
trying to move off Claude Haiku) couldn't pick the cheap option or see prices.

This pins:
  1. Every model OFFERED in the picker has a price (no blank-cost / silent
     FALLBACK_PRICING entries) — `PROVIDERS ⊆ ai_pricing.PRICING`.
  2. Known-deprecated ids are not offered.
  3. get_providers() annotates each label with its per-1M cost.
  4. get_model_catalog() returns cost + a live `available` flag (None when it
     can't be checked — never raises).
  5. The /api/provider-models endpoint returns the cost+availability catalog
     for every provider (happy path through the Flask client), and the test's
     provider coverage equals the full PROVIDERS set (no picker left untested).
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# Ids we have positively confirmed are dead and must never be offered.
_DEPRECATED_IDS = {
    "gemini-2.0-flash",            # Google deprecated — generateContent 404s
    "claude-sonnet-4-20250514",    # gone from Anthropic /v1/models
    "claude-opus-4-20250514",      # gone from Anthropic /v1/models
}


# ---------------------------------------------------------------------------
# 1. Catalog ⊆ pricing, no deprecated ids
# ---------------------------------------------------------------------------

class TestCatalogIsPricedAndCurrent:
    def test_every_offered_model_has_a_price(self):
        """A model in the picker with no price shows blank cost AND silently
        bills at FALLBACK_PRICING. Every offered model must be priced."""
        from ai_providers import PROVIDERS
        from ai_pricing import PRICING
        unpriced = []
        for prov, info in PROVIDERS.items():
            for mid in info.get("models", {}):
                if mid not in PRICING:
                    unpriced.append(f"{prov}/{mid}")
        assert not unpriced, (
            "Picker offers models with no price in ai_pricing.PRICING "
            "(blank cost + silent FALLBACK_PRICING):\n  "
            + "\n  ".join(unpriced)
            + "\nAdd a price or drop the model from PROVIDERS."
        )

    def test_no_deprecated_models_offered(self):
        from ai_providers import PROVIDERS
        offered = {mid for info in PROVIDERS.values()
                   for mid in info.get("models", {})}
        bad = offered & _DEPRECATED_IDS
        assert not bad, f"Picker offers known-deprecated models: {sorted(bad)}"

    def test_cheap_gemini_flash_is_offered(self):
        """Regression: the cheap standard tier the operator needs must be
        pickable (it was missing)."""
        from ai_providers import PROVIDERS
        assert "gemini-2.5-flash" in PROVIDERS["google"]["models"]


# ---------------------------------------------------------------------------
# 2. Cost labels
# ---------------------------------------------------------------------------

class TestCostLabels:
    def test_cost_label_format(self):
        from ai_pricing import cost_label
        # gemini-2.5-flash = $0.35 / $0.70
        assert cost_label("gemini-2.5-flash") == "$0.35 in / $0.7 out per 1M"
        assert cost_label("claude-haiku-4-5-20251001") == "$1 in / $5 out per 1M"

    def test_cost_label_none_for_unknown(self):
        from ai_pricing import cost_label, price_for
        assert cost_label("totally-made-up-model") is None
        assert price_for("totally-made-up-model") is None

    def test_get_providers_annotates_every_priced_label_with_cost(self):
        from ai_providers import get_providers
        from ai_pricing import cost_label
        provs = get_providers()
        for pkey, info in provs.items():
            for mid, label in info["models"].items():
                cl = cost_label(mid)
                assert cl and cl in label, (
                    f"{pkey}/{mid} label missing cost: {label!r}"
                )

    def test_get_providers_without_cost_is_raw(self):
        from ai_providers import get_providers, PROVIDERS
        assert get_providers(with_cost=False) is PROVIDERS


# ---------------------------------------------------------------------------
# 3. Live availability catalog
# ---------------------------------------------------------------------------

class TestModelCatalogAvailability:
    def test_catalog_available_none_when_no_key(self, monkeypatch):
        """No working key → availability unknown (None), never an exception,
        and cost still present."""
        import ai_providers
        monkeypatch.setattr(ai_providers, "_working_key_for_provider",
                            lambda provider: "")
        ai_providers._AVAIL_CACHE.clear()
        cat = ai_providers.get_model_catalog("google")
        assert cat, "catalog should still list models without a key"
        for m in cat:
            assert m["available"] is None
            assert m["cost_label"]  # cost independent of availability

    def test_catalog_marks_availability_from_live_ids(self, monkeypatch):
        import ai_providers
        monkeypatch.setattr(ai_providers, "_working_key_for_provider",
                            lambda provider: "fake-key")
        monkeypatch.setattr(
            ai_providers, "_live_model_ids",
            lambda provider, key: {"gemini-2.5-flash", "gemini-2.5-flash-lite"},
        )
        ai_providers._AVAIL_CACHE.clear()
        cat = {m["id"]: m for m in ai_providers.get_model_catalog("google")}
        assert cat["gemini-2.5-flash"]["available"] is True
        assert cat["gemini-2.5-flash-lite"]["available"] is True
        # gemini-2.5-pro is in the catalog but not in the live set
        assert cat["gemini-2.5-pro"]["available"] is False

    def test_availability_degrades_on_list_error(self, monkeypatch):
        """A list-models failure must yield None (unknown), not raise."""
        import ai_providers

        def boom(provider, key):
            raise RuntimeError("network down")

        monkeypatch.setattr(ai_providers, "_working_key_for_provider",
                            lambda provider: "fake-key")
        monkeypatch.setattr(ai_providers, "_live_model_ids", boom)
        ai_providers._AVAIL_CACHE.clear()
        assert ai_providers.available_model_ids("google", force=True) is None


# ---------------------------------------------------------------------------
# 4. Endpoint happy-path — every provider's picker is reachable
# ---------------------------------------------------------------------------

@pytest.fixture
def logged_in_client(tmp_main_db, monkeypatch):
    import config
    config.DB_PATH = tmp_main_db
    # Keep the endpoint network-free + deterministic in tests.
    import ai_providers
    monkeypatch.setattr(ai_providers, "available_model_ids",
                        lambda provider, force=False: None)
    from app import create_app
    from models import create_user
    create_user("picker@test.com", "password123", "Picker", is_admin=True)
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        client.post("/login", data={"email": "picker@test.com",
                                    "password": "password123"},
                    follow_redirects=True)
        yield client


# Static coverage set — MUST equal the full provider list (guards against a
# new provider being added to PROVIDERS without a picker smoke test).
_COVERED_PROVIDERS = ["anthropic", "openai", "google", "deepseek"]


def test_endpoint_coverage_matches_all_providers():
    from ai_providers import PROVIDERS
    assert set(_COVERED_PROVIDERS) == set(PROVIDERS), (
        "Providers changed — add the new provider to _COVERED_PROVIDERS so "
        "its picker keeps a smoke test (and to the Settings dropdown)."
    )


class TestProviderModelsEndpoint:
    @pytest.mark.parametrize("provider", _COVERED_PROVIDERS)
    def test_endpoint_returns_cost_catalog(self, logged_in_client, provider):
        resp = logged_in_client.get(
            "/api/provider-models?provider=" + provider)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["provider"] == provider
        assert data["models"], f"no models returned for {provider}"
        for m in data["models"]:
            assert "id" in m and "label" in m
            assert m["cost_label"], f"{provider}/{m['id']} missing cost_label"
            # availability stubbed to None in this fixture
            assert m["available"] is None

    def test_google_endpoint_offers_cheap_flash(self, logged_in_client):
        resp = logged_in_client.get("/api/provider-models?provider=google")
        ids = {m["id"] for m in resp.get_json()["models"]}
        assert "gemini-2.5-flash" in ids
        assert "gemini-2.0-flash" not in ids

    def test_endpoint_requires_provider(self, logged_in_client):
        resp = logged_in_client.get("/api/provider-models")
        assert resp.status_code == 400
