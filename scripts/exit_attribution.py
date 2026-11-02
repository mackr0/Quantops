#!/usr/bin/env python3
"""Exit attribution — where the realized P&L comes from (2026-11-02).

The query that found the trailing-stop loss engine (CHANGELOG
2026-11-02): realized P&L since --since across every active profile,
grouped by instrument × side × signal, plus the trailing-stop detail
(fills, median holding time, share of stopped longs that had been up
>1% first) and the per-arm P&L vs initial capital. Run it any time; the
re-measurement after the breakeven-then-trail change is this script
on a later date. Read-only.

    venv/bin/python3 scripts/exit_attribution.py [--since 2026-08-24] [--arm-map]
"""
from __future__ import annotations

import argparse
import collections
from contextlib import closing
import os
import re
import sqlite3
import statistics as st
import sys
from datetime import datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-08-24")
    args = ap.parse_args()
    from models import get_active_profiles, build_user_context_from_profile
    profiles = get_active_profiles()
    print(f"=== exit attribution since {args.since} — {len(profiles)} profiles, "
          f"{datetime.utcnow():%Y-%m-%d %H:%MZ} ===")
    agg = collections.defaultdict(lambda: [0, 0.0])
    trail = []
    arm_pnl = collections.defaultdict(float)
    for prof in profiles:
        ctx = build_user_context_from_profile(prof["id"])
        with closing(sqlite3.connect(ctx.db_path)) as c:
            c.row_factory = sqlite3.Row
            for r in c.execute("""
                SELECT side, occ_symbol IS NOT NULL AS opt, UPPER(COALESCE(signal_type,'')) sig,
                       COALESCE(strategy,'') strat, COUNT(*) n, SUM(pnl) s
                FROM trades WHERE pnl IS NOT NULL AND data_quality IS NULL
                  AND COALESCE(status,'') NOT IN ('canceled','expired','rejected') AND timestamp >= ?
                GROUP BY 1,2,3,4""", (args.since,)):
                key = ("option" if r["opt"] else "stock", r["side"],
                       "activity" if r["strat"] == "alpaca_activity" else r["sig"][:20])
                agg[key][0] += r["n"]; agg[key][1] += r["s"]
                arm_pnl[prof["name"]] += r["s"]
            for r in c.execute("""
                SELECT t.pnl, t.reason, t.timestamp, e.timestamp ets, e.max_favorable_excursion mfe,
                       COALESCE(e.fill_price, e.price) epx, e.side eside
                FROM trades t LEFT JOIN trades e
                  ON e.id = CAST(substr(t.reason, instr(t.reason,'entry_trade=')+12) AS INTEGER)
                WHERE t.pnl IS NOT NULL AND t.data_quality IS NULL AND UPPER(t.signal_type)='PROTECTIVE_TRAILING'
                  AND COALESCE(t.status,'') NOT IN ('canceled','expired','rejected') AND t.timestamp >= ?""",
                               (args.since,)):
                trail.append(dict(r))
    print(f"\n{'instr':7} {'side':10} {'signal':22} {'n':>6} {'pnl':>11}")
    total = 0.0
    for k, (n, s) in sorted(agg.items(), key=lambda kv: kv[1][1]):
        total += s
        print(f"{k[0]:7} {k[1]:10} {k[2]:22} {n:6} {s:+11,.0f}")
    print(f"{'TOTAL realized':41} {total:+11,.0f}")
    if trail:
        pn = [t["pnl"] for t in trail]
        hold, up_first, n_long = [], 0, 0
        for t in trail:
            if t["ets"]:
                try:
                    d = (datetime.fromisoformat(t["timestamp"][:19])
                         - datetime.fromisoformat(t["ets"][:19])).total_seconds() / 86400
                    hold.append(d)
                except ValueError:
                    pass
            if t["eside"] == "buy" and t["mfe"] and t["epx"]:
                n_long += 1
                up_first += (t["mfe"] - t["epx"]) / t["epx"] > 0.01
        widths = collections.Counter(
            (re.search(r"trailing-stop ([\d.]+)%", t["reason"] or "") or [None, "?"])[1] for t in trail)
        print(f"\ntrailing-stop fills: n={len(trail)} pnl={sum(pn):+,.0f} median={st.median(pn):+.0f} "
              f"winners={sum(x > 0 for x in pn)/len(pn):.0%} widths={dict(widths.most_common(4))}")
        if hold:
            print(f"  holding before the trail fired: median {st.median(hold):.1f}d, "
                  f"share <1 day {sum(h < 1 for h in hold)/len(hold):.0%}")
        if n_long:
            print(f"  stopped longs that had been up >1% first: {up_first}/{n_long} = {up_first/n_long:.0%}")
    print("\nper profile realized:")
    for name, s in sorted(arm_pnl.items()):
        print(f"  {name:20} {s:+10,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
