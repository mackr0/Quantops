"""Strategy registry — central discovery for all alpha strategies.

Phases 6 + 7 of the Quant Fund Evolution roadmap (see ROADMAP.md).

Every strategy is a self-contained module exposing the same interface.
The registry discovers built-in strategies (declared below) plus any
auto-generated strategies that exist as `strategies/auto_*.py` files on
disk. Phase 3 alpha decay monitoring can mark any of them deprecated;
Phase 7 lifecycle tracking can additionally hold an auto-strategy in
`shadow` mode (discovered but excluded from the active set).

To add a new built-in strategy:
  1. Create strategies/your_strategy.py with module-level constants
     NAME, APPLICABLE_MARKETS and a function find_candidates(ctx, universe).
  2. Add it to STRATEGY_MODULES below.
  3. Once it accumulates >=50 resolved predictions, the validation gate
     and alpha decay monitor handle the rest automatically.

Auto-generated strategies (phase 7) land on disk via `strategy_generator`
and are picked up by `discover_strategies()` without any edit to this file.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Importable strategy modules. Each module must define:
#   NAME: str                — must match strategy_type stored in ai_predictions
#   APPLICABLE_MARKETS: list — which market_types this strategy works in
#   find_candidates(ctx, universe) -> list[dict]
#       returns dicts with at least: symbol, signal, score, votes, reason
STRATEGY_MODULES = [
    # Original 6 (Phase 6)
    "strategies.market_engine",
    "strategies.insider_cluster",
    "strategies.earnings_drift",
    "strategies.vol_regime",
    "strategies.max_pain_pinning",
    "strategies.gap_reversal",
    # Expanded seed library (10 additional hand-coded strategies)
    "strategies.short_term_reversal",
    "strategies.sector_momentum_rotation",
    "strategies.analyst_upgrade_drift",
    "strategies.fifty_two_week_breakout",
    "strategies.short_squeeze_setup",
    "strategies.high_iv_rank_fade",
    "strategies.insider_selling_cluster",
    "strategies.news_sentiment_spike",
    "strategies.volume_dryup_breakout",
    "strategies.macd_cross_confirmation",
    # Phase 1 of LONG_SHORT_PLAN.md — dedicated bearish strategies.
    # Built specifically for short setups, not bullish patterns flipped.
    "strategies.breakdown_support",
    "strategies.distribution_at_highs",
    "strategies.failed_breakout",
    "strategies.parabolic_exhaustion",
    "strategies.relative_weakness_in_strong_sector",
    "strategies.relative_weakness_universe",
    # Phase 3 of LONG_SHORT_PLAN.md — real alpha sources.
    "strategies.earnings_disaster_short",
    "strategies.catalyst_filing_short",
    "strategies.sector_rotation_short",
    "strategies.iv_regime_short",
]


def _auto_strategy_modules() -> List[str]:
    """Return `strategies.auto_*` paths for any auto-generated modules on disk."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        names = [
            f[:-3] for f in os.listdir(here)
            if f.startswith("auto_") and f.endswith(".py") and f != "__init__.py"
        ]
    except OSError:
        return []
    return [f"strategies.{n}" for n in sorted(names)]


_STOCK_MARKETS = ("stocks",)
_CRYPTO_MARKETS = ("crypto",)


def _strategy_applies_to_market(applicable: List[str], market_type: str) -> bool:
    """Return True iff a strategy's APPLICABLE_MARKETS allows this market.

    2026-05-19 — semantics changed. Previously this required an
    exact match (e.g., a strategy listing
    `["small","midcap"]` was excluded from `largecap`). That
    arbitrary within-stock walling-off didn't fit the current
    AI-driven architecture: the AI is the picker, and the right
    place to decide whether a signal matters is the AI, not a
    static registry filter. A largecap genuinely going parabolic
    SHOULD light up `parabolic_exhaustion` — the AI will weigh it.

    New rule:
      - `"*"` in applicable    → universal, runs everywhere
      - Crypto profile         → applicable must list `"crypto"`
      - Any stock profile      → applicable must list AT LEAST ONE
                                  stock market (any of micro/small/
                                  midcap/largecap)

    Stock-vs-crypto distinction is kept because the data sources
    are genuinely different (crypto fetches BTC/USD via the crypto
    endpoint; stock fetches AAPL via the equities endpoint). But
    within the stock universe, every stock-applicable strategy
    runs for every stock profile.
    """
    if "*" in applicable:
        return True
    if market_type in _CRYPTO_MARKETS:
        return any(m in applicable for m in _CRYPTO_MARKETS)
    if market_type in _STOCK_MARKETS:
        return any(m in applicable for m in _STOCK_MARKETS)
    # Unknown market_type — fall back to exact match (defensive)
    return market_type in applicable


def discover_strategies(market_type: str) -> List[Any]:
    """Import every strategy module (built-in + auto-generated) applicable to a market.

    Applicability rule changed 2026-05-19 — see
    `_strategy_applies_to_market` for the rationale. tl;dr: any
    stock-applicable strategy runs on any stock profile; crypto
    handled separately."""
    import importlib
    out = []
    for mod_path in STRATEGY_MODULES + _auto_strategy_modules():
        try:
            mod = importlib.import_module(mod_path)
        except (ImportError, AttributeError, SyntaxError) as _imp_exc:
            # Per-module import loop; one bad strategy module
            # shouldn't kill registry load. Surface for follow-up
            # so a broken strategy file doesn't quietly disappear.
            logger.warning(
                "strategy module %s failed to import: %s: %s",
                mod_path, type(_imp_exc).__name__, _imp_exc,
            )
            continue
        applicable = getattr(mod, "APPLICABLE_MARKETS", [])
        if _strategy_applies_to_market(applicable, market_type):
            out.append(mod)
    return out


def _auto_strategy_statuses(db_path: str) -> Dict[str, str]:
    """Return {name: status} for every row in auto_generated_strategies."""
    try:
        from strategy_generator import list_strategies
        return {s["name"]: s["status"] for s in list_strategies(db_path)}
    except Exception:
        return {}


def _is_stock_applicable(applicable: List[str]) -> bool:
    """True iff this strategy can run on stock universes."""
    return ("*" in applicable
            or any(m in _STOCK_MARKETS for m in applicable))


def _is_crypto_applicable(applicable: List[str]) -> bool:
    """True iff this strategy can run on the crypto universe."""
    return "*" in applicable or "crypto" in applicable


def get_active_strategies(market_type: str,
                          db_path: Optional[str] = None,
                          *,
                          enable_stocks: bool = True,
                          enable_crypto: bool = False) -> List[Any]:
    """Discover applicable strategies and return the actively-trading set.

    Filtering:
      * Deprecated strategies (Phase 3) are excluded.
      * Auto-generated strategies are included only when their lifecycle
        status is `active`. Shadow strategies are discovered but returned
        by `get_shadow_strategies()` instead.
      * 2026-05-19 — per-profile asset-class flags. `enable_stocks=True`
        keeps every stock-applicable strategy; `enable_crypto=True` keeps
        every crypto-applicable strategy. A universal (`"*"`) strategy
        qualifies under either flag. Defaults preserve current
        behavior (stocks on, crypto off).

    `market_type` is still consulted for `discover_strategies` (the
    APPLICABLE_MARKETS list filter), but within-stock filtering is now
    a no-op — any stock-applicable strategy runs on any stock profile.
    """
    deprecated: set = set()
    auto_status: Dict[str, str] = {}
    if db_path:
        try:
            from alpha_decay import list_deprecated
            deprecated = {d["strategy_type"] for d in list_deprecated(db_path)}
        except (ImportError, KeyError, AttributeError, OSError) as _dep_exc:
            logger.debug(
                "deprecated-strategy lookup failed: %s: %s",
                type(_dep_exc).__name__, _dep_exc,
            )
        auto_status = _auto_strategy_statuses(db_path)

    active = []
    for mod in discover_strategies(market_type):
        name = getattr(mod, "NAME", "")
        if not name or name in deprecated:
            continue
        if getattr(mod, "AUTO_GENERATED", False):
            if auto_status.get(name) != "active":
                continue
        applicable = getattr(mod, "APPLICABLE_MARKETS", [])
        # Asset-class enablement gate (2026-05-19). A strategy is kept
        # iff the operator has the relevant asset class enabled for
        # this profile.
        keep = False
        if enable_stocks and _is_stock_applicable(applicable):
            keep = True
        if enable_crypto and _is_crypto_applicable(applicable):
            keep = True
        if not keep:
            continue
        active.append(mod)
    return active


def get_shadow_strategies(market_type: str,
                          db_path: Optional[str]) -> List[Any]:
    """Return auto-generated strategies currently in `shadow` lifecycle state.

    Shadow strategies run alongside the active set but their output is
    recorded only (ai_predictions rows) — no capital is deployed. This is
    how a Phase 7 strategy earns its stripes before being promoted.
    """
    if not db_path:
        return []
    auto_status = _auto_strategy_statuses(db_path)
    if not auto_status:
        return []
    shadows = []
    for mod in discover_strategies(market_type):
        name = getattr(mod, "NAME", "")
        if not name or not getattr(mod, "AUTO_GENERATED", False):
            continue
        if auto_status.get(name) == "shadow":
            shadows.append(mod)
    return shadows
