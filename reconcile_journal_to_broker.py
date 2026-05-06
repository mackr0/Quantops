"""Reconcile each profile's journal against broker truth.

The journal-broker drift problem: the periodic
_task_reconcile_trade_statuses used to read the journal as its own
source of truth for virtual profiles, so it could never detect drift.
On 2026-05-06 we found 40/126 (31%) "open" journal entries across
11 profiles were phantoms — entries that were canceled-without-fill,
or that the broker had already closed via a protective stop without
the journal getting the SELL/COVER row.

This module is the broker-aware reconcile that closes the loop:
  - cancel-without-fill — entry order canceled/expired/rejected
    with filled_qty=0. Mark journal status='canceled'.
  - broker-sold-via-stop (long) — entry filled, broker has 0 shares.
    Find matching broker SELL fill, INSERT a SELL row, mark BUY closed.
  - broker-covered-via-stop (short) — same pattern for shorts. Match
    a broker BUY fill, INSERT a COVER row, mark SHORT closed.
  - partial-sale drift (long) — broker has SOME shares but fewer than
    the journal claims; a stop fired for a portion. Backfill SELL
    rows for the closed portion. BUY stays open with reduced qty (the
    FIFO consumes the SELL from the lot — original BUY row qty isn't
    edited).
  - partial-cover drift (short) — symmetrical.
  - partial-fill on entry — entry order canceled with filled_qty>0.
    Update journal qty to filled_qty, fix the entry price to actual
    fill, then re-evaluate as a normal open position.
  - api errors — retry with exponential backoff before flagging
    ambiguous so a transient broker hiccup doesn't leave drift open.

Use:
  python3 reconcile_journal_to_broker.py            # dry-run
  python3 reconcile_journal_to_broker.py --apply    # write changes
  python3 reconcile_journal_to_broker.py --profile 11 --apply
  python3 reconcile_journal_to_broker.py --quiet    # cron-friendly
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Number of retries for transient broker API failures before flagging
# the entry as ambiguous. Each retry waits 2^attempt seconds.
_API_MAX_RETRIES = 3


def _to_utc_iso(value) -> Optional[datetime]:
    """Coerce a journal timestamp (TEXT) to a UTC-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _broker_qty_for(positions, symbol: str) -> float:
    """Return the broker's current qty for a symbol. Negative = short."""
    sym_u = (symbol or "").upper()
    for p in positions:
        if (getattr(p, "symbol", "") or "").upper() == sym_u:
            try:
                return float(getattr(p, "qty", 0) or 0)
            except Exception:
                return 0
    return 0


def _retrying_call(func, *args, **kwargs):
    """Call a broker API function with exponential-backoff retries on
    transient failure. Returns (result, exception_or_None)."""
    last_exc = None
    for attempt in range(_API_MAX_RETRIES):
        try:
            return func(*args, **kwargs), None
        except Exception as exc:
            last_exc = exc
            if attempt < _API_MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
    return None, last_exc


def _find_matching_exit_fill(api, symbol: str, qty: float, after_ts: datetime,
                              broker_exit_side: str,
                              already_used_order_ids: set) -> Optional[dict]:
    """Find a broker order on `broker_exit_side` (sell|buy) that filled
    `qty` shares of `symbol` after `after_ts`.

    Long exit → broker_exit_side='sell' (we sell to close).
    Short cover → broker_exit_side='buy' (we buy to cover).

    Multi-profile sharing means one Alpaca account hosts multiple
    profiles' positions. Each profile's BUY/SHORT has its own
    protective orders, so each profile's exit is a separate broker
    order — match by qty (filled_qty == journal qty). Across profiles
    with the same qty (rare), pick the oldest unused fill so a
    multi-profile pass attributes uniquely.

    Returns dict with order_id, filled_at, filled_qty, filled_avg_price,
    order_type — or None if no match.
    """
    orders, exc = _retrying_call(
        api.list_orders, status="all", symbols=[symbol], limit=200,
    )
    if orders is None:
        return None
    candidates = []
    for o in orders:
        if getattr(o, "side", "") != broker_exit_side:
            continue
        if getattr(o, "status", "") != "filled":
            continue
        oid = getattr(o, "id", None)
        if oid in already_used_order_ids:
            continue
        try:
            filled_qty = float(getattr(o, "filled_qty", 0) or 0)
        except Exception:
            continue
        if abs(filled_qty - qty) > 0.001:
            continue
        filled_at = getattr(o, "filled_at", None)
        if hasattr(filled_at, "isoformat"):
            fa_dt = filled_at if filled_at.tzinfo else filled_at.replace(tzinfo=timezone.utc)
        else:
            fa_dt = _to_utc_iso(filled_at)
        if fa_dt is None or fa_dt < after_ts:
            continue
        try:
            fill_price = float(getattr(o, "filled_avg_price", 0) or 0)
        except Exception:
            fill_price = 0
        if fill_price <= 0:
            continue
        candidates.append({
            "order_id": oid,
            "filled_at": fa_dt,
            "filled_qty": filled_qty,
            "filled_avg_price": fill_price,
            "order_type": getattr(o, "order_type", "?"),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["filled_at"])
    return candidates[0]


def _classify_long_phantom(api, row, broker_qty, used_sell_ids):
    """Return ('cancel'|'backfill'|'partial_entry'|'ambiguous', detail).

    Only called when broker_qty <= 0 — i.e. the long position is
    fully gone from the broker but journal still claims it open.
    """
    sym = (row["symbol"] or "").upper()
    qty = float(row["qty"] or 0)
    order_id = row["order_id"]
    ts = _to_utc_iso(row["timestamp"])
    if not order_id:
        return "ambiguous", {"reason": "no order_id in journal"}
    entry_order, exc = _retrying_call(api.get_order, order_id)
    if entry_order is None:
        return "ambiguous", {"reason": f"failed to fetch entry order after retries: {exc}"}
    entry_status = getattr(entry_order, "status", "?")
    try:
        entry_filled = float(getattr(entry_order, "filled_qty", 0) or 0)
    except Exception:
        entry_filled = 0

    if entry_status in ("canceled", "expired", "rejected") and entry_filled == 0:
        return "cancel", {"order_id": order_id, "entry_status": entry_status}
    if entry_status in ("canceled", "expired", "rejected") and entry_filled > 0:
        # Partial fill on entry. Treat the filled portion as real.
        return "partial_entry", {
            "order_id": order_id, "entry_status": entry_status,
            "actual_filled_qty": entry_filled,
            "entry_avg_fill_price": float(getattr(entry_order, "filled_avg_price", 0) or 0),
        }
    if entry_status == "filled":
        # Broker filled the BUY at some point. Now broker has 0 shares,
        # so a SELL must have happened.
        sell_fill = _find_matching_exit_fill(
            api, sym, qty, ts or datetime.now(timezone.utc),
            broker_exit_side="sell",
            already_used_order_ids=used_sell_ids,
        )
        if sell_fill is None:
            return "ambiguous", {
                "reason": "entry filled but no matching broker SELL fill found",
            }
        return "backfill", sell_fill
    return "ambiguous", {
        "reason": f"entry status={entry_status} filled_qty={entry_filled}",
    }


def _classify_short_phantom(api, row, broker_qty, used_cover_ids):
    """Mirror of _classify_long_phantom for shorts.

    A short journal entry stores side='short' (per
    P1.10 of LONG_SHORT_PLAN.md). When the broker covers via a
    buy-to-cover stop, the journal needs a 'cover' row. Otherwise
    the short stays "open" forever in get_virtual_positions.
    """
    sym = (row["symbol"] or "").upper()
    qty = float(row["qty"] or 0)
    order_id = row["order_id"]
    ts = _to_utc_iso(row["timestamp"])
    if not order_id:
        return "ambiguous", {"reason": "no order_id in journal"}
    entry_order, exc = _retrying_call(api.get_order, order_id)
    if entry_order is None:
        return "ambiguous", {"reason": f"failed to fetch entry order after retries: {exc}"}
    entry_status = getattr(entry_order, "status", "?")
    try:
        entry_filled = float(getattr(entry_order, "filled_qty", 0) or 0)
    except Exception:
        entry_filled = 0

    if entry_status in ("canceled", "expired", "rejected") and entry_filled == 0:
        return "cancel", {"order_id": order_id, "entry_status": entry_status}
    if entry_status in ("canceled", "expired", "rejected") and entry_filled > 0:
        return "partial_entry", {
            "order_id": order_id, "entry_status": entry_status,
            "actual_filled_qty": entry_filled,
            "entry_avg_fill_price": float(getattr(entry_order, "filled_avg_price", 0) or 0),
        }
    if entry_status == "filled":
        cover_fill = _find_matching_exit_fill(
            api, sym, qty, ts or datetime.now(timezone.utc),
            broker_exit_side="buy",  # buying to cover
            already_used_order_ids=used_cover_ids,
        )
        if cover_fill is None:
            return "ambiguous", {
                "reason": "entry filled but no matching broker BUY (cover) fill found",
            }
        return "backfill", cover_fill
    return "ambiguous", {
        "reason": f"entry status={entry_status} filled_qty={entry_filled}",
    }


def _detect_partial_sale(api, row, broker_qty, used_sell_ids):
    """For an open BUY where the broker still has SOME shares but
    journal qty differs from broker qty, look for partial protective
    fills.

    Multi-profile sharing complicates exact attribution: profile_4
    BUY 71 + profile_8 BUY 864, broker has 800 BMY. Could be 71+729,
    or 0+800, or any split. We use protective order ids if present;
    otherwise we accept the ambiguity and skip (no false-positive
    backfills — leave them as real_held).

    Returns ('backfill_partial', detail) or (None, None).
    """
    sym = (row["symbol"] or "").upper()
    journal_qty = float(row["qty"] or 0)
    if broker_qty <= 0 or broker_qty >= journal_qty:
        return None, None  # not a partial-sale candidate
    # Look for a protective order on this BUY that fired for the
    # missing portion.
    for col in ("protective_stop_order_id", "protective_tp_order_id",
                "protective_trailing_order_id"):
        try:
            stop_oid = row[col]
        except (KeyError, IndexError):
            stop_oid = None
        if not stop_oid:
            continue
        if stop_oid in used_sell_ids:
            continue
        order, exc = _retrying_call(api.get_order, stop_oid)
        if order is None:
            continue
        if getattr(order, "status", "") != "filled":
            continue
        if getattr(order, "side", "") != "sell":
            continue
        try:
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        except Exception:
            continue
        missing = journal_qty - broker_qty
        if abs(filled_qty - missing) > 0.001:
            continue
        try:
            fill_price = float(getattr(order, "filled_avg_price", 0) or 0)
        except Exception:
            continue
        if fill_price <= 0:
            continue
        filled_at = getattr(order, "filled_at", None)
        if hasattr(filled_at, "isoformat"):
            fa_dt = filled_at if filled_at.tzinfo else filled_at.replace(tzinfo=timezone.utc)
        else:
            fa_dt = _to_utc_iso(filled_at)
        return "backfill_partial", {
            "order_id": stop_oid,
            "filled_at": fa_dt,
            "filled_qty": filled_qty,
            "filled_avg_price": fill_price,
            "order_type": getattr(order, "order_type", "?"),
        }
    return None, None


def _select_open_rows(conn) -> List[sqlite3.Row]:
    """Pull every open journal row (long + short). Tolerate missing
    protective_* columns by selecting them dynamically."""
    cur = conn.execute("PRAGMA table_info(trades)")
    cols = {r[1] for r in cur.fetchall()}
    base_cols = ["id", "symbol", "side", "qty", "status", "order_id",
                 "timestamp", "price"]
    extra_cols = [c for c in (
        "protective_stop_order_id", "protective_tp_order_id",
        "protective_trailing_order_id",
    ) if c in cols]
    all_cols = base_cols + extra_cols
    sql = (f"SELECT {','.join(all_cols)} FROM trades "
           "WHERE status='open' AND side IN ('buy', 'short', 'sell')")
    return conn.execute(sql).fetchall()


def reconcile_with_ctx(ctx, apply_changes: bool = False) -> Dict[str, list]:
    """Reconcile one profile from an already-built UserContext."""
    name = ctx.display_name or f"profile_{getattr(ctx, 'profile_id', '?')}"
    api = ctx.get_alpaca_api() if hasattr(ctx, "get_alpaca_api") else ctx.api
    db_path = ctx.db_path

    actions = {
        "cancel": [],
        "backfill_sell": [],   # long full close
        "backfill_cover": [],  # short full close
        "backfill_partial_sell": [],  # long partial close
        "fix_partial_entry": [],      # update journal qty/price to actual fill
        "ambiguous": [],
        "real_held": 0,
    }

    positions, exc = _retrying_call(api.list_positions)
    if positions is None:
        return {"error": f"failed to fetch positions after retries: {exc}", **actions}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = _select_open_rows(conn)

    used_sell_ids: set = set()
    used_cover_ids: set = set()

    for r in rows:
        sym = (r["symbol"] or "").upper()
        side = (r["side"] or "").lower()
        qty = float(r["qty"] or 0)

        # Determine if this is a long-open or short-open.
        # side='buy' → long open; side='short' → short open (P1.10).
        # side='sell' open is handled by the existing reconcile pass.
        if side == "sell":
            continue
        is_short = (side == "short")
        broker_qty = _broker_qty_for(positions, sym)

        # PARTIAL ENTRY FILL — independent of current broker state.
        # If the entry order status is canceled/expired/rejected with
        # filled_qty>0, correct the journal qty to actual filled, and
        # leave status='open' so next pass re-evaluates.
        order_id = r["order_id"]
        if order_id:
            entry_order, exc = _retrying_call(api.get_order, order_id)
            if entry_order is not None:
                entry_status = getattr(entry_order, "status", "?")
                try:
                    entry_filled = float(getattr(entry_order, "filled_qty", 0) or 0)
                except Exception:
                    entry_filled = 0
                if (entry_status in ("canceled", "expired", "rejected")
                        and 0 < entry_filled < qty - 0.001):
                    actions["fix_partial_entry"].append({
                        "trade_id": r["id"], "symbol": sym, "side": side,
                        "original_qty": qty,
                        "actual_filled_qty": entry_filled,
                        "entry_avg_fill_price": float(
                            getattr(entry_order, "filled_avg_price", 0) or 0,
                        ),
                        "order_id": order_id, "entry_status": entry_status,
                    })
                    continue

        # Normalize: for shorts, "real_held" means broker_qty < 0
        if is_short:
            real_held = broker_qty < -0.001
        else:
            real_held = broker_qty > 0.001

        if real_held:
            # Check for partial-sale drift (longs only — short partial
            # cover is a future enhancement gated on profile_10's first
            # observed case).
            if not is_short and broker_qty < qty - 0.001:
                kind, detail = _detect_partial_sale(
                    api, r, broker_qty, used_sell_ids,
                )
                if kind == "backfill_partial":
                    used_sell_ids.add(detail["order_id"])
                    actions["backfill_partial_sell"].append({
                        "trade_id": r["id"], "symbol": sym,
                        "journal_qty": qty, "broker_qty": broker_qty,
                        "buy_price": float(r["price"] or 0),
                        "sell_order_id": detail["order_id"],
                        "sell_price": detail["filled_avg_price"],
                        "sell_qty": detail["filled_qty"],
                        "sell_filled_at": detail["filled_at"].isoformat()
                            if detail.get("filled_at") else None,
                        "sell_order_type": detail["order_type"],
                    })
                    continue
                # No protective order ID matched the missing qty —
                # treat as real_held with documented drift. Don't
                # falsely backfill.
            actions["real_held"] += 1
            continue

        # Phantom — full close
        if is_short:
            kind, detail = _classify_short_phantom(
                api, r, broker_qty, used_cover_ids,
            )
        else:
            kind, detail = _classify_long_phantom(
                api, r, broker_qty, used_sell_ids,
            )

        if kind == "cancel":
            actions["cancel"].append({
                "trade_id": r["id"], "symbol": sym, "qty": qty,
                "side": side, **detail,
            })
        elif kind == "partial_entry":
            actions["fix_partial_entry"].append({
                "trade_id": r["id"], "symbol": sym,
                "side": side,
                "original_qty": qty,
                **detail,
            })
        elif kind == "backfill":
            if is_short:
                used_cover_ids.add(detail["order_id"])
                actions["backfill_cover"].append({
                    "trade_id": r["id"], "symbol": sym, "qty": qty,
                    "short_price": float(r["price"] or 0),
                    "cover_order_id": detail["order_id"],
                    "cover_price": detail["filled_avg_price"],
                    "cover_qty": detail["filled_qty"],
                    "cover_filled_at": detail["filled_at"].isoformat(),
                    "cover_order_type": detail["order_type"],
                })
            else:
                used_sell_ids.add(detail["order_id"])
                actions["backfill_sell"].append({
                    "trade_id": r["id"], "symbol": sym, "qty": qty,
                    "buy_price": float(r["price"] or 0),
                    "sell_order_id": detail["order_id"],
                    "sell_price": detail["filled_avg_price"],
                    "sell_qty": detail["filled_qty"],
                    "sell_filled_at": detail["filled_at"].isoformat(),
                    "sell_order_type": detail["order_type"],
                })
        elif kind == "ambiguous":
            actions["ambiguous"].append({
                "trade_id": r["id"], "symbol": sym, "qty": qty,
                "side": side, **detail,
            })

    if apply_changes:
        for a in actions["cancel"]:
            conn.execute(
                "UPDATE trades SET status='canceled' WHERE id=?",
                (a["trade_id"],),
            )
        for a in actions["fix_partial_entry"]:
            # Update qty + price to the broker's actual fill, leave
            # status='open'. Next reconcile pass will re-evaluate against
            # broker truth (which now sees the new qty).
            conn.execute(
                "UPDATE trades SET qty=?, price=?, fill_price=?, "
                "reason=COALESCE(reason || ' | ', '') || ? "
                "WHERE id=?",
                (a["actual_filled_qty"], a["entry_avg_fill_price"],
                 a["entry_avg_fill_price"],
                 f"reconcile: corrected partial-fill (was qty={a['original_qty']})",
                 a["trade_id"]),
            )
        for a in actions["backfill_sell"]:
            conn.execute(
                """INSERT INTO trades
                   (timestamp, symbol, side, qty, price, order_id, signal_type,
                    strategy, reason, status, fill_price)
                   VALUES (?, ?, 'sell', ?, ?, ?, 'reconcile_backfill',
                           'reconcile_backfill',
                           'broker exited via protective order — backfilled by reconcile',
                           'closed', ?)""",
                (a["sell_filled_at"], a["symbol"], a["sell_qty"],
                 a["sell_price"], a["sell_order_id"], a["sell_price"]),
            )
            conn.execute(
                "UPDATE trades SET status='closed' WHERE id=?",
                (a["trade_id"],),
            )
        for a in actions["backfill_cover"]:
            conn.execute(
                """INSERT INTO trades
                   (timestamp, symbol, side, qty, price, order_id, signal_type,
                    strategy, reason, status, fill_price)
                   VALUES (?, ?, 'cover', ?, ?, ?, 'reconcile_backfill',
                           'reconcile_backfill',
                           'broker covered via protective order — backfilled by reconcile',
                           'closed', ?)""",
                (a["cover_filled_at"], a["symbol"], a["cover_qty"],
                 a["cover_price"], a["cover_order_id"], a["cover_price"]),
            )
            conn.execute(
                "UPDATE trades SET status='closed' WHERE id=?",
                (a["trade_id"],),
            )
        for a in actions["backfill_partial_sell"]:
            # Insert a SELL row for the closed portion. The original
            # BUY row stays open with original qty — the FIFO consumes
            # the right amount from the lot when computing virtual
            # positions.
            conn.execute(
                """INSERT INTO trades
                   (timestamp, symbol, side, qty, price, order_id, signal_type,
                    strategy, reason, status, fill_price)
                   VALUES (?, ?, 'sell', ?, ?, ?, 'reconcile_backfill_partial',
                           'reconcile_backfill_partial',
                           'broker partially exited via protective order — backfilled by reconcile',
                           'closed', ?)""",
                (a["sell_filled_at"], a["symbol"], a["sell_qty"],
                 a["sell_price"], a["sell_order_id"], a["sell_price"]),
            )
        conn.commit()

    conn.close()

    # Run the existing FIFO P&L backfill so SELL rows get pnl computed.
    has_writes = (actions["cancel"] or actions["backfill_sell"]
                  or actions["backfill_cover"]
                  or actions["backfill_partial_sell"]
                  or actions["fix_partial_entry"])
    if apply_changes and has_writes:
        from journal import reconcile_trade_statuses
        broker_open_symbols = {
            (p.symbol or "").upper() for p in positions
            if float(getattr(p, "qty", 0) or 0) != 0
        }
        reconcile_trade_statuses(db_path=db_path, open_symbols=broker_open_symbols)

    actions["profile"] = name
    actions["profile_id"] = getattr(ctx, "profile_id", None)
    return actions


def reconcile_profile(profile_id: int, apply_changes: bool = False) -> Dict[str, list]:
    """CLI-style: build the ctx from profile_id, then delegate."""
    from models import build_user_context_from_profile
    ctx = build_user_context_from_profile(profile_id)
    return reconcile_with_ctx(ctx, apply_changes=apply_changes)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually write changes (default: dry-run)")
    ap.add_argument("--profile", type=int, default=None,
                    help="run for a single profile id (default: all 1..11)")
    ap.add_argument("--quiet", action="store_true",
                    help="cron-friendly: print only summary + errors")
    args = ap.parse_args()

    profile_ids = [args.profile] if args.profile else list(range(1, 12))

    grand = {"cancel": 0, "backfill_sell": 0, "backfill_cover": 0,
             "backfill_partial_sell": 0, "fix_partial_entry": 0,
             "ambiguous": 0, "real_held": 0, "errored": 0}

    if not args.quiet:
        print(f"=== Reconcile {'APPLY' if args.apply else 'DRY-RUN'} ===\n")

    for p_id in profile_ids:
        try:
            res = reconcile_profile(p_id, apply_changes=args.apply)
        except Exception as e:
            print(f"profile_{p_id}: ERROR {e}")
            grand["errored"] += 1
            continue
        if "error" in res:
            print(f"profile_{p_id} ({res.get('profile')}): ERROR {res['error']}")
            grand["errored"] += 1
            continue

        n_c = len(res["cancel"])
        n_bs = len(res["backfill_sell"])
        n_bc = len(res["backfill_cover"])
        n_bps = len(res["backfill_partial_sell"])
        n_fp = len(res["fix_partial_entry"])
        n_a = len(res["ambiguous"])
        n_r = res["real_held"]

        if not args.quiet or (n_c + n_bs + n_bc + n_bps + n_fp + n_a) > 0:
            print(f"p{p_id:>2} {res['profile'][:30]:<30s}  "
                  f"real={n_r:>3}  cancel={n_c:>2}  bs={n_bs:>2}  "
                  f"bc={n_bc:>2}  bps={n_bps:>2}  fp={n_fp:>2}  amb={n_a:>2}")
        if not args.quiet:
            for a in res["cancel"]:
                print(f"     CANCEL    #{a['trade_id']:<4} {a['symbol']:>5} {a.get('side',''):>5} "
                      f"qty={a['qty']:>6.0f}  entry_status={a['entry_status']}")
            for a in res["backfill_sell"]:
                pnl = (a["sell_price"] - a["buy_price"]) * a["qty"]
                sign = "+" if pnl >= 0 else ""
                print(f"     SELL      #{a['trade_id']:<4} {a['symbol']:>5} qty={a['qty']:>6.0f}  "
                      f"buy=${a['buy_price']:>7.2f} sell=${a['sell_price']:>7.2f}  "
                      f"realized={sign}${pnl:>9.2f}  ({a['sell_order_type']})")
            for a in res["backfill_cover"]:
                pnl = (a["short_price"] - a["cover_price"]) * a["qty"]
                sign = "+" if pnl >= 0 else ""
                print(f"     COVER     #{a['trade_id']:<4} {a['symbol']:>5} qty={a['qty']:>6.0f}  "
                      f"short=${a['short_price']:>7.2f} cover=${a['cover_price']:>7.2f}  "
                      f"realized={sign}${pnl:>9.2f}  ({a['cover_order_type']})")
            for a in res["backfill_partial_sell"]:
                pnl = (a["sell_price"] - a["buy_price"]) * a["sell_qty"]
                sign = "+" if pnl >= 0 else ""
                print(f"     PARTIAL   #{a['trade_id']:<4} {a['symbol']:>5} "
                      f"journal={a['journal_qty']:>5.0f} broker={a['broker_qty']:>5.0f}  "
                      f"sold={a['sell_qty']:>5.0f} @ ${a['sell_price']:>7.2f}  "
                      f"realized={sign}${pnl:>9.2f}")
            for a in res["fix_partial_entry"]:
                print(f"     FIX_QTY   #{a['trade_id']:<4} {a['symbol']:>5} "
                      f"was qty={a['original_qty']:>5.0f}  "
                      f"actual={a['actual_filled_qty']:>5.0f} @ ${a['entry_avg_fill_price']:>7.2f}")
            for a in res["ambiguous"]:
                print(f"     AMBIGUOUS #{a['trade_id']:<4} {a['symbol']:>5} {a.get('side',''):>5} "
                      f"qty={a['qty']:>6.0f}  reason: {a['reason']}")

        grand["cancel"] += n_c
        grand["backfill_sell"] += n_bs
        grand["backfill_cover"] += n_bc
        grand["backfill_partial_sell"] += n_bps
        grand["fix_partial_entry"] += n_fp
        grand["ambiguous"] += n_a
        grand["real_held"] += n_r

    print(f"\n=== TOTALS ===")
    for k, v in grand.items():
        print(f"  {k:<24s}: {v:>3}")
    if not args.apply:
        print(f"\nDry-run only. Re-run with --apply to write changes.")
    if grand["ambiguous"] > 0 or grand["errored"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
