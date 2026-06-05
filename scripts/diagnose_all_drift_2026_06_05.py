"""Read-only diagnostic: for every drifting symbol on Alpaca account
14, print the full journal history alongside the broker's actual
position so we can see which class each instance is and what fix
needs to apply."""
import os
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__),
)))

from models import get_user_profiles, build_user_context_from_profile
from virtual_audit import audit_cross_account
from client import get_api

ACCT_ID = 14

profiles = get_user_profiles(1)
pids = [
    p["id"] for p in profiles
    if p.get("enabled") and p.get("alpaca_account_id") == ACCT_ID
]
problems = audit_cross_account(ACCT_ID, pids)

print(f"=== Drift on account {ACCT_ID}: {len(problems)} items ===")
for p in problems:
    print(f"  {p}")

print()
print("=== Broker positions for drifting symbols ===")
import re
DRIFT_RE = re.compile(r"^(\S+): virtual total=(\S+) vs Alpaca=(\S+)")
sym_drifts = []
for p in problems:
    m = DRIFT_RE.match(p)
    if m:
        sym_drifts.append((m.group(1), float(m.group(2)), float(m.group(3))))

anchor_ctx = build_user_context_from_profile(pids[0])
api = get_api(anchor_ctx)
broker_by_sym = {
    pos.symbol: {
        "qty": float(pos.qty),
        "side": pos.side,
        "avg": float(pos.avg_entry_price),
    }
    for pos in api.list_positions()
}

for sym, virt, alp in sym_drifts:
    print(f"\n----- {sym} -----")
    bp = broker_by_sym.get(sym, {})
    print(f"  Broker: qty={bp.get('qty', 0):.0f} side={bp.get('side', '-')} "
          f"avg=${bp.get('avg', 0):.2f}")
    print(f"  Audit says: virtual={virt:.0f}  alpaca={alp:.0f}  "
          f"diff={abs(virt-alp):.0f}")
    print(f"  Journal rows per profile:")
    total_open_buy = 0.0
    total_open_sell = 0.0
    for pid in sorted(pids):
        db = f"/opt/quantopsai/quantopsai_profile_{pid}.db"
        try:
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, timestamp, side, qty, status, signal_type, "
                    "       order_id, occ_symbol, pnl "
                    "FROM trades WHERE symbol = ? AND "
                    "  COALESCE(status, 'open') != 'canceled' "
                    "ORDER BY timestamp",
                    (sym,),
                ).fetchall()
            if not rows:
                continue
            print(f"    pid={pid}: {len(rows)} active row(s)")
            for r in rows:
                status = r["status"] or "open"
                tag = "OPT " if r["occ_symbol"] else "STK "
                print(
                    f"      id={r['id']:<4} {tag}{r['timestamp'][:19]} "
                    f"side={r['side']:<5} qty={r['qty']:<6} "
                    f"status={status:<14} signal={r['signal_type']:<22} "
                    f"order={r['order_id'] or '-'}"
                )
                if status == "open":
                    if r["side"] == "buy":
                        total_open_buy += float(r["qty"] or 0)
                    elif r["side"] == "sell":
                        total_open_sell += float(r["qty"] or 0)
        except Exception as exc:
            print(f"    pid={pid}: read error: {type(exc).__name__}: {exc}")
    net = total_open_buy - total_open_sell
    print(f"  Sum of OPEN rows across EXP-A2: buys={total_open_buy:.0f} "
          f"sells={total_open_sell:.0f} net={net:.0f}")
