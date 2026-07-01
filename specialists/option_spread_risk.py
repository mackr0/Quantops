"""Option-specific risk specialist (Phase 4 of pipeline refactor).

Role: hunt structural failure modes specific to option positions —
risks that the stock-shaped specialists can't see because they don't
read IV, Greeks, DTE, or spread economics:

  - Max-loss-vs-budget: spread max loss exceeds the profile's
    per-trade risk budget. Closes audit finding #5 (multileg
    proposals bypass risk_assessor today, which only knows about
    stock-shaped 1:1 exposure).
  - IV-crush exposure: short premium with imminent earnings, or
    long premium bought at IV rank > 80 (premium will deflate
    even if direction is right).
  - Gamma blowup: short option positions inside 7 DTE with strike
    near spot — a 1% underlying move produces outsized P&L swings.
  - Spread economics: credit received doesn't justify max loss
    (credit/max-loss ratio < 0.20), making the trade negative-
    expectancy regardless of direction view.

Holds VETO authority — these are structural risks no other
specialist can catch (the stock-shaped risk_assessor reads
position_size only, not max-loss-at-expiry).

Phase 4a establishes this specialist's slot in the routing
framework. Phase 4b will integrate it into live ensemble cycles
when the option pipeline's execute() is wired.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from specialists._common import candidates_block, extract_verdict_array

logger = logging.getLogger(__name__)


NAME = "option_spread_risk"
DESCRIPTION = "Option-aware risk gatekeeper — IV crush, gamma, max-loss budget; VETO"
HAS_VETO_AUTHORITY = True
APPLIES_TO_PIPELINES = ("option",)


def build_prompt(candidates: List[Dict[str, Any]], ctx: Any) -> str:
    risk_budget = getattr(ctx, "max_per_trade_loss", None)
    budget_line = (
        f"Per-trade risk budget: ${risk_budget:.0f}\n"
        if isinstance(risk_budget, (int, float)) and risk_budget > 0 else ""
    )

    # 2026-05-12 — VETO thresholds are AI-tunable per-profile.
    # Surface the effective values so the LLM applies the
    # threshold the tuner has converged on (not hardcoded values
    # from training). Defaults fire when ctx lacks the attribute
    # (legacy callers / tests).
    iv_rank_veto = float(getattr(
        ctx, "option_spread_iv_rank_veto_threshold", 80.0,
    ))
    gamma_dte_veto = int(getattr(
        ctx, "option_spread_gamma_dte_veto_threshold", 7,
    ))
    credit_ratio_veto = float(getattr(
        ctx, "option_spread_credit_ratio_veto_threshold", 0.20,
    ))

    # 2026-05-12 — surface current book-Greeks context. The
    # specialist now sees not just the proposal but where the
    # portfolio already stands on net delta / gamma / vega / theta.
    # Without this, vetoing for "Greeks-portfolio-impact" was
    # impossible (specialist couldn't see what the book already
    # held). Pulled from compute_book_greeks via the live broker
    # positions; failure-tolerant — no Greeks line if anything
    # raises.
    greeks_line = ""
    try:
        positions = _current_positions(ctx)
        if positions:
            from pipelines.risk import (
                compute_book_greeks, make_underlying_spot_lookup,
            )
            # Greeks need the UNDERLYING spot, not the option premium —
            # pass a real spot lookup so net delta/gamma/vega/theta are
            # meaningful (without it the aggregator falls back to each
            # leg's current_price, i.e. the premium, and the budgets it
            # vetoes against are nonsense).
            book = compute_book_greeks(
                positions,
                price_lookup=make_underlying_spot_lookup(),
            ) or {}
            n_legs = int(book.get("n_options_legs") or 0)
            if n_legs > 0:
                greeks_line = (
                    f"Current book Greeks (BEFORE this proposal): "
                    f"net_delta={book.get('net_delta', 0):+.0f}sh, "
                    f"net_gamma={book.get('net_gamma', 0):+.4f}, "
                    f"net_vega=${book.get('net_vega', 0):+,.0f}/vol, "
                    f"net_theta=${book.get('net_theta', 0):+,.0f}/day, "
                    f"options_legs={n_legs}\n"
                )
    except Exception:
        greeks_line = ""

    # Surface the per-profile options risk caps so the specialist can
    # reason about whether THIS proposal would push the book past one of
    # them. The primary control is max_options_risk_pct — the aggregate
    # capital-at-risk (max-loss) budget as % of NAV (the options-delta cap
    # is retired to a wide backstop and no longer shown). theta/vega caps
    # remain. Tunable per-profile by the self-tuner.
    budget_caps_lines = []
    for cap_name, cap_label in [
        ("max_options_risk_pct",
         "max options capital-at-risk (max-loss) / NAV"),
        ("max_theta_burn_dollars_per_day",
         "max $theta burn / day"),
        ("max_short_vega_dollars",
         "max short $vega"),
    ]:
        v = getattr(ctx, cap_name, None)
        if v is not None:
            if "pct" in cap_name:
                budget_caps_lines.append(f"  {cap_label}: {v*100:.1f}%")
            else:
                budget_caps_lines.append(f"  {cap_label}: ${v:.0f}")
    budget_caps_block = (
        "Per-profile Greek-budget caps:\n" + "\n".join(budget_caps_lines) + "\n"
        if budget_caps_lines else ""
    )

    return f"""You are an option-specific risk specialist. Your lens is structural
option failure modes — not direction, not technicals. Other specialists
cover those. You judge each candidate ONLY through the option-economics
lens.

{budget_line}{greeks_line}{budget_caps_block}For each candidate, consider:
  - SPREAD MAX-LOSS: does the structural max loss
    (spread_max_loss × 100 × contracts) exceed the per-trade risk
    budget? If yes — VETO.
  - IV CRUSH: is this LONG premium with iv_rank > {iv_rank_veto:.0f}
    (premium will deflate even on a directional win)? Or SHORT
    premium with earnings inside the spread's DTE (event will spike
    IV against you)? If yes — VETO.
  - GAMMA RISK: SHORT options with DTE < {gamma_dte_veto} and strike
    within 2% of spot — gamma exposure makes P&L unstable. VETO
    unless explicitly a 0DTE strategy.
  - CREDIT/MAX-LOSS ratio: for credit spreads, credit received /
    max loss should be at least {credit_ratio_veto:.2f}. Below that,
    the trade is negative-expectancy regardless of directional view.
    VETO.

Candidates:
{candidates_block(candidates, specialist_name="option_spread_risk")}

Return a STRICT JSON ARRAY — starts with `[` and ends with `]`. Every
candidate must appear EXACTLY ONCE. No markdown fences, no prose, no
single top-level object. Each entry:
  {{
    "symbol": "TICKER",
    "verdict": "BUY" | "SELL" | "HOLD" | "VETO",
    "confidence": 0-100,
    "reasoning": "one-sentence option-economics rationale"
  }}

VERDICT DISCIPLINE:
  HOLD = the DEFAULT. No structural option-economics red flag found.
         Don't VETO for "I don't like the trade" — that's HOLD.
  VETO = SPECIFIC structural problem: max loss exceeds budget,
         iv-crush exposure, near-expiry gamma blowup, credit
         insufficient vs max loss. Name the risk in the reasoning.
  BUY  = option economics ACTIVELY support the trade (rare —
         e.g., short premium at iv_rank > {iv_rank_veto + 10:.0f} with no event risk).
  SELL = the option position should be closed (only relevant when
         a candidate is an existing-position close decision).

You MUST return exactly {len(candidates)} entries, one per candidate,
in the same order as the list above.
"""


def _current_positions(ctx: Any) -> List[Dict[str, Any]]:
    """Get this profile's OWN-BOOK positions (isolation-critical).

    Reads the profile's own book via ``client.get_positions(ctx=ctx)`` —
    NEVER ``api.list_positions()``. The latter returns the shared Alpaca
    conduit's AGGREGATE positions across every virtual profile on that
    account, which would leak another profile's exposure into this
    VETO-authority specialist's book-Greeks context (a profile must judge
    only its own book). Mirrors the own-book routing in
    ``adversarial_reviewer``.

    ``occ_symbol`` is carried through explicitly: a virtual ``Position``'s
    ``"symbol"`` is the UNDERLYING (not the 21-char OCC), so without it
    ``compute_book_greeks`` would misclassify every option leg as stock and
    silently drop all option Greeks. Failure-tolerant — returns [] on any
    error so the specialist still renders the rest of its prompt context.
    """
    try:
        from client import get_positions
        positions = get_positions(ctx=ctx) or []
        # Convert to plain dicts compatible with compute_book_greeks.
        # get_positions returns Position objects (dict-like .get); guard for
        # any raw object that slips through.
        out: List[Dict[str, Any]] = []
        for p in positions:
            getter = p.get if hasattr(p, "get") else None
            if getter is not None:
                occ = getter("occ_symbol")
                underlying = getter("symbol", "") or ""
                qty = float(getter("qty") or 0)
                cur = float(getter("current_price") or 0)
            else:
                occ = getattr(p, "occ_symbol", None)
                underlying = getattr(p, "symbol", "") or ""
                qty = float(getattr(p, "qty", 0) or 0)
                cur = float(getattr(p, "current_price", 0) or 0)
            out.append({
                # option legs key off the OCC; stocks off the underlying
                "symbol": occ or underlying,
                "occ_symbol": occ,
                "qty": qty,
                "current_price": cur,
            })
        return out
    except Exception as exc:
        logger.debug("own-book positions unavailable (fail-open, no Greeks "
                     "line): %s", exc)
        return []


def parse_response(raw: str) -> List[Dict[str, Any]]:
    return extract_verdict_array(raw)
