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


# Prices as of 2026 roadmap planning. Update as providers change pricing.
# The leading entries are the ones we actually use; the rest are reasonable
# fallbacks so unknown models don't silently produce $0.
PRICING: Dict[str, Dict[str, float]] = {
    # Anthropic Claude
    "claude-haiku-4-5-20251001":   {"input":  1.00, "output":  5.00},
    "claude-haiku-4-5":             {"input":  1.00, "output":  5.00},
    "claude-sonnet-4-6":            {"input":  3.00, "output": 15.00},
    "claude-opus-4-6":              {"input": 15.00, "output": 75.00},
    "claude-opus-4-6[1m]":          {"input": 15.00, "output": 75.00},
    # OpenAI (typical 2025/2026 tiers)
    "gpt-5-mini":                   {"input":  0.40, "output":  1.60},
    "gpt-5":                        {"input":  2.00, "output": 10.00},
    # Google
    "gemini-2.5-flash":             {"input":  0.35, "output":  0.70},
    "gemini-2.5-pro":               {"input":  1.25, "output":  5.00},
}

# Fallback used when model is unknown. Conservative (mid-tier) so unknown-
# model spend isn't reported as $0 and hide real costs.
FALLBACK_PRICING = {"input": 3.00, "output": 15.00}


def estimate_cost_usd(model: Optional[str],
                      input_tokens: int,
                      output_tokens: int) -> float:
    """Compute a USD cost estimate from token counts.

    Returns 0.0 when both token counts are zero (e.g., a cached call).
    Falls back to FALLBACK_PRICING for unknown models — prefer reporting
    an over-estimate than a silent zero.
    """
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    if input_tokens == 0 and output_tokens == 0:
        return 0.0

    prices = PRICING.get(model) if model else None
    if not prices:
        prices = FALLBACK_PRICING

    cost = (input_tokens * prices["input"] / 1_000_000.0
            + output_tokens * prices["output"] / 1_000_000.0)
    return round(cost, 6)
