"""Option-aware return % computation (Phase 5c of pipeline refactor).

Replaces Phase 5b's safety-floor deferral with actual option-economics
math. Two paths:

  Single-leg (signal IN ('OPTIONS', 'OPTION_EXERCISE') with
  occ_symbol populated):
    return_pct = (current_premium - entry_premium) / entry_premium * 100

  Multileg (signal == 'MULTILEG_OPEN' with option_order_id populated):
    current_spread_value = sum(current_premium * qty for each leg)
    entry_spread_value   = sum(entry_premium * qty for each leg)
    return_pct = (current_spread_value - entry_spread_value)
                 / abs(entry_spread_value) * 100

The signs of qty (long > 0, short < 0) make a credit spread's entry
value negative (received credit) and a debit spread's entry value
positive (paid debit). The return_pct measures movement away from
entry as a percentage of the absolute entry value — same direction
semantics as the stock case.

Win/loss thresholds:
  - Stocks resolve win/loss at ±2% return.
  - Options swing 10-100× more violently per unit underlying move.
    A 25% premium swing is roughly equivalent to a 2% stock swing
    in terms of "did the AI's directional thesis play out."

  Single-leg long premium: win at +25% return, loss at -25%.
  Single-leg short premium (qty<0): inverted — win at -25%, loss at +25%.
  Multileg: win at +25% of entry magnitude (P&L > 25% of entry),
    loss at -50% (we lost half the spread's max-credit/debit). The
    asymmetric thresholds reflect the asymmetric P&L of spreads.

Returns None when the resolver can't compute (no occ_symbol/order_id,
premium fetch failed). Caller defers to Phase 5b safety floor — row
stays pending.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Win/loss thresholds (percentage points of return).
OPTION_WIN_PCT_LONG = 25.0     # long premium gain ≥ +25% → win
OPTION_LOSS_PCT_LONG = -25.0   # long premium loss ≥ -25% → loss
OPTION_WIN_PCT_SHORT = -25.0   # short premium gain (theta wins) → win
OPTION_LOSS_PCT_SHORT = 25.0   # short premium runs against → loss
MULTILEG_WIN_PCT = 25.0        # spread profit ≥ +25% of entry → win
MULTILEG_LOSS_PCT = -50.0      # spread loss ≥ 50% of entry → loss


def compute_option_return_pct(
    prediction: Dict[str, Any],
    fetch_premium: Optional[Callable[[str], Optional[float]]] = None,
    get_legs: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
) -> Optional[float]:
    """Compute the option-economics return % for a prediction row.

    `prediction` is the row dict from ai_predictions including:
      - predicted_signal, price_at_prediction
      - occ_symbol (single-leg) OR option_order_id (multileg)

    `fetch_premium(occ_symbol) -> float | None`  — looks up the
        current premium. Defaults to `client._fetch_option_premium`
        which hits Alpaca's options snapshots endpoint.
    `get_legs(combo_order_id) -> List[{occ_symbol, qty, price, side}]`
        — looks up multileg leg rows from the trades table.
        Defaults to `journal.get_multileg_legs_by_combo_order`.

    Returns the return % (signed), or None if not computable.
    """
    signal = (prediction.get("predicted_signal") or "").upper()
    entry_price = float(prediction.get("price_at_prediction") or 0)
    if entry_price <= 0:
        return None

    # Wire defaults at call time so tests can inject mocks.
    if fetch_premium is None:
        from client import _fetch_option_premium
        fetch_premium = lambda occ: _fetch_option_premium(occ)
    if get_legs is None:
        from journal import get_multileg_legs_by_combo_order
        db_path = prediction.get("db_path")
        get_legs = lambda combo_id: (
            get_multileg_legs_by_combo_order(db_path, combo_id)
            if db_path else []
        )

    if signal in ("OPTIONS", "OPTION_EXERCISE"):
        return _resolve_single_leg(prediction, fetch_premium)
    if signal == "MULTILEG_OPEN":
        return _resolve_multileg(prediction, fetch_premium, get_legs)
    # Unknown option signal — defer
    return None


def _resolve_single_leg(
    prediction: Dict[str, Any],
    fetch_premium: Callable[[str], Optional[float]],
) -> Optional[float]:
    """Single-leg option: return_pct = (current - entry) / entry * 100."""
    occ = prediction.get("occ_symbol")
    if not occ:
        return None
    try:
        current = fetch_premium(occ)
    except Exception as exc:
        logger.debug("fetch_premium(%s) failed: %s", occ, exc)
        return None
    if current is None or current <= 0:
        return None
    entry = float(prediction.get("price_at_prediction") or 0)
    if entry <= 0:
        return None
    return (current - entry) / entry * 100.0


def _resolve_multileg(
    prediction: Dict[str, Any],
    fetch_premium: Callable[[str], Optional[float]],
    get_legs: Callable[[str], List[Dict[str, Any]]],
) -> Optional[float]:
    """Multileg: net spread value vs entry; signed by direction.

    For each leg:
      value_contribution = current_premium × qty   (signed by qty)

    Sum across all legs → current_spread_value.
    Entry value is similarly summed from logged leg prices.

    return_pct = (current_value - entry_value) / |entry_value| × 100

    Sign semantics: a credit spread (net qty short) has negative
    entry_value; if current_value > entry_value (closer to zero
    or positive), the spread has profited — return_pct is positive.
    A debit spread (net qty long) has positive entry_value; profit
    happens when current_value > entry_value (price moved
    favorably).
    """
    combo_id = prediction.get("option_order_id")
    if not combo_id:
        return None
    try:
        legs = get_legs(combo_id)
    except Exception as exc:
        logger.debug("get_legs(%s) failed: %s", combo_id, exc)
        return None
    if not legs:
        return None

    entry_value = 0.0
    current_value = 0.0
    for leg in legs:
        qty = float(leg.get("qty") or 0)
        entry_premium = float(leg.get("price") or 0)
        if qty == 0 or entry_premium <= 0:
            # Can't compute reliably — skip this leg, but if it
            # leaves us with zero data, defer.
            continue
        occ = leg.get("occ_symbol")
        if not occ:
            continue
        try:
            current_premium = fetch_premium(occ)
        except Exception:
            current_premium = None
        if current_premium is None or current_premium <= 0:
            return None  # Need all legs priced; partial data is wrong
        entry_value += entry_premium * qty * 100.0    # contract mult
        current_value += current_premium * qty * 100.0

    if abs(entry_value) < 1.0:
        # Spread cost basis effectively zero — can't compute pct.
        # E.g., perfect credit-equals-debit construction. Defer.
        return None

    return (current_value - entry_value) / abs(entry_value) * 100.0


def classify_option_outcome(
    return_pct: float, signal: str, signed_qty_hint: float = 0.0,
) -> Tuple[str, float]:
    """Map a return % to (outcome, return_pct).

    Outcome: 'win', 'loss', or 'neutral'.

    For single-leg with known position direction (signed_qty_hint
    nonzero), use the long/short thresholds. For multileg, use the
    spread thresholds. Conservative default for ambiguous cases:
    long-side thresholds.
    """
    signal = (signal or "").upper()
    if signal == "MULTILEG_OPEN":
        if return_pct >= MULTILEG_WIN_PCT:
            return ("win", return_pct)
        if return_pct <= MULTILEG_LOSS_PCT:
            return ("loss", return_pct)
        return ("neutral", return_pct)

    # Single-leg path: side determined by signed_qty if known
    if signed_qty_hint < 0:
        # Short premium — wins on theta decay (premium drops)
        if return_pct <= OPTION_WIN_PCT_SHORT:
            return ("win", return_pct)
        if return_pct >= OPTION_LOSS_PCT_SHORT:
            return ("loss", return_pct)
        return ("neutral", return_pct)

    # Long premium (default)
    if return_pct >= OPTION_WIN_PCT_LONG:
        return ("win", return_pct)
    if return_pct <= OPTION_LOSS_PCT_LONG:
        return ("loss", return_pct)
    return ("neutral", return_pct)
