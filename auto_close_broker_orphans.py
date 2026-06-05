"""Autonomous remediation for `broker_orphan` drift on per-account
aggregate audits.

When the aggregate audit (`virtual_audit.audit_cross_account`) fires
on an Alpaca account because the broker holds more contracts than
the sum of all profile virtual books reflects, this module
automatically closes the orphan contracts at the broker so the next
audit pass clears.

The architectural rule (`feedback_ai_driven_no_manual_loop`): the
system either prevents the drift at write time (the atomic-placement
contract enforced by `test_atomic_journaling_audit_2026_05_19`) OR
self-heals when prevention failed. Asking the operator to log into
the broker dashboard and manually close contracts is the failure
mode, not the design.

Per the atomic-placement contract this module enforces on its own
close submits:
  - Every broker close written here is paired with a journal write
    in the same try/except (the existing close path already does
    this via `_submit_alpaca_order_raw` + immediate `log_trade`).
  - On journal write failure: cancel the close order, halt the
    profile, log an alert. Same pattern as `_log_strategy_legs`.

Per `feedback_fix_class_not_instance`: this closes the symmetric
class to the open-path atomic placement. The open path prevents
broker fills with no journal row; this remediates the post-hoc
case where the journal records intent-to-close but the broker
never executed the close.
"""
from __future__ import annotations

import logging
import re
from contextlib import closing
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Parse one drift line written by `virtual_audit.audit_cross_account`:
#   "SYMBOL_OR_OCC: virtual total=X vs Alpaca=Y (diff=Z shares)"
_DRIFT_LINE_RE = re.compile(
    r"^(?P<sym>\S+): virtual total=(?P<v>-?\d+(?:\.\d+)?) "
    r"vs Alpaca=(?P<a>-?\d+(?:\.\d+)?) "
    r"\(diff=(?P<d>\d+(?:\.\d+)?) shares\)"
)

# OCC option contract symbols. Standard form is 21 chars (6-char
# root right-padded with spaces + YYMMDD + C|P + 8-digit strike in
# 1000ths). Alpaca returns unpadded form for shorter roots
# (e.g. "NVDA260710C00240000" not "NVDA  260710C00240000"), so the
# regex accepts 1-6 alphas + the rest.
_OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def _is_occ(symbol: str) -> bool:
    """True iff the symbol is a 21-char OCC option contract."""
    return bool(_OCC_RE.match(symbol or ""))


def _parse_drift_problems(
    problems: List[str],
) -> List[Dict[str, float]]:
    """Convert the audit's text drift lines into structured dicts.
    Drops malformed lines (defensive — the audit format may evolve)."""
    out: List[Dict[str, float]] = []
    for p in problems:
        m = _DRIFT_LINE_RE.match(p)
        if not m:
            continue
        sym = m.group("sym")
        try:
            v = float(m.group("v"))
            a = float(m.group("a"))
            d = float(m.group("d"))
        except ValueError:
            continue
        out.append({
            "symbol": sym,
            "virtual_qty": v,
            "alpaca_qty": a,
            "diff": d,
        })
    return out


def _broker_position(api, occ_symbol: str) -> Optional[Dict]:
    """Return the current broker position for an OCC (or None if the
    broker is flat on it). Used to determine the close side
    (sell_to_close for long; buy_to_close for short)."""
    try:
        positions = api.list_positions()
    except Exception as exc:
        logger.warning(
            "list_positions failed during orphan remediation "
            "(%s: %s) — skipping this cycle, will retry next audit",
            type(exc).__name__, exc,
        )
        return None
    for p in positions:
        if getattr(p, "symbol", None) == occ_symbol:
            return {
                "qty": float(p.qty),
                "side": getattr(p, "side", "long"),
                "avg_entry_price": float(
                    getattr(p, "avg_entry_price", 0.0) or 0.0,
                ),
            }
    return None


def _target_profile_for_close(profile_ids: List[int]) -> Optional[int]:
    """Pick which profile's journal carries the AUTO_RECONCILE_CLOSE
    row. Deterministic: lowest enabled profile_id on the account.
    The choice is bookkeeping — every profile on this account
    contributes to the aggregate drift; attributing the close to one
    of them keeps the audit math consistent without arbitrating
    between profiles."""
    if not profile_ids:
        return None
    return min(profile_ids)


def remediate_account_drift(
    alpaca_account_id: int,
    profile_ids: List[int],
    problems: List[str],
) -> List[Dict]:
    """Auto-close every orphan OCC contract on this Alpaca account.

    Returns a list of remediation result dicts (one per OCC):
      {
        "occ_symbol": str,
        "diff_qty": float,
        "action": "AUTO_CLOSED" | "SKIP" | "ERROR" | "BROKER_FLAT",
        "close_order_id": Optional[str],
        "reason": str,
      }

    Idempotent over the audit signature — if the close has already
    been submitted but the broker hasn't filled yet, the next cycle
    sees `virtual=N+orphan vs Alpaca=N+orphan diff=0` and this
    function returns nothing for that OCC.
    """
    from models import build_user_context_from_profile
    from client import get_api
    from options_exits import submit_option_close
    from journal import log_trade

    drift = _parse_drift_problems(problems)
    if not drift:
        return []

    target_pid = _target_profile_for_close(profile_ids)
    if target_pid is None:
        return [{
            "occ_symbol": "(no profile)",
            "diff_qty": 0,
            "action": "SKIP",
            "close_order_id": None,
            "reason": "no enabled profiles on this account",
        }]
    target_ctx = build_user_context_from_profile(target_pid)
    api = get_api(target_ctx)
    db_path = target_ctx.db_path
    results: List[Dict] = []

    for d in drift:
        sym = d["symbol"]
        diff_qty = d["diff"]
        if diff_qty <= 0:
            continue

        # Only auto-close OCC option contracts. Stock-side drift is
        # a different class and is handled by the existing reconcile
        # paths (see reconcile_journal_to_broker.py).
        if not _is_occ(sym):
            results.append({
                "occ_symbol": sym,
                "diff_qty": diff_qty,
                "action": "SKIP",
                "close_order_id": None,
                "reason": "not an OCC symbol — stock-side drift "
                          "handled elsewhere",
            })
            continue

        # broker_qty > virtual_qty (the audit's broker_orphan shape).
        # Determine close side from the broker's actual side; default
        # to long if the position can't be read (sell_to_close is the
        # more common shape).
        pos = _broker_position(api, sym)
        if pos is None:
            results.append({
                "occ_symbol": sym,
                "diff_qty": diff_qty,
                "action": "BROKER_FLAT",
                "close_order_id": None,
                "reason": "broker reports no position — drift may "
                          "have cleared between audit and remediation",
            })
            continue

        close_side = "sell" if pos["side"] == "long" else "buy"
        qty_to_close = int(round(diff_qty))

        # Submit the close. The existing `submit_option_close` uses
        # the direct-POST path with the correct position_intent so
        # Alpaca recognizes it as closing the existing position.
        close_result = submit_option_close(
            api,
            occ_symbol=sym,
            qty=qty_to_close,
            side_to_close=close_side,
        )

        if close_result.get("status") != "submitted":
            reject_reason = str(close_result.get("reason", ""))
            # Classify retryable rejections (after-hours, transient
            # rate limits) so the next scheduler audit-cycle retries
            # instead of treating the drift as permanent. Options
            # market orders are an Alpaca-side restriction during
            # regular hours only — the close will succeed at the
            # next market open without operator intervention.
            retryable = (
                "market hours" in reject_reason.lower()
                or "42210000" in reject_reason
                or "429" in reject_reason  # rate limit
            )
            results.append({
                "occ_symbol": sym,
                "diff_qty": diff_qty,
                "action": "DEFERRED" if retryable else "ERROR",
                "close_order_id": None,
                "reason": (
                    "broker rejected close — will retry next cycle: "
                    if retryable
                    else "broker rejected close: "
                ) + reject_reason,
            })
            continue

        close_order_id = close_result.get("order_id")
        # Atomic-placement contract on the close itself: write a
        # journal row immediately; cancel the broker close and halt
        # the profile if the write fails.
        try:
            log_trade(
                symbol=_underlying_from_occ(sym),
                side=close_side,
                qty=qty_to_close,
                price=None,  # filled via _task_update_fills
                order_id=close_order_id,
                signal_type="AUTO_RECONCILE_CLOSE",
                strategy="aggregate_audit_orphan_remediation",
                reason=(
                    f"Auto-close orphan {sym} qty={qty_to_close} "
                    f"on Alpaca account {alpaca_account_id} "
                    f"(audit reported virtual={d['virtual_qty']:.0f} "
                    f"vs broker={d['alpaca_qty']:.0f}); attributed "
                    f"to first profile on account "
                    f"(pid={target_pid})"
                ),
                status="pending_fill",
                occ_symbol=sym,
                db_path=db_path,
            )
            results.append({
                "occ_symbol": sym,
                "diff_qty": diff_qty,
                "action": "AUTO_CLOSED",
                "close_order_id": close_order_id,
                "reason": (
                    f"submitted {close_side}_to_close qty="
                    f"{qty_to_close} (order_id={close_order_id})"
                ),
            })
            logger.info(
                "Auto-closed broker orphan %s qty=%s side=%s "
                "(order_id=%s, profile=%s, account=%s)",
                sym, qty_to_close, close_side, close_order_id,
                target_pid, alpaca_account_id,
            )
        except Exception as exc:
            # Broker close was accepted; journal write failed. Cancel
            # the close and halt the profile per the contract.
            logger.error(
                "AUTO_RECONCILE_CLOSE journal write failed for %s "
                "(close order_id=%s): %s: %s — cancelling broker close",
                sym, close_order_id, type(exc).__name__, exc,
            )
            try:
                api.cancel_order(close_order_id)
            except Exception as cancel_exc:
                logger.error(
                    "Rollback of auto-close FAILED for %s: %s: %s",
                    sym, type(cancel_exc).__name__, cancel_exc,
                )
            try:
                from halt_helpers import halt_and_alert
                halt_and_alert(
                    profile_id=target_pid,
                    db_path=db_path,
                    alert_type="auto_close_journal_breach",
                    title=(
                        "Auto-close journal-write breach: "
                        + sym
                    ),
                    detail=(
                        f"occ={sym} qty={qty_to_close} "
                        f"close_order_id={close_order_id} "
                        f"journal_exc={type(exc).__name__}: {exc}"
                    ),
                )
            except Exception as halt_exc:
                logger.error(
                    "halt_and_alert FAILED during auto-close "
                    "rollback: %s: %s",
                    type(halt_exc).__name__, halt_exc,
                )
            results.append({
                "occ_symbol": sym,
                "diff_qty": diff_qty,
                "action": "ERROR",
                "close_order_id": close_order_id,
                "reason": (
                    "journal write failed — broker close rolled "
                    "back, profile halted"
                ),
            })

    return results


def _underlying_from_occ(occ: str) -> str:
    """Extract the underlying ticker from an OCC symbol. Handles both
    the standard 21-char padded form and Alpaca's unpadded form by
    walking backwards from the strike (`\\d{8}`) past the C|P right
    code and the 6-digit date to find where the alphabetic root ends.
    """
    if not occ:
        return ""
    m = _OCC_RE.match(occ)
    if not m:
        return occ.strip()
    # Strike is the last 8 digits, right code is 1 char before that,
    # date is 6 digits before that — root is everything before.
    root_end = len(occ) - 8 - 1 - 6
    return occ[:root_end].strip()
