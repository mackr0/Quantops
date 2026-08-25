"""One-off repair — 2026-08-25: re-book own option round-trips whose
ENTRY rows reconcile_option_orphans mislabeled 'auto_closed_external'.

Variant of the 2026-08-10 incident the 08-10 guard missed: the exit
row had already been flipped 'closed' by the fill machine, so the
live-rows own-close check saw one row and fell through to the external
path — whose FILL "evidence" was this profile's OWN exit fill. The
entry premium vanished from the cash algebra: equity-identity drift of
+$185 / +$185 / +$1,100.01 / +$4,550 on profiles 229/230/231/238 and
journal-cash phantoms of $2,620.13 / $405.02 / $1,265.06 on accounts
61/62/63 — every cent the sum of the mislabeled entries' premiums and
their invisible realized P&L.

Fleet-generic and evidence-based: for every profile, take each OCC
with at least one 'auto_closed_external' row and gather ALL of its
fill-bearing rows (open / pending_fill / closed / auto_closed_external
— everything except the never-filled dead set). Where the group's
signed quantities net to FLAT and EVERY row's own order is
broker-verified FILLED at its journaled qty, flip the
auto_closed_external rows to status='closed' (pnl NULL) and run
recompute_realized_pnl so fill-true P&L is booked. Groups that don't
fully verify are left untouched (genuine externals keep their
semantics). Idempotent. DRY-RUN by default; --apply to write.
"""
from __future__ import annotations

import sqlite3
import sys

_DEAD = ("canceled", "expired", "rejected", "done_for_day",
         "pending_protective", "auto_reconciled_phantom_close")


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
            ph_dead = ",".join("?" * len(_DEAD))
            rows = conn.execute(
                "SELECT id, side, qty, order_id, "
                "       COALESCE(status, 'open') AS status "
                "FROM trades WHERE occ_symbol = ? "
                f"AND COALESCE(status, 'open') NOT IN ({ph_dead})",
                (occ,) + _DEAD).fetchall()
            if len(rows) < 2:
                print(f"p{pid} {occ}: only {len(rows)} fill-bearing "
                      "row(s) — cannot verify a round-trip; left as-is")
                continue
            net = 0.0
            ok = True
            for r in rows:
                qty = float(r["qty"] or 0)
                if qty <= 0 or not r["order_id"]:
                    ok = False
                    break
                net += qty if (r["side"] or "").lower() in (
                    "buy", "cover") else -qty
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
                print(f"p{pid} {occ}: group does not verify as an own "
                      f"round-trip (ok={ok}, net={net}) — left as-is")
                continue
            ids = [r["id"] for r in rows
                   if r["status"] == "auto_closed_external"]
            print(f"p{pid} {occ}: own round-trip verified "
                  f"({len(rows)} rows; flipping ids {ids}) — "
                  + ("REPAIRING" if apply else "would repair"))
            if apply and ids:
                ph = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE trades SET status='closed', pnl=NULL, "
                    f"reason = COALESCE(reason || ' | ', '') || ? "
                    f"WHERE id IN ({ph})",
                    (["repair 2026-08-25: own round-trip entry "
                      "mislabeled auto_closed_external (closing fill "
                      "was OUR OWN journaled order; exit row was "
                      "already closed, hiding the pair from the 08-10 "
                      "guard); re-booked as ordinary close, pnl via "
                      "recompute"] + ids),
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
