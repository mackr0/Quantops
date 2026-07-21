"""2026-07-21 — universe price-band migration (CONFIRMED NO-OP in prod:
the dry-run proved every active profile already stores max_price=10000 —
the $20 value existed only in code defaults, never in live rows). Kept
as the verification tool that caught that attribution error; safe and
idempotent to run any time.

WHY: the $20 ceiling structurally excluded every liquid Energy /
Staples / Real-Estate / mega-cap name. The sector-stratified universe
guarantees 5 slots per sector — but cannot conjure names the price
band excluded, so those sectors were unrepresentable, the AI's own
beta/rotation guidance urged sectors it could never be shown, and
cycles starved. The system exists to TRADE and evaluate AI judgment;
a universe that contradicts the AI's instructions poisons both.

WHAT: UPDATE trading_profiles SET max_price = 1000.0 for active,
non-crypto profiles whose max_price is currently 20.0 (profiles with
operator-customized bands are left alone and reported). min_price
stays 10.0 — the documented institutional floor. The shared universe
cache keys on (segment, min, max, volume), so the next scheduler
cycle builds a fresh stratified universe automatically; no restart
ordering matters beyond deploying the new code first.

Dry-run by default; --apply to write. Idempotent.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/opt/quantopsai")

NEW_MAX = 10000.0
OLD_MAX = 20.0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from models import get_active_profiles, _get_conn
    from contextlib import closing

    profiles = get_active_profiles()
    to_change, skipped = [], []
    for p in profiles:
        if (p.get("market_type") or p.get("segment") or "stocks") == "crypto":
            skipped.append((p["id"], p.get("name"), "crypto"))
            continue
        cur = float(p.get("max_price") or 0)
        if abs(cur - OLD_MAX) < 0.001:
            to_change.append((p["id"], p.get("name")))
        else:
            skipped.append((p["id"], p.get("name"),
                            f"max_price={cur:g} (custom — untouched)"))

    print(f"=== universe band migration "
          f"({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    for pid, name in to_change:
        print(f"  p{pid} {name}: max_price {OLD_MAX:g} → {NEW_MAX:g}")
    for pid, name, why in skipped:
        print(f"  p{pid} {name}: SKIP ({why})")
    if not to_change:
        print("Nothing to change.")
        return 0
    if not args.apply:
        print(f"\nDRY-RUN: {len(to_change)} profile(s) would change. "
              f"Re-run with --apply.")
        return 0

    with closing(_get_conn()) as conn:
        for pid, _ in to_change:
            conn.execute(
                "UPDATE trading_profiles SET max_price = ? "
                "WHERE id = ? AND ABS(max_price - ?) < 0.001",
                (NEW_MAX, pid, OLD_MAX),
            )
        conn.commit()
    print(f"\nAPPLIED to {len(to_change)} profile(s). The next scheduler "
          f"cycle rebuilds the shared universe under the new band "
          f"(fresh cache key) — all 11 sectors become sourceable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
