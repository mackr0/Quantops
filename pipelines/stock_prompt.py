"""Stock-only AI prompt builder.

Phase 3 of the instrument-class pipeline refactor (2026-05-11).
See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md`.

Renders ONLY stock-relevant features (technicals, sector context,
sentiment, news). Does NOT include IV / Greeks / DTE / spread
economics — those belong in `pipelines/option_prompt.py`.

Used by `pipelines.stock.StockPipeline.build_prompt()`.

Phase 3 is the FORK: stock prompts no longer mix in option-specific
features that the AI doesn't need for stock decisions, and option
prompts no longer have to inherit a stock-shaped frame. Audit
finding #4 (option candidates seeing only stock technicals) is
fixed by construction: the option prompt builder lives in its own
module and is responsible for option-specific features.

This is the minimum-viable per-pipeline prompt. The legacy
`ai_analyst._build_batch_prompt` continues to handle the
production-running prompt for now; Phase 4+ will route the
dispatcher through these new builders. Until then, these builders
exist as a CAPABILITY ready to be wired up.
"""
from __future__ import annotations

import json
from typing import List


def build_prompt(ctx, candidates: List) -> str:
    """Render a stock-only AI prompt for the given candidates.

    Each candidate is a `pipelines.Candidate` instance. Stock
    candidates' `extra` dict typically carries: rsi, macd_signal,
    sma_short, sma_long, sector_momentum, news_sentiment, etc.

    The prompt explicitly does NOT include any option-specific
    fields (IV, Greeks, DTE, strike, spread max-loss/max-gain) —
    those belong to the option pipeline's prompt builder.
    """
    if not candidates:
        return _empty_prompt(ctx)
    rendered_candidates = []
    for c in candidates:
        feature_summary = _stock_features_only(c.extra or {})
        rendered_candidates.append(
            f"- **{c.symbol}** "
            f"({c.signal}, score {c.score:.2f}, ${c.price:.2f}): "
            f"{json.dumps(feature_summary)}"
        )
    return (
        f"You are evaluating STOCK candidates for "
        f"{getattr(ctx, 'segment', 'this profile')}.\n\n"
        f"For each candidate decide BUY / STRONG_BUY / WEAK_BUY / "
        f"SHORT / SELL / HOLD with a confidence (0-100) and a "
        f"one-line reason citing the strongest signal in the "
        f"feature summary.\n\n"
        f"Candidates:\n"
        + "\n".join(rendered_candidates)
    )


def _empty_prompt(ctx) -> str:
    """Returned when there are no candidates this cycle. Keeps the
    interface uniform — callers always get a string."""
    return (
        f"No stock candidates this cycle for "
        f"{getattr(ctx, 'segment', 'this profile')}. Return an "
        f"empty trades list."
    )


# ---------------------------------------------------------------------------
# Per-instrument feature filter — central place that enforces the
# stock-only rule. If a feature key is in `_OPTION_ONLY_KEYS`, it
# does NOT belong in a stock prompt and is dropped here.
# ---------------------------------------------------------------------------

_OPTION_ONLY_KEYS = frozenset({
    "iv_rank", "iv", "implied_vol", "implied_volatility",
    "delta", "gamma", "theta", "vega", "rho",
    "dte", "days_to_expiry",
    "strike", "spread_max_loss", "spread_max_gain", "spread_width",
    "option_strategy", "occ_symbol", "premium",
    "bid_ask_spread", "option_bid", "option_ask",
})


def _stock_features_only(extra: dict) -> dict:
    """Strip option-specific keys from a candidate's feature dict.

    Defense-in-depth: even if a stock candidate accidentally has
    option keys in its extras (e.g., a bug in candidate generation
    leaks IV rank), the prompt builder drops them before they reach
    the AI. The bug stays caught at the prompt boundary.
    """
    return {
        k: v for k, v in extra.items()
        if k not in _OPTION_ONLY_KEYS
    }
