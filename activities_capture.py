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
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Activity types we handle. FILL/PFILL deliberately omitted — those
# are written at submit time via log_trade.
# 2026-07-25 — "OPXRC" was never a valid Alpaca activity type (the API
# rejected it on EVERY capture run since 2026-07-20: "invalid activity
# type: OPXRC"), so exercise events were silently uncapturable. The
# valid name is OPEXC (probed live against the API).
# 2026-10-26 — "MA" (merger/acquisition): WBD was cashed out at
# $31.0167/share on 10-12 (3 shares → $93.05); with no handler the
# entry row rotted 'open', the integrity gate read broker=0 vs
# virtual=3, the reconciler halted the profile on an orphan_close, and
# the fleet sat kill-switched for two weeks. Probed live: accepted.
_HANDLED_TYPES = ("DIV", "OPEXP", "OPASN", "OPEXC", "MA", "FEE")

# ── Broker fees (2026-11-01) ─────────────────────────────────────────
# Alpaca paper charges real regulatory/exchange fees as account-level
# FEE activities, one row per fee kind per trading day, each naming the
# base it was charged on ("REG fee for proceed of $25811.31 on
# 2026-10-23", "CAT fee for proceed of 6 trades on …", "ORF fee for
# proceed of 2 contracts on …", "OPT TAF fee for proceed of 2 contracts
# (2 trades) on …", "OPT REG fee for proceed of $1365 on …", "TAF fee for
# proceed of 1332 shares (29 trades) on …") — except
# the flat "OCC Clearing Fee" (one row per option fill). Verified on
# the 2026-10-23 account-61 day: the broker's own FILL activities for
# the day reproduce every base exactly (sell proceeds to the cent, CAT
# = fill events stock+option, ORF = option contracts), and every fill
# maps to exactly one book by its order_id. So a fee is split across
# the account's books pro-rata on the base the broker names, from the
# broker's own fills, by own order_id — no guessing — and the split is
# refused (audit alert) when the books' fills do not reproduce the
# stated base or a fill belongs to no book. Booked as cash-only
# 'cash_debit' rows (pnl = −share) so the per-book identity holds and
# the account-level cash parity no longer needs a broker-side
# adjustment for them (aggregate_audit adjusts only for fees NOT yet
# journaled).
_FEE_SHAPES = (
    ("REG", re.compile(
        r"^REG fee for proceed of \$([\d.,]+) on (\d{4}-\d{2}-\d{2})")),
    ("OPT_REG", re.compile(
        r"^OPT REG fee for proceed of \$([\d.,]+) on (\d{4}-\d{2}-\d{2})")),
    ("CAT", re.compile(
        r"^CAT fee for proceed of (\d+) trades on (\d{4}-\d{2}-\d{2})")),
    ("TAF", re.compile(
        r"^TAF fee for proceed of (\d+) shares \(\d+ trades\) "
        r"on (\d{4}-\d{2}-\d{2})")),
    ("OPT_TAF", re.compile(
        r"^OPT TAF fee for proceed of (\d+) contracts \(\d+ trades\) "
        r"on (\d{4}-\d{2}-\d{2})")),
    ("ORF", re.compile(
        r"^ORF fee for proceed of (\d+) contracts on (\d{4}-\d{2}-\d{2})")),
    ("OCC", re.compile(r"^OCC Clearing Fee")),
)
_FILLS_TTL_S = 3600.0
_fills_memo: Dict[Tuple[Any, str], Tuple[float, list]] = {}
_order_ids_memo: Dict[str, Tuple[float, set]] = {}
_leg_parent_memo: Dict[Tuple[Any, str], Tuple[float, Dict[str, str]]] = {}

# Corporate-action types the journal CANNOT book (stock-for-stock
# reorgs, splits, spin-offs, share journals, symbol changes). All
# probed live 2026-10-26 as accepted by the API (SSO/SSP are rejected
# and therefore absent). When one touches a symbol this profile
# holds, the pass writes an explanatory audit alert so the resulting
# drift halt names its cause instead of reading as a mystery orphan.
# Fetched once per account per hour (memo below) — the typed activity
# endpoints are the ones already tripping 429s.
_REORG_EXPLAIN_TYPES = ("REORG", "CSD", "CSW", "JNLS", "SC", "SPIN")
_REORG_FETCH_TTL_S = 3600.0
_reorg_fetch_memo: Dict[Tuple[Any, str], Tuple[float, list]] = {}

# ── Shared-account attribution (2026-10-26) ──────────────────────────
# The broker reports an account-level event (one DIV row for the
# account's NET position; one MA row for the account's whole holding).
# Four virtual books share each account, so the event must be split
# by each book's OWN position at the broker's entitlement instant —
# 16:00 ET on the last trading day BEFORE the record date (T+1: ex-
# date == record date, holders at the prior close are entitled) — by
# FILL time (a resting protective's journal timestamp is its submit
# time; p237's VTR trailing stop sat 9 days before filling). Validated
# against all 11 real dividend events on the three Exp-2 accounts:
# Σ(sibling own qty) == broker 'Pos QTY' for 11/11. Fail-CLOSED: if
# the siblings' books do not sum to the broker's quantity the event is
# NOT booked and an audit alert names it.
_NEVER_FILLED_STATUSES = (
    "'pending_protective', 'canceled', 'expired', 'rejected', "
    "'done_for_day', 'auto_reconciled_phantom_close'"
)
_UUID_RX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_REC_DATE_RX = re.compile(r"Rec Date:\s*(\d{4}-\d{2}-\d{2})")
_CASH_MERGER_RX = re.compile(r"Cash Merger \$([0-9]+(?:\.[0-9]+)?) per share")
_ATTRIBUTION_TTL_S = 3600.0
_attribution_memo: Dict[str, Tuple[float, Dict[int, float]]] = {}


def clear_memos() -> None:
    """Drop the per-process attribution / reorg-fetch memos (tests)."""
    _attribution_memo.clear()
    _reorg_fetch_memo.clear()
    _fills_memo.clear()
    _order_ids_memo.clear()
    _leg_parent_memo.clear()


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _trades_cols(conn) -> set:
    """Column probe. A failed read propagates (sqlite3.Error) so every
    caller takes its UNVERIFIABLE path — never a guessed schema."""
    return {r[1] for r in conn.execute("PRAGMA table_info(trades)")}


def _entitlement_snapshot_utc(event_date: str) -> Optional[str]:
    """16:00 America/New_York on the last weekday BEFORE `event_date`
    (YYYY-MM-DD), returned as a naive-UTC ISO string comparable with
    journal timestamps. A weekday holiday is harmless: no fills happen
    on it, so the position at its 16:00 equals the prior close."""
    from zoneinfo import ZoneInfo
    try:
        d = datetime.strptime(str(event_date)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    close_et = datetime(d.year, d.month, d.day, 16, 0,
                        tzinfo=ZoneInfo("America/New_York"))
    return (close_et.astimezone(timezone.utc)
            .replace(tzinfo=None).isoformat())


def _utc_iso(value) -> Optional[str]:
    """Normalize a broker/journal timestamp (datetime, pandas
    Timestamp or ISO text, aware or naive-UTC) to naive-UTC ISO with a
    'T' separator so string comparison is chronological."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(
                str(value).replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


def _own_stock_qty_at(api, db_path: str, symbol: str,
                      snapshot_iso: str) -> Optional[float]:
    """This profile's signed net STOCK quantity in `symbol` at the
    instant `snapshot_iso` (naive-UTC ISO), from its own filled rows
    — the same row set that moves cash (get_virtual_cash's statuses),
    each dated by its broker FILL time when it is a broker order.
    Positive = long, negative = short. None = unverifiable (a broker
    lookup failed) — the caller defers the event; never a guess."""
    from order_status_cache import get_order_cached
    try:
        with sqlite3.connect(db_path) as conn:
            cols = _trades_cols(conn)
            occ = " AND occ_symbol IS NULL" if "occ_symbol" in cols else ""
            dq = " AND data_quality IS NULL" if "data_quality" in cols else ""
            ace = ((" AND (COALESCE(status, 'open') != 'auto_closed_external'"
                    " OR COALESCE(fill_price, 0) > 0)")
                   if "fill_price" in cols else "")
            strat = "strategy" if "strategy" in cols else "NULL"
            rows = conn.execute(
                f"SELECT order_id, side, qty, timestamp, {strat} "
                "FROM trades WHERE UPPER(symbol) = ? "
                "AND side IN ('buy', 'sell', 'short', 'cover') "
                "AND substr(timestamp, 1, 10) <= ? "
                "AND COALESCE(status, 'open') NOT IN "
                f"({_NEVER_FILLED_STATUSES}){ace}{occ}{dq}",
                ((symbol or "").upper(), snapshot_iso[:10]),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("_own_stock_qty_at UNVERIFIABLE for %s/%s: %s",
                       db_path, symbol, exc)
        return None
    net = 0.0
    for order_id, side, qty, ts, strategy in rows:
        effective = _utc_iso(ts)
        oid = str(order_id or "")
        if strategy != "alpaca_activity" and _UUID_RX.match(oid):
            try:
                order = get_order_cached(api, oid)
            except Exception as exc:
                logger.warning(
                    "_own_stock_qty_at UNVERIFIABLE for %s/%s: order %s "
                    "lookup failed (%s: %s) — event deferred",
                    db_path, symbol, oid[:8], type(exc).__name__, exc)
                return None
            filled_at = _utc_iso(getattr(order, "filled_at", None))
            if filled_at is None:
                continue  # never filled at the broker → not a position
            effective = filled_at
        if effective is None or effective > snapshot_iso:
            continue
        q = float(qty or 0)
        net += q if side in ("buy", "cover") else -q
    return net


def _own_stock_qty_live(db_path: str, symbol: str) -> Optional[float]:
    """Signed live stock qty from the canonical position lens."""
    from journal import get_virtual_positions
    try:
        total = 0.0
        for pos in get_virtual_positions(db_path=db_path,
                                         price_fetcher=lambda s: 0.0,
                                         include_unpriced=True):
            if pos.get("occ_symbol"):
                continue
            if (pos.get("symbol") or "").upper() == (symbol or "").upper():
                total += float(pos.get("qty") or 0)
        return total
    except Exception as exc:
        logger.warning("_own_stock_qty_live UNVERIFIABLE for %s/%s: %s",
                       db_path, symbol, exc)
        return None


def _own_stock_avg_entry(db_path: str, symbol: str,
                         entry_side: str) -> Optional[float]:
    """Average executed entry price of this profile's live stock
    entry rows (`entry_side` 'buy' or 'short') — the basis for the
    realized pnl of a broker-initiated close. None = unverifiable."""
    try:
        with sqlite3.connect(db_path) as conn:
            cols = _trades_cols(conn)
            px = ("COALESCE(NULLIF(fill_price, 0), price)"
                  if "fill_price" in cols else "price")
            occ = " AND occ_symbol IS NULL" if "occ_symbol" in cols else ""
            row = conn.execute(
                f"SELECT COALESCE(SUM(qty * {px}), 0), COALESCE(SUM(qty), 0) "
                "FROM trades WHERE UPPER(symbol) = ? AND side = ? "
                "AND COALESCE(status, 'open') IN ('open', 'pending_fill')"
                f"{occ}",
                ((symbol or "").upper(), entry_side),
            ).fetchone()
            notional, qty = float(row[0] or 0), float(row[1] or 0)
            return (notional / qty) if qty > 0 else None
    except sqlite3.Error as exc:
        logger.warning("_own_stock_avg_entry UNVERIFIABLE for %s/%s: %s",
                       db_path, symbol, exc)
        return None


def _sibling_db_paths(ctx) -> Optional[List[Tuple[int, str]]]:
    """(profile_id, db_path) for every active profile on this ctx's
    execution account, this profile included. None when the account
    cannot be resolved — attribution then fails closed."""
    aid = getattr(ctx, "alpaca_account_id", None)
    pid = getattr(ctx, "profile_id", None)
    if not aid:
        return None
    try:
        from models import get_active_profiles, build_user_context_from_profile
        out: List[Tuple[int, str]] = []
        for prof in get_active_profiles():
            if prof.get("alpaca_account_id") != aid:
                continue
            out.append((prof["id"],
                        build_user_context_from_profile(prof["id"]).db_path))
        if pid is not None and all(p != pid for p, _ in out):
            out.append((pid, ctx.db_path))
        return out
    except Exception as exc:
        logger.warning("_sibling_db_paths UNVERIFIABLE for account %s: %s",
                       aid, exc)
        return None


def _attribute_account_event(ctx, api, activity_id: str, symbol: str,
                             snapshot_iso: str, account_qty: float,
                             ) -> Optional[Dict[int, float]]:
    """Split an account-level event across the account's books:
    {profile_id: own signed qty at the snapshot}. Returns None (book
    nothing) unless Σ own qty == the broker's `account_qty` within
    half a share — the exact decomposition invariant certify_books
    enforces. Successful splits are memoized per activity for an hour
    (the same event is re-examined by every sibling's pass)."""
    hit = _attribution_memo.get(activity_id)
    if hit is not None and (time.time() - hit[0]) < _ATTRIBUTION_TTL_S:
        return hit[1]
    sibs = _sibling_db_paths(ctx)
    if sibs is None:
        return None
    shares: Dict[int, float] = {}
    for pid, db_path in sibs:
        q = _own_stock_qty_at(api, db_path, symbol, snapshot_iso)
        if q is None:
            return None
        shares[pid] = q
    total = sum(shares.values())
    if abs(total - float(account_qty)) > 0.5:
        logger.warning(
            "activities_capture: %s %s does NOT decompose at %sZ — "
            "Σ own qty %+g across %d book(s) vs broker %+g; event NOT "
            "booked (fail-closed). Books: %s",
            activity_id, symbol, snapshot_iso[:16], total, len(shares),
            account_qty, {f"p{p}": q for p, q in shares.items()},
        )
        return None
    _attribution_memo[activity_id] = (time.time(), shares)
    return shares


def _alert_once(ctx, alert_type: str, severity: str, title: str,
                detail: str, key: str) -> None:
    """Write an audit_alerts row (the /issues surface) unless one
    already carries `key` in its detail — one alert per event."""
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return
    try:
        with sqlite3.connect(db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM audit_alerts WHERE detail LIKE ? LIMIT 1",
                (f"%{key}%",),
            ).fetchone()
            if exists:
                return
    except sqlite3.Error as exc:
        logger.debug("audit_alerts dedup read failed (%s) — writing", exc)
    from halt_helpers import _write_audit_alert
    _write_audit_alert(db_path, alert_type, severity, title, detail)


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


def _write_dividend(ctx, activity: Any, api=None) -> bool:
    """DIV → THIS PROFILE'S share of the account's dividend, as a
    cash-only row: side='dividend' (credit) or 'cash_debit' (a SHORT
    position pays the dividend), qty=1, price=|own amount|,
    pnl=own amount (signed, so realized composes with cash and the
    equity identity holds).

    Rewritten 2026-10-26 — the account-level DIV was being written
    VERBATIM into every profile sharing the account (the very defect
    the 07-25 option-event rewrite named and fixed for OPEXP/OPASN/
    OPEXC; this writer was left behind), and always as a CREDIT
    (price=abs(net), side='dividend') even when the account was net
    short and the broker DEBITED it. First dividends of Experiment 2
    (10-14..10-22): 44 rows over 12 books, each account's net amount
    booked 4×; on account 61 all four events belonged to p229 alone,
    yet p232/p235/p238 each carried +$312.29 of phantom cash with
    sign-mixed pnl — the "+358 decomposition gap" that held the kill
    switch, and a book that read CLEAN once recompute_realized_pnl
    flipped the pnl signs to match the wrong cash.

    Attribution: own signed qty at the broker's entitlement instant
    (`_entitlement_snapshot_utc` — the prior close, by fill time) ×
    `per_share_amount`; the siblings' own quantities must sum to the
    broker's position (net_amount / per_share_amount) or the event is
    not booked (fail-closed, audit alert). A profile with no position
    at the snapshot writes nothing. Field names per Alpaca's
    NonTradeActivity entity (verified 2026-05-17 / 2026-10-26): `id`,
    `symbol`, `date`, `per_share_amount`, `net_amount`, and the
    record date only inside `description` ("... Rec Date: YYYY-MM-DD").
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

    def _refuse(why: str) -> bool:
        logger.warning(
            "activities_capture: DIV %s (%s $%+.2f) NOT booked — %s. "
            "Fail-closed: the cash-parity audit will show the drift "
            "until the event is booked.", activity_id, symbol, amount, why)
        _alert_once(
            ctx, "dividend_unattributable", "warning",
            f"Dividend not booked: {symbol} ${amount:+.2f}",
            f"Activity {activity_id}: {why}. The shared account's "
            f"dividend could not be split across its books, so no "
            f"book received it. {getattr(activity, 'description', '')}",
            activity_id,
        )
        return False

    try:
        per_share = float(getattr(activity, "per_share_amount", None) or 0)
    except (ValueError, TypeError):
        per_share = 0.0
    if per_share <= 0:
        return _refuse("no per_share_amount on the activity")
    desc = str(getattr(activity, "description", "") or "")
    m = _REC_DATE_RX.search(desc)
    if not m:
        return _refuse("no 'Rec Date' in the activity description")
    record_date = m.group(1)
    account_qty = float(round(amount / per_share))
    if abs(account_qty * per_share - amount) > 0.011:
        return _refuse(
            f"net_amount {amount:+.2f} is not a whole number of shares "
            f"at {per_share}/sh")
    snapshot = _entitlement_snapshot_utc(record_date)
    if snapshot is None:
        return _refuse(f"unparseable record date {record_date!r}")
    if api is None:
        api = (ctx.get_alpaca_api() if hasattr(ctx, "get_alpaca_api")
               else getattr(ctx, "api", None))
    shares = _attribute_account_event(
        ctx, api, activity_id, symbol, snapshot, account_qty)
    if shares is None:
        return _refuse(
            f"the account's books do not sum to the broker's "
            f"{account_qty:+.0f} {symbol} at {snapshot[:16]}Z")
    own = shares.get(getattr(ctx, "profile_id", None), 0.0)
    if abs(own) < 0.5:
        logger.info(
            "activities_capture: DIV %s %s is not this profile's (own "
            "qty 0 at %sZ)", activity_id, symbol, snapshot[:16])
        return False
    own_amount = round(own * per_share, 2)
    if own_amount == 0:
        return False
    side = "dividend" if own_amount > 0 else "cash_debit"
    try:
        log_trade(
            symbol=symbol or "CASH",
            side=side,
            qty=1,
            price=abs(own_amount),
            order_id=activity_id,
            signal_type="DIVIDEND",
            strategy="alpaca_activity",
            reason=(
                f"dividend {symbol} ${per_share:.4f}/sh × own {own:+.0f} "
                f"at {snapshot[:16]}Z (close before rec {record_date}) "
                f"= ${own_amount:+.2f}; account net ${amount:+.2f} over "
                f"{account_qty:+.0f} sh split across {len(shares)} book(s)"
            ),
            status="closed",
            pnl=own_amount,
            decision_price=abs(own_amount),
            db_path=ctx.db_path,
        )
        logger.info(
            "[%s] DIV captured: %s own %+.0f × %.4f = $%+.2f as %s "
            "(account net $%+.2f; activity=%s)",
            getattr(ctx, "display_name", "?"), symbol, own, per_share,
            own_amount, side, amount, activity_id,
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


def _write_cash_merger(ctx, activity: Any, api=None) -> bool:
    """MA (cash merger): close THIS PROFILE'S stock position at the
    merger price. Born 2026-10-26 from WBD: Alpaca reports a cash
    merger as two MA rows on the removal date — a share leg
    (symbol, qty=-held, price=cost basis, net_amount=0, description
    'Cash Merger $<px> per share') and a cash leg (qty None,
    net_amount=held×px). The share leg drives the booking: own qty at
    the prior close (same attribution + Σ-check as dividends) is
    written as a 'sell' (long) / 'cover' (short) exit row at <px>
    with fill_price stamped (so the fill poller never polls it) and
    realized pnl from the profile's own average entry; the live entry
    rows are flipped 'closed' via journal.close_stock_entry_rows so
    the reconciler stops reading a broker-flat orphan. The cash leg
    writes nothing — the exit row IS the cash. A merger whose
    description does not name a cash price (stock consideration) is
    refused with an audit alert: the journal cannot book shares it
    was never told about."""
    from journal import log_trade, close_stock_entry_rows
    activity_id = str(getattr(activity, "id", ""))
    if not activity_id:
        return False
    if _has_activity(ctx.db_path, activity_id):
        return False
    symbol = (getattr(activity, "symbol", "") or "").upper()
    desc = str(getattr(activity, "description", "") or "")
    try:
        removed = -float(getattr(activity, "qty", None) or 0)
    except (ValueError, TypeError):
        removed = 0.0
    if removed == 0:
        logger.debug("activities_capture: MA %s %s cash leg — the share "
                     "leg books the exit; nothing to write", activity_id,
                     symbol)
        return False

    def _refuse(why: str, severity: str = "critical") -> bool:
        logger.warning(
            "activities_capture: MA %s (%s %+g) NOT booked — %s. The "
            "integrity gate / reconciler will hold the drift until the "
            "event is booked by hand.", activity_id, symbol, removed, why)
        _alert_once(
            ctx, "corporate_action_unbooked", severity,
            f"Corporate action not booked: {symbol} MA",
            f"Activity {activity_id} ({desc}): {why}. Broker removed "
            f"{removed:+g} {symbol} from the account.", activity_id)
        return False

    m = _CASH_MERGER_RX.search(desc)
    if not m:
        return _refuse("not a cash merger (no '$x per share' price in the "
                       "description) — stock consideration must be "
                       "booked by hand")
    per_share = float(m.group(1))
    snapshot = _entitlement_snapshot_utc(str(getattr(activity, "date", "")))
    if snapshot is None:
        return _refuse("activity has no usable date")
    if api is None:
        api = (ctx.get_alpaca_api() if hasattr(ctx, "get_alpaca_api")
               else getattr(ctx, "api", None))
    shares = _attribute_account_event(
        ctx, api, activity_id, symbol, snapshot, removed)
    if shares is None:
        return _refuse(f"the account's books do not sum to the broker's "
                       f"{removed:+g} {symbol} at {snapshot[:16]}Z")
    own = shares.get(getattr(ctx, "profile_id", None), 0.0)
    if abs(own) < 0.5:
        return False
    live = _own_stock_qty_live(ctx.db_path, symbol)
    if live is None or abs(live - own) > 0.5:
        return _refuse(f"own qty at the snapshot was {own:+g} but the live "
                       f"book holds {live} — the position changed since; "
                       "refusing to write a blind exit")
    entry_side = "buy" if own > 0 else "short"
    exit_side = "sell" if own > 0 else "cover"
    basis = _own_stock_avg_entry(ctx.db_path, symbol, entry_side)
    if basis is None:
        return _refuse("no live entry rows to take the cost basis from")
    qty = abs(own)
    pnl = round(((per_share - basis) if own > 0 else (basis - per_share))
                * qty, 2)
    try:
        log_trade(
            symbol=symbol,
            side=exit_side,
            qty=qty,
            price=per_share,
            order_id=activity_id,
            signal_type="MA",
            strategy="alpaca_activity",
            reason=(
                f"cash merger: {exit_side}-to-close x{qty:g} @ ${per_share} "
                f"(broker-initiated; account removed {removed:+g}; own "
                f"{own:+g} at {snapshot[:16]}Z; realized {pnl:+.2f} from "
                f"avg entry {basis:.4f})"
            ),
            status="closed",
            decision_price=per_share,
            fill_price=per_share,
            pnl=pnl,
            db_path=ctx.db_path,
        )
        flipped = close_stock_entry_rows(
            ctx.db_path, symbol, entry_side,
            f"position removed by cash merger (activity {activity_id})")
        if not flipped:
            logger.warning(
                "activities_capture: MA %s booked but flipped %s entry "
                "row(s) for %s — the reconciler may still see an open "
                "entry", activity_id, flipped, symbol)
        logger.info(
            "[%s] MA captured: %s %s-to-close x%g @ %.4f pnl %+.2f "
            "(activity=%s)", getattr(ctx, "display_name", "?"), symbol,
            exit_side, qty, per_share, pnl, activity_id)
        return True
    except (sqlite3.OperationalError, ValueError) as exc:
        logger.error("activities_capture: MA %s log_trade failed: %s",
                     activity_id, exc)
        return False


def _explain_unhandled_reorgs(ctx, api, since: datetime) -> int:
    """Corporate actions the journal cannot book (`_REORG_EXPLAIN_
    TYPES`): when one names a symbol this profile holds live, write
    one audit alert per event so the drift halt that follows is
    explained on /issues. Fetched once per account per hour. Returns
    the number of alerts written."""
    aid = getattr(ctx, "alpaca_account_id", None)
    written = 0
    for a_type in _REORG_EXPLAIN_TYPES:
        key = (aid, a_type)
        hit = _reorg_fetch_memo.get(key)
        if aid and hit and (time.time() - hit[0]) < _REORG_FETCH_TTL_S:
            acts = hit[1]
        else:
            try:
                acts = list(api.get_activities(
                    activity_types=a_type, after=since.isoformat()) or [])
            except Exception as exc:
                logger.warning(
                    "activities_capture: get_activities(%s) failed for "
                    "profile %s: %s", a_type,
                    getattr(ctx, "profile_id", "?"), exc)
                continue
            if aid:
                _reorg_fetch_memo[key] = (time.time(), acts)
        for a in acts:
            activity_id = str(getattr(a, "id", "") or "")
            symbol = (getattr(a, "symbol", "") or "").upper()
            if not activity_id or not symbol:
                continue
            live = _own_stock_qty_live(ctx.db_path, symbol)
            if not live:
                continue
            _alert_once(
                ctx, "corporate_action_unbooked", "critical",
                f"Unhandled corporate action: {symbol} {a_type}",
                f"Activity {activity_id} ({a_type}) on {symbol}: qty="
                f"{getattr(a, 'qty', None)} net=${getattr(a, 'net_amount', None)} "
                f"'{getattr(a, 'description', '')}'. This profile holds "
                f"{live:+g} {symbol}. The journal has no handler for "
                f"{a_type}; any broker drift / orphan halt on {symbol} is "
                f"this event — book it by hand.", activity_id)
            written += 1
    return written


def _is_option_symbol(sym: str) -> bool:
    """OCC option symbols are UNDERLYING + YYMMDD + C/P + strike."""
    return bool(sym) and len(sym) > 6 and any(c.isdigit() for c in sym[1:7])


def _fee_shape(description: str, activity_date: str):
    """(kind, base, day) for a FEE description, or None when the shape
    is unknown. base is dollars (REG/OPT_REG), a count (CAT/OPT_TAF/
    ORF) or None (OCC); day is the trading day the fee was charged on."""
    desc = str(description or "").strip()
    for kind, rx in _FEE_SHAPES:
        m = rx.match(desc)
        if not m:
            continue
        if kind == "OCC":
            return kind, None, str(activity_date or "")[:10]
        raw, day = m.group(1), m.group(2)
        try:
            base = float(raw.replace(",", ""))
        except ValueError:
            return None
        return kind, base, day
    return None


def _account_fills_on(api, aid, day: str) -> list:
    """The account's FILL activities for trading day `day`
    (YYYY-MM-DD, UTC window), as dicts {order_id, symbol, side, qty,
    price, is_option}; memoized per (account, day) for an hour. Raises
    on a broker failure (the caller defers the fee)."""
    key = (aid, day)
    hit = _fills_memo.get(key)
    if hit and (time.time() - hit[0]) < _FILLS_TTL_S:
        return hit[1]
    from datetime import date as _date
    d0 = _date.fromisoformat(day)
    d1 = d0 + timedelta(days=1)
    out: list = []
    token = None
    while True:
        kw = dict(activity_types="FILL", after=f"{d0.isoformat()}T00:00:00Z",
                  until=f"{d1.isoformat()}T00:00:00Z", page_size=100,
                  direction="asc")
        if token:
            kw["page_token"] = token
        acts = list(api.get_activities(**kw) or [])
        for a in acts:
            sym = (getattr(a, "symbol", "") or "").upper()
            out.append({
                "order_id": str(getattr(a, "order_id", "") or ""),
                "symbol": sym,
                "side": str(getattr(a, "side", "") or "").lower(),
                "qty": float(getattr(a, "qty", 0) or 0),
                "price": float(getattr(a, "price", 0) or 0),
                "is_option": _is_option_symbol(sym),
            })
        if len(acts) < 100:
            break
        token = getattr(acts[-1], "id", None)
        if not token:
            break
    if aid:
        _fills_memo[key] = (time.time(), out)
    return out


def _account_leg_parents(api, aid, day: str) -> Dict[str, str]:
    """{leg order id → parent order id} for the account's multi-leg and
    bracket orders on `day`. The broker reports FILL activities under
    the LEG id, while a spread is journaled under its PARENT id (the
    MULTILEG rows share it) and `get_order(leg_id)` 404s — legs are only
    addressable through the parent (found 2026-11-01: p231's CDE put
    spread on 10-21 left both leg fills "owned by no book" and every
    fee for that day refused). Memoized per (account, day) for an hour.
    Raises on a broker failure (the caller defers)."""
    key = (aid, day)
    hit = _leg_parent_memo.get(key)
    if hit and (time.time() - hit[0]) < _FILLS_TTL_S:
        return hit[1]
    from datetime import date as _date
    d0 = _date.fromisoformat(day)
    d1 = d0 + timedelta(days=1)
    out: Dict[str, str] = {}
    for o in list(api.list_orders(status="all", after=f"{d0.isoformat()}T00:00:00Z",
                                  until=f"{d1.isoformat()}T00:00:00Z",
                                  limit=500, nested=True) or []):
        parent = str(getattr(o, "id", "") or "")
        for leg in (getattr(o, "legs", None) or []):
            leg_id = str(getattr(leg, "id", "") or "")
            if leg_id and parent:
                out[leg_id] = parent
    if aid:
        _leg_parent_memo[key] = (time.time(), out)
    return out


def _journal_order_ids(db_path: str) -> Optional[set]:
    """Every order_id this journal has ever recorded (5-minute memo).
    None = unreadable (fail closed)."""
    hit = _order_ids_memo.get(db_path)
    if hit and (time.time() - hit[0]) < 300.0:
        return hit[1]
    try:
        with sqlite3.connect(db_path) as conn:
            ids = {str(r[0]) for r in conn.execute(
                "SELECT order_id FROM trades WHERE order_id IS NOT NULL")}
    except sqlite3.Error as exc:
        logger.warning("_journal_order_ids UNVERIFIABLE for %s: %s",
                       db_path, exc)
        return None
    _order_ids_memo[db_path] = (time.time(), ids)
    return ids


def _fee_metric(kind: str, fills: list) -> float:
    """The base the broker charged `kind` on, computed from one book's
    fills for the day: sell proceeds (REG), option sell premium
    (OPT_REG), fill events (CAT), shares sold (TAF), option contracts
    sold (OPT_TAF), option contracts traded (ORF, OCC)."""
    if kind == "REG":
        return sum(f["qty"] * f["price"] for f in fills
                   if not f["is_option"] and f["side"].startswith("sell"))
    if kind == "OPT_REG":
        return sum(f["qty"] * f["price"] * 100.0 for f in fills
                   if f["is_option"] and f["side"].startswith("sell"))
    if kind == "CAT":
        return float(len(fills))
    if kind == "TAF":
        return sum(f["qty"] for f in fills
                   if not f["is_option"] and f["side"].startswith("sell"))
    if kind == "OPT_TAF":
        return sum(f["qty"] for f in fills
                   if f["is_option"] and f["side"].startswith("sell"))
    if kind in ("ORF", "OCC"):
        return sum(f["qty"] for f in fills if f["is_option"])
    return 0.0


def _split_cents(total_cents: int, weights: Dict[int, float]) -> Dict[int, int]:
    """Largest-remainder split of `total_cents` across books by weight:
    the shares are integers and sum exactly to the total."""
    w_sum = sum(w for w in weights.values() if w > 0)
    if total_cents == 0 or w_sum <= 0:
        return {pid: 0 for pid in weights}
    raw = {pid: (total_cents * max(w, 0.0) / w_sum) for pid, w in weights.items()}
    shares = {pid: int(v) for pid, v in raw.items()}
    short = total_cents - sum(shares.values())
    for pid, _ in sorted(((p, raw[p] - shares[p]) for p in raw),
                         key=lambda kv: (-kv[1], kv[0]))[:short]:
        shares[pid] += 1
    return shares


def _write_fee(ctx, activity: Any, api=None) -> bool:
    """FEE → THIS PROFILE'S share of an account-level broker fee as a
    cash-only 'cash_debit' row (pnl = −share). See the module notes
    above `_FEE_SHAPES` for the attribution rule and its validation."""
    from journal import log_trade
    activity_id = str(getattr(activity, "id", ""))
    if not activity_id:
        return False
    if _has_activity(ctx.db_path, activity_id):
        return False
    desc = str(getattr(activity, "description", "") or "")
    try:
        net = float(getattr(activity, "net_amount", 0) or 0)
    except (ValueError, TypeError):
        return False
    if net == 0:
        return False

    def _refuse(why: str) -> bool:
        logger.warning(
            "activities_capture: FEE %s ($%+.2f, %r) NOT booked — %s. "
            "Fail-closed: the account cash-parity audit still adjusts "
            "for this fee broker-side until it is booked.",
            activity_id, net, desc[:60], why)
        _alert_once(
            ctx, "fee_unattributable", "warning",
            f"Broker fee not booked: ${net:+.2f}",
            f"Activity {activity_id} ({desc}): {why}.", activity_id)
        return False

    shape = _fee_shape(desc, getattr(activity, "date", ""))
    if shape is None:
        return _refuse("unknown fee description shape")
    kind, base, day = shape
    if not day:
        return _refuse("no trading day on the activity")
    if api is None:
        api = (ctx.get_alpaca_api() if hasattr(ctx, "get_alpaca_api")
               else getattr(ctx, "api", None))
    sibs = _sibling_db_paths(ctx)
    if sibs is None:
        return _refuse("the account's books cannot be enumerated")
    try:
        fills = _account_fills_on(api, getattr(ctx, "alpaca_account_id", None), day)
    except Exception as exc:
        logger.warning("activities_capture: FEE %s deferred — fills for %s "
                       "unavailable (%s: %s)", activity_id, day,
                       type(exc).__name__, exc)
        return False
    # own order_id → book; every fill must belong to exactly one book
    by_book: Dict[int, list] = {pid: [] for pid, _ in sibs}
    owners: Dict[str, int] = {}
    for pid, db_path in sibs:
        ids = _journal_order_ids(db_path)
        if ids is None:
            return _refuse(f"book p{pid} unreadable")
        for f in fills:
            if f["order_id"] in ids:
                if f["order_id"] in owners and owners[f["order_id"]] != pid:
                    return _refuse(f"fill order {f['order_id'][:8]} is claimed "
                                   f"by two books")
                owners[f["order_id"]] = pid
    if any(f["order_id"] not in owners for f in fills):
        # A spread's fills carry LEG ids; the journal carries the PARENT.
        try:
            parents = _account_leg_parents(
                api, getattr(ctx, "alpaca_account_id", None), day)
        except Exception as exc:
            logger.warning("activities_capture: FEE %s deferred — orders for %s "
                           "unavailable (%s: %s)", activity_id, day,
                           type(exc).__name__, exc)
            return False
        for f in fills:
            if f["order_id"] in owners:
                continue
            parent = parents.get(f["order_id"])
            if parent is None:
                continue
            for pid, db_path in sibs:
                ids = _journal_order_ids(db_path) or set()
                if parent in ids:
                    owners[f["order_id"]] = pid
                    break
    unowned = sorted({f["order_id"][:8] for f in fills if f["order_id"] not in owners})
    if unowned:
        return _refuse(f"{len(unowned)} fill order(s) on {day} belong to no "
                       f"book: {unowned[:5]}")
    for f in fills:
        by_book[owners[f["order_id"]]].append(f)
    metrics = {pid: _fee_metric(kind, fl) for pid, fl in by_book.items()}
    total = sum(metrics.values())
    if base is None:
        if total <= 0:
            return _refuse(f"no option fills on {day} to carry an OCC fee")
    elif kind in ("REG", "OPT_REG"):
        if abs(total - base) > max(0.02, 0.001 * base):
            return _refuse(f"books' {kind} base {total:.2f} != broker's "
                           f"{base:.2f} on {day}")
    else:
        if abs(total - base) > 0.5:
            return _refuse(f"books' {kind} base {total:g} != broker's "
                           f"{base:g} on {day}")
    cents = _split_cents(int(round(abs(net) * 100)), metrics)
    own_cents = cents.get(getattr(ctx, "profile_id", None), 0)
    if own_cents <= 0:
        return False
    own = own_cents / 100.0
    side = "cash_debit" if net < 0 else "dividend"
    signed = -own if net < 0 else own
    try:
        log_trade(
            symbol="FEE",
            side=side,
            qty=1,
            price=own,
            order_id=activity_id,
            signal_type="FEE",
            strategy="alpaca_activity",
            reason=(f"broker fee {kind} {day}: own share ${signed:+.2f} of "
                    f"${net:+.2f} (base {metrics[getattr(ctx, 'profile_id', None)]:g}"
                    f"/{total:g} across {sum(1 for m in metrics.values() if m > 0)} "
                    f"book(s)); {desc[:80]}"),
            status="closed",
            pnl=signed,
            decision_price=own,
            db_path=ctx.db_path,
        )
        logger.info("[%s] FEE captured: %s %s own $%+.2f of $%+.2f (activity=%s)",
                    getattr(ctx, "display_name", "?"), kind, day, signed, net,
                    activity_id)
        return True
    except (sqlite3.OperationalError, ValueError) as exc:
        logger.error("activities_capture: FEE %s log_trade failed: %s",
                     activity_id, exc)
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
            # 2026-11-01 — PAGE. A single call returns at most one page
            # (100 rows); the 7-day window never hit it, but the fee
            # backfill from the 08-24 reset did (319 FEE rows on one
            # account → 100 fetched, 219 silently never seen).
            _token = None
            while True:
                _kw = dict(activity_types=_a_type, after=since.isoformat(),
                           page_size=100)
                if _token:
                    _kw["page_token"] = _token
                batch = list(api.get_activities(**_kw) or [])
                activities.extend(batch)
                if len(batch) < 100:
                    break
                _token = getattr(batch[-1], "id", None)
                if not _token:
                    break
        except Exception as exc:
            logger.warning(
                "activities_capture: get_activities(%s) failed for "
                "profile %s: %s",
                _a_type, getattr(ctx, "profile_id", "?"), exc,
            )

    for a in activities:
        a_type = getattr(a, "activity_type", "")
        if a_type == "DIV":
            if _write_dividend(ctx, a, api):
                summary["DIV"] += 1
        elif a_type in ("OPEXP", "OPASN", "OPEXC"):
            if _write_option_expiry_or_exercise(ctx, a):
                summary[a_type] += 1
        elif a_type == "MA":
            if _write_cash_merger(ctx, a, api):
                summary["MA"] += 1
        elif a_type == "FEE":
            if _write_fee(ctx, a, api):
                summary["FEE"] += 1
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
    try:
        n = _explain_unhandled_reorgs(ctx, api, since)
        if n:
            summary["REORG_ALERT"] = n
    except Exception as exc:
        logger.warning("activities_capture: reorg explain pass failed for "
                       "profile %s: %s", getattr(ctx, "profile_id", "?"), exc)
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
