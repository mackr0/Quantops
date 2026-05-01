"""Adversarial / red-team specialist.

Item 5b of COMPETITIVE_GAP_PLAN.md. Real funds run independent risk
teams whose job is to critique trades pre-execution. We don't have
that — risk_assessor is closest but it operates inside the same
ensemble logic and shares the same context the others see.

This specialist's role is different: deliberately hunt for the
failure mode in the proposal. Not "is this a good idea" but "what
would have to be true for this to lose money fast?" Looks for:
  - Correlation overlap with current book (multiple bets on same factor)
  - Single-name concentration (already too much in this name)
  - Regime mismatch (long beta in defensive regime)
  - Recent earnings (post-earnings drift / vol crush)
  - Factor exposure violation (e.g., adds long beta when book targets
    market-neutral)
  - Crowded trade indicators (high short interest, recent ETF rotation)

Like risk_assessor, has VETO authority. The two are intentionally
redundant — risk_assessor approaches "what risks exist?" and this
one approaches "what's the worst-case failure?". Different framings
catch different misses.
"""

from __future__ import annotations

from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array


NAME = "adversarial_reviewer"
DESCRIPTION = "Red-team reviewer — hunts failure modes pre-execution; holds VETO"
HAS_VETO_AUTHORITY = True


def _portfolio_summary(ctx: Any) -> str:
    """Render the current book + key risk knobs so the AI can spot
    correlation/concentration risks that wouldn't be visible from
    the candidate row alone.

    Best-effort: any failure → empty string (specialist still runs,
    just without the portfolio context).
    """
    try:
        from client import get_positions
        positions = get_positions(ctx=ctx) or []
    except Exception:
        positions = []

    if not positions:
        return "Current book: empty (no concentration / correlation issues to flag)."

    lines = ["Current book:"]
    for p in positions[:15]:
        sym = p.get("symbol", "?")
        qty = p.get("qty", 0)
        side = "LONG" if (qty and float(qty) > 0) else "SHORT"
        mv = p.get("market_value", 0) or 0
        unreal_pct = p.get("unrealized_plpc", 0) or 0
        try:
            unreal_pct = float(unreal_pct) * 100
        except Exception:
            unreal_pct = 0
        lines.append(f"  - {sym} {side} ${float(mv):,.0f} ({unreal_pct:+.1f}%)")
    return "\n".join(lines)


def _regime_summary(ctx: Any) -> str:
    regime = getattr(ctx, "market_regime", None) or "unknown"
    target_short = getattr(ctx, "target_short_pct", None)
    target_beta = getattr(ctx, "target_book_beta", None)
    bits = [f"regime={regime}"]
    if target_short is not None:
        bits.append(f"target_short_pct={target_short}")
    if target_beta is not None:
        bits.append(f"target_book_beta={target_beta}")
    return "Mandate: " + ", ".join(bits)


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    portfolio = _portfolio_summary(ctx)
    regime = _regime_summary(ctx)

    return f"""You are an ADVERSARIAL trade reviewer. Your job is NOT to
evaluate "is this a good trade?" — other specialists do that. Your job
is to deliberately hunt for the FAILURE MODE: what would have to be true
for this trade to lose money fast?

You operate as the independent risk team a real fund would have. You see
the candidate AND the current book AND the regime mandate. You should
flag VETO when you find a SPECIFIC, NAMED failure mode that the other
specialists are likely to miss.

{regime}

{portfolio}

Candidates to review:
{candidates_block(candidates)}

For each candidate, work through this checklist mentally:
  1. CORRELATION: does the book already have material exposure to this
     factor / sector / theme? Adding more is doubling down, not
     diversifying.
  2. CONCENTRATION: is this name already in the book? A 2nd entry on
     the same name compounds single-name risk.
  3. REGIME MISMATCH: does this trade work AGAINST the mandate?
     (e.g., a long-beta tech name when the regime is defensive and
     the mandate is market-neutral.)
  4. EARNINGS / EVENT RISK: is there a known catalyst within the
     typical hold (5-15 days) that would dominate the technical setup?
  5. CROWDED TRADE: high short interest on a long, or extreme
     positioning on a short? Squeeze risk cuts both ways.
  6. FACTOR DIRECTION: would this push the book's beta or sector tilt
     past the mandate?

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence failure-mode rationale"
  }}

VERDICT DISCIPLINE:
  HOLD  = DEFAULT. No specific failure mode identified. Uncertainty
          is HOLD, not VETO. "Probably fine" is HOLD.
  VETO  = a specific failure mode you can NAME. Examples of valid VETO
          reasoning: "Book already 35% energy and this adds another
          oil name", "Long-beta entry in defensive regime with target
          book_beta=0", "Earnings tomorrow — IV crush will dominate
          technicals", "Already long this exact name — single-name
          concentration".
          INVALID VETO reasons (use HOLD): "market is uncertain",
          "general caution", "this might not work", "low conviction".
  BUY   = the failure-mode hunt actually supports this entry — the
          ADVERSARIAL view is that this trade is robust.
  SELL  = the failure-mode hunt supports closing an existing position
          (not the candidate but a held position).

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order. If you write more than 2 VETOs out of a batch of 5,
re-examine — you are over-vetoing.
"""


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
