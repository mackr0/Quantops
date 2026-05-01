"""Phase E of OPTIONS_PROGRAM_PLAN.md — vol surface analysis + regime gate.

E1-E3 (IV term structure, skew, realized vs implied) already exist in
`options_oracle` (compute_term_structure, compute_iv_skew,
compute_iv_rank). The advisor consumes them via the per-candidate
options_oracle_summary line.

What was missing: E4 — an explicit VOL REGIME classifier that
translates the raw signals into strategy-direction guidance the AI
can act on. This module is that translator.

Regime classifications (per symbol):

  premium_rich   = IV well above realized (rank ≥ 75) → SELL premium
                     (iron condor, credit spreads, covered calls)
  premium_cheap  = IV well below realized (rank ≤ 25) → BUY premium
                     (debit spreads, long straddle/strangle)
  premium_neutral = IV ≈ realized → no edge from vol risk premium

  term_contango     = back-month > front-month IV → normal; calendars
                        favorable (sell front, buy back)
  term_backwardation = front > back → market pricing front-month event
                        risk; long-front / short-back diagonals possible

  skew_steep_put = put IV >> call IV → market fears a crash; puts rich
  skew_steep_call = call IV >> put IV → euphoria / pre-event call demand
  skew_neutral    = balanced

The combined regime drives strategy selection:

  premium_rich + skew_steep_put + ranging      → iron condor
  premium_rich + skew_neutral + bullish        → bull put spread
  premium_cheap + bullish                       → bull call spread
  premium_cheap + uncertain timing              → calendar spread
  term_backwardation + premium_cheap            → long front diagonal
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Thresholds — tunable. Conservative defaults that match the
# multi-leg advisor's IV regime gates.
PREMIUM_RICH_RANK_PCT = 75.0
PREMIUM_CHEAP_RANK_PCT = 25.0
SKEW_STEEP_PUT_RATIO = 1.30   # put_iv / call_iv ≥ 1.30 = steep put
SKEW_STEEP_CALL_RATIO = 0.85  # put_iv / call_iv ≤ 0.85 = steep call
TERM_CONTANGO_SLOPE = 0.02    # back_iv - front_iv ≥ 2pts → contango
TERM_BACKWARD_SLOPE = -0.02


def classify_vol_regime(oracle: Dict[str, Any]) -> Dict[str, Any]:
    """Translate raw oracle signals into a regime classification.

    Args:
        oracle: dict from options_oracle.get_options_oracle.

    Returns:
        {
          "has_signals": bool,
          "premium_regime": "rich" | "cheap" | "neutral",
          "skew_regime": "steep_put" | "steep_call" | "neutral",
          "term_regime": "contango" | "backwardation" | "flat",
          "iv_rank_pct": float | None,
          "favored_strategies": [list of strategy names appropriate
                                  to this combined regime],
          "rationale": str,
        }

    Returns has_signals=False when there's not enough data — caller
    should fall back to non-vol-aware strategy selection.
    """
    base = {
        "has_signals": False,
        "premium_regime": "neutral",
        "skew_regime": "neutral",
        "term_regime": "flat",
        "iv_rank_pct": None,
        "favored_strategies": [],
        "rationale": "",
    }
    if not oracle or not oracle.get("has_options"):
        return base

    rank = (oracle.get("iv_rank") or {}).get("rank_pct")
    skew_data = oracle.get("skew") or {}
    skew_ratio = skew_data.get("skew", 1.0)
    term = oracle.get("term_structure") or {}
    near_iv = term.get("near_iv")
    far_iv = term.get("far_iv")

    # Premium regime (IV vs realized via the iv_rank approximation)
    if rank is None:
        premium = "neutral"
    elif rank >= PREMIUM_RICH_RANK_PCT:
        premium = "rich"
    elif rank <= PREMIUM_CHEAP_RANK_PCT:
        premium = "cheap"
    else:
        premium = "neutral"

    # Skew regime
    if skew_ratio >= SKEW_STEEP_PUT_RATIO:
        skew_regime = "steep_put"
    elif skew_ratio <= SKEW_STEEP_CALL_RATIO:
        skew_regime = "steep_call"
    else:
        skew_regime = "neutral"

    # Term structure regime
    if near_iv is not None and far_iv is not None:
        slope = far_iv - near_iv
        if slope >= TERM_CONTANGO_SLOPE:
            term_regime = "contango"
        elif slope <= TERM_BACKWARD_SLOPE:
            term_regime = "backwardation"
        else:
            term_regime = "flat"
    else:
        term_regime = "flat"

    # Strategy selection from the regime cube
    favored: List[str] = []
    rationale_parts: List[str] = []

    if premium == "rich":
        favored.extend([
            "iron_condor", "bull_put_spread", "bear_call_spread",
            "covered_call",
        ])
        rationale_parts.append(
            f"IV rank {rank:.0f} → premium rich; favor SELL-premium plays"
        )
    elif premium == "cheap":
        favored.extend([
            "long_strangle", "long_straddle",
            "bull_call_spread", "bear_put_spread", "calendar_spread",
        ])
        rationale_parts.append(
            f"IV rank {rank:.0f} → premium cheap; favor BUY-premium plays"
        )

    if skew_regime == "steep_put":
        rationale_parts.append(
            f"Steep put skew (ratio {skew_ratio:.2f}) — market fears a "
            f"crash; downside puts are expensive"
        )
        if "iron_condor" in favored:
            # Asymmetric condor: wider put wing favorable
            rationale_parts.append(
                "Consider asymmetric iron condor (tighter call wing)"
            )
    elif skew_regime == "steep_call":
        rationale_parts.append(
            f"Steep call skew (ratio {skew_ratio:.2f}) — speculative "
            f"call demand; upside calls expensive"
        )

    if term_regime == "backwardation":
        rationale_parts.append(
            f"Term backwardation (front {near_iv:.0%} > back {far_iv:.0%}) "
            f"— front-month event risk priced in; consider long front "
            f"diagonals or wait through event"
        )
        if "calendar_spread" in favored:
            favored.remove("calendar_spread")  # backward calendars lose
    elif term_regime == "contango":
        if "calendar_spread" not in favored and premium == "neutral":
            favored.append("calendar_spread")
        rationale_parts.append(
            f"Term contango (front {near_iv:.0%} < back {far_iv:.0%}) "
            f"— front-month decay favors calendars"
        )

    return {
        "has_signals": rank is not None,
        "premium_regime": premium,
        "skew_regime": skew_regime,
        "term_regime": term_regime,
        "iv_rank_pct": rank,
        "favored_strategies": favored,
        "rationale": "; ".join(rationale_parts),
    }


def render_vol_regime_for_prompt(
    candidates: List[Dict[str, Any]],
    oracle_lookup,  # callable(symbol) -> oracle dict
    max_lines: int = 5,
) -> str:
    """Build a VOL REGIME prompt section.

    Per-candidate row showing premium / skew / term regime + favored
    strategies. AI uses this to bias its strategy selection alongside
    the multi-leg advisor's per-strategy recommendations.

    Returns empty string when no candidate has actionable signals.
    """
    if not candidates:
        return ""
    lines: List[str] = []
    for c in candidates[:max_lines]:
        sym = c.get("symbol")
        if not sym:
            continue
        try:
            oracle = oracle_lookup(sym)
        except Exception:
            oracle = None
        if not oracle:
            continue
        regime = classify_vol_regime(oracle)
        if not regime["has_signals"]:
            continue
        favored = ", ".join(regime["favored_strategies"][:3]) or "—"
        lines.append(
            f"  - {sym}: premium={regime['premium_regime']} "
            f"(rank {regime['iv_rank_pct']:.0f}), "
            f"skew={regime['skew_regime']}, term={regime['term_regime']} "
            f"→ favor: {favored}"
        )
        if regime["rationale"]:
            lines.append(f"      {regime['rationale']}")

    if not lines:
        return ""
    return (
        "VOL REGIME (use to bias multi-leg strategy selection):\n"
        + "\n".join(lines)
    )
