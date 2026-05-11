"""One-shot backfill: replace negative per-leg `price`/`fill_price`
on MULTILEG rows with the actual per-leg fill from Alpaca.

Caused by the bug fixed in `_record_multileg_legs` 2026-05-11: combo
path was writing the combo's signed net premium as the per-leg price.
For credit spreads that landed as a NEGATIVE number, which then made
`get_virtual_positions` silently drop the row (`if price <= 0:
continue`). 10+ multileg legs across multiple profiles invisible to
the AI's portfolio context.

Run on prod with:
    cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 \
        backfill_multileg_negative_prices.py

Idempotent: safe to re-run. Skips rows whose per-leg fill can't be
recovered from Alpaca (e.g., orders aged out of the 30-day API
window) — those need manual handling.
"""

import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def _enabled_profile_dbs():
    """Return [(profile_id, db_path)] for every enabled profile."""
    main = sqlite3.connect("quantopsai.db")
    rows = main.execute(
        "SELECT id FROM trading_profiles WHERE enabled=1 ORDER BY id"
    ).fetchall()
    main.close()
    return [(r[0], f"quantopsai_profile_{r[0]}.db") for r in rows]


def _backfill_one_profile(profile_id, db_path):
    """Find every MULTILEG row with price<0 in this profile, look up
    the per-leg fill from Alpaca via the combo's legs[], update the
    row in place. Returns dict of counters."""
    from models import build_user_context_from_profile
    from client import get_api

    ctx = build_user_context_from_profile(profile_id)
    api = get_api(ctx)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    bad_rows = conn.execute(
        "SELECT id, order_id, occ_symbol, symbol, side, qty, price, "
        "       fill_price "
        "FROM trades "
        "WHERE signal_type='MULTILEG' AND price < 0"
    ).fetchall()

    counters = {"found": len(bad_rows), "fixed": 0, "skipped": 0}

    # Cache combo-order lookups per order_id (multiple legs share one)
    combo_legs_cache = {}

    for row in bad_rows:
        order_id = row["order_id"]
        occ = row["occ_symbol"]
        if not order_id or not occ:
            counters["skipped"] += 1
            log.warning(
                "  row #%d: missing order_id or occ — skipping",
                row["id"],
            )
            continue

        if order_id not in combo_legs_cache:
            try:
                o = api.get_order(order_id)
                legs = getattr(o, "legs", []) or []
                combo_legs_cache[order_id] = {
                    getattr(l, "symbol", None): float(l.filled_avg_price)
                    for l in legs
                    if getattr(l, "filled_avg_price", None) is not None
                    and getattr(l, "symbol", None)
                }
            except Exception as exc:
                combo_legs_cache[order_id] = {}
                log.warning(
                    "  combo %s: get_order failed (%s) — skipping all "
                    "legs that share this order_id",
                    order_id, exc,
                )

        leg_price = combo_legs_cache[order_id].get(occ)
        if leg_price is None or leg_price <= 0:
            counters["skipped"] += 1
            log.warning(
                "  row #%d (%s leg of %s): no per-leg fill recoverable "
                "from Alpaca — skipping",
                row["id"], row["side"], occ,
            )
            continue

        conn.execute(
            "UPDATE trades SET price = ?, fill_price = ? WHERE id = ?",
            (leg_price, leg_price, row["id"]),
        )
        counters["fixed"] += 1
        log.info(
            "  row #%d (%s %s leg of %s): %.4f -> %.4f",
            row["id"], row["side"], row["qty"], occ,
            row["price"], leg_price,
        )

    conn.commit()
    conn.close()
    return counters


def main():
    grand = {"found": 0, "fixed": 0, "skipped": 0}
    for profile_id, db_path in _enabled_profile_dbs():
        if not os.path.exists(db_path):
            continue
        log.info("=== profile %d (%s) ===", profile_id, db_path)
        c = _backfill_one_profile(profile_id, db_path)
        log.info(
            "  profile %d: found=%d fixed=%d skipped=%d",
            profile_id, c["found"], c["fixed"], c["skipped"],
        )
        for k in grand:
            grand[k] += c[k]
    log.info(
        "TOTAL: found=%d fixed=%d skipped=%d",
        grand["found"], grand["fixed"], grand["skipped"],
    )
    return 0 if grand["skipped"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
