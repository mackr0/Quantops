"""Emergency revert: undo 2 weeks of self-tuner over-restriction.

Context. From 2026-04-22 to 2026-05-14, the self-tuner aggressively
tightened entry criteria across all profiles based on small-sample
loss patterns. Stock new entries fell from 24/day (Apr 30) to 0/day
(May 13-14). The cause is cumulative — each individual tuning step
passed its sanity check; their sum did not.

This script:
  1. Backs up master + every per-profile DB to a timestamped folder.
  2. Resets ai_confidence_threshold on profiles that drifted upward
     beyond their last-known-trading values.
  3. Pauses the self-tuner on every active profile (enable_self_tuning=0).
  4. Un-deprecates strategies that were deprecated on <30 samples
     (insufficient evidence). Strategies deprecated on >=30 samples
     stay deprecated.
  5. Logs every change to tuning_history with adjustment_type=
     'manual_revert' so the audit trail is preserved.

Per Mack's instruction: "more restrictive isn't the only path to success
— self-tuner was singularly focused on stopping losses not creating wins."
The permanent fix (sample-size minimum, two-sided tuning, aggregate
trade-eligibility floor) is a separate piece of work; this script only
undoes the immediate damage so trading can resume.

Run on prod with: python3 /tmp/revert_self_tuner_overcorrection.py
Idempotent: re-running is safe (skips already-applied changes).
"""
import datetime
import os
import shutil
import sqlite3
import sys

DB_DIR = "/opt/quantopsai"
TS = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
BACKUP_DIR = f"{DB_DIR}/backups/pre_revert_{TS}"

PROFILES_ALL = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]

# Reset pid->target_value for ai_confidence_threshold drift.
# Targets are the last value the profile had when stock entries were
# happening (Apr 30 / May 1 baseline).
RESET_AI_CONF = {
    3: 50,   # was 50 on May 1, drifted 50->70->80
    4: 25,   # was 25 on Apr 30 (24 buys/day book-wide)
    9: 50,   # was 50, jumped to 70 on May 4
    10: 50,  # was 60, jumped to 70 on May 4
    11: 50,  # was 50, jumped to 60 on May 4
}

# Strategies to un-deprecate per profile. Sample size shown in comment.
# Only un-deprecating where backing sample is <30 (insufficient evidence
# to permanently kill the strategy).
UN_DEPRECATE = {
    1: [
        ("macd_cross", 14),
        ("insider_selling_cluster", 10),  # "rolling-10 wr 0%"
        ("pullback_support", 10),         # "rolling-10 wr 0%"
    ],
    4: [
        ("gap_reversal", 19),
        ("dividend_yield", 13),
        ("ma_alignment", 10),
    ],
    5: [
        ("insider_cluster", 18),
    ],
    6: [
        ("max_pain_pinning", 15),
        ("vol_regime", 19),
        ("short_term_reversal", 19),
    ],
    7: [
        ("vol_regime", 14),
        ("max_pain_pinning", 10),
        ("gap_reversal", 14),
    ],
    8: [
        ("dividend_yield", 12),
        ("relative_strength", 11),
        ("index_correlation", 10),
    ],
    11: [
        ("index_correlation", 11),
    ],
}


def step_backup() -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    files = ["quantopsai.db"] + [
        f"quantopsai_profile_{pid}.db" for pid in PROFILES_ALL
    ]
    for fname in files:
        src = f"{DB_DIR}/{fname}"
        if not os.path.exists(src):
            print(f"  skipped (missing): {fname}")
            continue
        dst = f"{BACKUP_DIR}/{fname}"
        shutil.copy2(src, dst)
        print(f"  backed up: {fname} ({os.path.getsize(dst):,} bytes)")
    print(f"backup directory: {BACKUP_DIR}")


def _record_tuning_change(conn, pid, parameter_name, old_value, new_value,
                          reason):
    conn.execute(
        "INSERT INTO tuning_history "
        "(profile_id, user_id, adjustment_type, parameter_name, "
        " old_value, new_value, reason) "
        "VALUES (?, 1, 'manual_revert', ?, ?, ?, ?)",
        (pid, parameter_name, str(old_value), str(new_value), reason),
    )


def step_reset_ai_confidence_thresholds() -> None:
    master = f"{DB_DIR}/quantopsai.db"
    with sqlite3.connect(master) as conn:
        for pid, target in RESET_AI_CONF.items():
            row = conn.execute(
                "SELECT ai_confidence_threshold FROM trading_profiles "
                "WHERE id=?", (pid,),
            ).fetchone()
            if row is None:
                print(f"  pid {pid}: profile not found; skipping")
                continue
            current = row[0]
            if current == target:
                print(f"  pid {pid}: ai_confidence_threshold already "
                      f"{target}, skipping")
                continue
            conn.execute(
                "UPDATE trading_profiles SET ai_confidence_threshold=? "
                "WHERE id=?", (target, pid),
            )
            _record_tuning_change(
                conn, pid, "ai_confidence_threshold", current, target,
                "Manual revert (2026-05-14): self-tuner over-tightened "
                "across 14 days. Stock entries fell to 0/day. Restored "
                "to last-known-trading value.",
            )
            print(f"  pid {pid}: ai_confidence_threshold {current} -> "
                  f"{target}")
        conn.commit()


def step_pause_self_tuner() -> None:
    master = f"{DB_DIR}/quantopsai.db"
    with sqlite3.connect(master) as conn:
        rows = conn.execute(
            "SELECT id, enable_self_tuning FROM trading_profiles "
            "WHERE enabled=1",
        ).fetchall()
        for pid, current in rows:
            if current == 0:
                print(f"  pid {pid}: self-tuner already paused, skipping")
                continue
            conn.execute(
                "UPDATE trading_profiles SET enable_self_tuning=0 "
                "WHERE id=?", (pid,),
            )
            _record_tuning_change(
                conn, pid, "enable_self_tuning", current, 0,
                "Manual revert (2026-05-14): self-tuner paused while we "
                "add guardrails (sample-size minimum, aggregate trade-"
                "eligibility floor, two-sided tuning). 14-day "
                "compounding restriction ended in 0 stock entries/day.",
            )
            print(f"  pid {pid}: enable_self_tuning 1 -> 0")
        conn.commit()


def step_undeprecate_low_sample_strategies() -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for pid, strategies in UN_DEPRECATE.items():
        db = f"{DB_DIR}/quantopsai_profile_{pid}.db"
        if not os.path.exists(db):
            print(f"  pid {pid}: profile DB missing, skipping")
            continue
        with sqlite3.connect(db) as conn:
            for strat, sample_size in strategies:
                cur = conn.execute(
                    "UPDATE deprecated_strategies SET restored_at=? "
                    "WHERE strategy_type=? AND restored_at IS NULL",
                    (ts, strat),
                )
                if cur.rowcount > 0:
                    print(f"  pid {pid}: un-deprecated {strat} "
                          f"(was {sample_size} samples — insufficient)")
                else:
                    print(f"  pid {pid}: {strat} not currently deprecated, "
                          "skipping")
            conn.commit()


def step_verify() -> None:
    master = f"{DB_DIR}/quantopsai.db"
    with sqlite3.connect(master) as conn:
        rows = conn.execute(
            "SELECT id, name, ai_confidence_threshold, enable_self_tuning "
            "FROM trading_profiles WHERE enabled=1 ORDER BY id",
        ).fetchall()
        print("Final state per profile:")
        print(f"  {'pid':>4} {'name':<24} {'ai_conf':>7} {'self_tune':>9}")
        for pid, name, ai_conf, st in rows:
            print(f"  {pid:>4} {name:<24} {ai_conf:>7} {st:>9}")

    print()
    print("Active deprecated strategies after revert:")
    for pid in PROFILES_ALL:
        db = f"{DB_DIR}/quantopsai_profile_{pid}.db"
        if not os.path.exists(db):
            continue
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT strategy_type FROM deprecated_strategies "
                "WHERE restored_at IS NULL",
            ).fetchall()
            if rows:
                strategies = ", ".join(r[0] for r in rows)
                print(f"  pid {pid}: {strategies}")


def main():
    print("=" * 70)
    print("Self-tuner over-restriction revert")
    print("=" * 70)
    print()

    print("[1/5] Backing up databases...")
    step_backup()
    print()

    print("[2/5] Resetting ai_confidence_threshold drift...")
    step_reset_ai_confidence_thresholds()
    print()

    print("[3/5] Pausing self-tuner across all profiles...")
    step_pause_self_tuner()
    print()

    print("[4/5] Un-deprecating strategies on <30 samples...")
    step_undeprecate_low_sample_strategies()
    print()

    print("[5/5] Verification:")
    step_verify()
    print()
    print("DONE. Restart the scheduler to pick up the new state:")
    print("  systemctl restart quantopsai")


if __name__ == "__main__":
    sys.exit(main())
