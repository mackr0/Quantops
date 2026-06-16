"""One-command book certification — run after every reset, deploy,
or whenever the operator wants proof the money math is right.

Born 2026-06-11 after the hyper-accuracy audit: these four checks
caught every real incident that day (phantom P&L, phantom cash,
partial-fill orphans, cross-profile oversells). A fresh session can
run this instead of re-deriving the audit from scratch.

Checks (all must pass for exit code 0):
  1. BROKER DRIFT — per execution account: sum of member profiles'
     virtual stock books == the account's broker positions, symbol
     by symbol. The ground-truth invariant; any nonzero drift means
     shares exist without exactly one virtual owner.
  2. RECONCILE — dry-run reconcile_with_ctx across all profiles
     must produce zero cancel/backfill/ambiguous actions.
  3. DECOMPOSITION — per profile: (equity − initial_capital) −
     (Σ realized pnl + Σ unrealized) within $100. Equity truth
     comes from check 1; this verifies the realized/unrealized
     ATTRIBUTION adds up too.
  4. ISSUES — issues_collector.collect_issues(since_hours) must
     report 0 groups (use --since-hours 168 after a reset).

Run:
    venv/bin/python certify_books.py                  # last 24h issues window
    venv/bin/python certify_books.py --since-hours 168
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from contextlib import closing


def _stock_symbol(sym: str) -> bool:
    """OCC option symbols are UNDERLYING + YYMMDD + C/P + strike."""
    return not (len(sym) > 6 and any(c.isdigit() for c in sym[1:7]))


def check_broker_drift() -> list:
    from models import get_active_profiles, build_user_context_from_profile
    from client import get_positions
    findings = []
    by_account = collections.defaultdict(list)
    for prof in get_active_profiles():
        by_account[prof.get("alpaca_account_id")].append(prof["id"])
    for aid, pids in sorted(by_account.items(), key=lambda kv: str(kv[0])):
        broker = None
        virt: dict = collections.defaultdict(float)
        for pid in pids:
            ctx = build_user_context_from_profile(pid)
            if broker is None:
                api = ctx.get_alpaca_api()
                broker = {p.symbol: float(p.qty)
                          for p in api.list_positions()
                          if _stock_symbol(p.symbol)}
            for p in get_positions(ctx=ctx):
                if not p.get("occ_symbol"):
                    virt[p["symbol"]] += float(p["qty"])
        for s in set(broker or {}) | set(virt):
            b = (broker or {}).get(s, 0.0)
            v = virt.get(s, 0.0)
            if abs(b - v) > 0.5:
                findings.append(
                    f"account {aid} {s}: broker={b:,.0f} "
                    f"virtual={v:,.0f} drift={b - v:+,.0f}")
    return findings


def check_reconcile() -> list:
    from models import get_active_profile_ids
    from reconcile_journal_to_broker import (
        reconcile_profile, _all_journal_sell_order_ids,
    )
    findings = []
    pids = get_active_profile_ids()
    cross = _all_journal_sell_order_ids(pids)
    for pid in pids:
        r = reconcile_profile(pid, apply_changes=False,
                              cross_profile_used_ids=cross)
        for bucket in ("cancel", "backfill_sell", "backfill_cover",
                       "backfill_partial_sell", "fix_partial_entry",
                       "ambiguous"):
            n = len(r.get(bucket, []))
            if n:
                findings.append(f"p{pid}: {n} {bucket} action(s)")
    return findings


def check_decomposition(tolerance: float = 100.0) -> list:
    from models import get_active_profiles, build_user_context_from_profile
    from client import get_account_info, get_positions
    findings = []
    for prof in get_active_profiles():
        pid = prof["id"]
        ctx = build_user_context_from_profile(pid)
        acct = get_account_info(ctx=ctx)
        eq = float(acct["equity"])
        init = float(prof.get("initial_capital") or 0)
        upl = sum(float(p.get("unrealized_pl") or 0)
                  for p in get_positions(ctx=ctx))
        with closing(sqlite3.connect(
                f"quantopsai_profile_{pid}.db")) as c:
            from journal import data_quality_clause
            dq = data_quality_clause(c)
            # Exclude non-executed rows: a canceled/expired/rejected
            # trade realized nothing. A speculative pnl left on such a
            # row inflates realized P&L (the p121 −5,985 gap). 2026-06-16.
            realized = c.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE pnl IS NOT NULL "
                "AND COALESCE(status,'') NOT IN "
                "    ('canceled', 'expired', 'rejected')"
                f"{dq}").fetchone()[0]
        gap = (eq - init) - (float(realized) + upl)
        if abs(gap) > tolerance:
            findings.append(
                f"p{pid} {prof['name']}: equity={eq:,.2f} "
                f"P&L={eq - init:+,.2f} decomposition gap={gap:+,.0f}")
    return findings


def check_issues(since_hours: int) -> list:
    import issues_collector
    r = issues_collector.collect_issues(since_hours=since_hours)
    tg = r["total_groups"] if isinstance(r, dict) else r.total_groups
    if tg:
        return [f"issues page: {tg} group(s) in the last "
                f"{since_hours}h"]
    return []


def check_funding() -> list:
    """2026-06-12 — each execution account's broker equity must
    cover its profiles' combined capital. The 6-12 accounts passed
    every book check at 03:20 and were $0 by the open; books that
    reconcile over an unfunded account certify nothing."""
    from models import get_active_profiles, build_user_context_from_profile
    from account_funding_guard import funding_status
    findings = []
    seen = set()
    for prof in get_active_profiles():
        aid = prof.get("alpaca_account_id")
        if aid in seen or aid is None:
            continue
        seen.add(aid)
        ctx = build_user_context_from_profile(prof["id"])
        funded, detail = funding_status(ctx)
        if not funded:
            findings.append(detail)
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since-hours", type=int, default=24,
                    help="issues_collector window (168 after a reset)")
    ap.add_argument("--gap-tolerance", type=float, default=100.0,
                    help="max per-profile decomposition gap in $")
    args = ap.parse_args()

    all_clean = True
    for name, findings in (
        ("0. BROKER FUNDING", check_funding()),
        ("1. BROKER DRIFT", check_broker_drift()),
        ("2. RECONCILE", check_reconcile()),
        ("3. DECOMPOSITION", check_decomposition(args.gap_tolerance)),
        ("4. ISSUES", check_issues(args.since_hours)),
    ):
        if findings:
            all_clean = False
            print(f"{name}: FAIL")
            for f in findings:
                print(f"    {f}")
        else:
            print(f"{name}: PASS")
    print()
    print("CERTIFIED CLEAN" if all_clean else
          "FINDINGS ABOVE — investigate before trusting the books")
    return 0 if all_clean else 1


if __name__ == "__main__":
    sys.exit(main())
