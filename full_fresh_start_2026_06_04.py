"""Full FRESH-START for the EXP-A* experiment (2026-06-04).

Why this script exists
----------------------
The current EXP-A* experiment data is contaminated. Profiles pid15-24
have been halted daily since 2026-05-28 across ~40 trailing-stop
orphan fills (replace-chain class). The structural fix shipped this
session prevents new orphans but the contamination window (May 20 →
today) bleeds through every learning surface: ai_predictions,
specialist_outcomes, tuning_history, learned_patterns, meta_model_state.
Per operator: this is an EXPERIMENT to measure system behavior. Bad
data invalidates measurement. Restart from clean state.

Difference from full_reset_2026_05_18.py
----------------------------------------
The May 18 script was a MID-DAY RESTART variant — preserves
trading_profiles, preserves AI learning state, just swaps Alpaca keys
+ wipes journals. That pattern is exactly what we cannot use here
because the AI learning state is what's tainted.

This script uses the TRUE FRESH-START path (clean_orphaned_profiles
+ create_experiment_profiles), which:
  * Deletes every per-profile DB file outright (not truncates)
  * Deletes every trading_profiles row and rebuilds from manifest
  * Deletes every alpaca_accounts row and inserts new with new keys
  * Cascade-deletes activity_log / tuning_history / param_references
    rows referencing the now-deleted profiles
  * Wipes audit_alerts (the /issues page)
Plus this script adds the master-table extras the May 17 fresh-start
tooling didn't know about (because they were added later):
  * shared_ai_cache (AI prompt cache; tainted by contaminated cycles)
  * decision_log (cross-cycle decision audit)
  * kill_switch_history + kill_switch_state (halt-event log + current)
Plus the runtime / altdata log / journald cleanup the May 18 script
did right.

EXPLICITLY preserved
--------------------
  * All altdata DBs (insider, congress, 13F, Form4, biotech,
    stocktwits, ~1M+ rows of world data) — these are WORLD DATA, not
    experiment artifacts. Re-downloading would be wasteful and lossy.
  * Master-DB world-data caches: alt_data_cache, earnings_dates,
    earnings_history, factor_cache, sector_cache, symbol_names,
    app_store_history, app_store_snapshot_runs, pdufa_scrape_runs
  * Universe state: daily_active_universe_snapshots,
    historical_universe_additions, universe_audit_runs
  * users, user_segment_configs, migration_markers, user_api_usage

NEW keys to install (paper-trading Alpaca accounts created 2026-06-04
funded $1M each per docs/15 v2.1: A1 baselines, A2 ablations, A3
scale-tests):
  A1 = PKYU6DWM7OHHO2PSSAECB62XFX (6-4-acct1)
  A2 = PKQ6SHWJ4SKZE6VPCR2CL6QLJ4 (6-4-acct2)
  A3 = PKHDHEGKHJQRGTNW6GD3N6TOVR (6-4-acct3)

Run:
    cd /opt/quantopsai
    venv/bin/python full_fresh_start_2026_06_04.py            # dry-run
    venv/bin/python full_fresh_start_2026_06_04.py --apply    # execute
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from contextlib import closing


# (account_name, install_label, api_key, secret) — keyed on stable
# `name` (A1/A2/A3) matching EXP-Ax-* trading_profile prefix per
# the established convention in full_reset_2026_05_18.py.
NEW_KEYS = [
    ("A1", "6-4-acct1", "PKYU6DWM7OHHO2PSSAECB62XFX",
        "A5ompa1eZTcRa6FzavjcXKRTQa8X4CJqWxBzvJi53q6x"),
    ("A2", "6-4-acct2", "PKQ6SHWJ4SKZE6VPCR2CL6QLJ4",
        "9oB2rSCzYLs6eTMRF1XuWD4BzEyc6s9YUuQnvNimvYvT"),
    ("A3", "6-4-acct3", "PKHDHEGKHJQRGTNW6GD3N6TOVR",
        "FLsq3YJDSvMCSBuZYyqw5p69nYxrPjVefzDsxnLrP3gB"),
]

# Master-DB tables to wipe AFTER clean_orphaned_profiles handles its
# cascade. These hold contaminated state that isn't keyed on
# profile_id (so cascade can't touch them).
EXTRA_MASTER_WIPE = [
    "shared_ai_cache",     # AI prompt-response cache, profile-agnostic
    "decision_log",        # cross-cycle decision audit
    "kill_switch_history", # halt-event log
]
# kill_switch_state is a singleton (id=1). Special-cased below to
# reset rather than DELETE the row (UI expects the row to exist).

REPO_ROOT = "/opt/quantopsai"
MAIN_DB = f"{REPO_ROOT}/quantopsai.db"
VENV_PYTHON = f"{REPO_ROOT}/venv/bin/python"

# Runtime marker files / caches under /opt/quantopsai/ — kill them so
# scheduled tasks re-run from a clean slate after restart.
RUNTIME_FILES_TO_DELETE = [
    f"{REPO_ROOT}/cycle_data_*.json",
    f"{REPO_ROOT}/scheduler_status.json",
    f"{REPO_ROOT}/dynamic_screener_cache.json",
    f"{REPO_ROOT}/.sync_test_marker",
    f"{REPO_ROOT}/.daily_snapshot_done.marker",
    f"{REPO_ROOT}/.daily_summary_sent_p*.marker",
    f"{REPO_ROOT}/.weekly_digest_sent.marker",
    f"{REPO_ROOT}/.capital_rebalance_done.marker",
    f"{REPO_ROOT}/.post_mortem_done_p*.marker",
]


def step1_verify_keys() -> bool:
    """Auth-check each new key + confirm $1M equity + 0 positions.
    Refuses to proceed past this gate if anything is off."""
    print("\n=== STEP 1: verify new Alpaca keys (auth + $1M + 0 positions) ===")
    ok = True
    for name, label, k, s in NEW_KEYS:
        try:
            req = urllib.request.Request(
                "https://paper-api.alpaca.markets/v2/account",
                headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            eq = float(d["equity"])
            ca = float(d["cash"])
            req2 = urllib.request.Request(
                "https://paper-api.alpaca.markets/v2/positions",
                headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s},
            )
            with urllib.request.urlopen(req2, timeout=15) as r2:
                n_pos = len(json.loads(r2.read()))
            print(f"  {name} ({label:12}) equity=${eq:>12,.2f}  "
                  f"cash=${ca:>12,.2f}  positions={n_pos}")
            if abs(eq - 1_000_000) > 1:
                print(f"    WARNING: equity != $1M")
                ok = False
            if n_pos != 0:
                print(f"    WARNING: positions != 0 (broker side not "
                      f"clean — fresh-start assumes empty accounts)")
                ok = False
        except Exception as e:
            print(f"  {name} ({label}) FAILED: {type(e).__name__}: {e}")
            ok = False
    return ok


def step2_destroy_old_state(apply: bool) -> int:
    """Invoke clean_orphaned_profiles.py with --remove-all-alpaca-accounts
    + --clear-audit-alerts. This is the destructive core:
      - backs up every per-profile DB to backups/pre-orphan-cleanup-<TS>/
      - deletes every per-profile DB file
      - deletes every trading_profiles row
      - cascade-deletes activity_log/tuning_history/param_references
        rows whose profile_id no longer resolves
      - deletes every alpaca_accounts row (--remove-all-alpaca-accounts)
      - wipes audit_alerts table (--clear-audit-alerts)
    """
    print("\n=== STEP 2: destroy old profiles + DBs + alpaca_accounts ===")
    cmd = [
        VENV_PYTHON, f"{REPO_ROOT}/clean_orphaned_profiles.py",
        "--remove-all-alpaca-accounts", "--clear-audit-alerts",
    ]
    if apply:
        cmd.append("--apply")
    # Merge stderr into stdout because clean_orphaned_profiles logs
    # through Python's `logging` module, which defaults to stderr.
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             timeout=300, cwd=REPO_ROOT)
    for line in result.stdout.splitlines():
        print(f"  {line}")
    if result.returncode != 0:
        print(f"  ABORT: clean_orphaned_profiles exit code {result.returncode}")
        return result.returncode
    return 0


def step3_install_new_keys(apply: bool) -> None:
    """INSERT 3 fresh alpaca_accounts rows with the new keys. After
    step 2 these rows do not exist."""
    print("\n=== STEP 3: install new alpaca_accounts rows ===")
    sys.path.insert(0, REPO_ROOT)
    from crypto import encrypt
    with closing(sqlite3.connect(MAIN_DB)) as conn:
        # Resolve user_id — should be the same user the manifest builds
        # profiles for (default 1). Use the most-recent / only user.
        users = [r[0] for r in conn.execute(
            "SELECT id FROM users ORDER BY id LIMIT 1").fetchall()]
        if not users:
            raise RuntimeError(
                "No users in users table — cannot insert alpaca_accounts. "
                "Bootstrap a user first.")
        user_id = users[0]
        for name, label, k, s in NEW_KEYS:
            existing = conn.execute(
                "SELECT id FROM alpaca_accounts WHERE name=?",
                (name,)).fetchone()
            if existing:
                # Shouldn't happen after step 2's wipe, but be defensive.
                print(f"  {name}: row already exists (id={existing[0]}) — "
                      "UPDATE instead of INSERT")
                if apply:
                    conn.execute(
                        "UPDATE alpaca_accounts SET "
                        "alpaca_api_key_enc=?, alpaca_secret_key_enc=? "
                        "WHERE id=?",
                        (encrypt(k), encrypt(s), existing[0]))
                continue
            print(f"  {name} (label={label}) INSERT key={k[:8]}***")
            if apply:
                conn.execute(
                    "INSERT INTO alpaca_accounts (user_id, name, "
                    "alpaca_api_key_enc, alpaca_secret_key_enc) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, name, encrypt(k), encrypt(s)))
        if apply:
            conn.commit()
            print("  COMMITTED")
        else:
            print("  DRY-RUN — no write")


def step4_build_profiles(apply: bool) -> int:
    """Invoke create_experiment_profiles.py — builds the 13 EXP-A*
    profiles from the manifest. Idempotent: after step 2 there are no
    existing rows, so this is pure INSERT."""
    print("\n=== STEP 4: build 13 EXP-A* profiles from manifest ===")
    cmd = [VENV_PYTHON,
           f"{REPO_ROOT}/create_experiment_profiles.py"]
    if apply:
        cmd.append("--apply")
    # Merge stderr into stdout — create_experiment_profiles also logs
    # through Python's logging module (stderr by default).
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             timeout=120, cwd=REPO_ROOT)
    for line in result.stdout.splitlines():
        print(f"  {line}")
    if result.returncode != 0:
        print(f"  ABORT: create_experiment_profiles exit code "
              f"{result.returncode}")
        return result.returncode
    return 0


def step5_link_profiles(apply: bool) -> None:
    """Set trading_profiles.alpaca_account_id by parsing the EXP-Ax-
    prefix in the profile name. Mirrors step2b of
    full_reset_2026_05_18.py."""
    import re
    print("\n=== STEP 5: link trading_profiles.alpaca_account_id ===")
    with closing(sqlite3.connect(MAIN_DB)) as conn:
        accts = {r[1]: r[0] for r in conn.execute(
            "SELECT id, name FROM alpaca_accounts").fetchall()}
        if not accts:
            print("  alpaca_accounts EMPTY — step 3 didn't run / failed")
            return
        profs = conn.execute(
            "SELECT id, name, alpaca_account_id FROM trading_profiles"
        ).fetchall()
        rx = re.compile(r"^EXP-(A\d)-", re.IGNORECASE)
        linked = 0
        skipped = 0
        for pid, pname, current_aid in profs:
            m = rx.match(pname or "")
            if not m:
                skipped += 1
                continue
            group = m.group(1).upper()
            target_aid = accts.get(group)
            if target_aid is None:
                print(f"  WARN: profile {pid} '{pname}' group {group} "
                      "has no alpaca_accounts row — leaving NULL")
                skipped += 1
                continue
            if current_aid == target_aid:
                continue
            print(f"  pid={pid:>3} '{pname}' -> {group} "
                  f"(aid={target_aid})")
            linked += 1
            if apply:
                conn.execute(
                    "UPDATE trading_profiles SET alpaca_account_id=? "
                    "WHERE id=?", (target_aid, pid))
        if apply:
            conn.commit()
            print(f"  COMMITTED {linked} link(s); skipped {skipped}")
        else:
            print(f"  DRY-RUN — would link {linked}; skipped {skipped}")


def step6_verify_linkage() -> bool:
    """Refuse to proceed if any EXP-Ax- profile is mis-wired.
    Prevents the silent-yfinance-fallback class found 2026-05-19."""
    import re
    print("\n=== STEP 6: verify alpaca_accounts + profile linkage ===")
    with closing(sqlite3.connect(MAIN_DB)) as conn:
        n_accts = conn.execute(
            "SELECT COUNT(*) FROM alpaca_accounts").fetchone()[0]
        if n_accts == 0:
            print("  FAIL: alpaca_accounts is empty")
            return False
        accts = {r[1]: r[0] for r in conn.execute(
            "SELECT id, name FROM alpaca_accounts").fetchall()}
        rx = re.compile(r"^EXP-(A\d)-", re.IGNORECASE)
        bad = []
        n_exp = 0
        for pid, pname, aid in conn.execute(
            "SELECT id, name, alpaca_account_id FROM trading_profiles"
        ).fetchall():
            m = rx.match(pname or "")
            if not m:
                continue
            n_exp += 1
            group = m.group(1).upper()
            expected = accts.get(group)
            if aid != expected:
                bad.append((pid, pname, aid, group, expected))
        if bad:
            print(f"  FAIL: {len(bad)} profile(s) mis-linked:")
            for pid, pname, aid, group, expected in bad:
                print(f"    pid={pid} '{pname}' aid={aid} != "
                      f"expected {expected} (group {group})")
            return False
        print(f"  OK: {n_accts} accounts, {n_exp} EXP-Ax- profiles "
              "linked correctly")
        return True


def step7_extra_master_wipe(apply: bool) -> None:
    """Wipe master-DB tables not covered by clean_orphaned_profiles'
    cascade because they aren't keyed on profile_id."""
    print("\n=== STEP 7: wipe extra master-DB tables (cold-start) ===")
    with closing(sqlite3.connect(MAIN_DB)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        for t in EXTRA_MASTER_WIPE:
            if t not in tables:
                print(f"  {t:25} (missing — skip)")
                continue
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:25} {n:>6} rows"
                  f"{' — DELETE' if apply else ' — would delete'}")
            if apply:
                conn.execute(f"DELETE FROM {t}")
        # kill_switch_state is a singleton with id=1; UI expects the
        # row to exist. Reset to the defaults instead of deleting.
        if "kill_switch_state" in tables:
            cur = conn.execute(
                "SELECT enabled, reason FROM kill_switch_state "
                "WHERE id=1").fetchone()
            if cur is not None:
                en, rea = cur
                print(f"  kill_switch_state          singleton "
                      f"(enabled={en}, reason={rea!r})"
                      f"{' — RESET' if apply else ' — would reset'}")
                if apply:
                    conn.execute(
                        "UPDATE kill_switch_state "
                        "SET enabled=0, reason=NULL, set_at=NULL, "
                        "set_by=NULL WHERE id=1")
        if apply:
            conn.commit()
            print("  COMMITTED")


def step8_runtime_files(apply: bool) -> None:
    """Delete runtime marker + cache files so scheduled tasks re-run
    from a clean slate after restart."""
    print("\n=== STEP 8: delete runtime cache + marker files ===")
    deleted = 0
    for pattern in RUNTIME_FILES_TO_DELETE:
        for path in glob.glob(pattern):
            print(f"  {'rm' if apply else 'would rm'} {path}")
            if apply:
                try:
                    os.unlink(path)
                    deleted += 1
                except OSError as e:
                    print(f"    rm FAILED: {e}")
    if apply:
        print(f"  deleted {deleted} files")


def step9_altdata_logs(apply: bool) -> None:
    """Truncate today+yesterday altdata LOG FILES (text logs only,
    not the altdata DBs — those are world data, preserved). The
    /issues page tails these logs for the trailing 24h, so leaving
    them populated would show stale altdata warnings from the
    contaminated period."""
    print("\n=== STEP 9: truncate altdata + edgar_form4 log files ===")
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    targets = [
        f"{REPO_ROOT}/logs/altdata-{today:%Y%m%d}.log",
        f"{REPO_ROOT}/logs/altdata-{yesterday:%Y%m%d}.log",
    ]
    targets.extend(glob.glob(f"{REPO_ROOT}/logs/edgar_form4_*.log"))
    for path in targets:
        if not os.path.exists(path):
            continue
        sz = os.path.getsize(path)
        print(f"  {'truncate' if apply else 'would truncate'} "
              f"{path} (currently {sz}B)")
        if apply:
            try:
                bak = f"/tmp/{os.path.basename(path)}.pre-fresh-start.bak"
                shutil.copy2(path, bak)
                with open(path, "w") as f:
                    pass  # truncate
            except OSError as e:
                print(f"    FAILED: {e}")


def step10_rotate_journald(apply: bool) -> None:
    """Clear today's journald noise (restart spam, halt warnings)
    so /issues + journalctl start clean."""
    print("\n=== STEP 10: rotate + vacuum systemd journal ===")
    if apply:
        subprocess.run(["journalctl", "--rotate"], capture_output=True)
        result = subprocess.run(
            ["journalctl", "--vacuum-time=1s"],
            capture_output=True, text=True)
        for line in result.stdout.splitlines()[-5:]:
            print(f"  {line}")
    else:
        print("  would: journalctl --rotate && "
              "journalctl --vacuum-time=1s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually perform the reset (default: dry-run)")
    args = ap.parse_args()
    print("=" * 70)
    print(f"FULL FRESH-START 2026-06-04 (apply={args.apply})")
    print("=" * 70)
    if not args.apply:
        print(
            "NOTE — Dry-run shows each step against the CURRENT state. "
            "In --apply mode the steps run sequentially, so step 4 sees "
            "the deletions step 2 performs (existing profiles get CREATE, "
            "not UPDATE), and step 5 finds the rows step 4 just inserted."
        )

    if not step1_verify_keys():
        print("\nKEY VERIFICATION FAILED — aborting before any writes.")
        return 1

    rc = step2_destroy_old_state(args.apply)
    if rc != 0:
        print("\nSTEP 2 FAILED — aborting.")
        return rc

    step3_install_new_keys(args.apply)
    rc = step4_build_profiles(args.apply)
    if rc != 0:
        print("\nSTEP 4 FAILED — aborting before linkage.")
        return rc
    step5_link_profiles(args.apply)

    linkage_ok = True
    if args.apply:
        linkage_ok = step6_verify_linkage()
        if not linkage_ok:
            print("\nLINKAGE VERIFICATION FAILED — alpaca_accounts state "
                  "is broken. Investigate before restarting the "
                  "scheduler; data-source-health probes will fail every "
                  "cycle and yfinance fallback will silently fire.")
            # Continue through extra wipes anyway — those don't depend
            # on linkage and leaving them un-cleaned helps no one.

    step7_extra_master_wipe(args.apply)
    step8_runtime_files(args.apply)
    step9_altdata_logs(args.apply)
    step10_rotate_journald(args.apply)

    print()
    print("=" * 70)
    print(f"{'APPLIED' if args.apply else 'DRY-RUN'} — done")
    print("=" * 70)
    if not args.apply:
        print("Re-run with --apply to execute.")
    elif not linkage_ok:
        return 2
    else:
        print("\nNext steps:")
        print("  - Restart services: systemctl restart "
              "quantopsai quantopsai-web")
        print("  - Verify /issues is empty")
        print("  - Watch the next scan cycle for fresh trades with "
              "correct order_id linkage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
