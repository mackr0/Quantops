#!/usr/bin/env python3
"""Repair 2026-10-26 — re-book the shared-account dividends and the
WBD cash merger through the corrected activities writer.

The incident (kill switch since 10-13, fleet-wide since 10-21):
  * `activities_capture._write_dividend` wrote the ACCOUNT-level DIV
    (the broker's net amount) verbatim into EVERY profile sharing the
    account, always as a cash CREDIT — even when the account was net
    short and the broker DEBITED it. 44 rows across the 12 Exp-2 books
    for 11 events; on account 61 all four events belonged to p229
    alone, yet p232/p235/p238 each carried +$312.29 of phantom cash
    with sign-mixed pnl — the "+358 decomposition gap".
  * `MA` (merger) activities had no handler: WBD was cashed out at
    $31.0167/sh on 10-12; p230's 3-share entry rotted 'open' (broker=0
    vs virtual=3 → integrity halt; orphan_close → reconciler halt).

What it does, per active profile:
  1. VOIDS every journal row keyed by a broker DIV activity id since
     --since (status='canceled', pnl NULL, order_id re-keyed
     '<id>::voided' so the writer's idempotency check no longer sees
     it; the row stays for forensics) — via journal.void_activity_rows.
  2. Re-runs `capture_activities_for_profile` (the PRODUCTION writer)
     which books each profile's OWN share by its position at the
     broker's entitlement instant, and books the WBD merger exit.
  3. Prints each profile's equity-identity gap before and after.

DRY-RUN by default: prints the attribution for every event and the
rows it would void. --apply executes against the live journals.
--replay-dir DIR copies every profile DB into DIR first and applies
there — a full rehearsal on real data that cannot touch production.

Run on the droplet from /opt/quantopsai:
    venv/bin/python3 scripts/repair_shared_account_dividends_2026_10_26.py
    venv/bin/python3 scripts/repair_shared_account_dividends_2026_10_26.py --replay-dir /dev/shm/replay
    venv/bin/python3 scripts/repair_shared_account_dividends_2026_10_26.py --apply
"""
from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)


def _identity_gap(ctx, initial_capital: float) -> tuple:
    """(equity, realized, unrealized, gap) — certify_books' identity."""
    from client import get_account_info, get_positions
    from journal import data_quality_clause
    acct = get_account_info(ctx=ctx)
    eq = float(acct["equity"])
    upl = sum(float(p.get("unrealized_pl") or 0)
              for p in get_positions(ctx=ctx))
    with closing(sqlite3.connect(ctx.db_path)) as c:
        realized = c.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE pnl IS NOT NULL "
            "AND COALESCE(status,'') NOT IN ('canceled','expired','rejected')"
            f"{data_quality_clause(c)}").fetchone()[0]
    gap = (eq - initial_capital) - (float(realized) + upl)
    return eq, float(realized), upl, gap


def _slim_copy(src: str, dst: str) -> None:
    """Copy only the tables the repair touches (the journals are
    ~130MB each; the ai_* tables are dead weight for a rehearsal)."""
    if os.path.exists(dst):
        os.remove(dst)
    with closing(sqlite3.connect(dst)) as c:
        c.execute("ATTACH DATABASE ? AS s", (src,))
        for table in ("trades", "audit_alerts", "migration_markers"):
            ddl = c.execute(
                "SELECT sql FROM s.sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not ddl:
                continue
            c.execute(ddl[0])
            c.execute(f"INSERT INTO {table} SELECT * FROM s.{table}")
        c.commit()
        c.execute("DETACH DATABASE s")


def _replay_ctx(real_ctx, db_path: str):
    """A ctx that reads/writes `db_path` but borrows the real broker
    client (read-only calls: activities and order lookups)."""
    return SimpleNamespace(
        profile_id=real_ctx.profile_id,
        db_path=db_path,
        alpaca_account_id=getattr(real_ctx, "alpaca_account_id", None),
        display_name=getattr(real_ctx, "display_name", f"p{real_ctx.profile_id}"),
        is_virtual=getattr(real_ctx, "is_virtual", True),
        initial_capital=getattr(real_ctx, "initial_capital", None),
        get_alpaca_api=real_ctx.get_alpaca_api,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="execute against the live journals (default: dry run)")
    ap.add_argument("--replay-dir", default=None,
                    help="copy every profile DB here and apply to the copies")
    ap.add_argument("--since", default="2026-09-01",
                    help="earliest activity date to re-book (YYYY-MM-DD)")
    args = ap.parse_args()
    if args.apply and args.replay_dir:
        print("choose --apply OR --replay-dir, not both")
        return 2

    import activities_capture as ac
    from models import get_active_profiles, build_user_context_from_profile
    from journal import void_activity_rows

    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    profiles = get_active_profiles()
    ctxs = {}
    db_for = {}
    for prof in profiles:
        real = build_user_context_from_profile(prof["id"])
        db = real.db_path
        if args.replay_dir:
            os.makedirs(args.replay_dir, exist_ok=True)
            db = os.path.join(args.replay_dir, os.path.basename(real.db_path))
            _slim_copy(real.db_path, db)
        db_for[prof["id"]] = db
        ctxs[prof["id"]] = _replay_ctx(real, db)

    if args.replay_dir:
        # Siblings must resolve to the copies, never to production.
        def _sibs(ctx):
            aid = getattr(ctx, "alpaca_account_id", None)
            return [(p["id"], db_for[p["id"]]) for p in profiles
                    if p.get("alpaca_account_id") == aid] or None
        ac._sibling_db_paths = _sibs
    writing = bool(args.apply or args.replay_dir)
    mode = ("APPLY (live journals)" if args.apply else
            f"REPLAY on copies in {args.replay_dir}" if args.replay_dir else
            "DRY RUN")
    print(f"=== {mode} — activities since {args.since} ===\n")

    before = {}
    for prof in profiles:
        pid = prof["id"]
        before[pid] = _identity_gap(ctxs[pid], float(prof.get("initial_capital") or 0))

    by_account = collections.defaultdict(list)
    for prof in profiles:
        by_account[prof.get("alpaca_account_id")].append(prof)

    planned_voids = 0
    for aid, profs in sorted(by_account.items(), key=lambda kv: str(kv[0])):
        api = ctxs[profs[0]["id"]].get_alpaca_api()
        print(f"--- account {aid}: profiles {[p['id'] for p in profs]}")
        events = []
        for a_type in ("DIV", "MA"):
            try:
                events += list(api.get_activities(
                    activity_types=a_type, after=since.isoformat()) or [])
            except Exception as exc:
                print(f"    get_activities({a_type}) FAILED: {exc}")
        for a in sorted(events, key=lambda x: str(getattr(x, "date", ""))):
            a_type = getattr(a, "activity_type", "?")
            aid_ = str(getattr(a, "id", ""))
            sym = (getattr(a, "symbol", "") or "").upper()
            net = float(getattr(a, "net_amount", 0) or 0)
            desc = str(getattr(a, "description", "") or "")
            qty = getattr(a, "qty", None)
            print(f"  {getattr(a, 'date', '')!s:10} {a_type:3} {sym:5} "
                  f"qty={qty!s:5} net=${net:+9.2f}  {desc[:60]}")
            # Attribution preview through the production helper.
            ctx0 = ctxs[profs[0]["id"]]
            if a_type == "DIV":
                ps = float(getattr(a, "per_share_amount", 0) or 0)
                m = ac._REC_DATE_RX.search(desc)
                if ps > 0 and m:
                    snap = ac._entitlement_snapshot_utc(m.group(1))
                    acct_qty = float(round(net / ps))
                    shares = ac._attribute_account_event(
                        ctx0, api, aid_, sym, snap, acct_qty)
                    if shares is None:
                        print(f"      REFUSED (books do not sum to {acct_qty:+.0f} at {snap[:16]}Z)")
                    else:
                        print("      split @ " + snap[:16] + "Z: " + "  ".join(
                            f"p{p}={q:+.0f}→${q * ps:+.2f}" for p, q in shares.items() if q))
                else:
                    print("      REFUSED (no per_share_amount / Rec Date)")
            elif a_type == "MA" and qty:
                m = ac._CASH_MERGER_RX.search(desc)
                snap = ac._entitlement_snapshot_utc(str(getattr(a, "date", "")))
                if m and snap:
                    shares = ac._attribute_account_event(
                        ctx0, api, aid_, sym, snap, -float(qty))
                    if shares is None:
                        print("      REFUSED (books do not sum to the removed qty)")
                    else:
                        print("      split: " + "  ".join(
                            f"p{p}={q:+g}→${q * float(m.group(1)):+.2f}"
                            for p, q in shares.items() if q))
                else:
                    print("      REFUSED (not a cash merger)")
            # Existing rows keyed by this activity, per profile.
            for prof in profs:
                with closing(sqlite3.connect(db_for[prof["id"]])) as c:
                    rows = c.execute(
                        "SELECT id, side, price, pnl, status FROM trades "
                        "WHERE order_id = ?", (aid_,)).fetchall()
                for r in rows:
                    planned_voids += 1
                    print(f"      p{prof['id']} row #{r[0]} {r[1]} price={r[2]} "
                          f"pnl={r[3]} status={r[4]} → VOID")
                    if writing and a_type == "DIV":
                        n = void_activity_rows(
                            db_for[prof["id"]], aid_,
                            "VOIDED 2026-10-26: account-level dividend was "
                            "booked into every sibling at the account's net "
                            "amount; re-booked per book by the corrected writer")
                        if n != 1:
                            print(f"      !! void returned {n}")
        print()

    print(f"rows to void: {planned_voids}")
    if not writing:
        print("\nDRY RUN — nothing written. Re-run with --replay-dir DIR to "
              "rehearse on copies, or --apply to execute.")
        return 0

    print("\n--- re-booking through capture_activities_for_profile ---")
    ac.clear_memos()
    for prof in profiles:
        pid = prof["id"]
        summary = ac.capture_activities_for_profile(ctxs[pid], since=since)
        print(f"  p{pid}: {summary}")

    print("\n--- equity identity (equity − initial) − (realized + unrealized) ---")
    worst = 0.0
    for prof in profiles:
        pid = prof["id"]
        b = before[pid]
        eq, rz, upl, gap = _identity_gap(ctxs[pid], float(prof.get("initial_capital") or 0))
        worst = max(worst, abs(gap))
        print(f"  p{pid} {prof['name']:18} gap before={b[3]:+9.2f}  after={gap:+9.2f}"
              f"   (equity {eq:,.2f} realized {rz:+,.2f} unrealized {upl:+,.2f})")
    print(f"\nworst remaining |gap|: {worst:.2f}")
    return 0 if worst < 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
