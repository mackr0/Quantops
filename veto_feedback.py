"""Option veto feedback (selection-engine P3, 2026-07-01).

Turns THIS profile's own per-(strategy x sector) option-veto history into a RAR
discount the opportunity ledger applies to each option spread BEFORE selection,
so the AI stops spending picks on spreads its own specialists will only veto
(the ~97% option-veto storm that left cash idle). Own-book only; fail-open (no
data / any error -> no discount, nothing suppressed).

Policy (design decision #3 — "partial blend, ~30 rows min, floored discount"):
a per-(strategy x sector) discount applies only after a MEANINGFUL sample
(>= _MIN_SAMPLES proposals), equals the observed veto rate P(veto), and is
CAPPED at _MAX_DISCOUNT so even a heavily-vetoed strategy keeps enough RAR that
a genuinely great trade can still surface (a floored discount, never a hard
block). P4 refines this with realized would-be outcomes — only down-rank a
(strategy x sector) where the vetoes actually avoided losses.

Data source is `journal.option_proposal_outcomes` (physically separate from
ai_predictions), so this signal can never contaminate real-trade stats.
See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 30       # decision #3: ~30 proposals minimum before we trust it
_MAX_DISCOUNT = 0.5     # cap: never discount an option's RAR by more than half
_MIN_RESOLVED = 10      # resolved would-be outcomes needed to trust veto-QUALITY
_STALE_RESOLVE_DAYS = 21  # past this many days past expiry with no priceable
                          # close, a pending row is retired 'unresolvable'


def load_veto_discounts(db_path) -> Dict[Tuple[str, str], float]:
    """Map (strategy, sector) -> discount in (0, _MAX_DISCOUNT] from this
    profile's OWN option-proposal history:

        discount = clamp( P(veto) * veto_quality , 0, _MAX_DISCOUNT )

    P(veto) is the observed veto rate for the pair (needs >= _MIN_SAMPLES
    proposals). `veto_quality` is the fraction of that pair's RESOLVED would-be
    vetoes that would actually have LOST — so once we have evidence, we ONLY
    down-rank a strategy whose vetoes genuinely avoided losses; vetoes that
    blocked would-be WINNERS shrink the discount toward 0 (the AI's specialists
    were wrong, so stop suppressing that expression). Before >= _MIN_RESOLVED
    outcomes accrue, veto_quality defaults to 1.0 (trust the veto — the P3
    prior). Own-book only; fail-open to {} on any error."""
    try:
        from journal import option_veto_counts, option_veto_quality_counts
        # veto-quality per pair (needs enough RESOLVED would-be outcomes)
        quality: Dict[Tuple[str, str], float] = {}
        for strategy, sector, resolved, losses in \
                option_veto_quality_counts(db_path):
            if resolved >= _MIN_RESOLVED:
                quality[(str(strategy), str(sector or ""))] = (
                    (losses / resolved) if resolved > 0 else 1.0)
        out: Dict[Tuple[str, str], float] = {}
        for strategy, sector, vetoed, total in option_veto_counts(db_path):
            if not strategy or total < _MIN_SAMPLES:
                continue
            key = (str(strategy), str(sector or ""))
            p_veto = (vetoed / total) if total > 0 else 0.0
            q = quality.get(key, 1.0)   # trust the veto until proven wrong
            discount = min(_MAX_DISCOUNT, max(0.0, p_veto * q))
            if discount > 0:
                out[key] = round(discount, 4)
        return out
    except Exception as exc:
        logger.debug("load_veto_discounts unavailable (fail-open): %s", exc)
        return {}


def _intrinsic_expiry_pnl(strategy, lo, hi, max_loss, max_gain, spot):
    """Would-be P&L per contract of a VERTICAL spread at expiry, from the
    underlying close alone: piecewise-linear, clamped to [-max_loss, +max_gain]
    between the two strikes. bull_* profit rises with spot; bear_* falls. None
    for a non-directional strategy (iron_condor/strangle — not resolved here).
    """
    try:
        lo = float(lo); hi = float(hi); spot = float(spot)
        ml = float(max_loss); mg = float(max_gain)
        if hi <= lo:
            return None
        strat = str(strategy or "")
        if strat.startswith("bull"):
            frac = max(0.0, min(1.0, (spot - lo) / (hi - lo)))
        elif strat.startswith("bear"):
            frac = max(0.0, min(1.0, (hi - spot) / (hi - lo)))
        else:
            return None
        return round(-ml + frac * (ml + mg), 2)
    except Exception:
        return None


def resolve_option_proposal_outcomes(db_path, spot_lookup, today):
    """Resolve pending VETOED option-proposal rows to their TRUE would-be P&L
    once expiry has passed, from the underlying's expiry-day close (intrinsic —
    no illiquid near-expiry option marks). `spot_lookup(symbol, expiry_str)`
    returns the underlying close on/at expiry, or None to leave the row pending
    (retry next cadence). `today` is 'YYYY-MM-DD' (injected — no wall clock).
    Own-book (db_path); fail-open. Returns the count resolved."""
    if not db_path or not today or spot_lookup is None:
        return 0
    try:
        from journal import (pending_veto_outcomes, mark_veto_outcome_resolved,
                             mark_veto_outcome_unresolvable)
    except Exception as exc:
        logger.warning("veto-outcome resolver unavailable: %s", exc)
        return 0
    from datetime import date as _date
    try:
        today_d = _date.fromisoformat(str(today)[:10])
    except Exception as exc:
        logger.warning("veto-outcome resolver: bad today=%r: %s", today, exc)
        return 0

    def _retire_if_stale(row):
        """A pending row well past expiry that still can't be priced (delisted
        underlying / non-trading-day expiry) will never resolve — retire it so
        it neither re-queries forever nor pollutes the signal."""
        try:
            exp_d = _date.fromisoformat(str(row.get("expiry"))[:10])
            if (today_d - exp_d).days > _STALE_RESOLVE_DAYS:
                mark_veto_outcome_unresolvable(db_path, row.get("id"))
                logger.info("veto outcome id=%s retired unresolvable "
                            "(%s %s exp %s)", row.get("id"), row.get("symbol"),
                            row.get("strategy"), row.get("expiry"))
        except Exception as exc:
            logger.debug("staleness check failed for id=%s: %s",
                         row.get("id"), exc)

    resolved = 0
    for row in pending_veto_outcomes(db_path, today):
        try:
            spot = spot_lookup(row.get("symbol"), row.get("expiry"))
            pnl = None
            if spot is not None and float(spot) > 0:
                pnl = _intrinsic_expiry_pnl(
                    row.get("strategy"), row.get("lo_strike"),
                    row.get("hi_strike"), row.get("max_loss_per_contract"),
                    row.get("max_gain_per_contract"), spot)
                if pnl is None:
                    # priced vertical that failed the payoff formula (malformed
                    # data) — surface it, then let staleness retire it.
                    logger.debug("veto outcome id=%s priced but unpriceable "
                                 "P&L (%s lo=%s hi=%s): will retire if stale",
                                 row.get("id"), row.get("strategy"),
                                 row.get("lo_strike"), row.get("hi_strike"))
            if pnl is None:
                _retire_if_stale(row)           # data not ready OR malformed
                continue
            outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "neutral")
            if mark_veto_outcome_resolved(db_path, row.get("id"),
                                          outcome=outcome, pnl=pnl):
                resolved += 1
        except Exception as exc:
            logger.warning("resolve veto outcome id=%s failed: %s",
                           row.get("id"), exc)
    return resolved


def discount_for(discounts: Dict[Tuple[str, str], float],
                 strategy: Any, sector: Any) -> float:
    """Look up the RAR discount for a (strategy, sector) pair; 0.0 when absent
    or when `discounts` is empty."""
    if not discounts:
        return 0.0
    return float(discounts.get((str(strategy or ""), str(sector or "")), 0.0))


def apply_veto_discount(rar: float, discount: float) -> float:
    """Apply a veto discount to an option's RAR. Only POSITIVE RAR is reduced
    (a spread likely to be vetoed is worth proportionally less because it
    probably won't execute); a negative RAR is left unchanged — discounting it
    would move it toward zero and RANK IT HIGHER, which is backwards."""
    try:
        r = float(rar)
        d = min(_MAX_DISCOUNT, max(0.0, float(discount)))
        if r > 0 and d > 0:
            return round(r * (1.0 - d), 4)
        return r
    except Exception:
        return rar
