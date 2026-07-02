"""Risk-adjusted expression scoring (selection-engine P2, 2026-07-01).

Scores the STOCK expression and each OPTION-spread expression of a candidate on
ONE dimensionless axis so the AI compares them apples-to-apples and picks the
genuinely-best risk/reward trade (a healthy mix is the OUTPUT of ranking, not a
bias). See docs/SELECTION_ENGINE_DESIGN.md.

    EV$  = P_win · reward_net$ − (1 − P_win) · risk_net$
    RAR  = EV$ / risk_net$      # expected profit per dollar at risk, cost-netted

RAR only needs the reward/risk RATIO plus P_win, so it is equity-independent
except for the cost term (absolute $), which the caller nets in. These functions
are PURE (no I/O) and fail-safe — every messy input (missing P_win, unpriced
option, zero risk) resolves to a conservative number, never a crash.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def rar(p_win: float, reward_net: float, risk_net: float) -> float:
    """Risk-adjusted return = expected $ returned per $ at risk (cost-netted).

    reward_net / risk_net may be dollars OR percents as long as BOTH are the
    same unit (the ratio is what matters). Returns a very negative number when
    risk is non-positive (unsizeable → never preferred)."""
    try:
        p = _clip01(float(p_win))
        r_net = float(risk_net)
        if r_net <= 0:
            return -1.0
        return p * (float(reward_net) / r_net) - (1.0 - p)
    except Exception:
        return -1.0


def option_pop(spot: float, short_strike: float, dte_days: float, iv: float,
               right: str, is_credit: bool,
               breakeven: Optional[float] = None,
               implied_move_pct: Optional[float] = None) -> float:
    """Conservative probability-of-profit for a vertical spread — the MIN of a
    delta-based estimate and (when available) a breakeven-distance estimate, so
    optimistic analytic POP can't over-rank credit spreads. Falls back to 0.5
    (no information) when nothing is computable."""
    pops = []
    # (a) short-strike delta rule
    try:
        from options_trader import compute_greeks
        g = compute_greeks(float(spot), float(short_strike), float(dte_days),
                           float(iv), is_call=(str(right).upper() == "C"))
        if g and g.get("delta") is not None:
            d = abs(float(g["delta"]))
            # credit spread profits if the short strike finishes OTM (~1−|δ|);
            # debit spread needs to reach past it (~|δ|).
            pops.append(_clip01(1.0 - d if is_credit else d))
    except Exception as _delta_exc:
        # delta-based POP estimate unavailable → fall back to the
        # breakeven estimate / 0.5; surface for follow-up (never silent).
        logger.debug("option_pop delta estimate failed: %s: %s",
                     type(_delta_exc).__name__, _delta_exc)
    # (b) breakeven distance vs the market's own implied move (1σ ≈ implied move)
    if (breakeven is not None and implied_move_pct and spot and spot > 0):
        try:
            dist_frac = abs(float(breakeven) - float(spot)) / float(spot)
            sigma = float(implied_move_pct) / 100.0
            if sigma > 0:
                z = dist_frac / sigma  # breakeven distance in σ
                # crude but monotone: P(not breaching) rises with z. For a
                # credit spread BE is on the safe side (further = safer); for a
                # debit BE must be reached (further = harder), so invert.
                p_be = _clip01(0.5 + 0.5 * _tanh(z))
                pops.append(p_be if is_credit else _clip01(1.0 - p_be))
        except Exception as _be_exc:
            # breakeven-distance POP estimate unavailable → fall back to
            # the delta estimate / 0.5; surface for follow-up.
            logger.debug("option_pop breakeven estimate failed: %s: %s",
                         type(_be_exc).__name__, _be_exc)
    if not pops:
        return 0.5
    return min(pops)


def score_stock_opportunity(rec: Dict[str, Any], equity: float, p_win: float,
                            cost_pct: float = 0.1) -> Dict[str, Any]:
    """Opportunity object for a stock BUY/SHORT, from `stock_strategy_advisor`
    rec fields (size_pct %, stop_loss_pct %, take_profit_pct %). Dollar risk is
    materialized here for the first time (erasing the option's phantom
    'defined-risk' edge). cost_pct is round-trip cost as a % of the position."""
    size_frac = float(rec.get("size_pct", 0) or 0) / 100.0
    stop_pct = float(rec.get("stop_loss_pct", 0) or 0)
    tp_pct = float(rec.get("take_profit_pct", 0) or 0)
    ref = max(0.0, float(equity)) * size_frac          # capital-at-risk envelope
    risk = ref * (stop_pct / 100.0)
    reward = ref * (tp_pct / 100.0)
    cost = ref * (float(cost_pct) / 100.0)
    reward_net = reward - cost
    risk_net = risk + cost
    score = rar(p_win, reward_net, risk_net)
    return {
        "expression": "stock",
        "underlying": rec.get("symbol"),
        "action": rec.get("action"),
        "risk_dollars": round(risk_net, 2),
        "reward_dollars": round(reward_net, 2),
        "p_win": round(_clip01(p_win), 4),
        "rar": round(score, 4),
        "ev_dollars": round(score * risk_net, 2),
        "size_pct": rec.get("size_pct"),
        "stop_loss_pct": stop_pct,
        "take_profit_pct": tp_pct,
        "rationale": rec.get("rationale"),
    }


# Conservative per-contract, per-leg half-spread for options (USD). Options
# have far wider bid/ask spreads than stocks, so scoring a spread with ZERO
# transaction cost while the stock expression is cost-charged systematically
# over-ranks options — the exact 18:1 skew the ledger exists to prevent. This
# fixed model (no live bid/ask needed) charges each leg's half-spread on both
# open and close; ~$5/contract ≈ a $0.10-wide market on a liquid contract. It
# deliberately errs toward NOT under-charging options. P4 can refine with real
# per-leg quotes.
_OPTION_HALF_SPREAD_PER_LEG_USD = 5.0


def score_option_opportunity(rec: Dict[str, Any], equity: float, p_win: float,
                             ref_dollars: Optional[float] = None
                             ) -> Optional[Dict[str, Any]]:
    """Opportunity object for a priced option-spread rec (P1 fields:
    max_loss_per_contract, max_gain_per_contract). Sizes to the same
    capital-at-risk envelope as the stock (qty = floor(REF$/max_loss)) AND nets
    a transaction cost (per-leg half-spread, round-trip) so it ranks
    apples-to-apples with the cost-charged stock expression. Returns None when
    the rec is unsizeable (no max-loss → caller refuses it)."""
    max_loss_c = rec.get("max_loss_per_contract")
    if not max_loss_c or max_loss_c <= 0:
        return None                                    # unsizeable — refuse
    max_gain_c = rec.get("max_gain_per_contract")
    ref = ref_dollars if ref_dollars is not None else max(0.0, float(equity))
    qty = max(1, int(ref // float(max_loss_c)))
    risk = float(max_loss_c) * qty
    # unpriced max_gain (fail-open width fallback) → conservative reward=0
    reward = (float(max_gain_c) * qty) if max_gain_c else 0.0
    # Transaction cost: prefer the REAL per-leg half-spread round-trip stamped
    # by `_price_option_rec` from live quotes; fall back to a conservative fixed
    # per-leg model (per-leg half-spread × legs × open+close) when a two-sided
    # market wasn't quotable. Never zero (charging zero over-ranks options).
    rt_cost = rec.get("roundtrip_cost_per_contract")
    if rt_cost is not None and float(rt_cost) > 0:
        cost = float(rt_cost) * qty
    else:
        strikes = rec.get("strikes")
        n_legs = len(strikes) if isinstance(strikes, dict) and strikes else 2
        cost = _OPTION_HALF_SPREAD_PER_LEG_USD * n_legs * 2 * qty  # round trip
    reward_net = reward - cost
    risk_net = risk + cost
    score = rar(p_win, reward_net, risk_net)
    return {
        "expression": "option",
        "underlying": rec.get("symbol"),
        "action": "MULTILEG_OPEN",
        "strategy": rec.get("strategy"),
        "qty": qty,
        "risk_dollars": round(risk_net, 2),
        "reward_dollars": round(reward_net, 2),
        "p_win": round(_clip01(p_win), 4),
        "rar": round(score, 4),
        "ev_dollars": round(score * risk_net, 2),
        "priced": bool(rec.get("priced")),
        "breakeven": rec.get("breakeven"),
        "strikes": rec.get("strikes"),
        "expiry": rec.get("expiry"),
        "rationale": rec.get("rationale"),
    }


# --- small pure helpers (avoid a numpy dependency in the hot path) ----------

def _clip01(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return 0.5


def _tanh(x: float) -> float:
    import math
    try:
        return math.tanh(float(x))
    except Exception:
        return 0.0
