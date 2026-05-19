"""Strategy applicability: stock-vs-crypto split, no within-stock filtering.

2026-05-19. The pre-AI-era registry filtered strategies by exact
market_type match — a strategy listing `["small", "midcap"]`
was excluded from `largecap` because the author thought the
pattern didn't apply. That decision belongs to the AI now, not
a static registry filter.

New semantics for `strategies._strategy_applies_to_market`:
  - "*" in applicable → universal
  - crypto profile → applicable must list "crypto"
  - stock profile (largecap/midcap/small/micro) → applicable must
    list ANY stock market

Stock vs crypto stays a real distinction (data sources differ);
within stock, the AI sees everything.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from strategies import (
    _strategy_applies_to_market, get_active_strategies, STRATEGY_MODULES,
)


class TestStockProfilesSeeAllStockStrategies:
    """The 2026-05-19 regression class: every stock-applicable
    strategy must run for every stock market_type, regardless of
    which specific stock markets its APPLICABLE_MARKETS lists."""

    @pytest.mark.parametrize("market_type",
                              ["largecap", "midcap", "small", "micro"])
    def test_strategy_listing_some_stock_markets_runs_on_every_stock_market(
        self, market_type,
    ):
        # A strategy that lists only ["small", "midcap"] historically
        # was excluded from largecap. New behavior: it should apply.
        assert _strategy_applies_to_market(
            ["small", "midcap"], market_type,
        ) is True
        # Single-stock-market lists also apply to any stock profile
        assert _strategy_applies_to_market(
            ["midcap"], market_type,
        ) is True
        # Universal still works
        assert _strategy_applies_to_market(["*"], market_type) is True

    def test_strategy_listing_only_crypto_does_not_run_on_stocks(self):
        """The stock-vs-crypto split is preserved. A hypothetical
        crypto-only strategy (none exist today) would not run on
        stock profiles."""
        assert _strategy_applies_to_market(["crypto"], "largecap") is False
        assert _strategy_applies_to_market(["crypto"], "small") is False


class TestCryptoProfilesGetOnlyCryptoStrategies:
    def test_strategy_with_crypto_in_list_runs(self):
        assert _strategy_applies_to_market(["crypto"], "crypto") is True
        # Mixed (crypto + stocks): runs on both sides
        assert _strategy_applies_to_market(
            ["small", "midcap", "largecap", "crypto"], "crypto",
        ) is True

    def test_strategy_listing_only_stock_markets_does_not_run_on_crypto(self):
        """A stock-universe strategy can't sensibly produce candidates
        for a BTC/USD-only profile — keep it out."""
        assert _strategy_applies_to_market(
            ["small", "midcap", "largecap"], "crypto",
        ) is False
        assert _strategy_applies_to_market(["largecap"], "crypto") is False

    def test_universal_strategy_runs_on_crypto(self):
        assert _strategy_applies_to_market(["*"], "crypto") is True


class TestActualStrategyCountForLargecap:
    """Pin the count: at the time of this commit, the registry has
    26 stock-applicable strategies (every entry in STRATEGY_MODULES
    except none — all list at least one stock market). All 26 must
    now activate for a largecap profile."""

    def test_largecap_gets_all_stock_applicable_strategies(self):
        active = get_active_strategies("largecap", db_path=None)
        # Must include the 3 that were previously walled off
        names = {getattr(m, "NAME", "") for m in active}
        assert "parabolic_exhaustion" in names, (
            "parabolic_exhaustion now applies to largecap "
            "(was filtered out pre-2026-05-19)"
        )
        assert "short_term_reversal" in names, (
            "short_term_reversal now applies to largecap"
        )
        assert "short_squeeze_setup" in names, (
            "short_squeeze_setup now applies to largecap"
        )

    def test_largecap_strategy_count_at_least_as_high_as_midcap(self):
        """The previous filtering produced largecap=23, midcap=26.
        Post-fix, largecap should equal midcap (both get all
        stock-applicable strategies)."""
        n_large = len(get_active_strategies("largecap", db_path=None))
        n_mid = len(get_active_strategies("midcap", db_path=None))
        assert n_large == n_mid, (
            f"Largecap should now match midcap (was 23 vs 26 before "
            f"the fix); got largecap={n_large} midcap={n_mid}"
        )


class TestPerProfileAssetClassFlags:
    """2026-05-19 — get_active_strategies honors per-profile
    enable_stocks / enable_crypto flags. Operator can disable a
    whole asset class for a profile without changing market_type."""

    def test_stocks_off_returns_no_stock_strategies(self):
        active = get_active_strategies(
            "largecap", db_path=None,
            enable_stocks=False, enable_crypto=False,
        )
        assert active == [], (
            "Both flags off must return no strategies"
        )

    def test_stocks_on_crypto_off_returns_stock_strategies(self):
        """Default for the user's current setup. All 26 stock
        strategies run; no crypto-only strategies (there are none
        in the registry today, but if there were, they'd be skipped)."""
        active = get_active_strategies(
            "largecap", db_path=None,
            enable_stocks=True, enable_crypto=False,
        )
        # Every active strategy must list at least one stock market
        # or "*" — none should be crypto-only
        from strategies import _is_stock_applicable
        for mod in active:
            apps = getattr(mod, "APPLICABLE_MARKETS", [])
            assert _is_stock_applicable(apps), (
                f"{getattr(mod, 'NAME', '?')} has applicable={apps} "
                f"but was kept under enable_stocks=True only"
            )

    def test_default_flags_match_current_behavior(self):
        """Defaults (stocks=True, crypto=False) should produce the
        same list a stock profile gets today."""
        active = get_active_strategies("largecap", db_path=None)
        # All 26 stock-applicable strategies
        assert len(active) >= 23, (
            f"Default flags should preserve current largecap "
            f"coverage; got {len(active)}"
        )


class TestUnknownMarketTypeFallbackToExactMatch:
    """Defensive: an operator could set market_type to something
    weird ("commodities"). Old exact-match semantics apply."""

    def test_unknown_market_uses_exact_match(self):
        assert _strategy_applies_to_market(
            ["commodities"], "commodities",
        ) is True
        assert _strategy_applies_to_market(
            ["largecap"], "commodities",
        ) is False
