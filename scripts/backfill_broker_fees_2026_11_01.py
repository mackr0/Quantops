#!/usr/bin/env python3
"""Backfill 2026-11-01 — book every broker FEE since the Experiment-2
reset through the production activities writer.

Fees were never journaled (the cash-parity audit adjusted for them
broker-side as an account total). `activities_capture._write_fee` now
splits each fee across the account's books pro-rata on the base the
broker names, from the broker's own FILL activities, by own order_id.
This script runs the normal `capture_activities_for_profile` for every
active profile with `since` pushed back to the reset date, then prints
per account: broker fee total vs journaled total, and how many fees the
writer refused (each leaves an `audit_alerts` row explaining why).

DRY-RUN by default: fetches the broker's FEE activities and reports
counts/totals and how many are already journaled — writes nothing.
--replay-dir DIR rehearses on slim copies of the journals (trades +
audit_alerts). --apply executes against the live journals.

    venv/bin/python3 scripts/backfill_broker_fees_2026_11_01.py
    venv/bin/python3 scripts/backfill_broker_fees_2026_11_01.py --replay-dir /tmp/replay
    venv/bin/python3 scripts/backfill_broker_fees_2026_11_01.py --apply
"""
from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)


def _slim_copy(src: str, dst: str) -> None:
    if os.path.exists(dst):
        os.remove(dst)
    with closing(sqlite3.connect(dst)) as c:
        c.execute("ATTACH DATABASE ? AS s", (src,))
        for table in ("trades", "audit_alerts", "migration_markers"):
            ddl = c.execute("SELECT sql FROM s.sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone()
            if ddl:
                c.execute(ddl[0])
                c.execute(f"INSERT INTO {table} SELECT * FROM s.{table}")
        c.commit()
        c.execute("DETACH DATABASE s")


def _replay_ctx(real_ctx, db_path: str):
    return SimpleNamespace(
        profile_id=real_ctx.profile_id, db_path=db_path,
        alpaca_account_id=getattr(real_ctx, "alpaca_account_id", None),
        display_name=getattr(real_ctx, "display_name", f"p{real_ctx.profile_id}"),
        is_virtual=getattr(real_ctx, "is_virtual", True),
        initial_capital=getattr(real_ctx, "initial_capital", None),
        get_alpaca_api=real_ctx.get_alpaca_api,
    )


def _broker_fees(api, since_iso: str):
    out, token = [], None
    while True:
        kw = dict(activity_types="FEE", after=since_iso, page_size=100, direction="desc")
        if token:
            kw["page_token"] = token
        page = list(api.get_activities(**kw) or [])
        out.extend(page)
        if len(page) < 100:
            break
        token = getattr(page[-1], "id", None)
    return out


def _journaled_fee_total(db_paths) -> tuple:
    total, n, refused = 0.0, 0, 0
    for db in db_paths:
        with closing(sqlite3.connect(db)) as c:
            row = c.execute(
                "SELECT COALESCE(SUM(pnl),0), COUNT(*) FROM trades WHERE data_quality IS NULL "
                "AND signal_type='FEE' AND COALESCE(status,'') = 'closed'").fetchone()
            total += float(row[0] or 0); n += int(row[1] or 0)
            try:
                refused += c.execute(
                    "SELECT COUNT(*) FROM audit_alerts WHERE alert_type='fee_unattributable'"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                pass
    return total, n, refused


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--replay-dir", default=None)
    ap.add_argument("--since", default="2026-08-24")
    args = ap.parse_args()
    if args.apply and args.replay_dir:
        print("choose --apply OR --replay-dir"); return 2

    import activities_capture as ac
    from models import get_active_profiles, build_user_context_from_profile
    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    profiles = get_active_profiles()
    ctxs, db_for = {}, {}
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
        def _sibs(ctx):
            aid = getattr(ctx, "alpaca_account_id", None)
            return [(p["id"], db_for[p["id"]]) for p in profiles
                    if p.get("alpaca_account_id") == aid] or None
        ac._sibling_db_paths = _sibs
    writing = bool(args.apply or args.replay_dir)
    print(f"=== {'APPLY' if args.apply else 'REPLAY ' + str(args.replay_dir) if args.replay_dir else 'DRY RUN'}"
          f" — fees since {args.since} ===\n")

    by_account = collections.defaultdict(list)
    for prof in profiles:
        by_account[prof.get("alpaca_account_id")].append(prof)

    broker = {}
    for aid, profs in sorted(by_account.items(), key=lambda kv: str(kv[0])):
        api = ctxs[profs[0]["id"]].get_alpaca_api()
        fees = _broker_fees(api, since.isoformat())
        total = sum(float(getattr(a, "net_amount", 0) or 0) for a in fees)
        shapes = collections.Counter((ac._fee_shape(getattr(a, "description", ""),
                                                     getattr(a, "date", "")) or ("?",))[0]
                                     for a in fees)
        ids = {str(a.id) for a in fees}
        journaled = set()
        for p in profs:
            with closing(sqlite3.connect(db_for[p["id"]])) as c:
                journaled |= {str(r[0]).split("::voided")[0] for r in c.execute(
                    "SELECT order_id FROM trades WHERE order_id LIKE '%::%'")}
        broker[aid] = (total, len(fees))
        print(f"--- account {aid}: {len(fees)} fee rows, ${total:+.2f}; "
              f"{len(ids & journaled)} already journaled; shapes {dict(shapes)}")
    if not writing:
        print("\nDRY RUN — nothing written. --replay-dir DIR to rehearse, --apply to execute.")
        return 0

    print("\n--- booking through capture_activities_for_profile ---")
    ac.clear_memos()
    for prof in profiles:
        pid = prof["id"]
        summary = ac.capture_activities_for_profile(ctxs[pid], since=since)
        print(f"  p{pid}: FEE={summary.get('FEE', 0)} (other: "
              f"{ {k: v for k, v in summary.items() if k != 'FEE' and v} })")
        time.sleep(2)  # be gentle with the broker's rate limit

    print("\n--- verification: broker fee total vs journaled, per account ---")
    worst = 0.0
    for aid, profs in sorted(by_account.items(), key=lambda kv: str(kv[0])):
        total, n_broker = broker[aid]
        journaled, n_rows, refused = _journaled_fee_total([db_for[p["id"]] for p in profs])
        diff = round(total - journaled, 2)
        worst = max(worst, abs(diff))
        print(f"  account {aid}: broker ${total:+.2f} ({n_broker} fees)  journaled ${journaled:+.2f} "
              f"({n_rows} rows)  unbooked ${diff:+.2f}  refused alerts={refused}")
    print(f"\nworst unbooked per account: ${worst:.2f} (refused fees stay adjusted broker-side "
          "by the parity audit and are listed on /issues)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
