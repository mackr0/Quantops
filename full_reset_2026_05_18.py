"""Full reset for 2026-05-18 mid-day restart.

Steps (in order):
  1. Verify the 3 new Alpaca paper accounts (auth + $1M cash each)
  2. Re-encrypt + write new keys onto alpaca_accounts rows id=4,5,6
     (mappings: A1→4, A2→5, A3→6 — preserved from earlier setup)
  3. Wipe per-profile journal tables via reset_for_clean_experiment
  4. Wipe master DB runtime state the user explicitly asked to clear:
     audit_alerts, scrape_runs, daily_snapshots, aggregate_drift
     records (anything the /issues page surfaces)
  5. Delete runtime cache / marker files on disk

Does NOT touch:
  - trading_profiles configs
  - alpaca_accounts table structure (just rewrites key columns)
  - altdata DBs (world data)
  - AI learning state (specialist_outcomes, tuning_history,
    learned_patterns) — same default as reset_for_clean_experiment

NEW keys to install:
  alpaca_accounts.id=4  (A1) → PKWUDRMBHUHNEABYIBASNCIKOG  / 45g7K4Uca2uTM4takzfn2u3huVY4XPxK1LHaXtkHRLaG
  alpaca_accounts.id=5  (A2) → PKMKYXEANCJLWVUHUAEDHTECST  / CNBvyRA2W7tmY1PaYFh1Z8NcxLszqamBdtU5HzQjCGwH
  alpaca_accounts.id=6  (A3) → PKXZ7D3RADZ2RPGN2QUHC6BZPJ  / 85EQGriG7ZUrH8saKr5caFYV6GFvN4YuPW2fGjQJwv5d

Run:
    /opt/quantopsai/venv/bin/python full_reset_2026_05_18.py --apply
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

NEW_KEYS = [
    (4, "5-18-acct1", "PKWUDRMBHUHNEABYIBASNCIKOG",
        "45g7K4Uca2uTM4takzfn2u3huVY4XPxK1LHaXtkHRLaG"),
    (5, "5-18-acct2", "PKMKYXEANCJLWVUHUAEDHTECST",
        "CNBvyRA2W7tmY1PaYFh1Z8NcxLszqamBdtU5HzQjCGwH"),
    (6, "5-18-acct3", "PKXZ7D3RADZ2RPGN2QUHC6BZPJ",
        "85EQGriG7ZUrH8saKr5caFYV6GFvN4YuPW2fGjQJwv5d"),
]

# Master-DB tables that the /issues page and dashboard surface — wipe
# all run-time state from today so the restart looks truly fresh.
MASTER_TABLES_TO_WIPE = [
    "audit_alerts",      # the drift detector's open + resolved items
    "scrape_runs",       # altdata cron job result rows
    "daily_snapshots",   # equity-curve history
    "activity_log",      # cross-profile activity feed (per-profile is
                         # wiped by reset_for_clean_experiment)
]

# Runtime marker files / caches under /opt/quantopsai/ — kill them so
# scheduled tasks re-run from a clean slate after restart.
RUNTIME_FILES_TO_DELETE = [
    "/opt/quantopsai/cycle_data_*.json",
    "/opt/quantopsai/scheduler_status.json",
    "/opt/quantopsai/dynamic_screener_cache.json",
    "/opt/quantopsai/.sync_test_marker",
    "/opt/quantopsai/.daily_snapshot_done.marker",
    "/opt/quantopsai/.daily_summary_sent_p*.marker",
    "/opt/quantopsai/.weekly_digest_sent.marker",
    "/opt/quantopsai/.capital_rebalance_done.marker",
    "/opt/quantopsai/.post_mortem_done_p*.marker",
]


def step1_verify_keys() -> bool:
    import urllib.request, json
    print("\n=== STEP 1: verify new Alpaca keys ===")
    ok = True
    for acct_id, label, k, s in NEW_KEYS:
        try:
            req = urllib.request.Request(
                "https://paper-api.alpaca.markets/v2/account",
                headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            eq = float(d["equity"])
            ca = float(d["cash"])
            n_pos = 0
            req2 = urllib.request.Request(
                "https://paper-api.alpaca.markets/v2/positions",
                headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s},
            )
            with urllib.request.urlopen(req2, timeout=15) as r2:
                n_pos = len(json.loads(r2.read()))
            print(f"  acct_id={acct_id} {label:12} equity=${eq:>12,.2f}  cash=${ca:>12,.2f}  positions={n_pos}")
            if abs(eq - 1_000_000) > 1:
                print(f"    WARNING: equity != $1M")
                ok = False
            if n_pos != 0:
                print(f"    WARNING: positions != 0")
                ok = False
        except Exception as e:
            print(f"  acct_id={acct_id} {label} FAILED: {type(e).__name__}: {e}")
            ok = False
    return ok


def step2_install_keys(apply: bool):
    print("\n=== STEP 2: install new keys on alpaca_accounts rows ===")
    sys.path.insert(0, "/opt/quantopsai")
    from crypto import encrypt
    with closing(sqlite3.connect("/opt/quantopsai/quantopsai.db")) as conn:
        for acct_id, label, k, s in NEW_KEYS:
            enc_k = encrypt(k)
            enc_s = encrypt(s)
            cur = conn.execute(
                "SELECT name FROM alpaca_accounts WHERE id=?", (acct_id,),
            ).fetchone()
            if not cur:
                print(f"  acct_id={acct_id} NOT FOUND — skipping")
                continue
            old_name = cur[0]
            print(f"  acct_id={acct_id} ({old_name} → {label})  key={k[:8]}***")
            if apply:
                conn.execute(
                    "UPDATE alpaca_accounts SET "
                    "  alpaca_api_key_enc=?, alpaca_secret_key_enc=?, name=? "
                    "WHERE id=?",
                    (enc_k, enc_s, label, acct_id),
                )
        if apply:
            conn.commit()
            print("  COMMITTED")
        else:
            print("  DRY-RUN — no write")


def step3_wipe_journals(apply: bool):
    print("\n=== STEP 3: per-profile journal wipe (reset_for_clean_experiment.py) ===")
    cmd = ["/opt/quantopsai/venv/bin/python",
           "/opt/quantopsai/reset_for_clean_experiment.py"]
    if apply:
        cmd.append("--apply")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                            cwd="/opt/quantopsai")
    # Print only summary lines
    for line in result.stdout.splitlines():
        if any(s in line for s in ("WIPED", "PRESERVED", "BACKUP",
                                    "Reset complete", "DRY-RUN",
                                    "TOTAL", "===", "profile",
                                    "trades", "ai_predictions")):
            print(f"  {line}")
    if result.returncode != 0:
        print(f"  reset script returncode={result.returncode}")
        print(f"  stderr: {result.stderr[:500]}")


def step4_wipe_master_tables(apply: bool):
    print("\n=== STEP 4: wipe master-DB runtime tables ===")
    with closing(sqlite3.connect("/opt/quantopsai/quantopsai.db")) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        for t in MASTER_TABLES_TO_WIPE:
            if t not in tables:
                print(f"  {t:20} (missing — skip)")
                continue
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:20} {n:>6} rows{' — DELETE' if apply else ' — would delete'}")
            if apply:
                conn.execute(f"DELETE FROM {t}")
        if apply:
            conn.commit()
            print("  COMMITTED")


def step5_wipe_runtime_files(apply: bool):
    print("\n=== STEP 5: delete runtime cache + marker files ===")
    deleted = 0
    for pattern in RUNTIME_FILES_TO_DELETE:
        for path in glob.glob(pattern):
            print(f"  {'rm' if apply else 'would rm'} {path}")
            if apply:
                os.unlink(path)
                deleted += 1
    if apply:
        print(f"  deleted {deleted} files")


def step6_rotate_journald(apply: bool):
    """Clear today's noisy journald entries (15 fake DB-corruption
    alerts, restart spam) so /issues + journalctl start clean."""
    print("\n=== STEP 6: rotate + vacuum systemd journal ===")
    if apply:
        subprocess.run(["journalctl", "--rotate"], capture_output=True)
        result = subprocess.run(
            ["journalctl", "--vacuum-time=1s"],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines()[-5:]:
            print(f"  {line}")
    else:
        print("  would: journalctl --rotate && journalctl --vacuum-time=1s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually perform the reset (default: dry-run)")
    args = ap.parse_args()
    if not step1_verify_keys():
        print("\nKEY VERIFICATION FAILED — aborting before any writes.")
        return 1
    step2_install_keys(args.apply)
    step3_wipe_journals(args.apply)
    step4_wipe_master_tables(args.apply)
    step5_wipe_runtime_files(args.apply)
    step6_rotate_journald(args.apply)
    print()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"=== {mode} ===")
    if not args.apply:
        print("Re-run with --apply to execute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
