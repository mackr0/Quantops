"""Force-pass the journal-to-broker reconciler across every enabled
profile and print what it would do. Dry-run by default; pass
--apply to actually write the changes.

The kill switch should be ON before running this so no new trades
land while we're cleaning up.

Output per profile: number of cancel / backfill_sell / backfill_cover /
backfill_partial_sell / ambiguous / fix_partial_entry / real_held
actions, plus a one-line preview of each action up to 6 per category.
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__),
)))

from models import get_user_profiles, get_active_profile_ids
from reconcile_journal_to_broker import (
    reconcile_profile, _all_journal_sell_order_ids,
)
from kill_switch import is_active as kill_is_active


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write changes (default: dry-run only)",
    )
    parser.add_argument(
        "--user", type=int, default=1,
        help="User id (default 1)",
    )
    args = parser.parse_args()

    ks_on, ks_reason = kill_is_active()
    print(f"Kill switch: {'ON' if ks_on else 'OFF'}  reason: {ks_reason}")
    if not ks_on and args.apply:
        print(
            "REFUSING to apply with kill switch OFF — flip the kill "
            "switch ON first so no new trades land while the reconciler "
            "is writing."
        )
        sys.exit(2)
    print()

    profiles = [p for p in get_user_profiles(args.user) if p.get("enabled")]
    print(f"Reconciling {len(profiles)} enabled profile(s)  "
          f"apply={args.apply}")
    print()
    cross_used = _all_journal_sell_order_ids(get_active_profile_ids())

    grand = {
        "cancel": 0,
        "backfill_sell": 0,
        "backfill_cover": 0,
        "backfill_partial_sell": 0,
        "ambiguous": 0,
        "fix_partial_entry": 0,
        "real_held": 0,
    }

    for p in sorted(profiles, key=lambda x: x["id"]):
        pid = p["id"]
        name = p.get("name", "?")
        print(f"--- pid={pid} {name} ---")
        try:
            res = reconcile_profile(
                pid,
                apply_changes=args.apply,
                cross_profile_used_ids=cross_used,
            )
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            continue
        counts = {
            k: (len(res.get(k, [])) if isinstance(res.get(k, []), list)
                else int(res.get(k, 0)))
            for k in grand
        }
        print(f"  cancel={counts['cancel']}  "
              f"backfill_sell={counts['backfill_sell']}  "
              f"backfill_cover={counts['backfill_cover']}  "
              f"backfill_partial_sell={counts['backfill_partial_sell']}  "
              f"ambiguous={counts['ambiguous']}  "
              f"fix_partial_entry={counts['fix_partial_entry']}  "
              f"real_held={counts['real_held']}")
        for k in grand:
            grand[k] += counts[k]
        for bucket in ("cancel", "backfill_sell", "backfill_cover",
                        "backfill_partial_sell", "ambiguous",
                        "fix_partial_entry"):
            items = res.get(bucket, [])
            if not items:
                continue
            print(f"  {bucket} ({len(items)}):")
            for a in items[:6]:
                sym = a.get("symbol", "?")
                qty = (a.get("qty") or a.get("sell_qty")
                       or a.get("cover_qty") or a.get("filled_qty") or "?")
                src = a.get("source", "?")
                trade_id = a.get("trade_id", "?")
                print(f"    trade_id={trade_id} sym={sym} qty={qty} "
                      f"source={src}")
            if len(items) > 6:
                print(f"    ... and {len(items) - 6} more")
        print()

    print("=" * 60)
    print(f"GRAND TOTAL ({'APPLIED' if args.apply else 'DRY-RUN'}):")
    for k, v in grand.items():
        print(f"  {k:<25} {v}")


if __name__ == "__main__":
    main()
