"""One-off repair — 2026-08-05: journal p213's orphaned NEE $91 call.

2026-08-04 17:12:28Z, p213's bull call spread on NEE fell apart in a
429 rate-limit storm: leg 0 (buy 5× NEE260911C00091000) FILLED
(order 64df7a8a-d796-41ef-955f-51c5e9925fa1, 5 @ $0.97); leg 1's
submit and the rollback reversal both died on 429, and the pre-fix
code only journaled the open after a successful reversal — so the
fill was never journaled anywhere: broker_orphan +5 on account 56 and
$485 of the cash-parity drift.

This script writes the journal row the fixed code would have written,
FROM BROKER EVIDENCE ONLY: it re-reads the order live and refuses to
write unless the broker still reports those exact fills. Idempotent:
refuses if any row already carries the order_id.

Run on prod:  venv/bin/python3 repair_nee_rollback_orphan_2026_08_05.py --apply
"""
from __future__ import annotations

import sys

ORDER_ID = "64df7a8a-d796-41ef-955f-51c5e9925fa1"
PROFILE_ID = 213
OCC = "NEE260911C00091000"


def main() -> int:
    apply = "--apply" in sys.argv
    from models import build_user_context_from_profile
    ctx = build_user_context_from_profile(PROFILE_ID)
    db_path = ctx.db_path
    api = ctx.get_alpaca_api()

    import sqlite3
    with sqlite3.connect(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE order_id = ?",
            (ORDER_ID,)).fetchone()[0]
    if n:
        print(f"REFUSED: {n} row(s) already carry order {ORDER_ID[:8]} "
              f"in profile {PROFILE_ID} — nothing to repair.")
        return 1

    order = api.get_order(ORDER_ID)
    status = (getattr(order, "status", "") or "").lower()
    filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    fill_price = float(getattr(order, "filled_avg_price", 0) or 0)
    sym = getattr(order, "symbol", None)
    side = getattr(order, "side", None)
    filled_at = getattr(order, "filled_at", None)
    print(f"broker order {ORDER_ID[:8]}: status={status} side={side} "
          f"symbol={sym} filled={filled_qty} @ {fill_price}")
    if not (status == "filled" and side == "buy" and sym == OCC
            and filled_qty == 5.0 and fill_price > 0):
        print("REFUSED: broker state does not match the incident "
              "evidence — investigate before writing anything.")
        return 1

    ts = (filled_at.isoformat() if hasattr(filled_at, "isoformat")
          else str(filled_at or "2026-08-04T17:12:28+00:00"))
    if not apply:
        print("DRY-RUN: would journal buy 5 %s @ %.2f ts=%s into "
              "profile %d (%s). Re-run with --apply." % (
                  OCC, fill_price, ts, PROFILE_ID, db_path))
        return 0

    from journal import log_trade
    row_id = log_trade(
        symbol="NEE",
        side="buy",
        qty=filled_qty,
        price=fill_price,
        order_id=ORDER_ID,
        signal_type="MULTILEG",
        strategy="multileg_rollback",
        reason=("rollback-orphan recovery: bull_call_spread leg 0 "
                "filled 2026-08-04 17:12:28Z; leg 1 and the reversal "
                "both died on 429 and the pre-fix rollback never "
                "journaled the open (see CHANGELOG 2026-08-05). Row "
                "written from live broker evidence."),
        status="open",
        fill_price=fill_price,
        occ_symbol=OCC,
        expiry="2026-09-11",
        strike=91.0,
        spread_max_loss=filled_qty * fill_price * 100,
        db_path=db_path,
    )
    print(f"WROTE row #{row_id}: buy {filled_qty:g} {OCC} @ "
          f"${fill_price:.2f} (order {ORDER_ID[:8]}) into profile "
          f"{PROFILE_ID}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
