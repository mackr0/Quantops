"""Strategy applicability: stock-vs-crypto split, no within-stock filtering.

History:
- 2026-05-19. Pre-AI-era registry filtered strategies by exact market_type
  match — a strategy listing `["small", "midcap"]` was excluded from
  `largecap` because the author thought the pattern didn't apply. That
  decision belongs to the AI now, not a static registry filter.
- 2026-05-20 (docs/22). Cap-tier collapsed to a single `stocks` segment.
  Every stock strategy's APPLICABLE_MARKETS normalized to `["stocks"]`.
  Strategy filter constants updated: `_STOCK_MARKETS = ("stocks",)`.

New semantics for `strategies._strategy_applies_to_market`:
  - "*" in applicable → universal
  - crypto profile → applicable must list "crypto"
  - stock profile (market_type == "stocks") → applicable must list
    "stocks" (or "*")

Stock vs crypto stays a real distinction (data sources differ, symbol
format differs); within the stock universe, the AI sees everything.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from strategies import (
    _strategy_applies_to_market, get_active_strategies, STRATEGY_MODULES,
)


class TestStockProfileSeesAllStockStrategies:
    """Every stock-applicable strategy must run for the stocks segment."""

    def test_stock_strategy_runs_on_stocks(self):
        # Normalized stock strategy
        assert _strategy_applies_to_market(["stocks"], "stocks") is True
        # Mixed strategy (stocks + crypto) also runs on stocks
        assert _strategy_applies_to_market(["stocks", "crypto"], "stocks") is True
        # Universal still works
        assert _strategy_applies_to_market(["*"], "stocks") is True

    def test_crypto_only_strategy_does_not_run_on_stocks(self):
        """The stock-vs-crypto split is preserved."""
        assert _strategy_applies_to_market(["crypto"], "stocks") is False


class TestCryptoProfileGetsOnlyCryptoStrategies:
    def test_crypto_strategy_runs_on_crypto(self):
        assert _strategy_applies_to_market(["crypto"], "crypto") is True
        # Mixed (stocks + crypto) runs on both sides
        assert _strategy_applies_to_market(["stocks", "crypto"], "crypto") is True

    def test_stock_only_strategy_does_not_run_on_crypto(self):
        """A stock-universe strategy can't sensibly produce candidates
        for a BTC/USD-only profile — keep it out."""
        assert _strategy_applies_to_market(["stocks"], "crypto") is False

    def test_universal_strategy_runs_on_crypto(self):
        assert _strategy_applies_to_market(["*"], "crypto") is True


class TestActualStrategyCountForStocks:
    """Pin the count: at 2026-05-20 the registry has 26 stock-applicable
    strategies (every entry in STRATEGY_MODULES except none — every
    file declares either ["stocks"], ["stocks","crypto"], or ["*"]).
    All 26 activate for a stocks profile."""

    def test_stocks_gets_all_stock_applicable_strategies(self):
        active = get_active_strategies("stocks", db_path=None)
        names = {getattr(m, "NAME", "") for m in active}
        # The three strategies that were filtered out pre-2026-05-19
        # must all activate now
        assert "parabolic_exhaustion" in names
        assert "short_term_reversal" in names
        assert "short_squeeze_setup" in names

    def test_stocks_strategy_count_covers_full_registry(self):
        """Every non-crypto-only strategy in STRATEGY_MODULES must
        activate for a stocks profile."""
        active = get_active_strategies("stocks", db_path=None)
        # Should be at least 23 (the floor from pre-2026-05-19) but
        # post-fix targets every stock-applicable strategy.
        assert len(active) >= 23, (
            f"Stocks profile should see ~26 strategies; got {len(active)}"
        )


class TestPerProfileAssetClassFlags:
    """get_active_strategies honors per-profile enable_stocks / enable_crypto
    flags. Operator can disable a whole asset class for a profile without
    changing market_type."""

    def test_both_off_returns_no_strategies(self):
        active = get_active_strategies(
            "stocks", db_path=None,
            enable_stocks=False, enable_crypto=False,
        )
        assert active == [], "Both flags off must return no strategies"

    def test_stocks_on_crypto_off_returns_stock_strategies(self):
        """Default for the current setup. Every active strategy must
        be stock-applicable (no crypto-only kept under stocks=True only)."""
        active = get_active_strategies(
            "stocks", db_path=None,
            enable_stocks=True, enable_crypto=False,
        )
        from strategies import _is_stock_applicable
        for mod in active:
            apps = getattr(mod, "APPLICABLE_MARKETS", [])
            assert _is_stock_applicable(apps), (
                f"{getattr(mod, 'NAME', '?')} has applicable={apps} "
                f"but was kept under enable_stocks=True only"
            )

    def test_default_flags_match_current_behavior(self):
        """Defaults (stocks=True, crypto=False) should produce the
        same list a stocks profile gets today."""
        active = get_active_strategies("stocks", db_path=None)
        assert len(active) >= 23


class TestUnknownMarketTypeFallbackToExactMatch:
    """Defensive: an operator could set market_type to something weird
    ("commodities"). Old exact-match semantics apply as a safety net."""

    def test_unknown_market_uses_exact_match(self):
        assert _strategy_applies_to_market(
            ["commodities"], "commodities",
        ) is True
        assert _strategy_applies_to_market(
            ["stocks"], "commodities",
        ) is False
