"""Capture non-trade Alpaca account activities into the journal.

Order fills (FILL/PFILL) reach the journal via `order_guard` +
`log_trade` at trade-submission time. But the broker also generates
events the journal misses by default:

  DIV    dividend cash credit
  OPEXP  option expiration (close at $0 if OTM)
  OPASN  option assignment (short option taken)
  OPEXC  option exercise (we exercised a long option)

If those events aren't reflected, broker_cash and broker_value
silently drift from journal_cash and journal_value — exactly the
class of "hidden loss/gain" the user surfaced after the 2026-05-13
cash-logic incident.

This module pulls the activity stream from Alpaca and writes
matching journal rows. Idempotency: the Alpaca activity `id` is
written as `trades.order_id` so re-running the capture never
double-writes the same event.

Schedule: hourly per-profile via multi_scheduler. Mid-day capture
catches dividends and assignments before the close-of-day audits
run, so the existing five integrity audits won't false-flag them.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)


# Activity types we handle. FILL/PFILL deliberately omitted — those
# are written at submit time via log_trade.
# 2026-07-25 — "OPXRC" was never a valid Alpaca activity type (the API
# rejected it on EVERY capture run since 2026-07-20: "invalid activity
# type: OPXRC"), so exercise events were silently uncapturable. The
# valid name is OPEXC (probed live against the API).
_HANDLED_TYPES = ("DIV", "OPEXP", "OPASN", "OPEXC")


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _has_activity(db_path: str, activity_id: str) -> bool:
    """Idempotency check: have we already written a journal row for
    this Alpaca activity?"""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE order_id = ? LIMIT 1",
                (activity_id,),
            ).fetchone()
            return row is not None
    except sqlite3.OperationalError as exc:
        logger.warning(
            "activities_capture: dedup check failed for %s in %s: %s",
            activity_id, db_path, exc,
        )
        return False


def _write_dividend(ctx, activity: Any) -> bool:
    """Dividend cash credit → trades row with side='dividend',
    qty=1, price=dividend_amount. Adds 'dividend' to the credit
    branch of get_virtual_account_info (see journal.py).

    Field names per Alpaca's NonTradeActivity entity docs
    (verified 2026-05-17): `id`, `activity_type='DIV'`, `symbol`,
    `date`, `qty`, `per_share_amount`, **`net_amount`** (the
    canonical dollar-amount field). No `amount` fallback — that was
    speculative and isn't a documented field.
    """
    from journal import log_trade
    activity_id = str(getattr(activity, "id", ""))
    if not activity_id:
        logger.warning("activities_capture: DIV without id, skipping")
        return False
    if _has_activity(ctx.db_path, activity_id):
        return False
    symbol = (getattr(activity, "symbol", "") or "").upper()
    raw_amount = getattr(activity, "net_amount", None)
    if raw_amount is None:
        logger.warning(
            "activities_capture: DIV %s has no net_amount field — "
            "Alpaca response shape differs from documented "
            "NonTradeActivity. Skipping row; cash-parity audit will "
            "detect the resulting drift within 10 min.",
            activity_id,
        )
        return False
    try:
        amount = float(raw_amount)
    except (ValueError, TypeError):
        logger.warning(
            "activities_capture: DIV %s net_amount=%r is not numeric",
            activity_id, raw_amount,
        )
        return False
    if amount == 0:
        return False
    getattr(activity, "date", None) or _utcnow_iso()
    try:
        log_trade(
            symbol=symbol or "CASH",
            side="dividend",
            qty=1,
            price=abs(amount),
            order_id=activity_id,
            signal_type="DIVIDEND",
            strategy="alpaca_activity",
            reason=f"dividend credit {symbol or '?'} ${amount:.2f}",
            status="closed",
            pnl=amount,
            decision_price=abs(amount),
            db_path=ctx.db_path,
        )
        logger.info(
            "[%s] DIV captured: %s $%.2f (activity=%s)",
            ctx.display_name if hasattr(ctx, "display_name") else "?",
            symbol, amount, activity_id,
        )
        return True
    except (sqlite3.OperationalError, ValueError) as exc:
        logger.error(
            "activities_capture: DIV %s log_trade failed: %s",
            activity_id, exc,
        )
        return False


def _own_occ_net(db_path: str, occ_symbol: str) -> float:
    """This profile's LIVE net contracts for `occ_symbol` (buys −
    sells over non-terminal rows, broker-filled prices not required —
    a just-opened leg with a pending price still owns its event).
    Positive = long, negative = short, 0 = not this profile's OCC."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN qty "
                "                         WHEN side='sell' THEN -qty "
                "                         ELSE 0 END), 0) "
                "FROM trades WHERE occ_symbol = ? "
                "AND COALESCE(status, 'open') IN ('open', 'pending_fill')",
                (occ_symbol,),
            ).fetchone()
            return float(row[0] or 0)
    except sqlite3.OperationalError as exc:
        # 2026-07-27 fail-closed sweep: 0.0 said "not this profile's
        # OCC" on a failed read — the settlement writer would then
        # book the event against the wrong basis or skip ownership.
        # None = unverifiable; the writer defers this activity to the
        # next idempotent pass.
        logger.warning("_own_occ_net UNVERIFIABLE for %s/%s: %s",
                        db_path, occ_symbol, exc)
        return None


def _own_occ_avg_premium(db_path: str, occ_symbol: str,
                         entry_side: str) -> float:
    """Average entry premium of this profile's live rows for the OCC
    on the given side — the basis for the realized pnl of a $0
    expiry/assignment close."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(qty * COALESCE(NULLIF(fill_price, 0),"
                " price)), 0), COALESCE(SUM(qty), 0) "
                "FROM trades WHERE occ_symbol = ? AND side = ? "
                "AND COALESCE(status, 'open') IN ('open', 'pending_fill')",
                (occ_symbol, entry_side),
            ).fetchone()
            notional, qty = float(row[0] or 0), float(row[1] or 0)
            return (notional / qty) if qty > 0 else 0.0
    except sqlite3.OperationalError as exc:
        # 2026-07-27 fail-closed sweep: a fabricated $0 basis books a
        # wrong realized pnl on the settlement leg. None = defer to
        # the next idempotent activities pass.
        logger.warning("_own_occ_avg_premium UNVERIFIABLE for %s/%s: %s",
                        db_path, occ_symbol, exc)
        return None


def _write_option_expiry_or_exercise(ctx, activity: Any) -> bool:
    """OPEXP/OPASN/OPEXC: close THIS PROFILE'S option leg.

    Rewritten 2026-07-25 after Friday's expiry — the first real
    assignment this system ever received — hit four latent defects at
    once (all caught live, all broker-verified):
      1. NO OWNERSHIP GATE: the account-level activity was written into
         every profile sharing the account (the CVX 185C assignment
         landed in all five acct-56 books; only p212 held the spread).
         Now: a profile books the event ONLY when its own journal holds
         a live position in that exact OCC (own-book rule).
      2. NEGATIVE QTY SKIPPED: Alpaca reports expiring LONG legs with
         negative qty (removal); `qty <= 0: return False` silently
         dropped every one (FCX 70C ×5 never booked). Now: abs(qty).
      3. HARDCODED side='sell': correct only for long legs. A short
         leg's removal is a BUY-to-close; writing a sell added a
         phantom short. Now: side follows the profile's own position
         direction.
      4. PNL NEVER BOOKED: the old docstring claimed "the FIFO matcher
         attributes the premium" — false: recompute_realized_pnl skips
         price<=0 rows, so $0 closes never realized their premium and
         the equity identity drifted by it. Now: pnl is stamped
         explicitly from the profile's own average entry premium
         (short: +premium kept; long: −premium lost).

    CASH: the activity's own net_amount is 0 even for assignments —
    Alpaca settles ITM assignment/exercise via same-day MISC "Options
    Trade" rows (observed: the fully-ITM 185/190 spread settled
    +37,000/−38,000 = net −1,000 = spread width, no fills, no share
    positions). That settlement cash is booked separately by
    `_write_option_settlements` as a cash-only 'cash_debit'/'dividend'
    row. The leg close here stays at $0.
    """
    from journal import log_trade
    activity_id = str(getattr(activity, "id", ""))
    if not activity_id:
        return False
    if _has_activity(ctx.db_path, activity_id):
        return False
    activity_type = getattr(activity, "activity_type", "?")
    occ_symbol = (getattr(activity, "symbol", "") or "").upper()
    if not occ_symbol:
        logger.warning(
            "activities_capture: %s %s has no symbol field — "
            "Alpaca response shape differs from documented "
            "NonTradeActivity. Skipping; qty-parity audit will "
            "detect the resulting position drift within 10 min.",
            activity_type, activity_id,
        )
        return False
    try:
        qty = abs(float(getattr(activity, "qty", 0) or 0))
    except (ValueError, TypeError):
        logger.warning(
            "activities_capture: %s %s has unparseable qty",
            activity_type, activity_id,
        )
        return False
    if qty <= 0:
        return False
    # OWNERSHIP GATE — own-book rule. Not this profile's OCC → not
    # this profile's event. (PROFILE_ORDER_ISOLATION.md.)
    net = _own_occ_net(ctx.db_path, occ_symbol)
    if net is None:
        # 2026-07-27 fail-closed sweep: ownership unverifiable —
        # defer; the activities pass is idempotent and retries.
        logger.warning(
            "activities_capture: ownership UNVERIFIABLE for %s — "
            "activity deferred to next pass.", occ_symbol)
        return False
    if abs(net) < 0.5:
        return False
    qty = min(qty, abs(net))
    if net > 0:
        # Closing a LONG leg: sell-to-close @0; premium paid is lost.
        side = "sell"
        premium = _own_occ_avg_premium(ctx.db_path, occ_symbol, "buy")
    else:
        # Closing a SHORT leg: BUY-to-close @0; premium received kept.
        side = "buy"
        premium = _own_occ_avg_premium(ctx.db_path, occ_symbol, "sell")
    if premium is None:
        # 2026-07-27 fail-closed sweep: basis unverifiable — a
        # fabricated $0 premium books a wrong realized pnl. Defer.
        logger.warning(
            "activities_capture: basis UNVERIFIABLE for %s — "
            "activity deferred to next pass.", occ_symbol)
        return False
    if net > 0:
        pnl = round(-premium * qty * 100.0, 2)
    else:
        pnl = round(premium * qty * 100.0, 2)
    import re
    m = re.search(r"(\d{6}[CP]\d{8})$", occ_symbol)
    underlying = occ_symbol[:m.start()] if m else occ_symbol
    try:
        log_trade(
            symbol=underlying or "?",
            side=side,
            qty=qty,
            price=0.0,
            order_id=activity_id,
            signal_type=activity_type,
            strategy="alpaca_activity",
            reason=(
                f"{activity_type} {occ_symbol}: {side}-to-close "
                f"x{qty:.0f} @ $0 (broker-initiated; own net was "
                f"{net:+.0f}; realized {pnl:+.2f} from avg premium "
                f"{premium:.4f})"
            ),
            occ_symbol=occ_symbol if m else None,
            status="closed",
            decision_price=0.0,
            pnl=pnl,
            db_path=ctx.db_path,
        )
        # Close the origin leg row(s) too — both sides of the pair
        # terminal, per house convention (roll manager / reconciler).
        # Via the journal layer (the authorized mutation surface).
        from journal import close_leg_rows_for_occ
        flipped = close_leg_rows_for_occ(
            ctx.db_path, occ_symbol,
            f"leg removed by {activity_type} (activity {activity_id})",
        )
        if not flipped:
            logger.warning(
                "activities_capture: origin-leg close flipped 0 rows "
                "for %s — the open leg may already be terminal",
                occ_symbol,
            )
        logger.info(
            "[%s] %s captured: %s %s-to-close x%.0f pnl %+.2f "
            "(activity=%s)",
            getattr(ctx, "display_name", "?"),
            activity_type, occ_symbol, side, qty, pnl, activity_id,
        )
        return True
    except (sqlite3.OperationalError, ValueError) as exc:
        logger.error(
            "activities_capture: %s %s log_trade failed: %s",
            activity_type, activity_id, exc,
        )
        return False


def _write_option_settlement(ctx, activity: Any,
                             group_occ: Dict[str, str]) -> bool:
    """MISC 'Options Trade' — the CASH side of an assignment/exercise.

    Alpaca settles ITM assignment/exercise via share-flow-at-strike
    rows (no fills, no share positions on paper): the 2026-07-24 CVX
    185/190 spread settled as +37,000 (assignment sale @185) and
    −38,000 (exercise buy @190) — each row group_id-linked to its
    option event, which names the OCC. Attribution: group → OCC →
    the profile whose journal carries that OCC (own-book rule; the
    leg may already be closed by the event handler in this same
    pass, so ownership means ANY journal rows for the OCC, not live
    ones). Booked as a cash-only row (side='cash_debit' for a debit,
    'dividend' for a credit — neither touches any position lens),
    pnl = net so realized composes with the $0 leg closes and the
    equity identity holds. Idempotent via the activity id."""
    from journal import log_trade
    activity_id = str(getattr(activity, "id", ""))
    if not activity_id:
        return False
    if _has_activity(ctx.db_path, activity_id):
        return False
    try:
        net = float(getattr(activity, "net_amount", 0) or 0)
    except (ValueError, TypeError):
        return False
    if net == 0:
        return False
    gid = str(getattr(activity, "group_id", "") or "")
    occ = group_occ.get(gid, "")
    if not occ:
        logger.warning(
            "activities_capture: settlement %s ($%.2f) has no "
            "group-linked option event — cannot attribute to a "
            "profile; cash-parity audit will surface the drift.",
            activity_id, net,
        )
        return False
    # Own-book gate: REAL journal rows for the OCC (live or closed
    # trades — a closed leg still owns its settlement). Dead rows
    # (canceled/expired/rejected) do NOT confer ownership: the
    # 2026-07-25 first run of this writer leaked the +37,000
    # assignment settlement into four foreign profiles because their
    # VOIDED duplicate rows (from the pre-fix capture bug) still
    # matched a bare any-rows check.
    try:
        with sqlite3.connect(ctx.db_path) as conn:
            owned = conn.execute(
                "SELECT 1 FROM trades WHERE occ_symbol = ? "
                "AND COALESCE(status, 'open') NOT IN "
                "('canceled', 'expired', 'rejected') LIMIT 1",
                (occ,),
            ).fetchone() is not None
    except sqlite3.OperationalError:
        owned = False
    if not owned:
        return False
    import re
    m = re.search(r"(\d{6}[CP]\d{8})$", occ)
    underlying = occ[:m.start()] if m else occ
    side = "cash_debit" if net < 0 else "dividend"
    try:
        log_trade(
            symbol=underlying or "?",
            side=side,
            qty=1,
            price=abs(net),
            order_id=activity_id,
            signal_type="OPTION_SETTLEMENT",
            strategy="alpaca_activity",
            reason=(
                f"option settlement {occ}: broker MISC 'Options "
                f"Trade' net ${net:+,.2f} (group {gid[:8]}; "
                f"share-flow-at-strike, no fill)"
            ),
            status="closed",
            pnl=round(net, 2),
            decision_price=abs(net),
            db_path=ctx.db_path,
        )
        logger.info(
            "[%s] settlement captured: %s $%+.2f (activity=%s)",
            getattr(ctx, "display_name", "?"), occ, net, activity_id,
        )
        return True
    except (sqlite3.OperationalError, ValueError) as exc:
        logger.error(
            "activities_capture: settlement %s log_trade failed: %s",
            activity_id, exc,
        )
        return False


def capture_activities_for_profile(ctx,
                                   since: Optional[datetime] = None,
                                   ) -> Dict[str, int]:
    """Pull non-trade activities from Alpaca since `since` (default:
    last 7 days) and write matching journal rows.

    Returns {activity_type: count_written}.
    """
    summary = {t: 0 for t in _HANDLED_TYPES}
    if since is None:
        since = datetime.now(tz=timezone.utc) - timedelta(days=7)
    try:
        api = ctx.get_alpaca_api() if hasattr(
            ctx, "get_alpaca_api") else getattr(ctx, "api", None)
        if api is None:
            from client import get_api
            api = get_api(ctx)
    except Exception as exc:
        logger.error(
            "activities_capture: get_api failed for profile %s: %s",
            getattr(ctx, "profile_id", "?"), exc,
        )
        return summary
    # 2026-07-20 — fetch PER TYPE. The previous comma-joined string
    # ('DIV,OPEXP,OPASN,OPXRC') was rejected by Alpaca as one unknown
    # type ("invalid activity type: DIV,OPEXP,OPASN,OPXRC"), which
    # silently killed the ENTIRE capture stream on every cycle — no
    # dividends, no option expiry/assignment events, ever. Per-type
    # calls survive an API/SDK that only accepts a single type, and a
    # failure in one type can no longer take down the rest.
    activities = []
    for _a_type in _HANDLED_TYPES:
        try:
            batch = api.get_activities(
                activity_types=_a_type,
                after=since.isoformat(),
            )
            activities.extend(list(batch) if batch is not None else [])
        except Exception as exc:
            logger.warning(
                "activities_capture: get_activities(%s) failed for "
                "profile %s: %s",
                _a_type, getattr(ctx, "profile_id", "?"), exc,
            )

    for a in activities:
        a_type = getattr(a, "activity_type", "")
        if a_type == "DIV":
            if _write_dividend(ctx, a):
                summary["DIV"] += 1
        elif a_type in ("OPEXP", "OPASN", "OPEXC"):
            if _write_option_expiry_or_exercise(ctx, a):
                summary[a_type] += 1
        else:
            logger.debug(
                "activities_capture: ignoring unhandled type %s", a_type,
            )

    # 2026-07-25 — MISC stream (Friday's expiry taught us both lessons
    # live): (a) some option events are reported ONLY as MISC rows —
    # the CVX 190C EXERCISE appeared in neither typed stream, only as
    # MISC description='Options Exercise'; (b) assignment/exercise CASH
    # settles via MISC 'Options Trade' rows (net_amount = the share-flow
    # at strike; the fully-ITM 185/190 spread settled +37,000/−38,000 =
    # net −1,000 with NO fills), group_id-linked to their option event.
    # Both are booked here: events route through the same handler
    # (own-book gated); settlement cash books as a cash-only
    # 'cash_debit'/'dividend' row attributed by group → OCC → owner.
    _MISC_EVENT_MAP = {
        "Options Assignment": "OPASN",
        "Options Expiry": "OPEXP",
        "Options Exercise": "OPEXC",
    }
    try:
        misc = api.get_activities(
            activity_types="MISC", after=since.isoformat(),
        ) or []
    except Exception as exc:
        logger.warning(
            "activities_capture: get_activities(MISC) failed for "
            "profile %s: %s", getattr(ctx, "profile_id", "?"), exc,
        )
        misc = []
    # group_id -> OCC symbol (from the option-event rows in the batch)
    group_occ: Dict[str, str] = {}
    for a in misc:
        desc = str(getattr(a, "description", "") or "")
        if desc in _MISC_EVENT_MAP:
            gid = str(getattr(a, "group_id", "") or "")
            sym = (getattr(a, "symbol", "") or "").upper()
            if gid and sym:
                group_occ[gid] = sym
    for a in misc:
        desc = str(getattr(a, "description", "") or "")
        if desc in _MISC_EVENT_MAP:
            # An option event only visible via MISC: route through the
            # normal handler with the mapped type. Dedup by activity id
            # makes this safe when the typed stream ALSO carried it.
            mapped = _MISC_EVENT_MAP[desc]
            try:
                a.activity_type = mapped
            except Exception:
                try:
                    a._raw["activity_type"] = mapped
                except Exception as _mp_exc:
                    logger.warning(
                        "activities_capture: could not retype MISC "
                        "event %s (%s) — skipping",
                        getattr(a, "id", "?"), _mp_exc,
                    )
                    continue
            if _write_option_expiry_or_exercise(ctx, a):
                summary[mapped] = summary.get(mapped, 0) + 1
        elif desc == "Options Trade":
            if _write_option_settlement(ctx, a, group_occ):
                summary["SETTLEMENT"] = summary.get("SETTLEMENT", 0) + 1
    return summary


def capture_activities_for_all_profiles(profile_ids: Iterable[int],
                                        ) -> Dict[int, Dict[str, int]]:
    """Batch: capture activities for every active profile."""
    from models import build_user_context_from_profile
    out: Dict[int, Dict[str, int]] = {}
    for pid in profile_ids:
        try:
            ctx = build_user_context_from_profile(pid)
        except Exception as exc:
            logger.warning(
                "activities_capture: build_user_context_from_profile "
                "failed for %s: %s", pid, exc,
            )
            continue
        out[pid] = capture_activities_for_profile(ctx)
    return out
