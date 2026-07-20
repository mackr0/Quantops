"""2026-07-20 — one-time repair: journal the time-stop covers that
submitted at Monday's open but never reached the journal.

ROOT CAUSE (diagnosed from prod ledger + broker orders + logs)
  All 12 equity shorts opened Jul 9-10 crossed `short_max_hold_days`
  (10) simultaneously on Monday. Check Exits' time-stop trigger dict
  carried no "price" key; `_process_exit_trigger` submitted each
  cover, then `log_trade(price=trigger_signal["price"])` raised
  KeyError, and the A0 minimal-journal fallback re-read the SAME
  missing key — the exception escaped the except handler, so the
  covers were live at the broker with NO journal row, NO halt, NO
  alert. The door's `submitted_orders` ledger recorded every one
  (that is exactly what it exists for). p214 even re-fired and
  bought 10 extra GOOG (its journal still showed the short), which
  is now a real long lot at the broker.

WHAT THIS SCRIPT DOES (per profile)
  1. Find `submitted_orders` ledger rows since --since (default
     2026-07-20) with NO matching trades row — the unjournaled
     submits.
  2. Verify each against the broker: `api.get_order(order_id)` must
     return status='filled' with filled qty/price/time. Unfilled or
     unverifiable ledger rows are REPORTED, never journaled.
  3. Insert the missing rows chronologically, with the REAL broker
     order_id, fill price, and fill timestamp:
       - while the profile's book is net short the symbol → 'cover'
         (consumes short lots; realized P&L computed FIFO against
         the actual short entry fills)
       - any excess beyond the short → 'buy' (a genuine long lot the
         broker now holds — p214's double-fire)
       - a ledger 'sell' with no trades row would insert side='sell'
         (none expected in this incident; handled for completeness)
  4. Flip short ENTRY rows the new covers fully consume to
     status='closed' (reconciler open-row coherence; partial
     consumption leaves the entry open, matching reconciler
     convention).
  5. VERIFY per account: recompute every profile's virtual book and
     compare to live broker positions for the touched symbols. Apply
     mode backs up every DB first and restores ALL of them if
     verification fails. Dry-run does everything on temp copies and
     shows the verification verdict.

AFTERMATH
  Books match the broker → the integrity gate auto-releases the kill
  switch and the reconciler auto-clears its safety-net halts on the
  next cycle. No manual deactivation.

Dry-run by default. --apply to write. --profile N to scope.
"""
from __future__ import annotations

import argparse
import collections
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import date

sys.path.insert(0, "/opt/quantopsai")
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

DEFAULT_SINCE = "2026-07-20"
MARKER = "REPAIR-TIMESTOP-20260720"


def _stock_symbol(sym: str) -> bool:
    from certify_books import _stock_symbol as _cs
    return _cs(sym)


def _virtual_stock(db_path: str) -> dict:
    from journal import get_virtual_positions
    out = collections.defaultdict(float)
    for p in get_virtual_positions(db_path=db_path,
                                   price_fetcher=lambda s: 0.0):
        if not p.get("occ_symbol"):
            out[(p["symbol"] or "").upper()] += float(p["qty"] or 0)
    return dict(out)


def _unjournaled_ledger_rows(conn, since: str):
    """submitted_orders rows with no trades row for the order_id."""
    tabs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "submitted_orders" not in tabs:
        return []
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(submitted_orders)").fetchall()]
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM submitted_orders s "
        "WHERE NOT EXISTS (SELECT 1 FROM trades t "
        "                  WHERE t.order_id = s.order_id) "
        "ORDER BY rowid ASC",
    ).fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        ts = str(d.get("submitted_at") or d.get("created_at") or "")
        if ts >= since:
            out.append(d)
    return out


def _open_short_lots(conn, symbol: str):
    """Open short entry rows for the symbol, FIFO order, with the
    per-lot remaining after already-journaled covers (gvp-consistent:
    covers consume shorts FIFO)."""
    dead = ("canceled", "expired", "rejected", "done_for_day",
            "auto_reconciled_phantom_close", "auto_closed_external",
            "pending_fill", "pending_protective", "needs_review")
    shorts = conn.execute(
        "SELECT id, qty, COALESCE(fill_price, price) AS px "
        "FROM trades WHERE symbol = ? AND side = 'short' "
        "AND occ_symbol IS NULL AND COALESCE(status,'open') = 'open' "
        "ORDER BY timestamp ASC, id ASC", (symbol,),
    ).fetchall()
    covers = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM trades "
        "WHERE symbol = ? AND side = 'cover' AND occ_symbol IS NULL "
        f"AND COALESCE(status,'open') NOT IN "
        f"({', '.join('?' * len(dead))})",
        (symbol, *dead),
    ).fetchone()[0]
    lots = []
    consumed = float(covers or 0)
    for rid, qty, px in shorts:
        take = min(float(qty), consumed)
        consumed -= take
        lots.append({"id": rid, "qty": float(qty),
                     "remaining": float(qty) - take,
                     "px": float(px or 0)})
    return lots


def repair_profile(pid, ctx, api, apply_mode: bool, since: str,
                   workdb: str, log: list) -> set:
    """Insert missing rows for verified ledger fills. Returns touched
    symbols."""
    touched = set()
    with closing(sqlite3.connect(workdb)) as conn:
        conn.row_factory = sqlite3.Row
        ledger = _unjournaled_ledger_rows(conn, since)
        stock_ledger = [d for d in ledger
                        if not d.get("occ_symbol")
                        and _stock_symbol(str(d.get("symbol") or ""))]
        if not stock_ledger:
            return touched
        log.append(f"profile {pid}: {len(stock_ledger)} unjournaled "
                   f"ledger submit(s) since {since}")

        verified = []
        for d in stock_ledger:
            oid = str(d["order_id"])
            try:
                o = api.get_order(oid)
            except Exception as exc:
                log.append(f"  p{pid} {d['symbol']} {oid[:8]}: broker "
                           f"lookup FAILED ({exc}) — SKIPPED, review "
                           f"manually")
                continue
            status = str(getattr(o, "status", ""))
            filled = float(getattr(o, "filled_qty", 0) or 0)
            price = float(getattr(o, "filled_avg_price", 0) or 0)
            filled_at = str(getattr(o, "filled_at", "") or "")
            if status != "filled" or filled <= 0 or price <= 0:
                log.append(f"  p{pid} {d['symbol']} {oid[:8]}: broker "
                           f"status={status} filled={filled} — nothing "
                           f"to journal (no fill)")
                continue
            verified.append({
                "symbol": str(d["symbol"]).upper(),
                "side": str(getattr(o, "side", d.get("side") or "")),
                "qty": filled, "price": price,
                "filled_at": filled_at.replace("Z", "+00:00")[:26],
                "order_id": oid,
            })
        verified.sort(key=lambda v: v["filled_at"])

        for v in verified:
            sym = v["symbol"]
            touched.add(sym)
            if v["side"] == "buy":
                lots = _open_short_lots(conn, sym)
                net_short = sum(l["remaining"] for l in lots)
                cover_qty = min(v["qty"], net_short)
                buy_qty = v["qty"] - cover_qty
                if cover_qty > 0:
                    # Realized P&L FIFO against the actual short fills
                    pnl = 0.0
                    rem = cover_qty
                    for l in lots:
                        if rem <= 0:
                            break
                        take = min(l["remaining"], rem)
                        pnl += (l["px"] - v["price"]) * take
                        l["remaining"] -= take
                        rem -= take
                    conn.execute(
                        "INSERT INTO trades (timestamp, symbol, side, "
                        " qty, price, fill_price, order_id, "
                        " signal_type, strategy, status, pnl, reason) "
                        "VALUES (?, ?, 'cover', ?, ?, ?, ?, 'SELL', "
                        "        'time_stop', 'closed', ?, ?)",
                        (v["filled_at"], sym, cover_qty, v["price"],
                         v["price"], v["order_id"], round(pnl, 2),
                         f"{MARKER}: time-stop cover journaled from "
                         f"door ledger + broker fill (order was live, "
                         f"log_trade KeyError'd on missing trigger "
                         f"price)"),
                    )
                    log.append(f"  p{pid} INSERT cover {sym} "
                               f"{cover_qty:g} @ {v['price']} "
                               f"pnl={pnl:+.2f} oid={v['order_id'][:8]}")
                    # Flip fully-consumed short entries
                    for l in lots:
                        if l["remaining"] <= 0.001 and l["qty"] > 0:
                            n = conn.execute(
                                "UPDATE trades SET status='closed', "
                                "reason = COALESCE(reason || ' | ', '')"
                                " || ? WHERE id = ? AND "
                                "COALESCE(status,'open') = 'open'",
                                (f"{MARKER}: short fully covered",
                                 l["id"]),
                            ).rowcount
                            if n:
                                log.append(f"  p{pid} CLOSE short "
                                           f"entry id={l['id']} {sym}")
                if buy_qty > 0.001:
                    conn.execute(
                        "INSERT INTO trades (timestamp, symbol, side, "
                        " qty, price, fill_price, order_id, "
                        " signal_type, strategy, status, reason) "
                        "VALUES (?, ?, 'buy', ?, ?, ?, ?, 'BUY', "
                        "        'time_stop_refire', 'open', ?)",
                        (v["filled_at"], sym, buy_qty, v["price"],
                         v["price"], v["order_id"],
                         f"{MARKER}: re-fired time-stop bought beyond "
                         f"the short — genuine long lot at broker"),
                    )
                    log.append(f"  p{pid} INSERT buy {sym} "
                               f"{buy_qty:g} @ {v['price']} (long lot) "
                               f"oid={v['order_id'][:8]}")
            else:
                conn.execute(
                    "INSERT INTO trades (timestamp, symbol, side, qty, "
                    " price, fill_price, order_id, signal_type, "
                    " strategy, status, reason) "
                    "VALUES (?, ?, 'sell', ?, ?, ?, ?, 'SELL', "
                    "        'ledger_backfill', 'closed', ?)",
                    (v["filled_at"], sym, v["qty"], v["price"],
                     v["price"], v["order_id"],
                     f"{MARKER}: unjournaled ledger sell backfilled "
                     f"from broker fill"),
                )
                log.append(f"  p{pid} INSERT sell {sym} {v['qty']:g} "
                           f"@ {v['price']} oid={v['order_id'][:8]}")
        conn.commit()
    return touched


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run on copies)")
    ap.add_argument("--profile", type=int, default=None)
    ap.add_argument("--since", default=DEFAULT_SINCE,
                    help=f"ledger window start (default {DEFAULT_SINCE})")
    args = ap.parse_args()

    from models import get_active_profiles, build_user_context_from_profile

    by_account = collections.defaultdict(list)
    for prof in get_active_profiles():
        if args.profile and prof["id"] != args.profile:
            continue
        by_account[prof.get("alpaca_account_id")].append(prof["id"])

    print(f"=== time-stop unjournaled-cover repair "
          f"({'APPLY' if args.apply else 'DRY-RUN'}) — "
          f"{date.today().isoformat()} ===")
    all_clean = True
    for aid, pids in sorted(by_account.items(), key=lambda kv: str(kv[0])):
        log = []
        ctxs = {pid: build_user_context_from_profile(pid) for pid in pids}
        api = None
        for pid in pids:
            try:
                api = ctxs[pid].get_alpaca_api()
                break
            except Exception:
                continue
        if api is None:
            print(f"account {aid}: NO API ACCESS — skipped")
            all_clean = False
            continue

        workdb, backups = {}, {}
        for pid in pids:
            src = ctxs[pid].db_path
            if args.apply:
                bak = f"{src}.bak-timestop-2026-07-20"
                if not os.path.exists(bak):
                    shutil.copy2(src, bak)
                backups[pid] = bak
                workdb[pid] = src
            else:
                tmp = os.path.join(
                    tempfile.mkdtemp(prefix="timestoprepair-"),
                    os.path.basename(src))
                shutil.copy2(src, tmp)
                workdb[pid] = tmp

        touched = set()
        for pid in pids:
            touched |= repair_profile(pid, ctxs[pid], api, args.apply,
                                      args.since, workdb[pid], log)

        # Verify touched symbols: Σ(profile virtual) == broker
        clean = True
        if touched:
            broker = {p.symbol: float(p.qty)
                      for p in api.list_positions()
                      if _stock_symbol(p.symbol)}
            virt = collections.defaultdict(float)
            for pid in pids:
                for s, q in _virtual_stock(workdb[pid]).items():
                    virt[s] += q
            for sym in sorted(touched):
                b, v = broker.get(sym, 0.0), virt.get(sym, 0.0)
                if abs(b - v) > 0.5:
                    clean = False
                    log.append(f"  VERIFY FAIL {sym}: broker={b:g} "
                               f"virtual={v:g} drift={b - v:+g}")
                else:
                    log.append(f"  verify {sym}: broker={b:g} "
                               f"virtual={v:g} OK")
        log.append(f"account {aid}: "
                   f"{'no unjournaled submits' if not touched else ''}"
                   f" verification {'CLEAN' if clean else 'FAILED'}")

        if args.apply and not clean:
            for pid in pids:
                shutil.copy2(backups[pid], ctxs[pid].db_path)
            log.append(f"account {aid}: RESTORED all profile DBs from "
                       f"backups — nothing changed")
            all_clean = False
        elif not clean:
            all_clean = False
        for line in log:
            print(line)
        print()

    if all_clean:
        print("ALL ACCOUNTS CLEAN." + (
            " Kill switch and reconciler halts auto-clear on the next "
            "scheduler cycle." if args.apply else
            " Re-run with --apply to write."))
    else:
        print("ISSUES REMAIN — see lines above."
              + (" Live DBs restored from backups." if args.apply
                 else ""))
    return 0 if all_clean else 1


if __name__ == "__main__":
    sys.exit(main())
