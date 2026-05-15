"""Single-leg option exit logic (TODO #7, 2026-05-11).

Closes the safety gap: today's `portfolio_manager.check_stop_loss_take_profit`
skips ALL option positions (commit message: "Skip option positions
— same reasoning as check_trailing_stops"). That's safe for
multileg spread legs (protected by structural max loss = debit
paid) but UNSAFE for single-leg longs, which can lose 100% of
premium with no automated exit.

This module adds three exit triggers for SINGLE-LEG LONG options:
  1. Premium stop-loss: close at -50% premium drop from entry.
  2. Premium take-profit: close at +100% premium gain from entry.
  3. DTE exit: close at ≤ 7 days to expiry (avoid gamma blowup).

Multileg legs are explicitly skipped — they're managed at the
spread level, and independently closing one leg would orphan its
partner. Determined by checking the entry trade's signal_type:
'MULTILEG' → skip; 'OPTIONS' (single-leg) → eligible.

Short single-leg options (qty < 0) are also skipped this commit —
short premium economics differ (theta is GOOD), thresholds need
different design. A future iteration can add short-side exits.

Submission is via Alpaca's raw POST endpoint with `position_intent
= sell_to_close` (long) or `buy_to_close` (short, when added). The
SDK's `submit_order` doesn't expose position_intent, hence the raw
POST path borrowed from `options_multileg._submit_alpaca_order_raw`.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date as _date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Default thresholds — kept as named module values so tests and
# legacy non-ctx callers still work. The live trading path resolves
# the effective value from `ctx` (per-profile, AI-tunable) via
# `_resolve_thresholds(ctx)` below. As of 2026-05-12, these are
# AI-tunable: OptionPipeline.tune() adjusts the matching ctx fields
# based on the option win rate over resolved option predictions.
#
# LONG SIDE — premium-paid positions (qty > 0):
PREMIUM_STOP_LOSS_PCT = -0.50      # -50% premium drop → close
PREMIUM_TAKE_PROFIT_PCT = 1.00     # +100% premium gain → close
DTE_EXIT_THRESHOLD_DAYS = 7        # ≤ N days to expiry → close
# SHORT SIDE — premium-collected positions (qty < 0):
SHORT_PREMIUM_TAKE_PROFIT_PCT = -0.50  # premium dropped 50% → close (win)
SHORT_PREMIUM_STOP_LOSS_PCT = 1.00     # premium up 100% → close (loss)


def _resolve_thresholds(ctx: Any) -> Dict[str, float]:
    """Resolve the effective exit thresholds from ctx, falling back
    to module defaults when ctx is None or missing a field. Returns
    a dict so the caller can use one named lookup per trigger."""
    return {
        "premium_stop_loss_pct": float(getattr(
            ctx, "option_premium_stop_loss_pct", PREMIUM_STOP_LOSS_PCT
        ) if ctx is not None else PREMIUM_STOP_LOSS_PCT),
        "premium_take_profit_pct": float(getattr(
            ctx, "option_premium_take_profit_pct", PREMIUM_TAKE_PROFIT_PCT
        ) if ctx is not None else PREMIUM_TAKE_PROFIT_PCT),
        "dte_exit_threshold_days": int(getattr(
            ctx, "option_dte_exit_threshold_days", DTE_EXIT_THRESHOLD_DAYS
        ) if ctx is not None else DTE_EXIT_THRESHOLD_DAYS),
        "short_premium_take_profit_pct": float(getattr(
            ctx, "option_short_premium_take_profit_pct",
            SHORT_PREMIUM_TAKE_PROFIT_PCT,
        ) if ctx is not None else SHORT_PREMIUM_TAKE_PROFIT_PCT),
        "short_premium_stop_loss_pct": float(getattr(
            ctx, "option_short_premium_stop_loss_pct",
            SHORT_PREMIUM_STOP_LOSS_PCT,
        ) if ctx is not None else SHORT_PREMIUM_STOP_LOSS_PCT),
    }


def _entry_signal_type(db_path: str, occ_symbol: str) -> Optional[str]:
    """Look up the most recent OPEN entry trade for `occ_symbol` and
    return its signal_type (or None if not found). Used to
    distinguish single-leg ('OPTIONS') from multileg ('MULTILEG')
    positions — multileg legs MUST NOT be exited independently."""
    if not db_path or not occ_symbol:
        return None
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                """SELECT signal_type FROM trades
                   WHERE occ_symbol = ? AND status = 'open'
                   ORDER BY timestamp DESC LIMIT 1""",
                (occ_symbol,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _is_multileg_leg(db_path: str, occ_symbol: str) -> bool:
    """True iff the position is part of a multileg spread."""
    return _entry_signal_type(db_path, occ_symbol) == "MULTILEG"


def _days_to_expiry(occ_symbol: str,
                      today: Optional[_date] = None) -> Optional[int]:
    """Parse YYMMDD from the OCC symbol; return integer days
    from today. Returns None if parsing fails."""
    if not occ_symbol:
        return None
    try:
        from options_trader import parse_occ_symbol
        parsed = parse_occ_symbol(occ_symbol)
        expiry = parsed["expiry"]
        ref = today or _date.today()
        return (expiry - ref).days
    except Exception:
        return None


def check_single_leg_option_exits(
    positions: List[Any],
    db_path: str,
    today: Optional[_date] = None,
    ctx: Any = None,
) -> List[Dict[str, Any]]:
    """Return exit signals for single-leg long option positions
    that hit a premium-stop, take-profit, or DTE threshold.

    Thresholds are read from `ctx` (per-profile, AI-tunable). When
    ctx is None — legacy callers, tests — the module defaults
    (PREMIUM_STOP_LOSS_PCT etc.) apply.

    Each signal dict:
      {
        "occ_symbol": <padded OCC>,
        "qty": int (always positive),
        "side_to_close": "sell" (long → sell-to-close),
        "trigger": "premium_stop" | "premium_take_profit" | "dte_exit",
        "reason": human-readable,
        "premium_pct_change": signed float,
      }
    """
    triggered: List[Dict[str, Any]] = []
    if not positions:
        return triggered

    t = _resolve_thresholds(ctx)

    for pos in positions:
        if not _pos_is_option(pos):
            continue
        occ = _pos_occ(pos)
        if not occ:
            continue
        # Multileg legs are managed at the spread level — never
        # close a single leg independently.
        if _is_multileg_leg(db_path, occ):
            continue

        qty = float(_pos_get(pos, "qty") or 0)
        if qty == 0:
            continue
        entry = float(_pos_get(pos, "avg_entry_price") or 0)
        current = float(_pos_get(pos, "current_price") or 0)
        is_short = qty < 0

        # Premium-based triggers require both prices.
        if entry > 0 and current > 0:
            pct_change = (current - entry) / entry

            if not is_short:
                # LONG: stop on premium drop, take-profit on rise.
                if pct_change <= t["premium_stop_loss_pct"]:
                    triggered.append(_make_signal(
                        occ, qty, "premium_stop",
                        f"Premium dropped {pct_change:+.0%} (threshold "
                        f"{t['premium_stop_loss_pct']:+.0%})",
                        pct_change,
                    ))
                    continue
                if pct_change >= t["premium_take_profit_pct"]:
                    triggered.append(_make_signal(
                        occ, qty, "premium_take_profit",
                        f"Premium gained {pct_change:+.0%} (threshold "
                        f"{t['premium_take_profit_pct']:+.0%})",
                        pct_change,
                    ))
                    continue
            else:
                # SHORT: take-profit when premium DROPS (theta wins);
                # stop when premium RISES against us. 2026-05-12.
                if pct_change <= t["short_premium_take_profit_pct"]:
                    triggered.append(_make_signal(
                        occ, qty, "short_premium_take_profit",
                        f"Short premium decayed {pct_change:+.0%} "
                        f"(threshold "
                        f"{t['short_premium_take_profit_pct']:+.0%}) "
                        f"— closing to lock in {abs(pct_change)*100:.0f}% "
                        f"of credit",
                        pct_change,
                    ))
                    continue
                if pct_change >= t["short_premium_stop_loss_pct"]:
                    triggered.append(_make_signal(
                        occ, qty, "short_premium_stop",
                        f"Short premium expanded {pct_change:+.0%} "
                        f"(threshold "
                        f"{t['short_premium_stop_loss_pct']:+.0%}) "
                        f"— closing before further expansion",
                        pct_change,
                    ))
                    continue

        # DTE-based trigger — independent of premium price + side.
        dte = _days_to_expiry(occ, today=today)
        if dte is not None and dte <= t["dte_exit_threshold_days"]:
            pct_change = (
                (current - entry) / entry
                if entry > 0 and current > 0 else 0.0
            )
            triggered.append(_make_signal(
                occ, qty, "dte_exit",
                f"DTE={dte} <= threshold="
                f"{t['dte_exit_threshold_days']} "
                f"(closing to avoid gamma blowup)",
                pct_change,
            ))

    return triggered


def submit_option_close(
    api,
    occ_symbol: str,
    qty: int,
    side_to_close: str = "sell",
    limit_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Submit a sell_to_close (long) or buy_to_close (short) order
    via Alpaca's raw POST endpoint. Returns a result dict.

    Uses the same `_submit_alpaca_order_raw` path the multileg
    executor uses — bypasses the SDK's narrow signature so we can
    set `position_intent` correctly. Without position_intent, Alpaca
    sometimes treats the order as opening a new position rather
    than closing the existing one.

    Defaults to MARKET order for fast exit; pass `limit_price` to
    submit a limit order at a specific premium (preferred for wide-
    spread contracts).
    """
    from options_multileg import _submit_alpaca_order_raw
    intent = ("sell_to_close" if side_to_close == "sell"
              else "buy_to_close")
    payload = {
        "symbol": occ_symbol.replace(" ", ""),  # Alpaca wants unpadded
        "qty": int(qty),
        "side": side_to_close,
        "type": "limit" if limit_price is not None else "market",
        "time_in_force": "day",
        "position_intent": intent,
    }
    if limit_price is not None:
        payload["limit_price"] = float(limit_price)
    try:
        result = _submit_alpaca_order_raw(api, payload)
        return {
            "action": "OPTION_CLOSE",
            "occ_symbol": occ_symbol,
            "qty": qty,
            "side": side_to_close,
            "order_id": getattr(result, "id", None),
            "status": "submitted",
        }
    except Exception as exc:
        logger.warning(
            "Option close failed for %s qty=%s side=%s: %s",
            occ_symbol, qty, side_to_close, exc,
        )
        return {
            "action": "ERROR",
            "occ_symbol": occ_symbol,
            "reason": str(exc),
            "status": "failed",
        }


# ---------------------------------------------------------------------------
# Helpers — read fields from either Position objects OR plain dicts
# ---------------------------------------------------------------------------

def _pos_is_option(pos) -> bool:
    if hasattr(pos, "is_option"):
        try:
            return bool(pos.is_option)
        except AttributeError as _io_exc:
            # Duck-typed is_option attribute access; falls through
            # to OCC heuristic. Surface for follow-up.
            logger.debug(
                "is_option attribute access failed: %s: %s",
                type(_io_exc).__name__, _io_exc,
            )
    occ = _pos_occ(pos)
    return bool(occ) and len(occ) >= 15


def _pos_occ(pos) -> Optional[str]:
    if hasattr(pos, "occ_symbol"):
        v = pos.occ_symbol
        if v:
            return v
    if isinstance(pos, dict):
        return pos.get("occ_symbol")
    try:
        return pos["occ_symbol"]
    except Exception:
        return None


def _pos_get(pos, key, default=None):
    if hasattr(pos, key):
        try:
            return getattr(pos, key)
        except AttributeError as _dt_exc:
            # Duck-typed attribute access; falls through to
            # dict-style lookup. Surface for follow-up.
            logger.debug(
                "duck-typed attribute access failed: %s: %s",
                type(_dt_exc).__name__, _dt_exc,
            )
    if isinstance(pos, dict):
        return pos.get(key, default)
    try:
        return pos[key]
    except Exception:
        return default


def _make_signal(occ_symbol, qty, trigger, reason, pct_change):
    """side_to_close is opposite of the position direction:
      long position (qty > 0)  → sell-to-close
      short position (qty < 0) → buy-to-close
    """
    side = "buy" if qty < 0 else "sell"
    return {
        "occ_symbol": occ_symbol,
        "qty": abs(int(qty)),
        "side_to_close": side,
        "trigger": trigger,
        "reason": reason,
        "premium_pct_change": pct_change,
    }
