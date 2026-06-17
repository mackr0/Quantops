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
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def own_broker_order_ids(db_path: Optional[str],
                         symbol: Optional[str] = None) -> set:
    """Return the set of Alpaca order_ids THIS profile's journal has
    recorded — from the `order_id` column and every
    `protective_*_order_id` column. Optionally restricted to one
    `symbol`.

    2026-06-16 — the cornerstone of profile order isolation on a
    SHARED Alpaca account. Any broker order whose id is in this set
    was created by THIS profile; any id NOT in this set belongs to a
    sibling and must never be canceled/consumed. Callers cancel only
    the intersection of (broker open orders) ∩ (this set). See
    PROFILE_ORDER_ISOLATION.md.
    """
    import sqlite3
    from contextlib import closing
    ids: set = set()
    if not db_path:
        return ids
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(trades)").fetchall()}
            id_cols = [c for c in (
                "order_id", "protective_stop_order_id",
                "protective_tp_order_id", "protective_trailing_order_id",
            ) if c in cols]
            if not id_cols:
                return ids
            where = "WHERE UPPER(symbol) = ?" if symbol else ""
            params = ((symbol.upper(),) if symbol else ())
            sql = f"SELECT {', '.join(id_cols)} FROM trades {where}"
            for row in conn.execute(sql, params):
                for v in row:
                    if v:
                        ids.add(v)
            # Long-vol hedge orders live in their own table, not
            # `trades` — include them so hedge order_ids are also
            # recognized as THIS profile's own. The feature is gated
            # off by default, but the helper must be complete.
            has_hv = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='long_vol_hedges'").fetchone()
            if has_hv:
                hv = {r[1] for r in conn.execute(
                    "PRAGMA table_info(long_vol_hedges)").fetchall()}
                hv_cols = [c for c in ("order_id", "close_order_id")
                           if c in hv]
                if hv_cols:
                    for row in conn.execute(
                            f"SELECT {', '.join(hv_cols)} "
                            f"FROM long_vol_hedges"):
                        for v in row:
                            if v:
                                ids.add(v)
    except sqlite3.Error as exc:
        logger.debug("own_broker_order_ids(%s) failed: %s", symbol, exc)
    return ids


def own_protective_order_ids(db_path: Optional[str]) -> set:
    """Return the set of order_ids that are PROTECTIVE orders (stop /
    take-profit / trailing, incl. bracket children) for this profile —
    from the `protective_*_order_id` columns and from rows that are
    themselves protective placements (`status='pending_protective'` or
    a `PROTECTIVE_*` signal_type).

    2026-06-16 — used to EXCLUDE protective orders from the stale-limit
    canceller. A bracket take-profit is a limit order that lives for
    the whole position; canceling it as "stale" strips protection (and
    the OCO cancels the paired stop too). See PROFILE_ORDER_ISOLATION.md.
    """
    import sqlite3
    from contextlib import closing
    ids: set = set()
    if not db_path:
        return ids
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(trades)").fetchall()}
            for col in ("protective_stop_order_id", "protective_tp_order_id",
                        "protective_trailing_order_id"):
                if col not in cols:
                    continue
                for r in conn.execute(
                        f"SELECT {col} FROM trades WHERE {col} IS NOT NULL"):
                    if r[0]:
                        ids.add(r[0])
            # Rows that ARE protective placements.
            where = []
            if "status" in cols:
                where.append("COALESCE(status,'') = 'pending_protective'")
            if "signal_type" in cols:
                where.append("signal_type LIKE 'PROTECTIVE%'")
            if "order_id" in cols and where:
                for r in conn.execute(
                        "SELECT order_id FROM trades WHERE order_id IS NOT NULL "
                        "AND (" + " OR ".join(where) + ")"):
                    if r[0]:
                        ids.add(r[0])
    except sqlite3.Error as exc:
        logger.debug("own_protective_order_ids failed: %s", exc)
    return ids


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
            # 2026-05-21 — restrict the median to STOCK buys only
            # (occ_symbol IS NULL). The requested_qty being checked
            # here is a share count (option BUYs bypass this guard
            # entirely via the OCC-symbol check above). Pooling
            # option-contract qtys (1-4 contracts) into the median
            # made it ~1.0 for options-heavy profiles, so EVERY
            # legitimate stock BUY (100s-1000s of shares) read as
            # 100-1000× median and got blocked. Caught 2026-05-21:
            # fleet-wide stock BUYs (ACHR 1134, GRAB 1899, SMR 301)
            # blocked because the profiles had been trading mostly
            # option spreads, dragging the median to 1.00.
            #
            # Probe for the occ_symbol column — older test fixtures
            # use a minimal schema without it. When absent, fall back
            # to the all-buys query (pre-2026-05-21 behavior).
            has_occ = bool(conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info('trades') "
                "WHERE name = 'occ_symbol'"
            ).fetchone()[0])
            if has_occ:
                rows = conn.execute(
                    "SELECT qty FROM trades "
                    "WHERE side = 'buy' AND qty IS NOT NULL AND qty > 0 "
                    "  AND (occ_symbol IS NULL OR occ_symbol = '') "
                    "ORDER BY id DESC LIMIT ?",
                    (_RECENT_QTY_WINDOW,),
                ).fetchall()
            else:
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


def allowable_sell_qty(
    api, symbol: str, requested_qty: int,
    db_path: Optional[str] = None,
) -> tuple:
    """Pre-trade guard: return (allowed_qty, reason) for a SELL of `requested_qty`.

    2026-06-09 rewrite. Pre-rewrite, this checked the AGGREGATE broker
    position across all profiles sharing the Alpaca account, and if the
    aggregate was smaller than `requested_qty`, it would DOWNSIZE to
    the aggregate. That mechanism is exactly how one profile consumed
    sibling profiles' shares: pid 42 proposed SELL 2979 LXEH; the
    aggregate broker pool was 2979 (because siblings had been buying);
    guard said "ok, downsize unnecessary, go ahead"; pid 42 sold all
    2979, including 1191 shares that virtually belonged to pid 45,
    1788 to pid 44, etc. Pid 42's journal recorded the sell; the other
    profiles' journals were never updated — instant phantom positions
    on 4 sibling rows.

    The fix: a profile may sell ONLY what its OWN journal says it
    holds. The aggregate broker pool is consulted only as a sanity
    check — if broker < own_virtual_qty, that's drift (something
    closed our position outside this profile's awareness) and we
    REFUSE rather than silently consume sibling shares.

    Returns (allowed_qty, reason):
      - (requested_qty, "ok"): own journal has the qty, broker confirms
        sufficient longs, proceed.
      - (0, "refused: profile virtually holds N, requested M"): the
        AI's proposal exceeds the profile's own virtual qty. Either
        the AI hallucinated, or there's stale state. Don't trade.
      - (0, "refused: drift detected — broker has N, journal has M"):
        broker has fewer longs than this profile claims. Likely a
        sibling already consumed our share (the pre-rewrite bug) or
        an external action closed the position. Refuse and surface
        loudly so the operator can investigate.
      - (requested_qty, "ok: option contract — guard bypassed"):
        options have a separate guard surface.
      - (requested_qty, "permissive: broker API failed"): on broker
        error, default to permissive — submit_order will surface a
        real failure if there is one. (Does NOT skip the per-profile
        check; that runs first if db_path is provided.)

    Caller MUST honor the returned allowed_qty (refuse-as-skip or
    submit-as-requested). The downsize path is gone — there is no
    case where this returns a positive qty less than requested.
    """
    if requested_qty <= 0:
        return (0, "refused: non-positive qty")
    target = (symbol or "").upper()
    # Options contracts have a different qty convention and intentional
    # shorts (covered call, bull put spread); skip the guard.
    if len(target) > 6 and any(c.isdigit() for c in target[1:7]):
        # OCC symbols look like UNDERLYING + 6-digit-date (YYMMDD) + P/C
        return (requested_qty, "ok: option contract — guard bypassed")

    # Per-profile virtual qty from THIS profile's own journal.
    # Computed from open buy rows minus matching sells/exits via FIFO.
    # The cross-profile aggregate broker pool is NOT consulted to
    # compute this number — that's the whole point of the rewrite.
    own_virtual_qty: Optional[int] = None
    if db_path:
        try:
            from journal import get_virtual_positions
            for pos in get_virtual_positions(db_path):
                if (pos.get("symbol") or "").upper() != target:
                    continue
                try:
                    own_virtual_qty = int(float(pos.get("qty", 0) or 0))
                except (ValueError, TypeError):
                    own_virtual_qty = 0
                break
            if own_virtual_qty is None:
                own_virtual_qty = 0
        except Exception as exc:
            logger.warning(
                "allowable_sell_qty: get_virtual_positions failed for %s "
                "(db=%s) — refusing rather than risk sibling-share "
                "consumption: %s", symbol, db_path, exc,
            )
            return (
                0,
                f"refused: virtual-qty lookup failed for {symbol} "
                f"({type(exc).__name__})",
            )
        if requested_qty > own_virtual_qty:
            logger.warning(
                "allowable_sell_qty: REFUSED SELL %s %d — this profile "
                "virtually holds only %d. The AI proposed more than the "
                "profile owns; refusing to consume sibling shares.",
                symbol, requested_qty, own_virtual_qty,
            )
            return (
                0,
                f"refused: profile virtually holds {own_virtual_qty}, "
                f"requested {requested_qty}",
            )

    # Sanity check against the broker. If the broker has fewer shares
    # than this profile claims, drift exists (something closed our
    # position without updating the journal — most likely a sibling
    # already over-sold under the OLD downsize policy, or an external
    # action). Refuse loud so the operator investigates. Do NOT
    # silently downsize — that's the bug class this rewrite kills.
    try:
        positions = api.list_positions()
    except Exception as exc:
        logger.warning(
            "allowable_sell_qty: broker list_positions failed for %s — "
            "permissive fallback (per-profile check already passed if "
            "db_path provided): %s", symbol, exc,
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
    # Drift check: if the profile claims more shares than the broker
    # has, the discrepancy means our share of the pool may have been
    # consumed (by a sibling under the OLD downsize policy, or by an
    # external action). With db_path provided we compare to journal
    # claim. Without db_path (legacy callers) we fall back to comparing
    # to requested_qty so the historical guard still catches obvious
    # under-counted broker pools.
    drift_baseline = (
        own_virtual_qty
        if (own_virtual_qty is not None and own_virtual_qty > 0)
        else requested_qty
    )
    if broker_qty < drift_baseline:
        own_claim = (
            own_virtual_qty if own_virtual_qty is not None
            else "?"
        )
        logger.warning(
            "allowable_sell_qty: REFUSED SELL %s %d — broker has %d "
            "long (journal claim=%s). Drift detected — refusing to "
            "submit rather than risk consuming sibling shares.",
            symbol, requested_qty, broker_qty, own_claim,
        )
        return (
            0,
            f"refused: drift detected — broker has {broker_qty}, "
            f"journal claims {own_claim}, requested {requested_qty}",
        )
    return (requested_qty, "ok")


def allowable_cover_qty(
    api, symbol: str, requested_qty: int,
    db_path: Optional[str] = None,
) -> tuple:
    """Pre-trade guard for buy-to-cover (closing a short). Mirror of
    `allowable_sell_qty` for the short side.

    2026-06-09. A profile may cover only what its own journal says
    it virtually holds short. The aggregate broker short pool is
    consulted as a drift sanity-check — if the broker is less short
    than this profile claims, that's drift (likely a sibling already
    over-covered our share) and we REFUSE rather than consume
    sibling short positions.

    Threat model: pid A holds 100 NOK short, pid B also 100 short,
    aggregate broker short = 200. Pid A's stop fires for 100 cover.
    Pre-fix the cover path had NO cross-account guard at all —
    submit_order would have bought 100 NOK regardless of who owned
    the short, and Alpaca's FIFO would attribute the buy across the
    aggregate pool. Pid A's journal records the cover; pid B's short
    may actually have closed at the broker. Same class of bug as
    the sell side, opposite direction.

    Returns (allowed_qty, reason):
      - (requested_qty, "ok"): own journal has the short, broker
        confirms sufficient short, proceed.
      - (0, "refused: profile virtually holds N short, requested M"):
        AI / trigger proposal exceeds the profile's own virtual
        short qty.
      - (0, "refused: drift detected — broker short N, journal
        claims M"): broker is less short than this profile claims.
        Likely a sibling already consumed our short.
      - (requested_qty, "ok: option contract — guard bypassed").
      - (requested_qty, "permissive: broker API failed").

    Caller MUST honor the returned qty. Returns either
    `(requested_qty, "ok")` or `(0, reason)` — no partial sizing.
    """
    if requested_qty <= 0:
        return (0, "refused: non-positive qty")
    target = (symbol or "").upper()
    if len(target) > 6 and any(c.isdigit() for c in target[1:7]):
        return (requested_qty, "ok: option contract — guard bypassed")

    # Per-profile virtual short qty from this profile's own journal.
    # `get_virtual_positions` returns shorts as negative qty; abs()
    # to compare against the positive requested cover qty.
    own_short_qty: Optional[int] = None
    if db_path:
        try:
            from journal import get_virtual_positions
            for pos in get_virtual_positions(db_path):
                if (pos.get("symbol") or "").upper() != target:
                    continue
                try:
                    own_signed = int(float(pos.get("qty") or 0))
                except (ValueError, TypeError):
                    own_signed = 0
                # Shorts come back negative; zero or long means
                # this profile has no short to cover.
                own_short_qty = (
                    abs(own_signed) if own_signed < 0 else 0
                )
                break
            if own_short_qty is None:
                own_short_qty = 0
        except Exception as exc:
            logger.warning(
                "allowable_cover_qty: get_virtual_positions failed "
                "for %s (db=%s) — refusing rather than risk sibling-"
                "short consumption: %s", symbol, db_path, exc,
            )
            return (
                0,
                f"refused: virtual-qty lookup failed for {symbol} "
                f"({type(exc).__name__})",
            )
        if requested_qty > own_short_qty:
            logger.warning(
                "allowable_cover_qty: REFUSED COVER %s %d — this "
                "profile virtually holds only %d short. The trigger "
                "proposed more than the profile owns; refusing to "
                "consume sibling short positions.",
                symbol, requested_qty, own_short_qty,
            )
            return (
                0,
                f"refused: profile virtually holds {own_short_qty} "
                f"short, requested {requested_qty}",
            )

    # Drift sanity check against the broker. If broker is LESS short
    # than this profile claims, our share of the short pool may
    # have been consumed by a sibling or an external action.
    try:
        positions = api.list_positions()
    except Exception as exc:
        logger.warning(
            "allowable_cover_qty: broker list_positions failed for "
            "%s — permissive fallback: %s", symbol, exc,
        )
        return (requested_qty, f"permissive: broker API failed ({exc})")
    broker_short_qty = 0
    for p in positions:
        if (getattr(p, "symbol", "") or "").upper() == target:
            try:
                signed = int(float(getattr(p, "qty", 0) or 0))
            except Exception:
                signed = 0
            broker_short_qty = abs(signed) if signed < 0 else 0
            break
    drift_baseline = (
        own_short_qty
        if (own_short_qty is not None and own_short_qty > 0)
        else requested_qty
    )
    if broker_short_qty < drift_baseline:
        own_claim = (
            own_short_qty if own_short_qty is not None else "?"
        )
        logger.warning(
            "allowable_cover_qty: REFUSED COVER %s %d — broker is "
            "short %d (journal claim=%s). Drift detected — refusing "
            "to submit rather than risk consuming sibling shorts.",
            symbol, requested_qty, broker_short_qty, own_claim,
        )
        return (
            0,
            f"refused: drift detected — broker short "
            f"{broker_short_qty}, journal claims short {own_claim}, "
            f"requested {requested_qty}",
        )
    return (requested_qty, "ok")
