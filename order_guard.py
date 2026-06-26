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
    broker_drift_check: bool = True,
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
                # 2026-06-25 — match the STOCK position ONLY. A profile that
                # holds both a stock AND an option on the same underlying gets
                # one get_virtual_positions row PER option leg (each carries an
                # occ_symbol). A bare-symbol match grabs whichever row sorts
                # first — often an option leg (e.g. a -1 short-put leg) — so the
                # door computed the wrong sellable qty and REFUSED the stock's
                # protective stop, leaving the position naked. A stock sell
                # concerns ONLY the non-option row. (Option sells bypass this
                # guard entirely via the OCC-symbol check above.)
                if pos.get("occ_symbol"):
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

    # The per-profile journal check above is the load-bearing gate ("a
    # profile may sell ONLY what its own book holds"). The broker
    # cross-check below is a secondary drift sanity that needs a live
    # list_positions call. The guarded-api door (see GuardedAlpacaApi)
    # runs on EVERY sell with broker_drift_check=False so it stays
    # journal-only — fast, and structurally incapable of consulting a
    # sibling profile's holdings (the whole point). The explicit
    # trader/pipeline call sites keep the broker drift check.
    if not broker_drift_check:
        return (requested_qty, "ok: journal-only (own book holds it)")

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
                # 2026-06-25 — match the STOCK row ONLY (same occ-symbol-aware
                # fix as allowable_sell_qty): an option leg on the same
                # underlying must not be mistaken for the stock short.
                if pos.get("occ_symbol"):
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


# ───────────────────────────────────────────────────────────────────────
# THE DOOR — a single, unbypassable pre-submit gate on every broker order.
#
# Why this exists (2026-06-19): allowable_sell_qty ("a profile may sell
# only what its OWN journal holds") already existed, but it was only wired
# into the AI-driven exit paths (trader.py, trade_pipeline.py). The
# protective sweep (bracket_orders) — and stat-arb / the delta hedger /
# option rollbacks — called api.submit_order DIRECTLY, with no oversell
# guard. That unguarded door is exactly how the 2026-06-18 phantom equity
# happened: a re-armed protective SELL fired on a position the profile no
# longer held (own journal long = 0) and filled as a real, unowned short.
#
# Wrapping the per-profile api factory (user_context.get_alpaca_api) means
# EVERY submit_order — whatever module calls it — passes through this gate.
# A stock SELL may not exceed this profile's own journal long unless it
# explicitly declares intent="open_short". It's journal-only (the profile's
# own book, never the shared-account aggregate), so it is structurally
# incapable of selling another profile's shares. A new code path that tries
# to submit a naked sell can't bypass it without bypassing the factory —
# which a structural test forbids.
# ───────────────────────────────────────────────────────────────────────

#: An order may declare this as `intent=` to submit_order to mark a
#: DELIBERATE short entry (a sell-to-open that is *supposed* to exceed the
#: held long). The door consumes it and never forwards it to the broker.
INTENT_OPEN_SHORT = "open_short"


class OversellGuardError(Exception):
    """Raised by the guarded api door when a stock SELL would exceed the
    profile's own journal long and carries no short intent. Every
    submit_order call site already wraps the call in try/except and treats
    a raise as a non-fatal placement failure (the order is simply not
    sent) — which is exactly right for a re-armed naked sell: the bogus
    protective just isn't armed, the cycle continues."""


def _is_occ(symbol: str) -> bool:
    """OCC option symbol heuristic (UNDERLYING + YYMMDD + C/P + strike).
    Options carry their own position_intent enforcement (Alpaca-side), so
    the stock oversell door does not gate them."""
    s = (symbol or "").upper()
    return len(s) > 6 and any(c.isdigit() for c in s[1:7])


def _ensure_fresh_or_refuse(ctx, symbol: str) -> None:
    """Force `symbol` reconciled-to-broker-truth THIS cycle before any sell.

    Cheap when already fresh (a freshness-ledger read). When stale, runs a
    just-in-time reconcile (reuses the per-cycle reconcile machinery, stamps
    the symbol fresh). FAIL-CLOSED: if the reconcile cannot complete (broker
    unreachable, DB error), raise OversellGuardError so the door REFUSES the
    sell rather than act on a possibly-stale journal.

    This is the heart of the divergence-class fix (2026-06-23): staleness is
    the common root of every oversell/phantom instance (p166 PLUG, SMCI, the
    phantom-equity incident — the journal said we held shares the broker had
    already sold). Making staleness un-actable at the one door every order
    passes through collapses the whole class."""
    if ctx is None or not symbol:
        return
    try:
        from reconcile_journal_to_broker import ensure_symbol_fresh
        ensure_symbol_fresh(ctx, str(symbol))
    except OversellGuardError:
        raise
    except Exception as exc:
        who = (getattr(ctx, "display_name", None)
               or getattr(ctx, "db_path", None) or "?")
        logger.error(
            "OVERSELL DOOR: could not freshen %s to broker truth this cycle "
            "[%s]: %s: %s. Refusing the sell (fail-closed) rather than act on "
            "a possibly-stale journal.",
            str(symbol).upper(), who, type(exc).__name__, exc)
        raise OversellGuardError(
            "freshness reconcile failed for %s (%s) — sell refused" % (
                str(symbol).upper(), type(exc).__name__))


def assert_sell_within_own_book(api, ctx, kwargs: dict) -> None:
    """Enforce the oversell invariant for one submit_order call (kwargs).

    Pops the internal `intent` marker (never forwarded to the broker).
    FIRST forces the symbol reconciled-to-broker this cycle (freshness gate);
    then for a STOCK SELL with no `intent="open_short"`, refuses (raises
    OversellGuardError) if the qty exceeds this profile's own journal long.
    Mutates `kwargs` in place to strip `intent`. The freshness gate applies
    to options and declared shorts too (a rollback / partner close / short
    must not fire on a stale book); the QTY check is stock-long-only."""
    intent = kwargs.pop("intent", None)
    side = str(kwargs.get("side") or "").lower()
    symbol = kwargs.get("symbol")
    if side != "sell" or not symbol:
        return
    # FRESHNESS GATE — every sell (stock, option, declared short) must act on
    # a journal reconciled to broker truth this cycle, or be refused.
    _ensure_fresh_or_refuse(ctx, str(symbol))
    if _is_occ(str(symbol)):
        # Option qty semantics are enforced Alpaca-side (position_intent); the
        # freshness gate above still applies so a rollback / partner-leg close
        # cannot fire on a stale option leg.
        return
    if intent == INTENT_OPEN_SHORT:
        return  # deliberate short entry — allowed to sell beyond the long
    try:
        qty = int(float(kwargs.get("qty") or 0))
    except (TypeError, ValueError):
        return
    if qty <= 0:
        return
    db_path = getattr(ctx, "db_path", None)
    allowed, reason = allowable_sell_qty(
        api, str(symbol), qty, db_path=db_path, broker_drift_check=False)
    if allowed <= 0:
        who = getattr(ctx, "display_name", None) or db_path or "?"
        logger.error(
            "OVERSELL DOOR BLOCKED SELL %s qty=%d [%s]: %s. A profile may "
            "only sell what its OWN journal holds; this would create an "
            "unowned short (the phantom-equity vector). Order NOT sent. If "
            "this is a deliberate short, the caller must pass "
            "intent='open_short'.",
            str(symbol).upper(), qty, who, reason)
        raise OversellGuardError(
            "naked SELL %s qty=%d refused: %s" % (
                str(symbol).upper(), qty, reason))


class GuardedAlpacaApi:
    """Per-profile wrapper around the Alpaca REST client. The ONLY method
    it changes is submit_order — routed through assert_sell_within_own_book.
    Every other attribute/method delegates verbatim to the wrapped client.

    Bound to ONE profile's ctx (its own db_path), so it cannot consult
    another profile's holdings — order isolation by construction."""

    def __init__(self, api, ctx):
        # set via __dict__ to avoid tripping __getattr__ during init
        self.__dict__["_api"] = api
        self.__dict__["_ctx"] = ctx

    def __getattr__(self, name):
        # only reached for names not found normally (i.e. not _api/_ctx
        # and not submit_order) — delegate to the wrapped client.
        return getattr(self.__dict__["_api"], name)

    @property
    def unwrapped(self):
        """The raw underlying REST client (for the rare caller that needs
        the genuine object, e.g. isinstance checks). Order submission must
        still go through this wrapper."""
        return self.__dict__["_api"]

    def submit_order(self, *args, **kwargs):
        api = self.__dict__["_api"]
        ctx = self.__dict__["_ctx"]
        if args:
            # Positional args would let `side`/`qty` slip past the gate
            # (the door inspects kwargs). Every call site passes order
            # fields by keyword; enforce it so a positional call can't
            # silently bypass the oversell check.
            raise TypeError(
                "submit_order must be called with keyword arguments so the "
                "oversell door can inspect side/qty (got positional args)")
        if ctx is None:
            # No per-profile ctx → no journal to oversell-check against.
            # The no-ctx client (client.get_api(None)) exists only for
            # read-only data calls; an order through it cannot be guarded,
            # so refuse rather than send an unchecked naked sell.
            raise OversellGuardError(
                "order submission requires a per-profile ctx (a journal to "
                "oversell-check against); none was provided")
        # Capture the journal intent BEFORE the door strips it from kwargs, so
        # the recovery ledger can record whether a broker 'sell' is a short
        # ENTRY (open_short) vs a long close.
        intent = kwargs.get("intent")
        assert_sell_within_own_book(api, ctx, kwargs)
        # RETRY_OK: this is the pass-through door, not an originating call
        # site. Every real caller (trader, trade_pipeline, bracket_orders,
        # …) already wraps its submit_order in try/except — that is exactly
        # why the oversell raise above is non-fatal. Retry/error handling
        # belongs to the caller, not the gate.
        result = api.submit_order(**kwargs)
        # Durable-journaling recovery (2026-06-23): record the accepted order
        # the moment the broker returns it, BEFORE the caller's log_trade. If
        # that journal write is later lost (DB lock/disk), the order_id
        # survives in submitted_orders so the reconciler can reconstruct the
        # row from the broker fill instead of orphan-halting. Best-effort —
        # a recovery-record failure must never look like a submit failure.
        try:
            oid = (getattr(result, "id", None)
                   or getattr(result, "client_order_id", None))
            if oid is not None:
                from journal import record_submitted_order
                record_submitted_order(
                    getattr(ctx, "db_path", None), oid,
                    kwargs.get("symbol"), kwargs.get("side"),
                    kwargs.get("qty"), kwargs.get("occ_symbol"),
                    intent=intent)
        except Exception:
            logger.debug(
                "submitted_orders recovery record failed (non-fatal)",
                exc_info=True)
        return result


def guarded_api(api, ctx):
    """Wrap a per-profile REST client in the oversell door. Idempotent and
    None-safe so it can wrap the factory return unconditionally."""
    if api is None or isinstance(api, GuardedAlpacaApi):
        return api
    return GuardedAlpacaApi(api, ctx)
