"""Backfill OCC symbols on historical option-exit journal rows.

Bug class.
Per CHANGELOG 2026-05-20 PM (task #189): the AI's STRONG_SELL exit
path in `trade_pipeline.py:1143` was calling `api.submit_order(symbol=
underlying)` and `log_trade(symbol=underlying)` without distinguishing
stock vs option positions. Result: option-close journal rows have
`occ_symbol=NULL`, `option_strategy=NULL`, `price=underlying_spot`
(not premium), `strategy=ctx.segment` ("largecap"). Dashboard renders
them without the OPT badge; per-trade PnL math is shown against the
underlying spot price even though the actual realized PnL came from
option premium movement.

The exits DID execute correctly at the broker (the order_id on each
journal row references the actual Alpaca order). So broker history
is the authoritative source for what was really sold — OCC symbol,
option_strategy parse, strike, expiry, and the true fill price
(`filled_avg_price` from the order).

This script:
1. Scans every `quantopsai_profile_*.db` for rows where
   `occ_symbol IS NULL AND order_id IS NOT NULL` AND the row is a
   close-shaped signal (`STRONG_SELL` / `SELL` / `BUY` / `STRONG_BUY`
   / `MULTILEG`).
2. For each candidate, recovers OCC + option metadata via priority chain:
     (a) `api.get_order(order_id)` — if Alpaca's recorded symbol parses
         as an OCC, that's authoritative. Use `filled_avg_price` as the
         correct price.
     (b) FIFO match against unmatched OPEN option-leg rows on the same
         underlying — these rows DID journal `occ_symbol` correctly
         (they came from `options_multileg.py` / `options_trader.py`).
         Pair them by chronological FIFO.
     (c) Mark `unknown` (no UPDATE) and log to audit — operator can
         inspect manually if (a) and (b) both miss.
3. UPDATEs the row in-place with `occ_symbol`, `option_strategy`
   (parsed from OCC root + right code), `strike`, `expiry`, `price`
   (when from Alpaca), and `decision_price` (= price if not already
   distinct).
4. Writes JSONL audit log of every UPDATE + every skip with reason.

DEFAULT MODE IS DRY-RUN. Pass `--apply` to actually commit changes.

Idempotent: re-running skips rows that already have `occ_symbol`
populated (Step 1's WHERE clause).

Run:
  python3 scripts/backfill_option_exits_2026_05_20.py            # dry-run
  python3 scripts/backfill_option_exits_2026_05_20.py --apply    # commit

Per the memory rule `feedback_no_journal_sql_surgery`: this is
deterministic, broker-data-driven, audited — NOT manual SQL surgery.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

REPO = "/opt/quantopsai" if os.path.isdir("/opt/quantopsai") else os.getcwd()
sys.path.insert(0, REPO)

AUDIT_LOG_PATH = os.path.join(
    REPO, "scripts",
    f"backfill_option_exits_audit_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.jsonl",
)

# Signal types that are candidates for misclassification (the bug only
# affects close-shaped rows from the AI path). Skip explicit
# stock-only flows (DIVIDEND), broker-event flows (OPEXP/OPASN/OPXRC —
# activities_capture already writes occ_symbol correctly for these),
# and reconcile sentinels (synthesized rows, no broker order behind
# them — nothing to recover).
CANDIDATE_SIGNALS = {
    "STRONG_SELL", "SELL", "STRONG_BUY", "BUY",
    "MULTILEG", "OPTIONS",
}

EXCLUDE_SIGNAL_PREFIXES = (
    "DIVIDEND", "OPEXP", "OPASN", "OPXRC",
    "reconcile_backfill", "AUTO_RECONCILE",
)


def _is_occ_symbol(s: str) -> bool:
    """Same heuristic as position.py:_is_occ_symbol. Padded
    ('AAPL  260612C00150000', 21 chars) and unpadded forms accepted."""
    if not s or not isinstance(s, str):
        return False
    if len(s) < 14 or len(s) > 21:
        return False
    if not s[-8:].isdigit():
        return False
    if s[-9] not in ("C", "P"):
        return False
    head = s[:-9].rstrip()
    if len(head) < 7:
        return False
    if not head[-6:].isdigit():
        return False
    return True


def _parse_occ(occ: str) -> Dict[str, Any]:
    """Parse OCC -> underlying / expiry / strike / right.
    Robust to both padded (21-char) and unpadded (~14-21) forms."""
    s = occ.strip()
    strike_str = s[-8:]
    right = s[-9]
    yymmdd = s[-15:-9]
    underlying = s[:-15].rstrip().upper()
    expiry = datetime.strptime(yymmdd, "%y%m%d").date().isoformat()
    strike = int(strike_str) / 1000.0
    return {
        "underlying": underlying,
        "expiry": expiry,
        "strike": strike,
        "right": right,
    }


def _option_strategy_from_occ_and_side(occ: str, side: str) -> str:
    """Best-effort option_strategy label for backfilled rows. Without
    the original strategy context we can only label long/short × C/P.
    Real labels (bull_put_spread / bear_call_spread / iron_condor /
    etc.) come from multileg orchestration — single-leg AI exits
    don't carry that context, so 'long_call'/'long_put'/'short_call'/
    'short_put' is the honest label here."""
    parsed = _parse_occ(occ)
    right = parsed["right"]
    s = side.lower()
    if s == "buy":
        return "long_call" if right == "C" else "long_put"
    return "short_call" if right == "C" else "short_put"


def _list_profile_dbs() -> List[Tuple[int, str]]:
    """Return (profile_id, db_path) tuples for every per-profile DB
    that exists on this host."""
    out = []
    for fname in sorted(os.listdir(REPO)):
        m = re.match(r"^quantopsai_profile_(\d+)\.db$", fname)
        if m:
            out.append((int(m.group(1)), os.path.join(REPO, fname)))
    return out


def _build_api_for_profile(profile_id: int):
    """Build an Alpaca API client scoped to a profile's account."""
    try:
        from models import build_user_context_from_profile
        from client import get_api
        ctx = build_user_context_from_profile(profile_id)
        return ctx, get_api(ctx)
    except Exception as exc:
        print(f"[pid {profile_id}] failed to build API client: {exc}")
        return None, None


def _resolve_via_alpaca(api, order_id: str) -> Optional[Dict[str, Any]]:
    """Look up order; if its symbol parses as OCC, return recovered
    fields. Returns None on miss or any error (caller falls through to
    FIFO)."""
    if not order_id:
        return None
    try:
        order = api.get_order(order_id)
    except Exception as exc:
        return None  # Order purged or API miss; FIFO can still rescue
    raw_symbol = (getattr(order, "symbol", "") or "").strip().upper()
    if not raw_symbol or not _is_occ_symbol(raw_symbol):
        return None
    try:
        parsed = _parse_occ(raw_symbol)
    except Exception:
        return None
    filled_avg = getattr(order, "filled_avg_price", None)
    return {
        "source": "alpaca_order_history",
        "occ_symbol": raw_symbol,
        "underlying": parsed["underlying"],
        "strike": parsed["strike"],
        "expiry": parsed["expiry"],
        "right": parsed["right"],
        "filled_avg_price": float(filled_avg) if filled_avg else None,
    }


def _fifo_match_open_rows(conn: sqlite3.Connection, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Match this close row against the OLDEST unmatched OPEN option
    row on the same underlying.

    'Unmatched' = OPEN row exists with `occ_symbol IS NOT NULL` AND
    no other close row in the journal references its OCC yet. The
    natural pairing is: every OPEN must eventually pair to a CLOSE
    of the same OCC."""
    symbol = row["symbol"]
    # Find the oldest OPEN option leg on this underlying (where
    # occ_symbol is populated and side is "buy" for longs or "sell"
    # for shorts — the OPEN side of the leg).
    # We look for rows where the open side matches what this close is
    # reversing: a SELL close pairs with a BUY open (long unwind);
    # a BUY close pairs with a SELL open (short cover).
    close_side = (row.get("side") or "").lower()
    open_side = "buy" if close_side == "sell" else "sell"
    cursor = conn.execute(
        """
        SELECT id, occ_symbol, strike, expiry, side, qty, timestamp, order_id
        FROM trades
        WHERE symbol = ?
          AND occ_symbol IS NOT NULL
          AND side = ?
          AND timestamp < ?
        ORDER BY timestamp ASC
        """,
        (symbol, open_side, row["timestamp"]),
    )
    open_candidates = list(cursor.fetchall())
    if not open_candidates:
        return None
    # Identify already-matched OPENs: any close row in this DB that
    # references their OCC.
    closed_occs: set = set()
    for r in conn.execute(
        "SELECT DISTINCT occ_symbol FROM trades "
        "WHERE occ_symbol IS NOT NULL AND side = ? AND id != ?",
        (close_side, row.get("id")),
    ):
        if r[0]:
            closed_occs.add(r[0])
    for cand in open_candidates:
        if cand["occ_symbol"] in closed_occs:
            continue
        return {
            "source": "fifo_match_open_row",
            "occ_symbol": cand["occ_symbol"],
            "underlying": symbol,
            "strike": cand["strike"],
            "expiry": cand["expiry"],
            "right": cand["occ_symbol"][-9] if cand["occ_symbol"] and len(cand["occ_symbol"]) >= 9 else None,
            "filled_avg_price": None,  # FIFO match can't recover the true premium fill
            "paired_open_id": cand["id"],
        }
    return None


def _process_profile(
    profile_id: int, db_path: str, apply: bool, audit_fh
) -> Dict[str, int]:
    counts = {"scanned": 0, "candidate": 0, "resolved_alpaca": 0,
              "resolved_fifo": 0, "unresolved": 0, "updated": 0,
              "skipped_signal": 0}
    if not os.path.isfile(db_path):
        return counts

    ctx, api = _build_api_for_profile(profile_id)
    if api is None:
        print(f"[pid {profile_id}] no Alpaca client — FIFO-only resolution")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(
            """
            SELECT id, timestamp, symbol, side, qty, price, order_id,
                   signal_type, strategy, option_strategy, occ_symbol,
                   pnl
            FROM trades
            WHERE occ_symbol IS NULL
              AND order_id IS NOT NULL
              AND order_id != ''
            ORDER BY timestamp ASC
            """
        ))
        counts["scanned"] = len(rows)
        for row in rows:
            sig = (row["signal_type"] or "").strip()
            if any(sig.startswith(p) for p in EXCLUDE_SIGNAL_PREFIXES):
                counts["skipped_signal"] += 1
                continue
            if sig not in CANDIDATE_SIGNALS:
                counts["skipped_signal"] += 1
                continue
            counts["candidate"] += 1

            recovered = None
            if api is not None:
                recovered = _resolve_via_alpaca(api, row["order_id"])
                if recovered:
                    counts["resolved_alpaca"] += 1
            if recovered is None:
                recovered = _fifo_match_open_rows(conn, dict(row))
                if recovered:
                    counts["resolved_fifo"] += 1

            audit_entry = {
                "profile_id": profile_id,
                "trade_id": row["id"],
                "timestamp": row["timestamp"],
                "before": {
                    "symbol": row["symbol"],
                    "occ_symbol": row["occ_symbol"],
                    "price": row["price"],
                    "signal_type": row["signal_type"],
                    "strategy": row["strategy"],
                    "option_strategy": row["option_strategy"],
                },
                "order_id": row["order_id"],
            }

            if recovered is None:
                audit_entry["resolution"] = "UNRESOLVED"
                audit_fh.write(json.dumps(audit_entry) + "\n")
                counts["unresolved"] += 1
                continue

            opt_strategy = _option_strategy_from_occ_and_side(
                recovered["occ_symbol"], row["side"] or "sell",
            )
            new_price = recovered.get("filled_avg_price") or row["price"]
            audit_entry["resolution"] = recovered["source"]
            audit_entry["after"] = {
                "occ_symbol": recovered["occ_symbol"],
                "strike": recovered["strike"],
                "expiry": recovered["expiry"],
                "option_strategy": opt_strategy,
                "price": new_price,
            }
            if recovered["source"] == "fifo_match_open_row":
                audit_entry["paired_open_id"] = recovered.get("paired_open_id")
            audit_fh.write(json.dumps(audit_entry) + "\n")

            if apply:
                conn.execute(
                    """
                    UPDATE trades
                       SET occ_symbol = ?,
                           option_strategy = ?,
                           strike = ?,
                           expiry = ?,
                           price = ?,
                           decision_price = COALESCE(decision_price, ?)
                     WHERE id = ?
                    """,
                    (
                        recovered["occ_symbol"], opt_strategy,
                        recovered["strike"], recovered["expiry"],
                        new_price, new_price, row["id"],
                    ),
                )
                counts["updated"] += 1
            # API politeness pause
            if api is not None:
                time.sleep(0.05)
        if apply:
            conn.commit()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually UPDATE rows. Default is dry-run.")
    ap.add_argument("--profile-id", type=int, default=None,
                    help="Restrict to one profile (default: all).")
    args = ap.parse_args()

    profiles = _list_profile_dbs()
    if args.profile_id is not None:
        profiles = [(p, d) for p, d in profiles if p == args.profile_id]
    if not profiles:
        print("No profile DBs found.")
        return 1

    print(f"Mode: {'APPLY (writes)' if args.apply else 'DRY-RUN (no writes)'}")
    print(f"Profiles to scan: {[p for p, _ in profiles]}")
    print(f"Audit log: {AUDIT_LOG_PATH}")
    print()

    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    grand = {k: 0 for k in ("scanned", "candidate", "resolved_alpaca",
                              "resolved_fifo", "unresolved", "updated",
                              "skipped_signal")}
    with open(AUDIT_LOG_PATH, "w") as audit_fh:
        for pid, db_path in profiles:
            counts = _process_profile(pid, db_path, args.apply, audit_fh)
            print(
                f"[pid {pid:2d}] scanned={counts['scanned']:5d} "
                f"candidate={counts['candidate']:4d} "
                f"alpaca={counts['resolved_alpaca']:4d} "
                f"fifo={counts['resolved_fifo']:4d} "
                f"unresolved={counts['unresolved']:4d} "
                f"updated={counts['updated']:4d}"
            )
            for k in grand:
                grand[k] += counts[k]
    print()
    print("=" * 60)
    print("TOTAL:")
    for k, v in grand.items():
        print(f"  {k}: {v}")
    print(f"\nAudit log written: {AUDIT_LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
