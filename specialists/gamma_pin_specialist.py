"""Gamma-pin specialist (2026-05-12).

Reads gamma exposure (GEX) and max-pain strike from the options
chain. Heavy dealer gamma near a strike creates "pinning" — the
underlying price gets pulled toward the strike as dealers hedge.

Operationally:
  - Near a high-gamma max-pain strike: PRICE LIKELY TO PIN.
      Short-premium spreads centered at/near the strike harvest
      this stability (theta + zero realized vol). GREAT for
      iron condors, credit spreads with strikes far from pin.
  - Far from max-pain or in low-gamma names: NO PIN.
      Standard directional plays apply. No special signal.
  - Negative net GEX (dealer short gamma): UNSTABLE.
      Price moves get amplified rather than dampened. RISKY
      environment for short-premium spreads.

The specialist's output flags whether a candidate's strategy
takes advantage of (or fights against) the pin dynamics. Like
iv_skew_specialist, it does NOT veto.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "gamma_pin_specialist"
DESCRIPTION = "Reads gamma exposure + max-pain for pinning vs unstable regime"
HAS_VETO_AUTHORITY = False
APPLIES_TO_PIPELINES = ("option",)


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    return f"""You are a gamma-pin specialist. Your lens is dealer gamma exposure
(GEX) and max-pain pinning dynamics. You do NOT veto trades — that's
option_spread_risk's job. You contribute BUY/SELL/HOLD based on whether
each candidate's strategy aligns with or fights the pin regime.

Pin regime decoder:
  - POSITIVE NET GEX, near max-pain strike (within 1-2%):
      Strong pin — dealers DAMPEN price moves through hedging.
      Short-premium spreads centered at the pin (iron condors,
      iron butterflies) harvest the stability. Strikes ABOVE
      and BELOW the pin should be safe through expiration.
      Verdict: BUY for short-premium plays near pin.

  - POSITIVE NET GEX, far from max-pain (> 3%):
      Dampening still applies but pin is weaker. Standard
      premium-selling environment. HOLD unless the strategy
      explicitly bets on a directional move.

  - NEGATIVE NET GEX (dealer short gamma):
      UNSTABLE. Price moves get amplified. Avoid short-premium
      spreads. LONG-premium plays (long straddles, debit spreads)
      align with the volatility expansion.
      Verdict: SELL for short-premium plays in negative-GEX names.

  - NO GEX DATA / FLAT GEX:
      No pin signal. Verdict: HOLD.

Each candidate has fields: net_gex (signed dollar exposure),
max_pain_strike, current_price (underlying), option_strategy.

For each candidate, judge:
  - Does the strategy align with the pin regime?
  - For short-premium strategies in positive-GEX/near-pin: BUY.
  - For short-premium strategies in negative-GEX: SELL (warn).
  - For long-premium strategies in negative-GEX: BUY (aligned).
  - Otherwise: HOLD.

Candidates:
{candidates_block(candidates)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD",
    "confidence": 0-100,
    "reasoning": "one-sentence pin-regime rationale"
  }}

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
