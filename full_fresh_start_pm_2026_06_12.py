"""Full FRESH-START for the EXP-A* experiment (2026-06-12, PM rev).

Why this script exists
----------------------
Seventh reset (second of 2026-06-12). The morning reset installed
the 6-12 account keys, verified $1M each at 03:15 UTC — and by the
13:30 open the accounts were $0 AT THE BROKER (funding vanished at
the Alpaca dashboard level; paper accounts are immutable once
created, so the operator is creating a brand-new set). Every order
was rejected 'insufficient buying power' all day with no
escalation — the dead-day class. Shipped alongside this reset:
account_funding_guard (per-cycle broker-equity check that HALTS
onto the dashboard banner within one cycle), a pre-market
broker_accounts_funded smoke check, and certify_books check 0
(BROKER FUNDING, runs before everything else).

Sixth-reset rationale (morning rev) preserved below:
Sixth reset. The 2026-06-11 hyper-accuracy audit found and fixed
the money-math + race classes (phantom P&L from short protective
placeholders, phantom cash from canceled protectives, decision-vs-
fill drift, in-cycle cash race, reconciler snapshot race, the
poll-exit cascade, and the cross-profile oversell race — see
CHANGELOG 2026-06-11). The current accounts were repaired to ZERO
broker drift, but days 1-2 carry experiment-validity scars
(cap-blocked entries, false-halt windows, churned exits, the
oversell events). Operator chose a clean restart so every arm's
data is born under the certified fill-true regime.

NEW since the last reset: clean_orphaned_profiles prints a
MANIFEST DRIFT report during step 2's dry-run — read it before
--apply; intentional drift goes into create_experiment_profiles.
PROFILES first. The manifest now carries the operator's position
caps (AI profiles 999, BuyHoldSPY 1, Randoms 5) so they survive
this and every future rebuild.

Post-reset verification is now ONE command:
    venv/bin/python certify_books.py --since-hours 168
(requires CERTIFIED CLEAN: zero broker drift, zero reconcile
actions, decomposition gaps <= $100, issues page empty.)

Carries forward unchanged: RC1-RC11 (see the 06-09 / 06-10
docstrings), the 06-04 gap fixes (step 9 full 7-day altdata-log
sweep, step 9b non-ok scrape_runs clearing).

Restart from clean state on fresh 6-12-generation paper accounts.

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

NEW keys to install (paper-trading Alpaca accounts created 2026-06-10
funded $1M each per docs/15 v2.1: A1 baselines, A2 ablations, A3
scale-tests). Operator must paste the three new keys into NEW_KEYS
below before running. The script's step1_verify_keys gate refuses
to proceed if any key fails auth, equity != $1M, or n_positions != 0.

  A1 = 6-15-acct-1 (PK4JY7TE...) verified $1M, mult=4
  A2 = 6-15-acct-2 (PKRKBFOG...) verified $1M, mult=4
  A3 = 6-15-acct-3 (PKJC2CTY...) verified $1M, mult=4

Run:
    cd /opt/quantopsai
    venv/bin/python full_fresh_start_pm_2026_06_12.py            # dry-run
    venv/bin/python full_fresh_start_pm_2026_06_12.py --apply    # execute
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
    # 2026-06-12 (seventh-reset / PM rev) — brand-new paper accounts
    # replacing the zeroed 6-12 set (paper accounts are immutable;
    # the zeroed ones are being deleted at Alpaca.com). $1M each.
    # Step1_verify_keys auth-checks + confirms equity and 0
    # positions before any destructive writes happen. NOTE: the
    # per-cycle funding guard now re-verifies broker equity every
    # cycle after this gate — script-time verification alone
    # demonstrably isn't enough (see 2026-06-12 CHANGELOG).
    ("A1", "6-15-acct-1", "PK4JY7TELSWJHYPQSKCDB3BZ3I",
        "3Rbce44SkBm7obAx8F3GCRKHoquotjhdzrckrKaSF4JV"),
    ("A2", "6-15-acct-2", "PKRKBFOGBQH6IKR75VBDKXEGMY",
        "4KPQ9ziEsb8cWcNmCBiBrJbykv2wEXU9EoWHcv3GwBVK"),
    ("A3", "6-15-acct-3", "PKJC2CTYXABABAAOGL2WFQCQVO",
        "h5YvonmTQsY5tS9YJkSkzbRHrfdgozQMhgadCfXLFJu"),
]


# 2026-06-09 — fresh Google AI Studio key for the AI cycle. Replaces
# whatever step5b restores from the pre-reset snapshot, so every
# profile starts the new experiment with the same fresh credential.
# All 13 enabled profiles currently use google/gemini-2.5-flash-lite
# (verified pre-reset), so a single key applies uniformly.
#
# 2026-06-09 (post-leak rewrite) — the original commit hardcoded
# the key inline. Github's secret scanner caught it within minutes
# and the key was auto-revoked. Read from the environment variable
# RESET_NEW_GOOGLE_AI_KEY at apply time instead. Set the var BEFORE
# running --apply:
#     export RESET_NEW_GOOGLE_AI_KEY='your-fresh-AIza...'
#     venv/bin/python full_fresh_start_2026_06_09.py --apply
# step1_verify_keys + the rest are unaffected; only step5c reads
# this var.
NEW_GOOGLE_AI_KEY = os.environ.get("RESET_NEW_GOOGLE_AI_KEY", "")

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


_AI_CONFIG_SNAPSHOT: dict = {}  # populated by step1b; consumed by step5b


def step1b_snapshot_ai_config() -> None:
    """Snapshot (ai_provider, ai_model, ai_api_key_enc) keyed by name
    BEFORE the destroy step. The manifest in `create_experiment_profiles`
    sets ai_provider + ai_model (single source of truth), but the
    encrypted API key lives only in the master DB — it can't be in
    source code per the no-master-key memory rule. This snapshot
    carries the key across the reset boundary so step 5b can restore
    it onto the rebuilt rows; without it the new profiles 401 on
    the first AI cycle (caught 2026-06-04 reset)."""
    print("\n=== STEP 1b: snapshot AI config (per-profile API key) ===")
    if not os.path.exists(MAIN_DB):
        print("  master DB not found — nothing to snapshot")
        return
    try:
        with closing(sqlite3.connect(MAIN_DB)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT name, ai_provider, ai_model, ai_api_key_enc "
                "FROM trading_profiles WHERE enabled = 1"
            ).fetchall()
    except sqlite3.Error as exc:
        print(f"  WARN: snapshot read failed: {exc}")
        return
    for r in rows:
        # Only snapshot rows that ACTUALLY have a key — the
        # zero-length-blob default doesn't carry useful state forward.
        if not r["ai_api_key_enc"]:
            continue
        _AI_CONFIG_SNAPSHOT[r["name"]] = {
            "ai_provider": r["ai_provider"],
            "ai_model": r["ai_model"],
            "ai_api_key_enc": r["ai_api_key_enc"],
        }
    print(f"  snapshotted AI config for {len(_AI_CONFIG_SNAPSHOT)} "
          f"profile(s) (matched by name across reset)")


def step5b_restore_ai_config(apply: bool) -> None:
    """Restore (ai_provider, ai_model, ai_api_key_enc) onto each
    rebuilt profile by matching the step-1b snapshot to the new
    rows by `name`. Names are stable across resets; pids are not."""
    print("\n=== STEP 5b: restore per-profile AI keys (from step-1b snapshot) ===")
    if not _AI_CONFIG_SNAPSHOT:
        print("  snapshot empty — nothing to restore (operator must "
              "set ai_api_key_enc on each profile via Settings UI)")
        return
    try:
        with closing(sqlite3.connect(MAIN_DB)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name FROM trading_profiles "
                "WHERE enabled = 1"
            ).fetchall()
            restored = 0
            unmatched = []
            for r in rows:
                snap = _AI_CONFIG_SNAPSHOT.get(r["name"])
                if not snap:
                    unmatched.append(r["name"])
                    continue
                print(f"  pid{r['id']:>3} {r['name']:<32s} restore "
                      f"{snap['ai_provider']}/{snap['ai_model']} "
                      f"key={len(snap['ai_api_key_enc'])}B")
                if apply:
                    conn.execute(
                        "UPDATE trading_profiles SET "
                        "ai_provider=?, ai_model=?, ai_api_key_enc=? "
                        "WHERE id=?",
                        (snap["ai_provider"], snap["ai_model"],
                         snap["ai_api_key_enc"], r["id"]),
                    )
                    restored += 1
            if apply:
                conn.commit()
                print(f"  restored {restored} profile(s)")
            if unmatched:
                print(f"  WARN no snapshot for: {unmatched} — set "
                      "ai_api_key_enc manually via Settings UI")
    except sqlite3.Error as exc:
        print(f"  restore failed: {exc}")


def step5c_install_new_ai_key(apply: bool) -> None:
    """OVERRIDE the snapshot-restored ai_api_key_enc with the fresh
    Google AI key on every enabled profile. Runs AFTER step5b
    (which restores provider+model+old key from snapshot) so the
    final state is: provider/model preserved, key replaced with
    the new credential the operator provided for this restart.

    Why override rather than skip step5b: step5b also carries
    ai_provider + ai_model, which we want to preserve (manifest
    defaults may be stale or null on a fresh insert). Cheapest
    safe path is "restore old key, then overwrite key with new."

    Pre-reset verification (2026-06-09): all 13 enabled profiles
    used google/gemini-2.5-flash-lite. A single Google key is
    therefore correct for every profile. If a future restart mixes
    providers, this step must be partitioned by ai_provider before
    writing — a Google key written onto a Claude profile will 401
    on the first AI cycle.
    """
    print("\n=== STEP 5c: install new Google AI key on every profile ===")
    if (not NEW_GOOGLE_AI_KEY
            or NEW_GOOGLE_AI_KEY.startswith("TODO")):
        print("  NEW_GOOGLE_AI_KEY not set — skipping. Every profile "
              "will retain whatever step5b restored (or have a NULL "
              "key if no snapshot existed).")
        return
    sys.path.insert(0, REPO_ROOT)
    # Defer encrypt() import + call until we're actually writing. The
    # crypto module reads ENCRYPTION_KEY from env at call time; dry-
    # runs from environments without it loaded would otherwise crash
    # here without ever touching the master DB.
    enc = None
    if apply:
        try:
            from crypto import encrypt
            enc = encrypt(NEW_GOOGLE_AI_KEY)
        except ImportError as exc:
            print(f"  ABORT: cannot import crypto.encrypt: {exc}")
            return
        except ValueError as exc:
            print(f"  ABORT: encrypt failed (likely ENCRYPTION_KEY "
                  f"missing from env): {exc}")
            return
    try:
        with closing(sqlite3.connect(MAIN_DB)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, ai_provider FROM trading_profiles "
                "WHERE enabled = 1"
            ).fetchall()
            n_overridden = 0
            n_skipped_non_google = 0
            for r in rows:
                provider = (r["ai_provider"] or "").lower()
                if provider != "google":
                    print(f"  SKIP pid{r['id']:>3} '{r['name']}' "
                          f"(provider={provider!r}, not google)")
                    n_skipped_non_google += 1
                    continue
                size_note = f" ({len(enc)}B encrypted)" if apply else ""
                print(f"  pid{r['id']:>3} '{r['name']}' "
                      f"ai_api_key_enc <- new Google key{size_note}")
                if apply:
                    conn.execute(
                        "UPDATE trading_profiles "
                        "SET ai_api_key_enc = ? WHERE id = ?",
                        (enc, r["id"]),
                    )
                    n_overridden += 1
            if apply:
                conn.commit()
                print(f"  COMMITTED {n_overridden} profile(s)")
                if n_skipped_non_google:
                    print(f"  SKIPPED {n_skipped_non_google} non-google "
                          "profile(s) — their keys were preserved by "
                          "step5b's snapshot restore")
            else:
                print("  DRY-RUN — no write")
    except sqlite3.Error as exc:
        print(f"  step5c failed: {exc}")


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
    """Truncate EVERY altdata LOG FILE present (text logs only — not
    the altdata DBs, those are world data and preserved). The /issues
    page tails these logs for the trailing 168 hours (the page's
    default `?hours=168` arg surfaces 7-day-old warnings). Truncating
    only today+yesterday left ~6 days of stale entries visible after
    the 2026-06-04 reset; this version sweeps every altdata-*.log
    AND edgar_form4_*.log in the logs/ dir, so post-reset /issues
    is clean across the full 7-day window."""
    print("\n=== STEP 9: truncate ALL altdata + edgar_form4 log files ===")
    targets = sorted(glob.glob(f"{REPO_ROOT}/logs/altdata-*.log"))
    targets.extend(sorted(glob.glob(f"{REPO_ROOT}/logs/edgar_form4_*.log")))
    truncated = 0
    for path in targets:
        if not os.path.exists(path):
            continue
        sz = os.path.getsize(path)
        if sz == 0:
            continue  # already empty — nothing to do
        print(f"  {'truncate' if apply else 'would truncate'} "
              f"{path} (currently {sz}B)")
        if apply:
            try:
                bak = f"/tmp/{os.path.basename(path)}.pre-fresh-start.bak"
                shutil.copy2(path, bak)
                with open(path, "w") as f:
                    pass  # truncate
                truncated += 1
            except OSError as e:
                print(f"    FAILED: {e}")
    if apply:
        print(f"  truncated {truncated} non-empty log file(s)")


def step9b_altdata_scrape_runs(apply: bool) -> None:
    """DELETE non-ok rows from each altdata DB's `scrape_runs` table.

    `scrape_runs` is OPERATIONAL TELEMETRY (when scrapes ran + their
    status) — NOT world data. The actual altdata events (insider
    filings, congressional trades, biotech catalysts, sentiment, etc.)
    live in OTHER tables in those DBs and are PRESERVED. The /issues
    page surfaces non-ok scrape_runs (`_collect_scrape_runs` in
    issues_collector.py), so leaving stale failures from the
    contaminated period would show on /issues even after the rest of
    the reset.

    Failed scrapes from before today are stale telemetry — the next
    cron job either reproduces the failure (real bug, will resurface)
    or succeeds (the historical failure is now irrelevant). Clearing
    them doesn't lose anything load-bearing."""
    print("\n=== STEP 9b: clear non-ok scrape_runs in altdata DBs ===")
    altdata_dbs = sorted(glob.glob(f"{REPO_ROOT}/altdata/*/data/*.db"))
    cleared = 0
    for db in altdata_dbs:
        try:
            with closing(sqlite3.connect(db)) as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM scrape_runs "
                    "WHERE status != 'ok'"
                ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"  {db}: read failed ({e}); skipping")
            continue
        if not n:
            continue
        print(f"  {db}: "
              f"{'DELETE' if apply else 'would delete'} "
              f"{n} non-ok scrape_runs row(s)")
        if apply:
            try:
                with closing(sqlite3.connect(db)) as conn:
                    conn.execute(
                        "DELETE FROM scrape_runs WHERE status != 'ok'")
                    conn.commit()
                cleared += n
            except sqlite3.Error as e:
                print(f"    FAILED: {e}")
    if apply:
        print(f"  cleared {cleared} non-ok scrape_runs row(s) total")


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
    print(f"FULL FRESH-START 2026-06-12 PM rev (apply={args.apply})")
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

    # Snapshot pre-destroy state for restoration after rebuild.
    step1b_snapshot_ai_config()

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
    step5b_restore_ai_config(args.apply)
    step5c_install_new_ai_key(args.apply)

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
    step9b_altdata_scrape_runs(args.apply)
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
