"""2026-07-20 — one-time data repair: the July-17 monthly-opex
phantom-short incident.

WHAT HAPPENED
  The pre-fix options_lifecycle sweep decided assignment from a
  bar-close ITM estimate and synthesized equity legs (signal_type=
  'OPTION_EXERCISE', NO order_id) of contracts×100 shares at strike.
  Where the profile held fewer shares than the leg sold, the FIFO
  booked the surplus as a SHORT stock lot the broker never reported:

      account 55 GOOG: broker=0   virtual=-25  (75 held − 100 leg)
      account 55 WMT:  broker=0   virtual=-79  (21 held − 100 leg)
      account 56 GOOGL broker=-9  virtual=-37  ... etc.

  Monday's certify gate saw journal shorts the broker doesn't hold →
  kill switch; the reconciler saw broker-flat positions with no own
  exit evidence → profile halts (EXP-A1 / EXP-A3).

WHAT THIS SCRIPT DOES (per Alpaca account, across its profiles)
  1. Snapshot each profile DB to <db>.bak-opex-2026-07-20 (apply mode).
  2. Prefer BROKER EVIDENCE: for each fabricated leg's contract, look
     for OPASN/OPEXP/OPXRC evidence — journal rows captured by
     activities_capture AND a live get_activities call. OPEXP → the
     leg is a pure fabrication → DELETE. OPASN with qty → resize the
     leg to activity_qty×100.
  3. ALIGN the remainder: for each account symbol still drifting
     (|broker − Σvirtual| > 0.5 sh), shrink/delete the account's
     remaining fabricated legs (order_id IS NULL, since --since)
     toward broker truth, oldest first. Fabricated legs are the ONLY
     rows ever mutated; real orders are never touched.
  4. COHERENCE: re-derive FIFO consumption per touched symbol and
     flip open BUY entries that are now FULLY consumed to 'closed'
     (the reconciler's open-row scan halts on open entries the broker
     no longer backs). Partially-consumed entries stay open. Resized
     legs get order_id 'REPAIR-OPEX-20260720-<id>' + status='closed'.
  5. VERIFY: recompute virtual books vs live broker positions. In
     --apply mode a failed verification RESTORES every DB from its
     snapshot and exits non-zero. Dry-run performs steps 2-5 on temp
     copies and reports what WOULD happen, including the verification
     verdict — so you see the end state before touching anything.

AFTERMATH
  Once books match the broker, the integrity gate auto-releases the
  kill switch on its next clean cycle and the reconciler auto-clears
  the 'Reconciler safety net' halts (both are prefix-guarded — no
  manual deactivation needed, and manual halts are never touched).

Dry-run by default. --apply to write. --profile N to scope.
--since YYYY-MM-DD to widen/narrow the fabricated-leg window
(default 2026-07-16). Residual drift this script cannot explain from
fabricated legs is REPORTED, never guessed at (no-fuzzy rule).
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

DEFAULT_SINCE = "2026-07-16"
MARKER = "REPAIR-OPEX-20260720"


def _stock_symbol(sym: str) -> bool:
    from certify_books import _stock_symbol as _cs
    return _cs(sym)


def _occ_compact(occ: str) -> str:
    return (occ or "").replace(" ", "").upper()


def _broker_stock_positions(api) -> dict:
    return {p.symbol: float(p.qty) for p in api.list_positions()
            if _stock_symbol(p.symbol)}


def _virtual_stock(db_path: str) -> dict:
    from journal import get_virtual_positions
    out = collections.defaultdict(float)
    for p in get_virtual_positions(db_path=db_path,
                                   price_fetcher=lambda s: 0.0):
        if not p.get("occ_symbol"):
            out[(p["symbol"] or "").upper()] += float(p["qty"] or 0)
    return dict(out)


def _fabricated_legs(conn, since: str):
    """Pre-fix synthetic equity legs: OPTION_EXERCISE stock rows with
    no order_id (the fixed sweep always stamps OPX-EQ-*; real
    activity captures carry the Alpaca activity id)."""
    return [dict(r) for r in conn.execute(
        "SELECT id, timestamp, symbol, side, qty, price, reason "
        "FROM trades "
        "WHERE signal_type = 'OPTION_EXERCISE' "
        "AND occ_symbol IS NULL "
        "AND (order_id IS NULL OR order_id = '') "
        "AND timestamp >= ? "
        "ORDER BY timestamp ASC, id ASC",
        (since,),
    ).fetchall()]


def _leg_contract(conn, leg: dict) -> str:
    """Best-effort: the option row whose (pre-fix) assignment reason
    references this leg's symbol/strike — used for activity lookup."""
    row = conn.execute(
        "SELECT occ_symbol FROM trades "
        "WHERE occ_symbol IS NOT NULL AND symbol = ? "
        "AND ABS(COALESCE(strike, -1) - ?) < 0.001 "
        "AND (reason LIKE '%assigned at expiry%' "
        "     OR reason LIKE '%exercised at expiry%') "
        "ORDER BY id DESC LIMIT 1",
        (leg["symbol"], float(leg["price"] or -1)),
    ).fetchone()
    return row[0] if row else ""


def _activity_evidence(conn, api, occ: str) -> dict:
    """{OPASN: contracts, OPEXP: contracts, OPXRC: contracts} for a
    contract, from journal-captured activity rows plus a live fetch."""
    out: dict = {}
    if not occ:
        return out
    target = _occ_compact(occ)
    for st, qty in conn.execute(
        "SELECT signal_type, qty FROM trades "
        "WHERE occ_symbol IS NOT NULL AND signal_type IN "
        "('OPASN','OPEXP','OPXRC') "
        "AND REPLACE(UPPER(occ_symbol),' ','') = ?", (target,),
    ).fetchall():
        out[st] = out.get(st, 0.0) + float(qty or 0)
    if api is not None:
        try:
            for a in api.get_activities(
                    activity_types="OPASN,OPEXP,OPXRC",
                    after="2026-07-10"):
                if _occ_compact(getattr(a, "symbol", "") or "") != target:
                    continue
                st = getattr(a, "activity_type", "")
                if st in ("OPASN", "OPEXP", "OPXRC"):
                    out[st] = max(out.get(st, 0.0),
                                  abs(float(getattr(a, "qty", 0) or 0)))
        except Exception as exc:
            print(f"    (live activities fetch failed: {exc} — using "
                  f"journal-captured evidence only)")
    return out


def _set_leg_qty(conn, leg: dict, new_qty: float, why: str) -> str:
    lid = leg["id"]
    if new_qty <= 0.001:
        conn.execute("DELETE FROM trades WHERE id = ?", (lid,))
        return f"DELETE leg id={lid} {leg['symbol']} {leg['side']} " \
               f"{leg['qty']:g} @ {leg['price']} ({why})"
    conn.execute(
        "UPDATE trades SET qty = ?, status = 'closed', order_id = ?, "
        "reason = COALESCE(reason || ' | ', '') || ? WHERE id = ?",
        (new_qty, f"{MARKER}-{lid}",
         f"repair 2026-07-20: qty {leg['qty']:g} → {new_qty:g} ({why})",
         lid),
    )
    return f"RESIZE leg id={lid} {leg['symbol']} {leg['side']} " \
           f"{leg['qty']:g} → {new_qty:g} ({why})"


def _reflow_entry_statuses(conn, symbol: str) -> list:
    """Mirror get_virtual_positions' FIFO to decide which open BUY
    entries are now fully consumed → flip to 'closed'. Partially
    consumed (or unconsumed) entries stay open."""
    dead = ("canceled", "expired", "rejected", "done_for_day",
            "auto_reconciled_phantom_close", "auto_closed_external",
            "pending_fill", "pending_protective", "needs_review")
    rows = conn.execute(
        "SELECT id, side, qty, COALESCE(status,'open') AS status "
        "FROM trades WHERE symbol = ? AND occ_symbol IS NULL "
        f"AND COALESCE(status,'open') NOT IN "
        f"({', '.join('?' * len(dead))}) "
        "AND side IN ('buy','sell') AND COALESCE(qty,0) > 0 "
        "AND COALESCE(price, fill_price, 0) > 0 "
        "ORDER BY timestamp ASC, id ASC",
        (symbol, *dead),
    ).fetchall()
    lots = []  # [id, remaining, status]
    for rid, side, qty, status in rows:
        if side == "buy":
            lots.append([rid, float(qty), status])
        else:
            remaining = float(qty)
            for lot in lots:
                if remaining <= 0:
                    break
                take = min(lot[1], remaining)
                lot[1] -= take
                remaining -= take
    actions = []
    for rid, left, status in lots:
        if left <= 0.001 and status == "open":
            conn.execute(
                "UPDATE trades SET status='closed', "
                "reason = COALESCE(reason || ' | ', '') || ? "
                "WHERE id = ?",
                (f"repair 2026-07-20: entry fully consumed by "
                 f"assignment legs ({MARKER})", rid),
            )
            actions.append(f"CLOSE consumed entry id={rid} {symbol}")
    return actions


def repair_account(aid, pids, apply_mode: bool, since: str):
    """Repair one Alpaca account's profiles. Returns (clean, log)."""
    from models import build_user_context_from_profile
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
        return False, [f"account {aid}: NO API ACCESS — skipped"]
    broker = _broker_stock_positions(api)

    # Working copies: dry-run mutates throwaway temp copies so the
    # verification verdict is real; apply mutates live (after backup).
    workdb = {}
    backups = {}
    for pid in pids:
        src = ctxs[pid].db_path
        if apply_mode:
            bak = f"{src}.bak-opex-2026-07-20"
            if not os.path.exists(bak):
                shutil.copy2(src, bak)
            backups[pid] = bak
            workdb[pid] = src
        else:
            tmp = os.path.join(tempfile.mkdtemp(prefix="opexrepair-"),
                               os.path.basename(src))
            shutil.copy2(src, tmp)
            workdb[pid] = tmp

    conns = {}
    try:
        for pid in pids:
            conns[pid] = sqlite3.connect(workdb[pid])
            conns[pid].row_factory = sqlite3.Row
        # Step 2 — broker-evidence pass per fabricated leg
        legs_by_pid = {pid: _fabricated_legs(conns[pid], since)
                       for pid in pids}
        n_legs = sum(len(v) for v in legs_by_pid.values())
        log.append(f"account {aid}: {n_legs} fabricated leg(s) since "
                   f"{since} across profiles {pids}")
        for pid in pids:
            conn = conns[pid]
            for leg in list(legs_by_pid[pid]):
                occ = _leg_contract(conn, leg)
                ev = _activity_evidence(conn, api, occ)
                if ev.get("OPEXP") and not ev.get("OPASN"):
                    log.append("  p%s %s" % (pid, _set_leg_qty(
                        conn, leg, 0,
                        f"broker OPEXP for {occ}: never assigned")))
                    legs_by_pid[pid].remove(leg)
                elif ev.get("OPASN"):
                    true_sh = ev["OPASN"] * 100
                    if abs(true_sh - float(leg["qty"])) > 0.5:
                        log.append("  p%s %s" % (pid, _set_leg_qty(
                            conn, leg, true_sh,
                            f"broker OPASN x{ev['OPASN']:g} for {occ}")))
                    leg["qty"] = true_sh
            conn.commit()

        # Step 3 — align residual account drift using remaining legs
        virt = collections.defaultdict(float)
        for pid in pids:
            for s, q in _virtual_stock(workdb[pid]).items():
                virt[s] += q
        for sym in sorted(set(broker) | set(virt)):
            drift = broker.get(sym, 0.0) - virt.get(sym, 0.0)
            if abs(drift) <= 0.5:
                continue
            log.append(f"  {sym}: broker={broker.get(sym, 0.0):g} "
                       f"virtual={virt.get(sym, 0.0):g} "
                       f"drift={drift:+g} → aligning")
            need = drift  # >0: journal too short → shrink SELL legs
            for pid in pids:
                if abs(need) <= 0.5:
                    break
                conn = conns[pid]
                for leg in [l for l in legs_by_pid[pid]
                            if l["symbol"] == sym]:
                    if abs(need) <= 0.5:
                        break
                    q = float(conn.execute(
                        "SELECT qty FROM trades WHERE id=?",
                        (leg["id"],)).fetchone()[0])
                    if need > 0 and leg["side"] == "sell":
                        cut = min(q, need)
                        log.append("  p%s %s" % (pid, _set_leg_qty(
                            conn, {**leg, "qty": q}, q - cut,
                            "align to broker (no activity evidence)")))
                        need -= cut
                    elif need < 0 and leg["side"] == "buy":
                        cut = min(q, -need)
                        log.append("  p%s %s" % (pid, _set_leg_qty(
                            conn, {**leg, "qty": q}, q - cut,
                            "align to broker (no activity evidence)")))
                        need += cut
                conn.commit()
            if abs(need) > 0.5:
                log.append(f"  {sym}: RESIDUAL {need:+g} sh cannot be "
                           f"explained by fabricated legs — MANUAL "
                           f"REVIEW (script never guesses)")

        # Step 4 — entry-status coherence for every touched symbol
        touched = {(pid, l["symbol"]) for pid in pids
                   for l in _fabricated_legs(conns[pid], since)}
        touched |= {(pid, l["symbol"]) for pid in pids
                    for l in legs_by_pid[pid]}
        for pid, sym in sorted(touched):
            for a in _reflow_entry_statuses(conns[pid], sym):
                log.append(f"  p{pid} {a}")
            conns[pid].commit()

        # Step 5 — verify
        final = collections.defaultdict(float)
        for pid in pids:
            for s, q in _virtual_stock(workdb[pid]).items():
                final[s] += q
        clean = True
        for sym in sorted(set(broker) | set(final)):
            b, v = broker.get(sym, 0.0), final.get(sym, 0.0)
            if abs(b - v) > 0.5:
                clean = False
                log.append(f"  VERIFY FAIL {sym}: broker={b:g} "
                           f"virtual={v:g} drift={b - v:+g}")
        log.append(f"account {aid}: verification "
                   f"{'CLEAN' if clean else 'FAILED'}")

        if apply_mode and not clean:
            for pid in pids:
                conns[pid].close()
            for pid in pids:
                shutil.copy2(backups[pid], ctxs[pid].db_path)
            log.append(f"account {aid}: RESTORED all profile DBs from "
                       f"backups — nothing changed")
            return False, log
        return clean, log
    finally:
        for c in conns.values():
            try:
                c.close()
            # SILENT_OK: close-fail during cleanup — the connection is
            # being discarded either way and raising here would mask
            # the real result/exception of the repair pass.
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run on copies)")
    ap.add_argument("--profile", type=int, default=None,
                    help="scope to one profile id")
    ap.add_argument("--since", default=DEFAULT_SINCE,
                    help=f"fabricated-leg window start "
                         f"(default {DEFAULT_SINCE})")
    args = ap.parse_args()

    from models import get_active_profiles
    by_account = collections.defaultdict(list)
    for prof in get_active_profiles():
        if args.profile and prof["id"] != args.profile:
            continue
        by_account[prof.get("alpaca_account_id")].append(prof["id"])

    print(f"=== opex phantom-short repair "
          f"({'APPLY' if args.apply else 'DRY-RUN'}) — "
          f"{date.today().isoformat()} ===")
    all_clean = True
    for aid, pids in sorted(by_account.items(), key=lambda kv: str(kv[0])):
        clean, log = repair_account(aid, pids, args.apply, args.since)
        all_clean = all_clean and clean
        for line in log:
            print(line)
        print()

    if all_clean:
        print("ALL ACCOUNTS CLEAN." + (
            " Kill switch and reconciler halts will auto-clear on the "
            "next scheduler cycle." if args.apply else
            " Re-run with --apply to write."))
    else:
        print("RESIDUALS REMAIN — see MANUAL REVIEW / VERIFY FAIL "
              "lines above." + (
                  " Live DBs were restored from backups."
                  if args.apply else ""))
    return 0 if all_clean else 1


if __name__ == "__main__":
    sys.exit(main())
