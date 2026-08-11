"""One-off repair — 2026-08-10: re-book own option round-trips that
reconcile_option_orphans mislabeled 'auto_closed_external'.

The orphan backstop stamped ordinary own short-then-buy-back pairs as
externally closed (their closing FILL evidence was this profile's OWN
journaled order), which excludes their rows from the cash algebra and
hands their cash to the activities pass — which rightly books nothing
for ordinary fills. Net effect on account 56: AMGN +$518, PM −$315,
KO −$41 premium flow missing from journal cash (the +$142 cash-parity
residual, alongside small stock noise under tolerance).

Fleet-generic and evidence-based: for every profile, group
'auto_closed_external' rows by OCC; where the group's signed
quantities net to FLAT and EVERY row's own order is broker-verified
FILLED at its journaled qty, flip the group to status='closed'
(pnl NULL) and run recompute_realized_pnl so fills-true P&L is
booked. Groups that don't fully verify are left untouched (genuine
externals keep their semantics). Idempotent: already-'closed' rows
are never selected. DRY-RUN by default; --apply to write.
"""
from __future__ import annotations

import sqlite3
import sys


def repair_profile(pid: int, apply: bool) -> int:
    from models import build_user_context_from_profile
    from order_status_cache import get_order_cached
    ctx = build_user_context_from_profile(pid)
    db_path = ctx.db_path
    api = ctx.get_alpaca_api()
    fixed = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        occs = [r[0] for r in conn.execute(
            "SELECT DISTINCT occ_symbol FROM trades "
            "WHERE status = 'auto_closed_external' "
            "AND occ_symbol IS NOT NULL")]
        for occ in occs:
            rows = conn.execute(
                "SELECT id, side, qty, order_id FROM trades "
                "WHERE occ_symbol = ? AND status = "
                "'auto_closed_external'", (occ,)).fetchall()
            if len(rows) < 2:
                continue
            net = 0.0
            ok = True
            for r in rows:
                qty = float(r["qty"] or 0)
                if qty <= 0 or not r["order_id"]:
                    ok = False
                    break
                net += qty if (r["side"] or "").lower() == "buy" \
                    else -qty
                try:
                    order = get_order_cached(api, r["order_id"])
                except Exception:
                    ok = False
                    break
                if (order is None
                        or (getattr(order, "status", "") or ""
                            ).lower() != "filled"
                        or abs(float(getattr(order, "filled_qty", 0)
                                     or 0) - qty) > 0.001):
                    ok = False
                    break
            if not ok or abs(net) > 0.001:
                continue
            ids = [r["id"] for r in rows]
            print(f"p{pid} {occ}: own round-trip "
                  f"({len(ids)} rows, ids {ids}) — "
                  + ("REPAIRING" if apply else "would repair"))
            if apply:
                ph = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE trades SET status='closed', pnl=NULL, "
                    f"reason = COALESCE(reason || ' | ', '') || ? "
                    f"WHERE id IN ({ph})",
                    (["repair 2026-08-10: own round-trip mislabeled "
                      "auto_closed_external (closing fill was OUR OWN "
                      "journaled order); re-booked as ordinary close, "
                      "pnl via recompute"] + ids),
                )
                conn.commit()
            fixed += len(ids)
    if apply and fixed:
        from journal import recompute_realized_pnl
        recompute_realized_pnl(db_path=db_path)
        print(f"p{pid}: recompute_realized_pnl done")
    return fixed


def main() -> int:
    apply = "--apply" in sys.argv
    from models import get_active_profile_ids
    total = 0
    for pid in get_active_profile_ids():
        try:
            total += repair_profile(pid, apply)
        except Exception as exc:
            print(f"p{pid}: FAILED {type(exc).__name__}: {exc}")
    print(("REPAIRED" if apply else "DRY-RUN, would repair"),
          total, "rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
