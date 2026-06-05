"""Diagnostic for the 2026-06-05 EXP-A2 broker_orphan drift.

The aggregate audit fired on Alpaca Account 2 (EXP-A2 ablation
profiles) for three OCC option symbols where the broker holds more
contracts than the sum of all profiles' virtual books reflects:

  GOOGL260710C00410000  virtual=3 vs broker=4   (1 orphan)
  NVDA260710C00240000   virtual=4 vs broker=6   (2 orphans)
  NVDA260710P00195000   virtual=1 vs broker=3   (2 orphans)

This script reads (read-only) the broker's position + order history
for each OCC symbol alongside the per-profile journal rows, so the
operator can identify which specific contracts are orphans before
manually closing them at the broker.

Output: a per-OCC report with
  - Broker side: position qty + avg entry, list of fill orders with
    timestamp + qty + side + price + order_id + filled_avg_price
  - Journal side: per-profile trade rows for that OCC, with status +
    order_id + side + qty + price + timestamp
  - Reconciliation: which broker orders DO match a journal row, and
    which DON'T (the orphans the operator needs to close)

USAGE (read-only, safe to run from prod):

  cd /opt/quantopsai
  /opt/quantopsai/venv/bin/python \
    scripts/diagnose_exp_a2_broker_orphans_2026_06_05.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__),
)))

ORPHAN_OCCS = (
    "GOOGL260710C00410000",
    "NVDA260710C00240000",
    "NVDA260710P00195000",
)


def _exp_a2_profiles() -> List[Dict]:
    """Return every enabled EXP-A2-* profile on Alpaca Account 2.
    Uses the master DB's `trading_profiles` table. Account 2 is the
    account that hosts EXP-A2-* (per docs/15 experiment design)."""
    from models import get_user_profiles

    # The first user — single-operator deployment.
    profiles = get_user_profiles(1)
    return [
        p for p in profiles
        if p.get("enabled")
        and (p.get("name") or "").startswith("EXP-A2-")
    ]


def _ctx_for_profile(profile: Dict):
    from models import build_user_context_from_profile
    return build_user_context_from_profile(profile["id"])


def _broker_positions_for_occs(
    ctx, occs: Tuple[str, ...],
) -> Dict[str, Dict]:
    """Read Alpaca's current positions and return a {occ: position}
    map covering only the OCCs we care about."""
    from client import get_api

    api = get_api(ctx)
    out: Dict[str, Dict] = {}
    for p in api.list_positions():
        if p.symbol in occs:
            out[p.symbol] = {
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "side": p.side,
                "market_value": float(p.market_value),
                "cost_basis": float(p.cost_basis),
                "unrealized_pl": float(p.unrealized_pl),
            }
    return out


def _broker_orders_for_occ(ctx, occ: str) -> List[Dict]:
    """Read every Alpaca order that filled this OCC symbol, newest
    first. Filtered to filled / partially_filled status — pending /
    canceled / rejected orders didn't contribute to the position."""
    from client import get_api

    api = get_api(ctx)
    # list_orders supports `symbols=[...]` + `status='all'`; the
    # default page size is 50 which is enough for a single OCC over
    # the cohort lifetime.
    try:
        orders = api.list_orders(
            status="all", symbols=[occ], limit=500,
            direction="desc",
        )
    except TypeError:
        # Some SDK versions reject `symbols=` kwarg — fall back to
        # full list + filter.
        orders = [
            o for o in api.list_orders(status="all", limit=500,
                                         direction="desc")
            if getattr(o, "symbol", None) == occ
        ]
    out = []
    for o in orders:
        status = getattr(o, "status", None) or ""
        if status not in ("filled", "partially_filled"):
            continue
        out.append({
            "id": o.id,
            "submitted_at": str(getattr(o, "submitted_at", "")),
            "filled_at": str(getattr(o, "filled_at", "")),
            "side": o.side,
            "qty": float(getattr(o, "qty", 0) or 0),
            "filled_qty": float(getattr(o, "filled_qty", 0) or 0),
            "filled_avg_price": (
                float(o.filled_avg_price)
                if getattr(o, "filled_avg_price", None) else None
            ),
            "order_class": getattr(o, "order_class", "") or "",
            "position_intent": getattr(o, "position_intent", "") or "",
            "status": status,
            "client_order_id": getattr(o, "client_order_id", "") or "",
        })
    return out


def _journal_rows_for_occ(
    profile_id: int, db_path: str, occ: str,
) -> List[Dict]:
    """Return every journal row for this OCC in this profile's DB.
    Includes canceled / pending_fill rows so the operator sees the
    full picture, not just the open ones."""
    rows: List[Dict] = []
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                "SELECT id, timestamp, side, qty, price, fill_price, "
                "order_id, status, signal_type, option_strategy, "
                "strategy, reason "
                "FROM trades WHERE occ_symbol = ? "
                "ORDER BY timestamp",
                (occ,),
            ):
                rows.append(dict(r))
    except Exception as exc:
        rows.append({"_read_error": f"{type(exc).__name__}: {exc}"})
    return rows


def _reconcile_orders_to_journal(
    broker_orders: List[Dict],
    all_journal_rows: List[Tuple[int, str, Dict]],
) -> Tuple[List[Dict], List[Dict]]:
    """Match each filled broker order to a journal row by order_id.
    Returns (matched, orphan). Each is a list of broker order dicts.

    `all_journal_rows` is [(profile_id, profile_name, row), ...]
    across every EXP-A2 profile.
    """
    journal_order_ids = {
        row.get("order_id"): (pid, name, row)
        for pid, name, row in all_journal_rows
        if row.get("order_id")
    }
    matched: List[Dict] = []
    orphan: List[Dict] = []
    for o in broker_orders:
        oid = o.get("id")
        if oid in journal_order_ids:
            pid, name, row = journal_order_ids[oid]
            o2 = dict(o)
            o2["_journal_profile_id"] = pid
            o2["_journal_profile_name"] = name
            o2["_journal_row_id"] = row.get("id")
            o2["_journal_status"] = row.get("status")
            o2["_journal_qty"] = row.get("qty")
            matched.append(o2)
        else:
            orphan.append(o)
    return matched, orphan


def _format_money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def main() -> None:
    profiles = _exp_a2_profiles()
    if not profiles:
        print(
            "No enabled EXP-A2-* profiles found. The script is hard-"
            "coded to the 2026-06-05 drift; this is the expected path "
            "for that diagnostic. Update ORPHAN_OCCS + filter if the "
            "drift cohort changes."
        )
        return

    print("=" * 72)
    print("EXP-A2 BROKER_ORPHAN DIAGNOSTIC — 2026-06-05")
    print("=" * 72)
    print()
    print(f"Profiles in scope ({len(profiles)}):")
    for p in profiles:
        print(f"  pid={p['id']:<4} {p.get('name')}")
    print()

    # Use the first profile's ctx for broker reads — every profile
    # in EXP-A2 shares the same Alpaca account, so list_positions /
    # list_orders return identical data regardless of which one we
    # auth as.
    anchor_ctx = _ctx_for_profile(profiles[0])

    print("-" * 72)
    print("Broker positions on the 3 OCCs:")
    print("-" * 72)
    positions = _broker_positions_for_occs(anchor_ctx, ORPHAN_OCCS)
    for occ in ORPHAN_OCCS:
        pos = positions.get(occ)
        if not pos:
            print(f"  {occ}: NO BROKER POSITION (already closed?)")
            continue
        print(
            f"  {occ}: qty={pos['qty']:.0f} side={pos['side']} "
            f"avg_entry={_format_money(pos['avg_entry_price'])} "
            f"mkt_val={_format_money(pos['market_value'])} "
            f"u_pnl={_format_money(pos['unrealized_pl'])}"
        )
    print()

    for occ in ORPHAN_OCCS:
        print("=" * 72)
        print(f"OCC: {occ}")
        print("=" * 72)

        # Broker side
        print()
        print("[BROKER] filled orders, newest first:")
        broker_orders = _broker_orders_for_occ(anchor_ctx, occ)
        if not broker_orders:
            print("  (none)")
        else:
            for o in broker_orders:
                print(
                    f"  {o['filled_at'] or o['submitted_at']:<30} "
                    f"qty={o['filled_qty']:.0f}/{o['qty']:.0f} "
                    f"side={o['side']:<5} "
                    f"intent={o['position_intent'] or '—':<14} "
                    f"price={_format_money(o['filled_avg_price']):<10} "
                    f"id={o['id']}"
                )

        # Journal side, per profile
        print()
        print("[JOURNAL] rows per EXP-A2-* profile:")
        all_journal_rows: List[Tuple[int, str, Dict]] = []
        for p in profiles:
            db_path = _ctx_for_profile(p).db_path
            rows = _journal_rows_for_occ(p["id"], db_path, occ)
            for r in rows:
                if "_read_error" in r:
                    print(
                        f"  pid={p['id']} {p.get('name')}: "
                        f"READ ERROR: {r['_read_error']}"
                    )
                    continue
                all_journal_rows.append((p["id"], p.get("name"), r))
                print(
                    f"  pid={p['id']:<4} {p.get('name'):<32} "
                    f"id={r['id']:<5} ts={r['timestamp']:<20} "
                    f"side={r['side']:<5} qty={r['qty']:<3} "
                    f"status={r['status'] or '—':<14} "
                    f"order_id={r['order_id'] or '—'}"
                )
        if not all_journal_rows:
            print("  (no journal rows for this OCC across EXP-A2-*)")

        # Reconciliation
        print()
        print("[RECONCILIATION] broker orders matched vs orphan:")
        matched, orphan = _reconcile_orders_to_journal(
            broker_orders, all_journal_rows,
        )
        if matched:
            print(f"  MATCHED ({len(matched)} broker orders):")
            for o in matched:
                print(
                    f"    {o['filled_at'] or o['submitted_at']:<30} "
                    f"qty={o['filled_qty']:.0f} side={o['side']:<5} "
                    f"id={o['id']} -> journal pid="
                    f"{o['_journal_profile_id']} "
                    f"({o['_journal_profile_name']}) "
                    f"row={o['_journal_row_id']} "
                    f"jstatus={o['_journal_status']} "
                    f"jqty={o['_journal_qty']}"
                )
        if orphan:
            print(f"  ORPHAN ({len(orphan)} broker orders with NO "
                  "matching journal row — these are the contracts "
                  "to close manually):")
            total_orphan_qty = sum(o["filled_qty"] for o in orphan)
            for o in orphan:
                print(
                    f"    {o['filled_at'] or o['submitted_at']:<30} "
                    f"qty={o['filled_qty']:.0f} side={o['side']:<5} "
                    f"intent={o['position_intent'] or '—':<14} "
                    f"price={_format_money(o['filled_avg_price']):<10} "
                    f"id={o['id']}"
                )
            print(f"  TOTAL ORPHAN qty for {occ}: "
                  f"{total_orphan_qty:.0f} contract(s)")
        if not orphan and not matched:
            print("  (no broker orders for this OCC — already flat)")
        print()


if __name__ == "__main__":
    main()
