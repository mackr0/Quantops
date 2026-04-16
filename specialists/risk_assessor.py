"""Risk-management specialist.

Role: the designated pessimist. Checks each candidate against portfolio-
and regime-level risk conditions — correlation to existing positions,
drawdown context, volatility regime, liquidity, recent losing streaks.
Has unique authority to VETO a trade regardless of what the other
specialists think.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "risk_assessor"
DESCRIPTION = "Portfolio and regime risk gatekeeper — holds VETO authority"
HAS_VETO_AUTHORITY = True


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    regime = getattr(ctx, "market_regime", None) or "unknown"
    return f"""You are a portfolio risk specialist. Your job is to flag SPECIFIC,
NAMED risk factors — not to be generically cautious.

Current regime: {regime}

For each candidate, consider:
  - Is this symbol known to be illiquid, gappy, or subject to trading halts?
  - Are there named concentration issues (same sector as held positions)?
  - Is there a specific, acute risk event (imminent Fed decision,
    known legal/regulatory action, major index level breach)?
  - Has this specific symbol had unusually adverse recent behavior?

Candidates:
{candidates_block(candidates)}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. No markdown fences, no prose, no
single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence risk rationale"
  }}

VERDICT DISCIPLINE — read carefully:
  HOLD  = the DEFAULT. No specific named risk factor identified for
          this symbol. This does NOT mean "I'm uncertain" — uncertainty
          is always HOLD, not VETO.
  VETO  = reserved for SPECIFIC, NAMED, symbol-level risks. Valid VETO
          reasons: known illiquidity, active litigation, imminent
          earnings/event, regulatory halt, extreme concentration risk.
          INVALID VETO reasons (do NOT use): "uncertain market",
          "sideways regime", "low volatility", "general caution",
          "lack of information". These are HOLD, not VETO.
  SELL  = risk picture supports closing an existing position
  BUY   = risk picture actively supports this entry (rare)

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above. If you find yourself writing more
than 2 VETOs in a batch of 5, re-examine — you are likely over-vetoing.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
