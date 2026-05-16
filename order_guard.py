"""Pre-submission order guard.

Every order must pass through `check_can_submit` before calling
`api.submit_order`. This catches the bug where a scan cycle starts
within schedule but the pipeline takes long enough that the actual
order submission falls outside schedule.

Without this guard, after-hours trades happen accidentally on profiles
set to market_hours — the scheduler only checks schedule at cycle
start, not at order time.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def check_can_submit(ctx, symbol: str, side: str) -> bool:
    """Return True if the profile's schedule allows an order right now.

    Logs a warning and returns False if the order would fall outside
    the profile's configured trading window. The caller should skip
    the order — not queue it for later (Alpaca paper fills after-hours
    orders, which is what caused the original bug).
    """
    if ctx is None:
        return True

    now = datetime.now(_ET)

    if ctx.is_within_schedule(now):
        return True

    seg_label = getattr(ctx, "display_name", None) or getattr(ctx, "segment", "unknown")
    logger.warning(
        "[%s] Order BLOCKED: %s %s at %s ET is outside schedule (%s). "
        "The scan cycle started within schedule but the pipeline took "
        "long enough that execution fell outside the window.",
        seg_label, side.upper(), symbol,
        now.strftime("%-I:%M %p"), ctx.schedule_type,
    )
    return False


# Per-trade buy-side qty sanity. Blocks orders whose qty is wildly
# above the profile's recent median — a near-certain sign of a
# sizing-arithmetic bug. Picked at 20× so 5–20× still flows through
# (legitimate bigger trades), but the egregious cases that motivated
# this guard (NU 60×, KNX 28.5×, LEVI 129×, CSX 82× — all on prod
# 2026-05) get blocked BEFORE submit, not just alerted after fill.
EXCESSIVE_QTY_BLOCK_MULT = 20.0
_RECENT_QTY_WINDOW = 50
_MIN_HISTORY_FOR_MEDIAN = 10   # below this, no median-based block


def allowable_buy_qty(
    db_path: str, symbol: str, requested_qty: float,
) -> tuple:
    """Pre-submit guard: return (allowed_qty, reason) for a BUY of
    `requested_qty` shares.

    Reads the profile's last 50 BUY-side qtys from the journal and
    blocks the order if `requested_qty > median × EXCESSIVE_QTY_BLOCK_MULT`.
    Pre-2026-05-16 `position_runaway` detected this AFTER the fill;
    too late — the trade was already placed. Same median math, just
    enforced before `api.submit_order`.

    Returns (allowed_qty, reason):
      - (requested_qty, "ok"): passes the sanity check.
      - (0, "blocked: qty Nx median ..."): qty is absurd; block.
      - (requested_qty, "permissive: insufficient history"): profile
        has <10 BUY rows in journal so no median can be computed
        confidently. Default to permissive so new profiles aren't
        artificially throttled.
      - (requested_qty, "permissive: DB read failed"): on read error,
        default to permissive — fall back to the post-fact alert.

    Options contracts (OCC symbols) bypass this guard — option qty
    semantics (1 contract = 100 shares) make the median comparison
    nonsensical.
    """
    import sqlite3
    from contextlib import closing
    if requested_qty <= 0:
        return (0, "refused: non-positive qty")
    target = (symbol or "").upper()
    if len(target) > 6 and any(c.isdigit() for c in target[1:7]):
        # OCC option symbol — different qty semantics; skip.
        return (requested_qty, "ok: option contract — guard bypassed")
    if not db_path:
        return (requested_qty, "permissive: no db_path")
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute(
                "SELECT qty FROM trades "
                "WHERE side = 'buy' AND qty IS NOT NULL AND qty > 0 "
                "ORDER BY id DESC LIMIT ?",
                (_RECENT_QTY_WINDOW,),
            ).fetchall()
    except Exception as exc:
        logger.warning(
            "allowable_buy_qty: DB read failed for %s — permissive "
            "fallback: %s: %s",
            symbol, type(exc).__name__, exc,
        )
        return (requested_qty, f"permissive: DB read failed ({exc})")
    if len(rows) < _MIN_HISTORY_FOR_MEDIAN:
        return (
            requested_qty,
            f"permissive: only {len(rows)} buy rows (need "
            f"{_MIN_HISTORY_FOR_MEDIAN}) for median-based sanity",
        )
    qtys = sorted(float(r[0]) for r in rows)
    median_qty = qtys[len(qtys) // 2]
    threshold = median_qty * EXCESSIVE_QTY_BLOCK_MULT
    if requested_qty > threshold:
        multiple = (
            requested_qty / median_qty if median_qty > 0 else float("inf")
        )
        logger.error(
            "allowable_buy_qty: BLOCKED %s %s — qty %s is %.1f× the "
            "profile's recent median %.2f (threshold=%.0f×). Almost "
            "certainly a sizing-arithmetic bug. If this is a deliberate "
            "large entry, submit it manually; the guard intentionally "
            "errs on the side of blocking.",
            symbol, requested_qty, requested_qty, multiple,
            median_qty, EXCESSIVE_QTY_BLOCK_MULT,
        )
        return (
            0,
            f"blocked: qty {requested_qty} is {multiple:.1f}x median "
            f"({median_qty:.2f}), threshold {EXCESSIVE_QTY_BLOCK_MULT:.0f}x",
        )
    return (requested_qty, "ok")


def allowable_sell_qty(api, symbol: str, requested_qty: int) -> tuple:
    """Pre-trade guard: return (allowed_qty, reason) for a SELL of `requested_qty`.

    Caught 2026-05-06: 31 broker shorts had accumulated across the 3
    Alpaca accounts because multiple profiles share each account, and
    cumulative SELLs from independent profile stop-losses overshot the
    broker's actual long position. Each profile thought it was closing
    its own long; the broker went net-short by tens of thousands.

    Strategy: query broker BEFORE submitting any SELL. The broker is
    the only source of truth for "how many shares can I actually sell
    on this account?" The journal abstraction is per-profile and can't
    see cross-profile aggregation.

    Returns (allowed_qty, reason):
      - (requested_qty, "ok"): broker has enough longs, proceed.
      - (broker_qty, "downsized: broker has only N shares"): broker has
        SOME but fewer than requested. Downsize the SELL.
      - (0, "refused: would create short, broker has 0 long {symbol}"):
        broker has zero — submitting would open a short.
      - (requested_qty, "permissive: broker API failed"): on broker
        error, default to permissive — let the existing error handling
        in submit_order surface real failures.

    Caller MUST honor the returned allowed_qty (downsize or skip).
    Options contracts (occ_symbol) bypass this guard — option short
    legs are intentional and tracked separately.
    """
    if requested_qty <= 0:
        return (0, "refused: non-positive qty")
    target = (symbol or "").upper()
    # Options contracts have a different qty convention and intentional
    # shorts (covered call, bull put spread); skip the guard.
    if len(target) > 6 and any(c.isdigit() for c in target[1:7]):
        # OCC symbols look like UNDERLYING + 6-digit-date (YYMMDD) + P/C
        return (requested_qty, "ok: option contract — guard bypassed")
    try:
        positions = api.list_positions()
    except Exception as exc:
        logger.warning(
            "allowable_sell_qty: broker list_positions failed for %s — "
            "permissive fallback: %s", symbol, exc,
        )
        return (requested_qty, f"permissive: broker API failed ({exc})")
    broker_qty = 0
    for p in positions:
        if (getattr(p, "symbol", "") or "").upper() == target:
            try:
                broker_qty = int(float(getattr(p, "qty", 0) or 0))
            except Exception:
                broker_qty = 0
            break
    if broker_qty <= 0:
        logger.warning(
            "allowable_sell_qty: REFUSED SELL %s %d — broker has 0 long "
            "(would create a short via overshoot). Position is likely "
            "already closed by another profile sharing this account.",
            symbol, requested_qty,
        )
        return (0, f"refused: would create short, broker has 0 long {symbol}")
    if broker_qty < requested_qty:
        logger.warning(
            "allowable_sell_qty: DOWNSIZED SELL %s %d → %d (broker has "
            "only %d long across shared account)",
            symbol, requested_qty, broker_qty, broker_qty,
        )
        return (broker_qty, f"downsized: broker has only {broker_qty} shares")
    return (requested_qty, "ok")
