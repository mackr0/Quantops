"""Integration tests for the 11 Tier-2 + Tier-3 alt-data sources
(2026-05-17 expansion).

Doesn't try to test every external API call (those require network).
Instead pins the CONTRACT:
  - get_all_alternative_data return dict contains every expected key
  - each new function returns {} (not None, not exception) for
    unmapped tickers
  - sector-mapped helpers return data only for tickers in their map
  - the unified macro cache includes the new macro sub-keys
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


class TestTier2CorporateGracefulSkip:
    """Tickers outside the sector mapping return {} — quiet."""

    def test_github_returns_empty_for_non_tech(self):
        from altdata_tier2_corporate import get_github_activity
        assert get_github_activity("XYZ_UNKNOWN") == {}

    def test_fda_returns_empty_for_non_pharma(self):
        from altdata_tier2_corporate import get_fda_inspections
        assert get_fda_inspections("XYZ_UNKNOWN") == {}

    def test_nhtsa_returns_empty_for_non_auto(self):
        from altdata_tier2_corporate import get_nhtsa_recalls
        assert get_nhtsa_recalls("XYZ_UNKNOWN") == {}

    def test_sam_returns_empty_for_non_defense(self):
        from altdata_tier2_corporate import get_sam_gov_contracts
        assert get_sam_gov_contracts("XYZ_UNKNOWN") == {}


class TestTier3GracefulNoData:
    """Sources with placeholders or unmapped tickers return safe shapes."""

    def test_uspto_returns_empty_for_unmapped(self):
        from altdata_tier3 import get_uspto_patents
        assert get_uspto_patents("XYZ_UNKNOWN") == {}

    def test_epa_osha_returns_empty_for_unmapped(self):
        # AAPL is not in the heavy-industrial mapping → {} (no
        # noisy partial-name match attempted).
        from altdata_tier3 import get_epa_osha_violations
        assert get_epa_osha_violations("AAPL") == {}

    def test_epa_osha_returns_combined_shape_for_mapped(self):
        # XOM is mapped to "EXXON" — must return both EPA aggregate
        # keys AND OSHA aggregate keys (OSHA is now reachable via
        # the Cloudflare Worker proxy in osha_proxy/, gated by
        # OSHA_PROXY_URL + OSHA_PROXY_TOKEN env vars). Network /
        # proxy may be unavailable in CI; we only assert shape.
        from altdata_tier3 import get_epa_osha_violations
        r = get_epa_osha_violations("XOM")
        for k in ("epa_current_violator_count",
                  "epa_total_penalties_usd",
                  "osha_inspections_5y", "osha_violations_5y"):
            assert k in r, f"missing key {k}"

    def test_job_postings_returns_empty_for_unmapped(self):
        from altdata_tier3 import get_job_postings_count
        assert get_job_postings_count("XYZ_UNKNOWN") == {}


class TestUnifiedDictContract:
    """The big one — every new key appears in get_all_alternative_data."""

    EXPECTED_NEW_KEYS = {
        # Tier 1 (already shipped)
        "recent_8k_events", "activist_13dg", "macro",
        # Tier 2 corporate
        "github_activity", "fda_inspections", "nhtsa_recalls",
        "sam_gov_contracts",
        # Tier 3 (8 — FAA dropped 2026-05-17)
        "risk_factor_diff", "epa_osha_violations",
        "bls_jobless_claims", "wikipedia_edits", "uspto_patents",
        "job_postings", "insider_track_records",
        "star_manager_holdings",
    }

    def test_every_new_key_in_dict(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        from alternative_data import get_all_alternative_data
        # Stub every fetcher to return {} so we just check key presence
        original = [
            "get_insider_activity", "get_short_interest", "get_fundamentals",
            "get_options_unusual", "get_intraday_patterns",
            "get_finra_short_volume", "get_insider_cluster",
            "get_analyst_estimates", "get_insider_earnings_signal",
            "get_dark_pool_volume", "get_earnings_surprise",
            "get_congressional_recent", "get_13f_institutional",
            "get_biotech_milestones", "get_stocktwits_sentiment",
            "get_google_trends_signal", "get_wikipedia_pageviews_signal",
            "get_app_store_ranking",
        ]
        tier_stubs = [
            ("sec_8k_broad", "get_recent_8k_events"),
            ("sec_13dg_activist", "get_recent_13dg_activist"),
            ("altdata_tier2_corporate", "get_github_activity"),
            ("altdata_tier2_corporate", "get_fda_inspections"),
            ("altdata_tier2_corporate", "get_nhtsa_recalls"),
            ("altdata_tier2_corporate", "get_sam_gov_contracts"),
            ("altdata_tier3", "get_risk_factor_diff"),
            ("altdata_tier3", "get_epa_osha_violations"),
            ("altdata_tier3", "get_bls_jobless_claims"),
            ("altdata_tier3", "get_wikipedia_edits"),
            ("altdata_tier3", "get_uspto_patents"),
            ("altdata_tier3", "get_job_postings_count"),
            ("altdata_tier3", "get_insider_track_records"),
            ("altdata_tier3", "get_star_manager_holdings"),
        ]
        with ExitStack() as st:
            for name in original:
                st.enter_context(
                    patch(f"alternative_data.{name}", return_value={})
                )
            st.enter_context(
                patch("macro_data.get_all_macro_data", return_value={})
            )
            for mod, fn in tier_stubs:
                st.enter_context(patch(f"{mod}.{fn}", return_value={}))
            result = get_all_alternative_data("AAPL")
        missing = self.EXPECTED_NEW_KEYS - set(result.keys())
        assert not missing, f"Missing keys in unified dict: {missing}"
        assert len(result) >= 30


class TestMacroAggregator:
    """The 4 new macro sources (USDA, EIA, CFTC, sector_flow_diff)
    appear under macro_data.get_all_macro_data return."""

    def test_macro_aggregator_includes_tier2_keys(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        from macro_data import get_all_macro_data
        with ExitStack() as st:
            for fn in ("get_yield_curve", "get_etf_flows", "get_cboe_skew",
                       "get_fred_macro", "get_sector_momentum_ranking",
                       "get_market_gex_aggregate", "get_cross_asset_vol"):
                st.enter_context(
                    patch(f"macro_data.{fn}", return_value={})
                )
            for fn in ("get_usda_crop_reports",
                       "get_eia_energy_inventories",
                       "get_cftc_cot_positioning",
                       "get_sector_flow_differentials"):
                st.enter_context(
                    patch(f"altdata_tier2_macro.{fn}", return_value={})
                )
            result = get_all_macro_data()
        for k in ("usda_crops", "eia_energy", "cftc_cot",
                  "sector_flow_diff", "cross_asset_vol"):
            assert k in result, f"macro aggregator missing {k}"
