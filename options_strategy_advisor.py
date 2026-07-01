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
# IV regime thresholds with a deliberate NEUTRAL DEAD ZONE. When IV
# rank falls into the 45-60 band, no multileg recommendation is
# generated for the candidate — the AI must then evaluate the
# candidate as a stock opportunity (or skip).
#
# 2026-05-14 — restored the dead zone (was 55/55 / no dead zone since
# 2026-05-12). The no-dead-zone configuration caused EVERY candidate
# with IV data to receive a pre-built multileg recommendation. The AI,
# faced with a pre-analyzed options strategy next to a bare stock
# candidate, picked the options strategy nearly every time. Result:
# stock BUY signals collapsed from ~24/day (Apr 30) to 0/day (May 13)
# while multileg proposals grew. Confirmed via the 14-day audit.
#
# Important caveat: this is a band-aid. The proper fix is to evaluate
# stock-action and options-action as INDEPENDENT opportunity streams
# rather than competing alternatives for a single candidate slot. The
# dead zone reduces but doesn't eliminate the asymmetry. Tracked as
# Phase 2 self-tuner architecture work.
#
# Tunable per profile via ctx (`option_iv_rich_threshold`,
# `option_iv_cheap_threshold`); self-tuner can adjust but must maintain
# a ≥10-point dead zone — enforced by
# `tests/test_multileg_iv_dead_zone.py`.
MULTILEG_IV_RICH_THRESHOLD = 60.0
MULTILEG_IV_CHEAP_THRESHOLD = 45.0
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


# --- P1 (2026-07-01, selection-engine design): price option recs so the
# risk-adjusted scorer has real max-loss/gain/breakeven, not just strikes.
# See docs/SELECTION_ENGINE_DESIGN.md.
_PREMIUM_CACHE: Dict[Any, Any] = {}
_PREMIUM_CACHE_TTL = 45.0  # seconds


def _cached_option_premium(occ_symbol: str, side: str) -> float:
    """`client._fetch_option_premium` with a short TTL cache (avoids re-hitting
    Alpaca for the same contract within a prompt build / adjacent cycles).
    Market data only — own-book safe. Returns 0.0 on any failure."""
    import time as _t
    key = (occ_symbol, side)
    now = _t.time()
    hit = _PREMIUM_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _PREMIUM_CACHE_TTL:
        return hit[1]
    try:
        from client import _fetch_option_premium
        prem = float(_fetch_option_premium(occ_symbol, side=side) or 0.0)
    except Exception:
        prem = 0.0
    _PREMIUM_CACHE[key] = (now, prem)
    return prem


def _price_option_rec(rec: Dict[str, Any]) -> None:
    """Attach real dollar max-loss/max-gain/breakeven to a VERTICAL-spread rec
    by pricing its two legs. Fail-open: on ANY failure keep the CONSERVATIVE
    width×$100 max-loss (always >= the true loss) and leave max_gain/breakeven
    None, so the scorer treats it conservatively and never admits unpriced risk
    at $0. Non-verticals (condor/strangle) get the width fallback only (P1
    scope). Own-book safe (market data only)."""
    strat = rec.get("strategy", "")
    strikes = rec.get("strikes") or {}
    short_k = strikes.get("short")
    long_k = strikes.get("long")
    ks = [float(v) for v in strikes.values() if v is not None]
    width = (abs(float(short_k) - float(long_k))
             if short_k is not None and long_k is not None
             else (max(ks) - min(ks) if len(ks) >= 2 else 0.0))
    is_credit = strat in ("bull_put_spread", "bear_call_spread")
    rec["spread_width_points"] = width
    rec["is_credit"] = is_credit
    if short_k is not None:
        rec["short_strike"] = float(short_k)
    # Conservative fallback — always set (the scorer relies on this).
    rec["max_loss_per_contract"] = width * 100.0 if width > 0 else None
    rec["max_gain_per_contract"] = None
    rec["breakeven"] = None
    rec["priced"] = False
    if short_k is None or long_k is None:
        return  # non-vertical — width fallback only
    try:
        from datetime import date as _date
        from options_trader import format_occ_symbol
        from options_multileg import _vertical_pl_bounds
        right = "P" if strat in ("bull_put_spread", "bear_put_spread") else "C"
        exp = _date.fromisoformat(rec["expiry"])
        occ_short = format_occ_symbol(rec["symbol"], exp, float(short_k), right)
        occ_long = format_occ_symbol(rec["symbol"], exp, float(long_k), right)
        p_short = _cached_option_premium(occ_short, "sell")
        p_long = _cached_option_premium(occ_long, "buy")
        if p_short <= 0 or p_long <= 0:
            return  # untrusted marks — keep width fallback
        net_prem = abs(p_short - p_long)  # per-share, positive
        bounds = _vertical_pl_bounds(width, net_prem, is_credit)
        ml = bounds.get("max_loss_per_contract")
        mg = bounds.get("max_gain_per_contract")
        if ml and ml > 0:
            rec["max_loss_per_contract"] = float(ml)
            rec["max_gain_per_contract"] = float(mg) if mg else None
            if is_credit:
                rec["breakeven"] = (float(short_k) - net_prem if right == "P"
                                    else float(short_k) + net_prem)
            else:
                rec["breakeven"] = (float(long_k) + net_prem if right == "C"
                                    else float(long_k) - net_prem)
            rec["priced"] = True
            # Entry economics for the veto-feedback shadow resolver (P4): net
            # premium per contract + the two legs (occ + side as the AI would
            # trade them). Harmless extra fields for the ledger's P1 use.
            rec["entry_net_premium"] = round(net_prem * 100.0, 2)
            rec["legs"] = [{"occ": occ_short, "side": "sell"},
                           {"occ": occ_long, "side": "buy"}]
    except Exception as exc:
        logger.debug("option rec pricing failed (fail-open, width fallback): "
                     "%s", exc)


def evaluate_candidate_for_multileg(
    candidate: Dict[str, Any],
    iv_rank_pct: Optional[float] = None,
    regime: Optional[str] = None,
    ctx: Any = None,
    held: Optional[set] = None,
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
        held: this profile's OWN held underlyings (uppercased set). When
            the candidate's underlying is already on the book, return no
            recs — proposing an option spread on a name already held is the
            redundant long / net-zero-wash the adversarial_reviewer vetoes
            ~every cycle, so we suppress it BEFORE the prompt and the veto
            (own-book only; isolation-safe). None → no filtering (the menu
            renders exactly as before).

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
    # Own-book concentration guard (isolation-safe — `held` is THIS
    # profile's own underlyings only). Suppress option proposals on names
    # already on the book; otherwise the generator re-proposes them every
    # cycle and the adversarial_reviewer vetoes them post-selection,
    # burning an LLM round-trip each time.
    if held and str(symbol).upper() in held:
        return recs
    if iv_rank_pct is None:
        # Don't recommend any IV-conditional strategy without IV data
        return recs

    # 2026-05-12 — read tunable thresholds from ctx; fall back to
    # module defaults. With rich==cheap default (55/55), every IV
    # value triggers exactly one of the two branches (no dead zone).
    iv_rich_thresh = float(getattr(
        ctx, "option_iv_rich_threshold", MULTILEG_IV_RICH_THRESHOLD
    ) if ctx is not None else MULTILEG_IV_RICH_THRESHOLD)
    iv_cheap_thresh = float(getattr(
        ctx, "option_iv_cheap_threshold", MULTILEG_IV_CHEAP_THRESHOLD
    ) if ctx is not None else MULTILEG_IV_CHEAP_THRESHOLD)
    is_iv_rich = iv_rank_pct >= iv_rich_thresh
    is_iv_cheap = iv_rank_pct <= iv_cheap_thresh
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

    # P1 — price every rec (real max-loss/gain/breakeven; fail-open to the
    # conservative width×$100), so the risk-adjusted scorer ranks on real
    # numbers rather than concreteness.
    for _r in recs:
        _price_option_rec(_r)
    return recs


def _own_book_held_underlyings(ctx: Any) -> set:
    """This profile's OWN held underlyings (stock + option legs), uppercased.

    Isolation-safe: reads ONLY ``ctx``'s own virtual book via
    ``client.get_positions(ctx=ctx)`` (same own-book routing the
    adversarial_reviewer uses) — never the shared Alpaca conduit aggregate.
    Returns an empty set when concentration-awareness is off or anything is
    unavailable (fail-open: a missing held set must never block proposals).
    """
    try:
        import config as _cfg
        if not getattr(_cfg, "ENABLE_CONCENTRATION_AWARE", False):
            return set()
        from client import get_positions
        from book_fit import held_underlyings
        positions = get_positions(ctx=ctx) or []
        return {h.upper() for h in held_underlyings(positions) if h}
    except Exception as exc:
        logger.debug("own-book held-underlyings unavailable (fail-open, no "
                     "concentration filtering this build): %s", exc)
        return set()


def _options_budget_exhausted(ctx: Any) -> bool:
    """True when this profile's OPEN options capital-at-risk already meets or
    exceeds its max_options_risk_pct-of-NAV budget — so no new spread could be
    acted on. Lets the prompt suppress the whole options menu rather than
    offer trades the execution budget gate would only reject ("proposed but
    can't be acted on"). Own-book only (isolation-safe). Fail-open (False) on
    any error or when the budget gate is off."""
    try:
        risk_pct = getattr(ctx, "max_options_risk_pct", None)
        if not risk_pct or risk_pct <= 0:
            return False
        from client import get_account_info
        from journal import open_options_capital_at_risk
        equity = float((get_account_info(ctx=ctx) or {}).get("equity") or 0)
        if equity <= 0:
            equity = float(getattr(ctx, "initial_capital", 0) or 0)
        if equity <= 0:
            return False
        open_ml = open_options_capital_at_risk(getattr(ctx, "db_path", None))
        return open_ml >= risk_pct * equity
    except Exception as exc:
        logger.debug("options budget check unavailable (fail-open): %s", exc)
        return False


# NOTE (2026-07-01, selection-engine P2b): the standalone
# render_multileg_recs_for_prompt block was removed. Candidate option
# recommendations are now scored on the risk-adjusted axis and rendered
# INTERLEAVED with stock recs by `opportunity_ledger.render_opportunity_ledger`
# (which calls `evaluate_candidate_for_multileg` below with the same
# own-book / budget / IV gating). See docs/SELECTION_ENGINE_DESIGN.md.


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
            except Exception as exc:
                logger.debug("iv_rank_lookup(%s) failed (fail-open, "
                             "IV-conditional strategies skipped): %s", sym, exc)
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
