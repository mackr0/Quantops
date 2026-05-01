"""Options strategy advisor — surfaces option-strategy opportunities
on existing positions to the AI prompt.

Item 1a continued (COMPETITIVE_GAP_PLAN.md). The foundation in
options_trader.py provides the math + order primitives; this module
decides WHEN each strategy makes sense given current portfolio state
and IV regime.

Strategy rules (Phase 1 — single-leg only):

  COVERED_CALL when:
    - position is long ≥ 100 shares
    - position is at +5%+ unrealized gain (locking in some upside)
    - IV rank > 70 (premium is rich — getting paid to cap upside)
    - 30-45 days to expiry recommended (theta sweet spot)
    - Strike: ~5-10% above current price (don't cap too tight)

  PROTECTIVE_PUT when:
    - position is long ≥ 100 shares
    - position is at +10%+ unrealized gain (worth protecting)
    - OR: position is in the largest 25% by dollar exposure
    - 30-60 days to expiry
    - Strike: ~5% below current price (insurance, not lottery)

  CASH_SECURED_PUT when:
    - profile has free buying power
    - target name has IV rank > 70 (rich premium)
    - target name's price is at +20% from 52-week low (NOT crashing)

  LONG_PUT (outright bearish hedge or speculative):
    - When AI proposes SHORT but profile prefers defined-risk
    - When market regime is bearish (correlation crash hedge)

The advisor RECOMMENDS — it doesn't execute. The AI sees the
recommendation in its prompt and decides whether to take the trade.
This keeps the human (or AI) in the decision loop until we're
confident in the auto-execution layer.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Tunable thresholds. Documented inline so a future reader sees the
# rationale; tunable via ctx.* later when self-tuner is wired.
#
# COVERED CALL — sell premium against an existing winner. Two gates:
#   gain  ≥ 15%  : the position is a real winner. Capping further upside
#                  via the strike is acceptable because we've already
#                  captured material P&L. (Lower gates risk capping a
#                  momentum runner too early — typical pro programs use
#                  +15-20% as the floor.)
#   IVrank ≥ 60  : the option premium is "rich" relative to recent
#                  history (above-median). Selling rich is the whole
#                  thesis. 70 is "extreme" and rarely fires; 60 is the
#                  industry-standard "above median, write now" threshold.
COVERED_CALL_MIN_GAIN_PCT = 15.0
COVERED_CALL_MIN_IV_RANK = 60.0
COVERED_CALL_STRIKE_PCT_ABOVE = 7.0
COVERED_CALL_TARGET_DAYS_TO_EXPIRY = 35

# PROTECTIVE PUT — buy downside insurance on a winner. Three gates:
#   gain ≥ 10%   : enough at risk to make insurance worthwhile.
#   IVrank ≤ 50  : premium is "cheap" (below-median). Buying expensive
#                  insurance defeats the purpose — if IV is rich, defer
#                  and let it normalize. This gate was MISSING before
#                  the 2026-05-01 calibration; recommendations could
#                  fire at IV rank 95 (peak fear → puts overpriced).
#   IVrank None  : when we can't read IV, skip the strategy. Don't
#                  guess on insurance.
PROTECTIVE_PUT_MIN_GAIN_PCT = 10.0
PROTECTIVE_PUT_MAX_IV_RANK = 50.0
PROTECTIVE_PUT_STRIKE_PCT_BELOW = 5.0
PROTECTIVE_PUT_TARGET_DAYS_TO_EXPIRY = 45

# Phase B3 — multi-leg advisor. Strategy selection rules differ from
# the single-leg advisor (which targets HELD positions); multi-leg
# advisor targets CANDIDATES the screener has surfaced. Distinct
# constants so they can be tuned independently.
#
# IV regime thresholds — same definition (above 60 = rich, below 50 =
# cheap) as the single-leg advisor. The "neutral" 50-60 band falls
# through with no recommendation (not enough edge in either direction).
MULTILEG_IV_RICH_THRESHOLD = 60.0
MULTILEG_IV_CHEAP_THRESHOLD = 50.0
MULTILEG_VERTICAL_STRIKE_PCT_OTM = 5.0  # short leg sits this far OTM
MULTILEG_VERTICAL_WIDTH_PCT = 5.0       # long leg this far past short
MULTILEG_TARGET_DAYS_TO_EXPIRY = 35     # sweet spot: theta capture + liquid

# Iron condor (range-bound) — short legs sit ±this OTM
MULTILEG_CONDOR_INNER_PCT = 5.0
MULTILEG_CONDOR_OUTER_PCT = 10.0  # wing legs further out

# Long strangle (vol-expansion) — symmetric distance from spot
MULTILEG_STRANGLE_OTM_PCT = 7.0


def _next_friday(min_days: int) -> date:
    """Return the Friday at least `min_days` from today (most options
    expire Friday). Approximation — use Alpaca/yfinance available
    expiries when actually placing."""
    today = date.today()
    target = today + timedelta(days=min_days)
    days_to_friday = (4 - target.weekday()) % 7
    return target + timedelta(days=days_to_friday)


def _round_strike(price: float) -> float:
    """Round to a sane strike interval. <$25: $0.50; <$200: $1; else $5."""
    if price < 25:
        return round(price * 2) / 2
    if price < 200:
        return round(price)
    return round(price / 5) * 5


def evaluate_position_for_strategies(
    position: Dict[str, Any],
    iv_rank_pct: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """For a held position, return list of options-strategy recommendations.

    Args:
      position: dict with at least symbol, qty, avg_entry_price,
        current_price, unrealized_plpc (percent gain).
      iv_rank_pct: 0-100 IV rank for this symbol (from options_oracle).
        None means we don't know — skip IV-conditional strategies.

    Returns list of recommendation dicts (may be empty). Each rec is a
    candidate; the AI decides which to take.
    """
    recs: List[Dict[str, Any]] = []
    symbol = position.get("symbol")
    qty = float(position.get("qty", 0))
    current_price = float(position.get("current_price", 0))
    entry_price = float(position.get("avg_entry_price", 0))
    if not symbol or qty <= 0 or current_price <= 0 or entry_price <= 0:
        return recs

    # Need at least 100 shares for any strategy involving the existing position
    if qty < 100:
        return recs

    gain_pct = (current_price - entry_price) / entry_price * 100

    # COVERED CALL — long position with rich IV, locking in some upside
    if (gain_pct >= COVERED_CALL_MIN_GAIN_PCT
            and iv_rank_pct is not None
            and iv_rank_pct >= COVERED_CALL_MIN_IV_RANK):
        strike = _round_strike(
            current_price * (1 + COVERED_CALL_STRIKE_PCT_ABOVE / 100)
        )
        expiry = _next_friday(COVERED_CALL_TARGET_DAYS_TO_EXPIRY)
        contracts = int(qty // 100)
        if contracts > 0:
            recs.append({
                "strategy": "covered_call",
                "symbol": symbol,
                "shares_held": int(qty),
                "contracts": contracts,
                "strike": strike,
                "expiry": expiry.isoformat(),
                "rationale": (
                    f"Long {int(qty)} shares of {symbol} at +{gain_pct:.1f}% "
                    f"gain. IV rank {iv_rank_pct:.0f} — premium is rich. "
                    f"Selling {contracts}× {expiry.isoformat()} ${strike:.2f} "
                    f"call captures premium income while keeping ~{COVERED_CALL_STRIKE_PCT_ABOVE}% "
                    f"of upside before being capped."
                ),
            })

    # PROTECTIVE PUT — substantial gain at risk + cheap insurance.
    # Need both: enough gain that the insurance is worth buying AND
    # IV cheap enough that we're not overpaying. Skip when IV unknown.
    if (gain_pct >= PROTECTIVE_PUT_MIN_GAIN_PCT
            and iv_rank_pct is not None
            and iv_rank_pct <= PROTECTIVE_PUT_MAX_IV_RANK):
        strike = _round_strike(
            current_price * (1 - PROTECTIVE_PUT_STRIKE_PCT_BELOW / 100)
        )
        expiry = _next_friday(PROTECTIVE_PUT_TARGET_DAYS_TO_EXPIRY)
        contracts = int(qty // 100)
        if contracts > 0:
            recs.append({
                "strategy": "protective_put",
                "symbol": symbol,
                "shares_held": int(qty),
                "contracts": contracts,
                "strike": strike,
                "expiry": expiry.isoformat(),
                "rationale": (
                    f"Long {int(qty)} shares of {symbol} at +{gain_pct:.1f}% "
                    f"gain — substantial unrealized P&L worth protecting. "
                    f"IV rank {iv_rank_pct:.0f} — premium is cheap. "
                    f"Buying {contracts}× {expiry.isoformat()} ${strike:.2f} "
                    f"put caps downside at ~{PROTECTIVE_PUT_STRIKE_PCT_BELOW}% "
                    f"below current ({current_price:.2f}) for the cost of "
                    f"the premium."
                ),
            })

    return recs


def evaluate_candidate_for_multileg(
    candidate: Dict[str, Any],
    iv_rank_pct: Optional[float] = None,
    regime: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """For a screener candidate (NOT a held position), return list of
    multi-leg strategy recommendations. AI takes it from there.

    Args:
        candidate: dict with at least symbol, signal (BUY/SELL/HOLD),
            price (current/last close), and optionally a 'volatility_view'
            ("expansion" / "contraction" / None) hint.
        iv_rank_pct: 0-100 IV rank. None → most multi-leg strategies
            skipped (we don't price-blind on premium).
        regime: market regime hint. "ranging" pushes toward iron
            condors; "trending" pushes toward verticals.

    Strategy selection logic:
      Bullish (signal in BUY/STRONG_BUY) + IV rich      → bull_put_spread (credit)
      Bullish + IV cheap                                  → bull_call_spread (debit)
      Bearish (signal in SELL/STRONG_SELL/SHORT) + IV rich → bear_call_spread (credit)
      Bearish + IV cheap                                  → bear_put_spread (debit)
      HOLD/neutral + IV rich + ranging regime             → iron_condor (credit)
      HOLD/neutral + IV cheap + expansion expected        → long_strangle (debit)

    Returns list of recs (may be empty). Each rec includes strategy
    name, suggested strikes/expiry/qty, max_loss/max_gain estimates,
    and a rationale string the AI sees.
    """
    recs: List[Dict[str, Any]] = []
    symbol = candidate.get("symbol")
    signal = (candidate.get("signal") or "").upper()
    price = float(candidate.get("price") or 0)
    if not symbol or price <= 0:
        return recs
    if iv_rank_pct is None:
        # Don't recommend any IV-conditional strategy without IV data
        return recs

    is_iv_rich = iv_rank_pct >= MULTILEG_IV_RICH_THRESHOLD
    is_iv_cheap = iv_rank_pct <= MULTILEG_IV_CHEAP_THRESHOLD
    is_bullish = signal in ("BUY", "STRONG_BUY")
    is_bearish = signal in ("SELL", "STRONG_SELL", "SHORT", "STRONG_SHORT")
    expiry = _next_friday(MULTILEG_TARGET_DAYS_TO_EXPIRY)

    # Bullish credit: bull put spread — sell premium below the money
    if is_bullish and is_iv_rich:
        short_strike = _round_strike(
            price * (1 - MULTILEG_VERTICAL_STRIKE_PCT_OTM / 100))
        long_strike = _round_strike(
            short_strike - price * MULTILEG_VERTICAL_WIDTH_PCT / 100)
        if long_strike < short_strike:  # avoid degenerate strikes
            recs.append({
                "strategy": "bull_put_spread",
                "symbol": symbol,
                "expiry": expiry.isoformat(),
                "strikes": {"short": short_strike, "long": long_strike},
                "rationale": (
                    f"Bullish on {symbol} (signal={signal}); IV rank "
                    f"{iv_rank_pct:.0f} → premium rich. Short ${short_strike:.2f}P / "
                    f"long ${long_strike:.2f}P (5% OTM, $5-wide). "
                    f"Defined-risk credit; profits if {symbol} stays "
                    f"above ${short_strike:.2f}."
                ),
            })

    # Bullish debit: bull call spread — buy upside on cheap IV
    if is_bullish and is_iv_cheap:
        long_strike = _round_strike(
            price * (1 + MULTILEG_VERTICAL_STRIKE_PCT_OTM / 100))
        short_strike = _round_strike(
            long_strike + price * MULTILEG_VERTICAL_WIDTH_PCT / 100)
        if short_strike > long_strike:
            recs.append({
                "strategy": "bull_call_spread",
                "symbol": symbol,
                "expiry": expiry.isoformat(),
                "strikes": {"long": long_strike, "short": short_strike},
                "rationale": (
                    f"Bullish on {symbol} (signal={signal}); IV rank "
                    f"{iv_rank_pct:.0f} → premium cheap. Long ${long_strike:.2f}C / "
                    f"short ${short_strike:.2f}C (5% OTM, $5-wide). "
                    f"Defined-risk debit; max gain if {symbol} runs to "
                    f"${short_strike:.2f}+."
                ),
            })

    # Bearish credit: bear call spread
    if is_bearish and is_iv_rich:
        short_strike = _round_strike(
            price * (1 + MULTILEG_VERTICAL_STRIKE_PCT_OTM / 100))
        long_strike = _round_strike(
            short_strike + price * MULTILEG_VERTICAL_WIDTH_PCT / 100)
        if long_strike > short_strike:
            recs.append({
                "strategy": "bear_call_spread",
                "symbol": symbol,
                "expiry": expiry.isoformat(),
                "strikes": {"short": short_strike, "long": long_strike},
                "rationale": (
                    f"Bearish on {symbol} (signal={signal}); IV rank "
                    f"{iv_rank_pct:.0f} → premium rich. Short ${short_strike:.2f}C / "
                    f"long ${long_strike:.2f}C (5% OTM, $5-wide). "
                    f"Defined-risk credit; profits if {symbol} stays "
                    f"below ${short_strike:.2f}."
                ),
            })

    # Bearish debit: bear put spread
    if is_bearish and is_iv_cheap:
        long_strike = _round_strike(
            price * (1 - MULTILEG_VERTICAL_STRIKE_PCT_OTM / 100))
        short_strike = _round_strike(
            long_strike - price * MULTILEG_VERTICAL_WIDTH_PCT / 100)
        if long_strike > short_strike:
            recs.append({
                "strategy": "bear_put_spread",
                "symbol": symbol,
                "expiry": expiry.isoformat(),
                "strikes": {"long": long_strike, "short": short_strike},
                "rationale": (
                    f"Bearish on {symbol} (signal={signal}); IV rank "
                    f"{iv_rank_pct:.0f} → premium cheap. Long ${long_strike:.2f}P / "
                    f"short ${short_strike:.2f}P (5% OTM, $5-wide). "
                    f"Defined-risk debit; max gain if {symbol} drops to "
                    f"${short_strike:.2f} or lower."
                ),
            })

    # Neutral credit: iron condor — only fires when explicitly ranging
    if (signal in ("HOLD", "")
            and is_iv_rich
            and (regime or "").lower() in ("ranging", "neutral", "range_bound")):
        put_short = _round_strike(
            price * (1 - MULTILEG_CONDOR_INNER_PCT / 100))
        put_long = _round_strike(
            price * (1 - MULTILEG_CONDOR_OUTER_PCT / 100))
        call_short = _round_strike(
            price * (1 + MULTILEG_CONDOR_INNER_PCT / 100))
        call_long = _round_strike(
            price * (1 + MULTILEG_CONDOR_OUTER_PCT / 100))
        if put_long < put_short < call_short < call_long:
            recs.append({
                "strategy": "iron_condor",
                "symbol": symbol,
                "expiry": expiry.isoformat(),
                "strikes": {
                    "put_long": put_long, "put_short": put_short,
                    "call_short": call_short, "call_long": call_long,
                },
                "rationale": (
                    f"Range-bound on {symbol}; IV rank {iv_rank_pct:.0f} "
                    f"→ premium rich. Sell ${put_short:.2f}P/${call_short:.2f}C, "
                    f"protect at ${put_long:.2f}P/${call_long:.2f}C. "
                    f"Profits if {symbol} stays between ${put_short:.2f} "
                    f"and ${call_short:.2f}."
                ),
            })

    # Long-vol: long strangle when expansion expected and IV cheap
    if (signal in ("HOLD", "")
            and is_iv_cheap
            and candidate.get("volatility_view") == "expansion"):
        put_strike = _round_strike(
            price * (1 - MULTILEG_STRANGLE_OTM_PCT / 100))
        call_strike = _round_strike(
            price * (1 + MULTILEG_STRANGLE_OTM_PCT / 100))
        if put_strike < call_strike:
            recs.append({
                "strategy": "long_strangle",
                "symbol": symbol,
                "expiry": expiry.isoformat(),
                "strikes": {"put": put_strike, "call": call_strike},
                "rationale": (
                    f"Vol expansion expected on {symbol}; IV rank "
                    f"{iv_rank_pct:.0f} → premium cheap. Long "
                    f"${put_strike:.2f}P / ${call_strike:.2f}C. "
                    f"Profits on a big move in either direction."
                ),
            })

    return recs


def render_multileg_recs_for_prompt(
    candidates: List[Dict[str, Any]],
    iv_rank_lookup=None,
    regime: Optional[str] = None,
) -> str:
    """Build the MULTI-LEG STRATEGIES prompt block.

    iv_rank_lookup: callable(symbol) → IV rank 0-100 or None.
    regime: market regime ("ranging", "trending", etc.) — gates
        iron_condor recommendations.

    Output looks like:
      MULTI-LEG STRATEGIES (defined-risk options the AI may propose
      via PAIR_TRADE-style action — the AI decides whether to take
      any of these or stick with simple long/short):
        - AAPL: bull_put_spread expiring 2026-05-30 (sell $145P / buy $140P)
              — Rationale: ...
        - ...

    Returns empty string when there are no actionable recs.
    """
    if not candidates:
        return ""
    all_recs: List[Dict[str, Any]] = []
    for c in candidates:
        sym = c.get("symbol")
        iv_rank = None
        if iv_rank_lookup is not None and sym:
            try:
                iv_rank = iv_rank_lookup(sym)
            except Exception:
                iv_rank = None
        recs = evaluate_candidate_for_multileg(
            c, iv_rank_pct=iv_rank, regime=regime,
        )
        all_recs.extend(recs)

    if not all_recs:
        return ""

    lines = [
        "MULTI-LEG OPTIONS STRATEGIES (defined-risk; AI may propose "
        "via MULTILEG_OPEN action):"
    ]
    for r in all_recs[:8]:  # cap so prompt doesn't bloat
        lines.append(
            f"  - {r['symbol']} {r['strategy']} ({r['expiry']})"
        )
        lines.append(f"      Rationale: {r['rationale']}")
    if len(all_recs) > 8:
        lines.append(f"  ... and {len(all_recs) - 8} more")
    return "\n".join(lines)


def render_for_prompt(
    positions: List[Dict[str, Any]],
    iv_rank_lookup=None,
) -> str:
    """Build the OPTIONS STRATEGIES prompt block.

    iv_rank_lookup: optional callable(symbol) -> Optional[float] returning
    0-100 IV rank for a symbol. When None, IV-conditional strategies are
    skipped (we don't pretend to know IV).
    """
    if not positions:
        return ""
    all_recs: List[Dict[str, Any]] = []
    for pos in positions:
        sym = pos.get("symbol")
        iv_rank = None
        if iv_rank_lookup is not None and sym:
            try:
                iv_rank = iv_rank_lookup(sym)
            except Exception:
                iv_rank = None
        recs = evaluate_position_for_strategies(pos, iv_rank)
        all_recs.extend(recs)

    if not all_recs:
        return ""

    lines = ["\nOPTIONS STRATEGIES (recommended given current positions):"]
    for r in all_recs[:5]:  # cap at 5 to avoid prompt bloat
        lines.append(f"  • {r['strategy'].upper()} {r['symbol']}: "
                      f"{r['contracts']}× {r['expiry']} ${r['strike']:.2f} — "
                      f"{r['rationale']}")
    if len(all_recs) > 5:
        lines.append(f"  • ...and {len(all_recs) - 5} more not shown")
    lines.append(
        "  These are recommendations only — propose them in your "
        "trades list with action='OPTIONS' if you want to execute."
    )
    return "\n".join(lines) + "\n"
