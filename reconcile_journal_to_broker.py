"""Reconcile each profile's journal against broker truth.

Background: the periodic _task_reconcile_trade_statuses uses the
journal as its own source of truth for virtual profiles, so it can
never detect drift between journal and broker. Result observed
2026-05-06: 40/126 (31%) "open" journal entries across 11 profiles
were phantoms — BUYs that never filled at the broker, or BUYs that
filled but the broker subsequently sold via a protective stop without
the journal getting the SELL row.

This tool fixes both classes:
  1. cancel-without-fill — entry order_id has status canceled/expired
     /rejected and filled_qty=0. The journal entry is fictional. Mark
     status='canceled' (NOT 'closed' — distinguishes from real exits).
  2. broker-sold-but-journal-open — entry order_id status=filled but
     no current shares at broker. Find the matching broker SELL fill
     (by symbol + qty + timestamp window), INSERT a SELL row from
     the broker fill, mark the BUY status='closed', let
     reconcile_trade_statuses backfill realized P&L via FIFO.

Use:
  python3 reconcile_journal_to_broker.py            # dry-run
  python3 reconcile_journal_to_broker.py --apply    # write changes
  python3 reconcile_journal_to_broker.py --profile 11 --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


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
    sym_u = (symbol or "").upper()
    for p in positions:
        if (getattr(p, "symbol", "") or "").upper() == sym_u:
            try:
                return float(getattr(p, "qty", 0) or 0)
            except Exception:
                return 0
    return 0


def _find_matching_sell_fill(api, symbol: str, qty: float, after_ts: datetime,
                             already_used_order_ids: set) -> Optional[dict]:
    """Find a broker SELL order that filled the given qty on the given
    symbol after the given timestamp.

    Multi-profile sharing means one Alpaca account hosts multiple
    profiles' positions. Each profile's BUY has its own protective
    stops, so each profile's exit is a separate SELL order — match
    by qty filled_qty == journal qty. Across profiles with the same
    qty (rare), we pick the oldest unused fill so a multi-profile
    pass can attribute uniquely.
    """
    try:
        orders = api.list_orders(status="all", symbols=[symbol], limit=200)
    except Exception:
        return None
    candidates = []
    for o in orders:
        if getattr(o, "side", "") != "sell":
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


def reconcile_with_ctx(ctx, apply_changes: bool = False) -> Dict[str, list]:
    """Reconcile one profile from an already-built UserContext.

    Used both by the scheduler's per-cycle task (where ctx is already
    in hand) and by the CLI entry point that builds ctx from a
    profile id.
    """
    name = ctx.display_name or f"profile_{getattr(ctx, 'profile_id', '?')}"
    api = ctx.get_alpaca_api() if hasattr(ctx, "get_alpaca_api") else ctx.api
    db_path = ctx.db_path

    actions = {
        "cancel": [],          # entries to mark status='canceled'
        "backfill_sell": [],   # entries needing a SELL row + status='closed'
        "ambiguous": [],       # phantom but no broker fill matches
        "real_held": 0,        # entries left alone
    }

    try:
        positions = api.list_positions()
    except Exception as e:
        return {"error": f"failed to fetch positions: {e}", **actions}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, symbol, side, qty, status, order_id, timestamp, price "
        "FROM trades WHERE status='open' AND side='buy'"
    ).fetchall()

    # Track which broker SELL orders we've already attributed so two
    # journal entries with the same qty don't both match the same fill.
    used_sell_order_ids: set = set()

    for r in rows:
        sym = (r["symbol"] or "").upper()
        qty = float(r["qty"] or 0)
        order_id = r["order_id"]
        ts = _to_utc_iso(r["timestamp"])

        broker_qty = _broker_qty_for(positions, sym)
        if broker_qty > 0:
            actions["real_held"] += 1
            continue

        # Phantom — figure out which kind by looking up the entry order
        if not order_id:
            actions["ambiguous"].append({
                "trade_id": r["id"], "symbol": sym, "qty": qty,
                "reason": "no order_id in journal",
            })
            continue

        try:
            entry_order = api.get_order(order_id)
        except Exception as e:
            actions["ambiguous"].append({
                "trade_id": r["id"], "symbol": sym, "qty": qty,
                "reason": f"failed to fetch entry order: {e}",
            })
            continue

        entry_status = getattr(entry_order, "status", "?")
        try:
            entry_filled = float(getattr(entry_order, "filled_qty", 0) or 0)
        except Exception:
            entry_filled = 0

        if entry_status in ("canceled", "expired", "rejected") and entry_filled == 0:
            actions["cancel"].append({
                "trade_id": r["id"], "symbol": sym, "qty": qty,
                "order_id": order_id, "entry_status": entry_status,
            })
            continue

        if entry_status == "filled":
            sell_fill = _find_matching_sell_fill(
                api, sym, qty, ts or datetime.now(timezone.utc),
                used_sell_order_ids,
            )
            if sell_fill is None:
                actions["ambiguous"].append({
                    "trade_id": r["id"], "symbol": sym, "qty": qty,
                    "reason": "entry filled but no matching broker SELL fill found",
                })
                continue
            used_sell_order_ids.add(sell_fill["order_id"])
            actions["backfill_sell"].append({
                "trade_id": r["id"], "symbol": sym, "qty": qty,
                "buy_price": float(r["price"] or 0),
                "sell_order_id": sell_fill["order_id"],
                "sell_price": sell_fill["filled_avg_price"],
                "sell_qty": sell_fill["filled_qty"],
                "sell_filled_at": sell_fill["filled_at"].isoformat(),
                "sell_order_type": sell_fill["order_type"],
            })
        else:
            actions["ambiguous"].append({
                "trade_id": r["id"], "symbol": sym, "qty": qty,
                "reason": f"entry status={entry_status} filled_qty={entry_filled}",
            })

    if apply_changes:
        for a in actions["cancel"]:
            conn.execute(
                "UPDATE trades SET status='canceled' WHERE id=?",
                (a["trade_id"],),
            )
        for a in actions["backfill_sell"]:
            # Insert the SELL row reflecting the broker fill
            conn.execute(
                """INSERT INTO trades
                   (timestamp, symbol, side, qty, price, order_id, signal_type,
                    strategy, reason, status, fill_price)
                   VALUES (?, ?, 'sell', ?, ?, ?, 'reconcile_backfill',
                           'reconcile_backfill',
                           'broker exited via protective order — backfilled by reconcile_journal_to_broker',
                           'closed', ?)""",
                (a["sell_filled_at"], a["symbol"], a["sell_qty"],
                 a["sell_price"], a["sell_order_id"], a["sell_price"]),
            )
            conn.execute(
                "UPDATE trades SET status='closed' WHERE id=?",
                (a["trade_id"],),
            )
        conn.commit()

    conn.close()

    # After applying journal changes, run the existing FIFO P&L
    # backfill so the new SELL rows get a `pnl` and the BUY rows get
    # closed-status counters consistent with everything else.
    if apply_changes and (actions["cancel"] or actions["backfill_sell"]):
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
    args = ap.parse_args()

    profile_ids = [args.profile] if args.profile else list(range(1, 12))

    grand_totals = {"cancel": 0, "backfill_sell": 0, "ambiguous": 0, "real_held": 0}
    print(f"=== Reconcile {'APPLY' if args.apply else 'DRY-RUN'} ===\n")
    for p_id in profile_ids:
        try:
            res = reconcile_profile(p_id, apply_changes=args.apply)
        except Exception as e:
            print(f"profile_{p_id}: ERROR {e}")
            continue
        if "error" in res:
            print(f"profile_{p_id} ({res.get('profile')}): ERROR {res['error']}")
            continue
        n_c = len(res["cancel"])
        n_b = len(res["backfill_sell"])
        n_a = len(res["ambiguous"])
        print(f"p{p_id:>2} {res['profile'][:30]:<30s}  "
              f"real_held={res['real_held']:>3}  "
              f"to_cancel={n_c:>2}  to_backfill_sell={n_b:>2}  ambiguous={n_a:>2}")
        for a in res["cancel"]:
            print(f"     CANCEL    #{a['trade_id']:<4} {a['symbol']:>5} qty={a['qty']:>6.0f}  entry_status={a['entry_status']}")
        for a in res["backfill_sell"]:
            pnl = (a["sell_price"] - a["buy_price"]) * a["qty"]
            sign = "+" if pnl >= 0 else ""
            print(f"     BACKFILL  #{a['trade_id']:<4} {a['symbol']:>5} qty={a['qty']:>6.0f}  "
                  f"buy=${a['buy_price']:>7.2f} sell=${a['sell_price']:>7.2f}  "
                  f"realized={sign}${pnl:>9.2f}  ({a['sell_order_type']})")
        for a in res["ambiguous"]:
            print(f"     AMBIGUOUS #{a['trade_id']:<4} {a['symbol']:>5} qty={a['qty']:>6.0f}  reason: {a['reason']}")
        grand_totals["cancel"] += n_c
        grand_totals["backfill_sell"] += n_b
        grand_totals["ambiguous"] += n_a
        grand_totals["real_held"] += res["real_held"]

    print(f"\n=== TOTALS ===")
    print(f"  real_held:        {grand_totals['real_held']:>3}")
    print(f"  to_cancel:        {grand_totals['cancel']:>3}")
    print(f"  to_backfill_sell: {grand_totals['backfill_sell']:>3}")
    print(f"  ambiguous:        {grand_totals['ambiguous']:>3}")
    if not args.apply:
        print(f"\nDry-run only. Re-run with --apply to write changes.")


if __name__ == "__main__":
    sys.exit(main() or 0)
