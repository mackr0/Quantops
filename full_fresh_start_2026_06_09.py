"""Full FRESH-START for the EXP-A* experiment (2026-06-09).

Why this script exists
----------------------
Third contamination wave. After the 2026-06-05 fresh-start, drift
re-accumulated because the cross-profile share consumption pattern
was still active on both sell and cover paths, and several other
critical bugs surfaced during the day's investigation:

  RC1  cross-profile SELL consumption (`allowable_sell_qty` checked
       aggregate broker pool and DOWNSIZED to it, letting one
       profile sell shares the aggregate held but other profiles
       virtually owned). Fixed in commit ba1145b: per-profile
       virtual-qty cap + drift detection; no downsize path.

  RC2  cross-profile COVER consumption (same architecture, short
       side — buying-to-cover with no per-profile guard let one
       profile close sibling profiles' shorts via the aggregate
       short pool). Fixed in commit 32d0e21: `allowable_cover_qty`
       mirror + trader.py COVER branch wired through.

  RC3  ATR-derived stop/TP percentages were unbounded — for low-
       priced volatile stocks, ATR×3/price produced 80%+ TP targets
       and 50%+ stops that defeated their own purpose. 0 of 45
       closed trades on pid 42 hit their TP in 30 days. Fixed in
       commit 8540703: `risk_clamps.py` enforces TP [4%, 12%] and
       SL [3%, 7%].

  RC4  Broker-side take-profit orders were never placed — the
       function existed but no code path called it. TPs went through
       a 5-min polling cycle that missed intra-cycle spikes past
       the AI target. Fixed in commit 6f0a686: `ensure_protective_
       stops` now places a GTC limit at the entry's take_profit
       alongside the stop.

  RC5  Vertical-spread dispatcher trusted the AI's `{"short", "long"}`
       labels which the AI inverted for bear strategies (RGNT, POET
       Multi-leg build failures observed). Fixed in commit 15d4a85:
       dispatcher sorts strikes; builder assigns short/long by
       structural rule.

  RC6  Multileg sequential fallback submitted credit-spread legs
       shorts-first, hitting Alpaca's uncovered-short check on
       NOK and similar. Fixed earlier today: buys-first ordering
       in the sequential path.

  RC7  Catastrophic single-trade gate death spiral (gate threshold
       fell below max_position_dollars when recent_avg was small).
       Fixed earlier today: threshold floored at max_position_dollars.

  RC8  Brain badge correctness — MULTILEG_OPEN logged as a drop,
       stale 2h-window cross-cycle bleed. Fixed earlier today.

The aggregate drift detection alert (multi_scheduler.py:2886-2913)
is operational and was confirmed firing today against CHAI residual
drift — that's the safety net for whatever I haven't found.

With all critical bugs structurally prevented, restart from clean
state and let the experiment collect real data on architecture
that won't keep mutating underneath it.

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

NEW keys to install (paper-trading Alpaca accounts created 2026-06-09
funded $1M each per docs/15 v2.1: A1 baselines, A2 ablations, A3
scale-tests). Operator must paste the three new keys into NEW_KEYS
below before running. The script's step1_verify_keys gate refuses
to proceed if any key fails auth, equity != $1M, or n_positions != 0.

  A1 = TODO_PASTE_KEY_HERE
  A2 = TODO_PASTE_KEY_HERE
  A3 = TODO_PASTE_KEY_HERE

Run:
    cd /opt/quantopsai
    venv/bin/python full_fresh_start_2026_06_09.py            # dry-run
    venv/bin/python full_fresh_start_2026_06_09.py --apply    # execute
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
    # 2026-06-09 — paper-trading Alpaca accounts created 2026-06-09,
    # $1M each. Step1_verify_keys auth-checks each + confirms equity
    # and 0 positions before any destructive writes happen.
    ("A1", "6-9-acct1", "PKKZNIDVR5ZV6DBHK3INPF5WT7",
        "qf1zjTcJsHDvvoNMoCCw9W7qsKUzDfoLHHyC7q8om98"),
    ("A2", "6-9-acct2", "PKHJQLCTMG4OSG25OVSUY3IDGJ",
        "JC4RctqFeGMiEYts21MVhuvYGVVUtuKHtpKRJosapLPF"),
    ("A3", "6-9-acct3", "PKNXSPNRE6NDT7B4D6JSE6LKBN",
        "DAQjjkH7dCVDur7Xx1amcnGVnUB8XSCiVmMD9wsbaQax"),
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
    print(f"FULL FRESH-START 2026-06-09 (apply={args.apply})")
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
