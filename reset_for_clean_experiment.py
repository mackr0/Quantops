"""Clean-slate reset for the QuantOps virtual profiles.

Wipes all trade / prediction / virtual-state data from per-profile
DBs so the experiment can run fresh against the post-fix system.
Optionally closes broker positions first so the broker matches the
journal (both at zero) on the first new cycle.

PRESERVED:
  - Profile configs (trading_profiles) in quantopsai.db — your
    Mid Cap / Small Cap / etc. settings stay intact
  - User accounts + alpaca_account credentials
  - Altdata DBs (insider, congresstrades, edgar13f, edgar_form4,
    biotechevents, stocktwits) — these are world data, not your
    trading data
  - AI learning state by default (specialist_outcomes,
    tuning_history, learned_patterns) — pass --wipe-ai-memory
    for full cold start
  - Per-profile databases themselves (we TRUNCATE the trade /
    prediction tables, not DROP the files; schema stays)

WIPED:
  - trades (every row)
  - ai_predictions (every row)
  - virtual_profile_state (every row)
  - ai_cost_ledger (every row — fresh cost accounting)
  - activity_log (every row — fresh narrative)
  - Optional with --wipe-ai-memory: specialist_outcomes,
    tuning_history, post_mortems, learned_patterns,
    meta_model_state, etc.

ALWAYS:
  - Backup each profile DB to a timestamped folder before wipe
  - Log every action loudly

OPTIONAL FLAGS:
  --apply           Actually perform the wipe (default: dry-run)
  --close-broker    Submit market closes for every broker position
                    BEFORE the wipe (default: leave broker alone)
  --wipe-ai-memory  Also drop specialist scores, tuning history,
                    learned patterns (default: keep them)
  --user-id         Which user's profiles to reset (default 1)

Run on prod (recommended sequence):
    cd /opt/quantopsai && source .env
    # 1. Dry-run first — see what would happen
    /opt/quantopsai/venv/bin/python reset_for_clean_experiment.py
    # 2. If happy, with broker close (clean slate):
    /opt/quantopsai/venv/bin/python reset_for_clean_experiment.py \\
        --apply --close-broker
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# Tables ALWAYS wiped (trade + immediate state)
_ALWAYS_WIPE = (
    "trades",
    "ai_predictions",
    "virtual_profile_state",
    "ai_cost_ledger",
    "activity_log",
)

# Tables wiped only with --wipe-ai-memory (AI learning state)
_AI_MEMORY_WIPE = (
    "specialist_outcomes",
    "tuning_history",
    "post_mortems",
    "learned_patterns",
    "meta_model_state",
    "strategy_validations",
    "ai_shadow_calls",
)

_BACKUP_ROOT = "/opt/quantopsai/backups/pre-reset"


def _enabled_profile_dbs(user_id: int) -> List[Dict]:
    """Return [(profile_id, name, db_path, alpaca_account_id), ...]
    for every enabled profile of `user_id`."""
    from models import get_user_profiles
    profs = [p for p in get_user_profiles(user_id) if p.get("enabled")]
    out = []
    for p in profs:
        db = f"/opt/quantopsai/quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            # Local-dev path
            db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            log.warning(
                "  profile %d (%s): no DB file at expected path "
                "(%s) — skipping",
                p["id"], p["name"], db,
            )
            continue
        out.append({
            "id": p["id"], "name": p["name"], "db_path": db,
            "alpaca_account_id": p.get("alpaca_account_id"),
        })
    return out


def _backup_db(db_path: str, backup_dir: str) -> str:
    """Copy db_path to backup_dir using SQLite's online backup
    (safe with WAL). Returns the backup path."""
    os.makedirs(backup_dir, exist_ok=True)
    fname = os.path.basename(db_path)
    dest = os.path.join(backup_dir, fname)
    with sqlite3.connect(db_path) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)
    log.info("    backup → %s (%d bytes)", dest, os.path.getsize(dest))
    return dest


def _truncate_table(conn: sqlite3.Connection, table: str) -> int:
    """DELETE every row from `table`. Returns rowcount (-1 if table
    doesn't exist)."""
    try:
        cur = conn.execute(f"DELETE FROM {table}")
        return cur.rowcount or 0
    except sqlite3.OperationalError as exc:
        # Table doesn't exist in this profile DB
        log.debug("  %s: not present (%s)", table, exc)
        return -1


def _close_all_broker_positions(profiles: List[Dict]) -> Dict[str, int]:
    """Submit MARKET close orders for every broker position on every
    Alpaca account reached by `profiles`. Cached per-account so we
    don't double-submit when multiple profiles share an account."""
    from client import get_api
    from models import build_user_context_from_profile
    seen = set()
    counters = {"submitted": 0, "failed": 0, "accounts": 0}
    for p in profiles:
        acct = p.get("alpaca_account_id")
        if not acct or acct in seen:
            continue
        seen.add(acct)
        counters["accounts"] += 1
        try:
            ctx = build_user_context_from_profile(p["id"])
            api = get_api(ctx)
            positions = api.list_positions()
        except Exception as exc:
            log.error(
                "  acct %s: list_positions failed: %s: %s — "
                "SKIPPING close for this account",
                acct, type(exc).__name__, exc,
            )
            continue
        log.info(
            "  acct %s: %d positions to close",
            acct, len(positions),
        )
        for pos in positions:
            sym = getattr(pos, "symbol", "")
            qty = abs(float(getattr(pos, "qty", 0) or 0))
            side = "sell" if float(getattr(pos, "qty", 0)) > 0 else "buy"
            if qty <= 0:
                continue
            try:
                # Deliberate broker-flatten (drift-clear): the journal is
                # being wiped, so the per-profile oversell door (own-journal
                # bound) would refuse these sells. Bypass it explicitly via
                # the raw client — this is one of the few places we
                # intentionally sell what the journal no longer reflects.
                getattr(api, "unwrapped", api).submit_order(
                    symbol=sym, qty=int(qty), side=side,
                    type="market", time_in_force="day",
                )
                counters["submitted"] += 1
                log.info("    close %s %s %d", side, sym, int(qty))
            except Exception as exc:
                counters["failed"] += 1
                log.error(
                    "    close FAILED %s %s %d: %s: %s",
                    side, sym, int(qty), type(exc).__name__, exc,
                )
    return counters


def _reset_profile(
    profile: Dict, backup_dir: str, wipe_ai_memory: bool, apply: bool,
) -> Dict[str, int]:
    """Backup + truncate one profile's DB. Returns row counts."""
    log.info(
        "profile %d (%s) — db=%s",
        profile["id"], profile["name"], profile["db_path"],
    )
    counts: Dict[str, int] = {}
    if not apply:
        # Dry-run: just preview counts
        with closing(sqlite3.connect(profile["db_path"])) as conn:
            for t in _ALWAYS_WIPE + (
                _AI_MEMORY_WIPE if wipe_ai_memory else ()
            ):
                try:
                    n = conn.execute(
                        f"SELECT COUNT(*) FROM {t}"
                    ).fetchone()[0]
                    counts[t] = n
                except sqlite3.OperationalError:
                    counts[t] = -1
        for t, n in counts.items():
            if n > 0:
                log.info("    DRY would delete %d row(s) from %s", n, t)
        return counts

    # Real wipe: backup first, archive predictions for fine-tune
    # corpus, then truncate.
    _backup_db(profile["db_path"], backup_dir)
    # 2026-05-19 (Phase B1 data-collection upgrade) — archive
    # ai_predictions + ai_cycles + specialist_outcomes to JSONL
    # before they get wiped. Without this, every reset destroys
    # the future fine-tune corpus.
    try:
        from predictions_archive import archive_predictions
        archive_counts = archive_predictions(
            db_path=profile["db_path"],
            profile_id=profile["id"],
        )
        log.info(
            "    archived for fine-tune corpus: %s",
            archive_counts,
        )
    except Exception as _arc_exc:
        # If the archive fails we MUST NOT proceed with the wipe —
        # losing the data is worse than aborting the reset.
        log.error(
            "    ARCHIVE FAILED — refusing to wipe (would lose "
            "fine-tune corpus): %s: %s",
            type(_arc_exc).__name__, _arc_exc,
        )
        raise
    with closing(sqlite3.connect(profile["db_path"])) as conn:
        for t in _ALWAYS_WIPE:
            n = _truncate_table(conn, t)
            counts[t] = n
            if n > 0:
                log.info("    DELETED %d row(s) from %s", n, t)
        if wipe_ai_memory:
            for t in _AI_MEMORY_WIPE:
                n = _truncate_table(conn, t)
                counts[t] = n
                if n > 0:
                    log.info(
                        "    DELETED %d row(s) from %s (AI memory)",
                        n, t,
                    )
        conn.commit()
        # VACUUM after big delete so file size shrinks + free-list
        # gets returned. Cheap on these small DBs.
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            log.warning("    VACUUM failed: %s", exc)
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually perform the wipe (default: dry-run)")
    ap.add_argument(
        "--close-broker", action="store_true",
        help="Submit market closes for every broker position "
             "BEFORE wiping. Realizes any open P&L.",
    )
    ap.add_argument(
        "--wipe-ai-memory", action="store_true",
        help="Also wipe specialist scores / tuning history / "
             "learned patterns (default: keep them)",
    )
    ap.add_argument("--user-id", type=int, default=1)
    args = ap.parse_args()

    log.info("=" * 70)
    log.info("CLEAN-SLATE RESET (apply=%s, close_broker=%s, "
             "wipe_ai_memory=%s, user=%d)",
             args.apply, args.close_broker, args.wipe_ai_memory,
             args.user_id)
    log.info("=" * 70)

    profiles = _enabled_profile_dbs(args.user_id)
    if not profiles:
        log.error("No enabled profiles for user %d — nothing to reset.",
                  args.user_id)
        return 2
    log.info("Profiles in scope: %d", len(profiles))
    for p in profiles:
        log.info(
            "  pid=%d  name=%s  acct=%s  db=%s",
            p["id"], p["name"], p["alpaca_account_id"], p["db_path"],
        )

    # Optional: close broker positions first
    if args.close_broker:
        if not args.apply:
            log.info("DRY close_broker — would list positions but skip submit")
        else:
            log.info("Closing broker positions...")
            c = _close_all_broker_positions(profiles)
            log.info(
                "Closes: %d submitted, %d failed, across %d account(s)",
                c["submitted"], c["failed"], c["accounts"],
            )
            # Give Alpaca a moment to register the close fills before
            # the journal wipe — not strictly necessary (we're about
            # to nuke the journal anyway) but logs cleaner.
            time.sleep(2)

    # Backup dir
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = (
        f"{_BACKUP_ROOT}-{ts}"
        if args.apply else
        f"(dry-run, no backup dir)"
    )
    log.info("Backup dir: %s", backup_dir)

    # Per-profile wipe
    grand: Dict[str, int] = {}
    for p in profiles:
        c = _reset_profile(
            p, backup_dir, args.wipe_ai_memory, args.apply,
        )
        for t, n in c.items():
            grand[t] = grand.get(t, 0) + max(n, 0)

    log.info("=" * 70)
    if args.apply:
        log.info("DONE (apply=True). Per-table totals across all profiles:")
    else:
        log.info("DRY-RUN preview. Per-table totals across all profiles:")
    for t, n in sorted(grand.items()):
        if n > 0:
            log.info("  %-30s %d row(s)", t, n)
    log.info("=" * 70)
    if args.apply:
        log.info(
            "Next steps:\n"
            "  - Restart services so caches drop: systemctl restart "
            "quantopsai quantopsai-web\n"
            "  - Verify /issues page is empty\n"
            "  - Monday market open: watch the first scan cycle "
            "for fresh trades with correct order_id"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
