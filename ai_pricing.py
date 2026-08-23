"""Per-model AI pricing table.

Prices are **estimates** in USD per million tokens. They change over time
and vary by provider tier — treat computed totals as "order of magnitude"
not billing-grade. Update this table when the providers announce new
prices. Storing token counts in the ledger separately from USD means
re-pricing history is a one-place change here.

Format: {model_id: {"input": $/M_tokens, "output": $/M_tokens}}
"""

from __future__ import annotations

from typing import Dict, Optional


# 2026-08-23 — every price below was read from the provider's OFFICIAL
# pricing page on this date (platform.claude.com/docs/en/about-claude/
# pricing, developers.openai.com/api/docs/pricing, ai.google.dev/
# gemini-api/docs/pricing). Standard tier, non-batch, uncached, short
# context. Promotional rates are marked with their end date; when one
# lapses, update the number AND this date. The previous table had
# claude-opus-4-6 at 3x its real rate and gemini-2.5-flash / -pro
# output prices ~3.5x and 2x too low.
PRICES_VERIFIED_ON = "2026-08-23"

PRICING: Dict[str, Dict[str, float]] = {
    # ---- Anthropic Claude (Claude 4.7+ models use a tokenizer that
    #      produces ~30% more tokens for the same text — effective cost
    #      per call is higher than the per-token rate suggests) ----
    "claude-fable-5":               {"input": 10.00, "output": 50.00},
    "claude-opus-5":                {"input":  5.00, "output": 25.00},
    "claude-opus-4-8":              {"input":  5.00, "output": 25.00},
    "claude-opus-4-7":              {"input":  5.00, "output": 25.00},
    "claude-opus-4-6":              {"input":  5.00, "output": 25.00},
    "claude-opus-4-6[1m]":          {"input":  5.00, "output": 25.00},
    "claude-sonnet-5":              {"input":  2.00, "output": 10.00},
    "claude-sonnet-4-6":            {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5-20251001":    {"input":  1.00, "output":  5.00},
    "claude-haiku-4-5":             {"input":  1.00, "output":  5.00},
    # ---- OpenAI ----
    "gpt-5.6-sol":                  {"input":  4.00, "output": 20.00},  # promo through 2026-11-21 (list $5/$30)
    "gpt-5.6-terra":                {"input":  2.00, "output": 12.00},
    "gpt-5.6-luna":                 {"input":  0.20, "output":  1.20},
    "gpt-5.5":                      {"input":  5.00, "output": 30.00},
    "gpt-5.4":                      {"input":  2.50, "output": 15.00},
    "gpt-5.4-mini":                 {"input":  0.75, "output":  4.50},
    "gpt-5.4-nano":                 {"input":  0.20, "output":  1.25},
    "gpt-5.2":                      {"input":  1.75, "output": 14.00},
    "gpt-5.1":                      {"input":  1.25, "output": 10.00},
    "gpt-5":                        {"input":  1.25, "output": 10.00},
    "gpt-5-mini":                   {"input":  0.25, "output":  2.00},
    "gpt-5-nano":                   {"input":  0.05, "output":  0.40},
    "gpt-4.1":                      {"input":  2.00, "output":  8.00},
    "gpt-4.1-mini":                 {"input":  0.40, "output":  1.60},
    "gpt-4.1-nano":                 {"input":  0.10, "output":  0.40},
    "gpt-4o-mini":                  {"input":  0.15, "output":  0.60},
    "gpt-4o":                       {"input":  2.50, "output": 10.00},
    "o3":                           {"input":  2.00, "output":  8.00},
    "o3-mini":                      {"input":  1.10, "output":  4.40},
    "o4-mini":                      {"input":  1.10, "output":  4.40},
    # ---- Google Gemini ----
    "gemini-3.7-flash":             {"input":  0.75, "output":  3.75},  # promo through 2026-12-31 (then $1.50/$7.50)
    "gemini-3.6-flash":             {"input":  0.75, "output":  3.75},  # promo through 2026-12-31 (then $1.50/$7.50)
    "gemini-3.5-flash":             {"input":  1.50, "output":  9.00},
    "gemini-3.5-flash-lite":        {"input":  0.30, "output":  2.50},
    "gemini-3.1-flash-lite":        {"input":  0.25, "output":  1.50},
    "gemini-3.1-pro-preview":       {"input":  2.00, "output": 12.00},  # <=200K-token prompts
    "gemini-2.5-pro":               {"input":  1.25, "output": 10.00},  # <=200K-token prompts
    "gemini-2.5-pro-preview-03-25": {"input":  1.25, "output": 10.00},
    "gemini-2.5-flash":             {"input":  0.30, "output":  2.50},
    "gemini-2.5-flash-lite":        {"input":  0.10, "output":  0.40},
    "gemini-2.0-flash":             {"input":  0.15, "output":  0.60},  # shut down 2026-06-01
    # ---- DeepSeek (unchanged; not re-verified 2026-08-23) ----
    "deepseek-chat":                {"input":  0.14, "output":  0.28},
    "deepseek-reasoner":            {"input":  0.55, "output":  2.19},
}

# Fallback used when model is unknown. Conservative (mid-tier) so unknown-
# model spend isn't reported as $0 and hide real costs.
FALLBACK_PRICING = {"input": 3.00, "output": 15.00}


def _fmt_price(v: float) -> str:
    """Format a $/M price compactly: 0.35 -> "$0.35", 1.0 -> "$1", 15.0 -> "$15"."""
    s = ("%.2f" % float(v)).rstrip("0").rstrip(".")
    return "$" + s


def price_for(model: Optional[str]) -> Optional[Dict[str, float]]:
    """Return the {"input","output"} $/M price for a model, or None if we
    don't have a price (so callers can distinguish 'priced' from 'unknown'
    rather than silently using FALLBACK_PRICING)."""
    return PRICING.get(model) if model else None


def cost_label(model: Optional[str]) -> Optional[str]:
    """Human-readable per-1M-token price for a model, e.g.
    "$0.35 in / $0.70 out per 1M". Returns None for unpriced models so the
    UI can show them without inventing a number."""
    p = price_for(model)
    if not p:
        return None
    return "%s in / %s out per 1M" % (
        _fmt_price(p["input"]), _fmt_price(p["output"]))


# Discount applied to prompt tokens served from a provider's IMPLICIT cache
# (Gemini 2.5+/3.x: cached reads bill at ~10% of the input rate, e.g.
# $0.025/M vs $0.25/M on gemini-3.1-flash-lite). Single conservative knob;
# per-model overrides can move into PRICING if providers diverge.
CACHED_INPUT_DISCOUNT = 0.10


def estimate_cost_usd(model: Optional[str],
                      input_tokens: int,
                      output_tokens: int,
                      cached_tokens: int = 0) -> float:
    """Compute a USD cost estimate from token counts.

    `cached_tokens` (2026-07-02) is the SUBSET of input_tokens the provider
    served from its implicit cache, billed at CACHED_INPUT_DISCOUNT × the
    input rate — without this a cache hit is overstated ~10x in the ledger.

    Returns 0.0 when both token counts are zero (e.g., a cached call).
    Falls back to FALLBACK_PRICING for unknown models — prefer reporting
    an over-estimate than a silent zero.
    """
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    cached_tokens = max(0, min(int(cached_tokens or 0), input_tokens))
    if input_tokens == 0 and output_tokens == 0:
        return 0.0

    prices = PRICING.get(model) if model else None
    if not prices:
        prices = FALLBACK_PRICING

    full_rate = input_tokens - cached_tokens
    cost = (full_rate * prices["input"] / 1_000_000.0
            + cached_tokens * prices["input"] * CACHED_INPUT_DISCOUNT
              / 1_000_000.0
            + output_tokens * prices["output"] / 1_000_000.0)
    return round(cost, 6)
