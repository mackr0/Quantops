"""Full reset for 2026-05-18 mid-day restart.

Steps (in order):
  1. Verify the 3 new Alpaca paper accounts (auth + $1M cash each)
  2. Idempotent: ensure alpaca_accounts has rows named A1/A2/A3 with
     the keys in NEW_KEYS. INSERT when missing, UPDATE when present.
  2b. Link trading_profiles.alpaca_account_id by parsing the EXP-Ax-
      prefix in the profile name. Without this step, data-source-
      health probes (which read from alpaca_accounts only) silently
      fall back to yfinance for every non-profile-context data call.
  2c. Post-condition verification of the linkage; non-zero exit if
      anything is mis-wired.
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
    # (account_name, install_label, api_key, secret)
    # `account_name` is the stable identifier written to alpaca_accounts.name
    # (A1/A2/A3 — matches the EXP-Ax-* trading_profile naming convention).
    # `install_label` is descriptive only; not stored.
    # 2026-05-19: changed schema from (acct_id, ...) to (name, ...) — keying
    # on autoincrement id was brittle because rows can be deleted and the
    # counter advances. Keying on a stable name lets step2 be idempotent.
    ("A1", "5-18-acct1-3", "PKXJRX3Q3O3EVTYUW5CQTWJ6EE",
        "58yA1ud5PZPix4HzaGVXpk3Sk3eGW9KqrPqTZDmmeT1M"),
    ("A2", "5-18-acct2-3", "PKKZNAYTW2J7X6KKBAGMQR5ZI5",
        "BkcRG2HEidTaJ557pGRGNwLcRiSQNKmbRgvymRZBFXVM"),
    ("A3", "5-18-acct3-4", "PKDWQRPW7LG62ZY55D4EF463NX",
        "CtUTJvMnbxwyVMrVMmxtoqj75CRo8gAPYhYCu7BnDciy"),
]

# Master-DB tables that the /issues page and dashboard surface — wipe
# all run-time state from today so the restart looks truly fresh.
MASTER_TABLES_TO_WIPE = [
    "audit_alerts",      # the drift detector's open + resolved items
    "scrape_runs",       # altdata cron job result rows
    "daily_snapshots",   # equity-curve history
    "activity_log",      # cross-profile activity feed (per-profile is
                         # wiped by reset_for_clean_experiment)
    "param_references",  # Item 3 of docs/17 — day-1 reference values.
                         # MUST be wiped on full reset so the post-reset
                         # profile snapshots fresh references on its
                         # first tuning event (otherwise the new profile
                         # would be locked to pre-reset parameter values).
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
            n_pos = 0
            req2 = urllib.request.Request(
                "https://paper-api.alpaca.markets/v2/positions",
                headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s},
            )
            with urllib.request.urlopen(req2, timeout=15) as r2:
                n_pos = len(json.loads(r2.read()))
            print(f"  {name} ({label:12}) equity=${eq:>12,.2f}  cash=${ca:>12,.2f}  positions={n_pos}")
            if abs(eq - 1_000_000) > 1:
                print(f"    WARNING: equity != $1M")
                ok = False
            if n_pos != 0:
                print(f"    WARNING: positions != 0")
                ok = False
        except Exception as e:
            print(f"  {name} ({label}) FAILED: {type(e).__name__}: {e}")
            ok = False
    return ok


def _resolve_user_id_for_alpaca_account(conn) -> int:
    """alpaca_accounts.user_id requires a real user. Pick the same user
    that owns the trading_profiles (they're always all owned by one
    operator in this single-tenant deployment)."""
    row = conn.execute(
        "SELECT DISTINCT user_id FROM trading_profiles"
    ).fetchall()
    if len(row) == 1:
        return row[0][0]
    # Fallback to the first user in the users table.
    row2 = conn.execute(
        "SELECT id FROM users ORDER BY id LIMIT 1"
    ).fetchone()
    if row2:
        return row2[0]
    raise RuntimeError(
        "No trading_profiles or users — cannot determine user_id for "
        "alpaca_accounts row. Bootstrap a user first."
    )


def step2_install_keys(apply: bool):
    """Idempotent install: ensure a row named A1/A2/A3 exists in
    alpaca_accounts with the keys from NEW_KEYS. INSERT when the row
    is missing, UPDATE when it exists. Keyed on `name` (stable), not
    `id` (autoincrements past prior deletions)."""
    print("\n=== STEP 2: install new keys on alpaca_accounts rows ===")
    sys.path.insert(0, "/opt/quantopsai")
    from crypto import encrypt
    with closing(sqlite3.connect("/opt/quantopsai/quantopsai.db")) as conn:
        user_id = _resolve_user_id_for_alpaca_account(conn)
        for name, label, k, s in NEW_KEYS:
            enc_k = encrypt(k)
            enc_s = encrypt(s)
            cur = conn.execute(
                "SELECT id FROM alpaca_accounts WHERE name=?", (name,),
            ).fetchone()
            if cur:
                aid = cur[0]
                print(f"  {name} (id={aid}, install_label={label})  UPDATE  key={k[:8]}***")
                if apply:
                    conn.execute(
                        "UPDATE alpaca_accounts SET "
                        "  alpaca_api_key_enc=?, alpaca_secret_key_enc=? "
                        "WHERE id=?",
                        (enc_k, enc_s, aid),
                    )
            else:
                print(f"  {name} (install_label={label})  INSERT  key={k[:8]}***")
                if apply:
                    conn.execute(
                        "INSERT INTO alpaca_accounts (user_id, name, "
                        "alpaca_api_key_enc, alpaca_secret_key_enc) "
                        "VALUES (?, ?, ?, ?)",
                        (user_id, name, enc_k, enc_s),
                    )
        if apply:
            conn.commit()
            print("  COMMITTED")
        else:
            print("  DRY-RUN — no write")


def step2b_link_profiles(apply: bool):
    """Set trading_profiles.alpaca_account_id by parsing the EXP-Ax-…
    prefix in the profile name and pointing at the matching
    alpaca_accounts.name row.

    Reason: until 2026-05-19, the reset workflow ended with all 13
    profiles having per-profile alpaca_api_key_enc set but
    alpaca_account_id NULL. The data-source-health probes (which read
    from alpaca_accounts only) failed every cycle and the system
    silently fell back to yfinance for any non-profile-context data
    call. Wiring the FK is the structural fix."""
    import re
    print("\n=== STEP 2b: link trading_profiles.alpaca_account_id ===")
    with closing(sqlite3.connect("/opt/quantopsai/quantopsai.db")) as conn:
        accts = {
            row[1]: row[0] for row in conn.execute(
                "SELECT id, name FROM alpaca_accounts").fetchall()
        }
        if not accts:
            print("  alpaca_accounts EMPTY — run step2 first")
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
                print(f"  profile {pid} '{pname}' — no EXP-Ax- prefix, skipping")
                skipped += 1
                continue
            group = m.group(1).upper()
            target_aid = accts.get(group)
            if target_aid is None:
                print(f"  profile {pid} '{pname}' — group {group} has no "
                      "alpaca_accounts row, skipping")
                skipped += 1
                continue
            if current_aid == target_aid:
                continue
            print(f"  profile {pid:>3} '{pname}' -> {group} (aid={target_aid})"
                  f"{'' if current_aid is None else f' [was {current_aid}]'}")
            linked += 1
            if apply:
                conn.execute(
                    "UPDATE trading_profiles SET alpaca_account_id=? "
                    "WHERE id=?",
                    (target_aid, pid),
                )
        if apply:
            conn.commit()
            print(f"  COMMITTED {linked} link(s); skipped {skipped}")
        else:
            print(f"  DRY-RUN — would link {linked}; skipped {skipped}")


def step2c_verify_linkage() -> bool:
    """Post-condition: alpaca_accounts has rows AND every
    trading_profile (with a name matching EXP-Ax-…) has
    alpaca_account_id pointing at the right row. Returns True on
    success, prints diagnostics + returns False on failure."""
    import re
    print("\n=== STEP 2c: verify alpaca_accounts + profile linkage ===")
    with closing(sqlite3.connect("/opt/quantopsai/quantopsai.db")) as conn:
        n_accts = conn.execute(
            "SELECT COUNT(*) FROM alpaca_accounts"
        ).fetchone()[0]
        if n_accts == 0:
            print("  FAIL: alpaca_accounts is empty")
            return False
        accts = {
            row[1]: row[0] for row in conn.execute(
                "SELECT id, name FROM alpaca_accounts").fetchall()
        }
        rx = re.compile(r"^EXP-(A\d)-", re.IGNORECASE)
        bad = []
        for pid, pname, aid in conn.execute(
            "SELECT id, name, alpaca_account_id FROM trading_profiles"
        ).fetchall():
            m = rx.match(pname or "")
            if not m:
                continue  # non-experiment profile — out of scope
            group = m.group(1).upper()
            expected = accts.get(group)
            if aid != expected:
                bad.append((pid, pname, aid, group, expected))
        if bad:
            print(f"  FAIL: {len(bad)} profile(s) mis-linked:")
            for pid, pname, aid, group, expected in bad:
                print(f"    profile {pid} '{pname}' aid={aid} "
                      f"!= expected {expected} for group {group}")
            return False
        print(f"  OK: {n_accts} accounts, all EXP-Ax- profiles linked")
        return True


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


def step5b_clear_altdata_logs(apply: bool):
    """The /issues page reads from THREE sources, not just journald
    and the master-DB tables: it also tails altdata cron log FILES
    (`/opt/quantopsai/logs/altdata-*.log`, `edgar_form4_*.log`) for
    the trailing 24h. Missing this on the 2026-05-18 reset left
    422 yfinance "possibly delisted" ERROR groups from the 06:14
    altdata cron still visible on /issues even though every other
    source was cleared. Truncating (not deleting) preserves any
    active logger handles."""
    import datetime
    print("\n=== STEP 5b: truncate today + yesterday altdata logs ===")
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    targets = [
        f"/opt/quantopsai/logs/altdata-{today:%Y%m%d}.log",
        f"/opt/quantopsai/logs/altdata-{yesterday:%Y%m%d}.log",
    ]
    targets.extend(glob.glob("/opt/quantopsai/logs/edgar_form4_*.log"))
    for path in targets:
        if not os.path.exists(path):
            continue
        sz = os.path.getsize(path)
        print(f"  {'truncate' if apply else 'would truncate'} {path} (currently {sz}B)")
        if apply:
            # cp to /tmp/ for safety, then truncate in place
            try:
                bak = f"/tmp/{os.path.basename(path)}.pre-clear.bak"
                import shutil
                shutil.copy2(path, bak)
                with open(path, "w") as f:
                    pass  # truncate
            except OSError as e:
                print(f"    FAILED: {e}")


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
    step2b_link_profiles(args.apply)
    step3_wipe_journals(args.apply)
    step4_wipe_master_tables(args.apply)
    step5_wipe_runtime_files(args.apply)
    step5b_clear_altdata_logs(args.apply)
    step6_rotate_journald(args.apply)
    # Post-condition gate: refuse to finish "OK" if the alpaca_accounts
    # / trading_profiles linkage is broken. The 2026-05-19 outage
    # discovered that this state could persist undetected for hours,
    # producing silent yfinance fallback for every non-profile-context
    # Alpaca data call. After this script runs, the system MUST be in
    # a state where data-source-health probes can resolve credentials.
    linkage_ok = True
    if args.apply:
        linkage_ok = step2c_verify_linkage()
    print()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"=== {mode} ===")
    if not args.apply:
        print("Re-run with --apply to execute.")
    if args.apply and not linkage_ok:
        print("LINKAGE VERIFICATION FAILED — alpaca_accounts state is "
              "broken. Investigate before restarting the scheduler; "
              "data-source-health probes will fail on every cycle and "
              "system-wide yfinance fallback will silently fire.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
