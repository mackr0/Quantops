"""2026-06-16 — one-time data repair: reconcile each profile's OPEN
journal rows to broker truth BY EXACT OWN ORDER_ID.

The operator's invariant: a profile's position is the signed sum of
its OWN order_ids' fills. Legacy rows violated it in two proven ways:

  CASE A — phantom unfilled entry. A 'buy'/'short' row sits status=
    'open' but its broker order never filled (canceled/expired/
    rejected with filled_qty 0). The journal claims a position that
    does not exist at the broker. → mark the row 'canceled'.
    (The SOUN broker-11-vs-journal-thousands class.)

  CASE C — hedge short mislabeled 'sell'. A DELTA_HEDGE 'sell' row
    that FILLED at the broker opened a SHORT, but was journaled
    side='sell' (which get_virtual_positions drops for stocks), so
    the short is invisible. → re-tag side='short' so the book sees
    it; the (now-fixed) hedger then unwinds the excess via 'cover'.
    (The p128 JOBY −125 class.)

EVERYTHING ELSE is reported, never mutated — ambiguous cases are for
operator review, not guesswork (per the no-fuzzy-attribution rule).

Dry-run by default. Pass --apply to write. Pass --profile N to scope
to one profile. Per-order broker lookups use the same retry as the
reconciler. NOTHING here attributes across profiles; every decision
is keyed on the row's own order_id.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, "/opt/quantopsai")

from models import build_user_context_from_profile, get_active_profile_ids
from client import get_api


def _broker_order(api, order_id):
    """Fetch a broker order by id with light retry; None if unavailable."""
    import time
    for attempt in range(3):
        try:
            return api.get_order(order_id)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "404" in msg:
                return "NOT_FOUND"
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def repair_profile(profile_id: int, apply: bool):
    ctx = build_user_context_from_profile(profile_id)
    if not getattr(ctx, "alpaca_account_id", None):
        return {"profile_id": profile_id, "skipped": "no account"}
    api = get_api(ctx)
    db = ctx.db_path
    actions = {"cancel_phantom": [], "retag_short": [], "report": []}

    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, symbol, side, qty, status, order_id, signal_type, "
            "       occ_symbol, fill_price "
            "FROM trades "
            "WHERE COALESCE(status,'open') IN ('open','pending_fill') "
            "  AND order_id IS NOT NULL"
        ).fetchall()

        for r in rows:
            o = _broker_order(api, r["order_id"])
            if o is None:
                actions["report"].append(
                    (r["id"], r["symbol"], r["side"], r["qty"],
                     "broker lookup failed (retries exhausted)"))
                continue
            if o == "NOT_FOUND":
                actions["report"].append(
                    (r["id"], r["symbol"], r["side"], r["qty"],
                     "broker order not found — manual review"))
                continue
            bstatus = (getattr(o, "status", "") or "").lower()
            try:
                bfilled = float(getattr(o, "filled_qty", 0) or 0)
            except (TypeError, ValueError):
                bfilled = 0.0

            # CASE A — phantom unfilled entry → cancel the journal row.
            if (r["side"] in ("buy", "short")
                    and bstatus in ("canceled", "expired", "rejected")
                    and bfilled == 0):
                actions["cancel_phantom"].append(
                    (r["id"], r["symbol"], r["side"], r["qty"], bstatus))
                if apply:
                    conn.execute(
                        "UPDATE trades SET status='canceled', pnl=NULL, "
                        "reason=COALESCE(reason||' | ','')||? WHERE id=?",
                        ("repair: broker order %s with 0 fill — phantom "
                         "unfilled entry" % bstatus, r["id"]))
                continue

            # CASE C — DELTA_HEDGE 'sell' that filled = a short opened
            # but mislabeled. Re-tag to 'short' so the book sees it.
            if (r["signal_type"] == "DELTA_HEDGE"
                    and r["side"] == "sell"
                    and r["occ_symbol"] is None
                    and bstatus == "filled" and bfilled > 0):
                actions["retag_short"].append(
                    (r["id"], r["symbol"], r["qty"]))
                if apply:
                    fp = None
                    try:
                        fp = float(getattr(o, "filled_avg_price", 0) or 0) \
                            or None
                    except (TypeError, ValueError):
                        fp = None
                    conn.execute(
                        "UPDATE trades SET side='short', "
                        "fill_price=COALESCE(fill_price, ?), "
                        "reason=COALESCE(reason||' | ','')||? WHERE id=?",
                        (fp, "repair: DELTA_HEDGE sell re-tagged 'short' "
                         "(opened a short; was invisible to the book)",
                         r["id"]))
                continue

            # Everything else: report only.
            actions["report"].append(
                (r["id"], r["symbol"], r["side"], r["qty"],
                 "broker status=%s filled=%s (no automatic action)"
                 % (bstatus, bfilled)))

        if apply:
            conn.commit()

    actions["profile_id"] = profile_id
    actions["profile"] = getattr(ctx, "display_name", str(profile_id))
    return actions


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run)")
    ap.add_argument("--profile", type=int, default=None)
    args = ap.parse_args()
    pids = [args.profile] if args.profile else get_active_profile_ids()
    print("=== REPAIR %s ===" % ("APPLY" if args.apply else "DRY-RUN"))
    grand = {"cancel_phantom": 0, "retag_short": 0, "report": 0}
    for pid in pids:
        try:
            res = repair_profile(pid, args.apply)
        except Exception as e:
            print("p%s: ERROR %s" % (pid, e))
            continue
        if "skipped" in res:
            continue
        nc, nr, nrep = (len(res["cancel_phantom"]),
                        len(res["retag_short"]), len(res["report"]))
        if nc or nr or nrep:
            print("\np%s %s  cancel_phantom=%d retag_short=%d report=%d"
                  % (pid, res["profile"][:28], nc, nr, nrep))
            for a in res["cancel_phantom"]:
                print("   CANCEL   #%-5d %-6s %-5s qty=%-7s broker=%s"
                      % a)
            for a in res["retag_short"]:
                print("   ->SHORT  #%-5d %-6s qty=%s" % a)
            for a in res["report"]:
                print("   report   #%-5d %-6s %-5s qty=%-7s %s" % a)
        grand["cancel_phantom"] += nc
        grand["retag_short"] += nr
        grand["report"] += nrep
    print("\n=== TOTALS ===")
    for k, v in grand.items():
        print("  %-16s: %d" % (k, v))
    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write.")


if __name__ == "__main__":
    sys.exit(main() or 0)
