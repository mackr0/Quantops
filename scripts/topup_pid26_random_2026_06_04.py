"""One-off operator script: top-up pid26 (EXP-A1-RandomA) from 3 to 5
random picks (2026-06-04).

Background
----------
On the morning of 2026-06-04 the fresh experiment started; pid26's
deterministic random sample drew [CRK, GE, PG, VERV, CERE]. VERV and
CERE were rejected by Alpaca as inactive assets — the
pre-2026-06-04 random strategy logged the rejections and moved on
without substituting, leaving pid26 with 3 holdings while its sibling
replica pid27 had 5. The two random profiles bound variance against
each other; the day-1 capital imbalance compromised that.

The source-side fix (substitute inactive symbols in
`simple_strategies._pick_random_symbols`) shipped in the same commit
as this script. This script is the one-off remediation for pid26's
already-half-deployed state.

What this does
--------------
Picks 2 additional random symbols deterministically (separate seed
namespace `random_baseline_topup_2026_06_04` so the substitution is
auditable from the seed alone), verifies each is tradable + active at
Alpaca + not already held, sizes each at the same cash_per_pick the
strategy used for the first 3, submits BUYs through the same code
path the strategy uses (`_submit_and_log` -> atomic submit_order +
journal). The journal tag stays `strategy='random_stock_of_day'` so
the fire-once guard sees 5 entries going forward and the strategy
never re-picks for pid26.

Determinism: the seed uses `hashlib.sha256(repr(KEY).encode())`
instead of Python's `hash()`, because `hash()` is randomized per
interpreter process (via PYTHONHASHSEED) unless explicitly fixed.
SHA-256 is stable across runs; a re-run produces the same picks
(modulo `tradable` state at Alpaca).

Already executed on 2026-06-04 13:50 UTC; pid26's holdings are now
[CRK, GE, PG, TEAM, TXN]. This file is preserved in source control
for auditability + as a template for similar one-offs.

Usage
-----
  cd /opt/quantopsai && source .env
  venv/bin/python3 scripts/topup_pid26_random_2026_06_04.py            # dry-run
  venv/bin/python3 scripts/topup_pid26_random_2026_06_04.py --apply
"""
import hashlib
import random
import sqlite3
import sys

sys.path.insert(0, "/opt/quantopsai")

PROFILE_ID = 26
TARGET_TOTAL_PICKS = 5
ORIGINAL_PICKS = {"CRK", "GE", "PG", "VERV", "CERE"}  # 3 succeeded, 2 rejected
TOPUP_SEED_KEY = ("random_baseline_topup_2026_06_04", PROFILE_ID)


def _stable_seed(key) -> int:
    """Deterministic seed across processes, regardless of PYTHONHASHSEED."""
    digest = hashlib.sha256(repr(key).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def main(apply: bool) -> int:
    from segments import STOCK_UNIVERSE
    from simple_strategies import (
        _fetch_price, _submit_and_log, CASH_BUFFER, _virtual_equity,
    )
    from models import build_user_context_from_profile
    from client import get_api

    ctx = build_user_context_from_profile(PROFILE_ID)
    api = get_api(ctx)

    with sqlite3.connect(ctx.db_path) as conn:
        rows = conn.execute(
            "SELECT symbol FROM trades "
            "WHERE side='buy' AND status='open' "
            "AND strategy='random_stock_of_day'"
        ).fetchall()
    held = {r[0] for r in rows}
    n_held = len(held)
    n_to_add = TARGET_TOTAL_PICKS - n_held
    print(f"pid{PROFILE_ID}: held={n_held} ({sorted(held)}), "
          f"target={TARGET_TOTAL_PICKS}, need {n_to_add} more")
    if n_to_add <= 0:
        print("Nothing to do — already at target.")
        return 0

    equity = _virtual_equity(ctx)
    cash_per_pick = (equity * (1.0 - CASH_BUFFER)) / TARGET_TOTAL_PICKS
    print(f"equity=${equity:,.2f}  cash_per_pick=${cash_per_pick:,.2f}")

    pool = [s for s in STOCK_UNIVERSE if s not in ORIGINAL_PICKS]
    rng = random.Random(_stable_seed(TOPUP_SEED_KEY))
    candidates = rng.sample(pool, min(40, len(pool)))
    print(f"Drew {len(candidates)} candidates from pool of {len(pool)}; "
          f"will take the first {n_to_add} that are tradable.")

    picks = []
    skipped = []
    for sym in candidates:
        if len(picks) >= n_to_add:
            break
        try:
            asset = api.get_asset(sym)
            status = (getattr(asset, "status", "") or "").lower()
            tradable = bool(getattr(asset, "tradable", False))
            if status != "active" or not tradable:
                skipped.append((sym, f"status={status} tradable={tradable}"))
                continue
        except Exception as e:
            skipped.append((sym, f"get_asset error: {type(e).__name__}"))
            continue
        price = _fetch_price(api, sym)
        if not price or price <= 0:
            skipped.append((sym, "no price"))
            continue
        qty = int(cash_per_pick / price)
        if qty <= 0:
            skipped.append((sym, f"cash/pick={cash_per_pick:.2f} < price={price:.2f}"))
            continue
        picks.append((sym, price, qty))

    print(f"\nPlan: top up pid{PROFILE_ID} with {len(picks)} pick(s):")
    for sym, price, qty in picks:
        print(f"  buy {sym:>6s} qty={qty:>5d} @ ~${price:>8,.2f}  "
              f"(~${qty * price:,.2f})")
    if skipped:
        print(f"\nSkipped during scan ({len(skipped)}):")
        for sym, reason in skipped[:10]:
            print(f"  {sym}: {reason}")

    if not apply:
        print("\n--dry-run: no orders submitted. Re-run with --apply.")
        return 0

    if len(picks) < n_to_add:
        print(f"\nWARN: only found {len(picks)} tradable picks (wanted "
              f"{n_to_add}); applying what we have")

    placed = 0
    for sym, price, qty in picks:
        reason = (
            f"random_baseline TOP-UP 2026-06-04: pid{PROFILE_ID} day-1 "
            f"draw [CRK, GE, PG, VERV, CERE] had VERV+CERE rejected as "
            f"inactive at Alpaca; restoring to {TARGET_TOTAL_PICKS} "
            f"holdings via deterministic substitution (seed="
            f"random_baseline_topup_2026_06_04, profile={PROFILE_ID}). "
            f"{sym} @ ${price:.2f} x {qty} = ${qty * price:,.2f}."
        )
        ok = _submit_and_log(
            api, ctx, sym, "buy", qty, price,
            "random_stock_of_day", reason,
        )
        if ok:
            placed += 1
            print(f"  + placed buy {sym} qty={qty} @ ~${price:.2f}")
        else:
            print(f"  x FAILED buy {sym}")
    print(f"\nDone. Placed {placed}/{len(picks)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
