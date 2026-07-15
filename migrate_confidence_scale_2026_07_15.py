"""One-shot prod data repair for the 2026-07-15 fix pack.

Run ON THE DROPLET, after deploying the code (the app's schema
migration adds option_proposal_outcomes.veto_class on import; this
script also adds it defensively so ordering doesn't matter):

    cd /opt/quantopsai && venv/bin/python \
        migrate_confidence_scale_2026_07_15.py            # dry-run
    cd /opt/quantopsai && venv/bin/python \
        migrate_confidence_scale_2026_07_15.py --apply

WHAT IT REPAIRS (idempotent — safe to re-run):

1. trading_profiles.ai_confidence_threshold → the manifest's intended
   0-100 integer seeds (matched BY NAME against
   create_experiment_profiles.PROFILES — never a hardcoded id range).
   Live state being repaired: six profiles at 0 (the tuner's clamped
   fraction 0.75/0.8125 int-cast to 0), three at fraction 0.6, one at
   fraction 0.55.

2. param_references rows for ai_confidence_threshold → DELETED. They
   froze the fraction seeds as day-1 anchors, making the ±50%
   reference window clamp every future proposal back below 1 (an
   absorbing zero state). The next tuner touch re-records the anchor
   at true scale.

3. tuning_history confidence rows (all 'pending') → terminally voided:
   outcome_after='voided_scale_repair' + expired_at + reviewed_at +
   an explanatory suffix on reason. Voided rows are skipped by the
   cooldown, effectiveness, auto-reversal (never reviewed) and
   auto-expiry paths, so their fraction-scale old_values can never be
   replayed into config. One corrective 'operator_scale_repair' row is
   written per repaired profile (outcome_after='n/a' immediately — an
   informational row must never enter the improved/worsened review
   flow, where a 'worsened' verdict would auto-reverse the repair).
   Deliberate side effect: the corrective row holds the 3-day tuner
   cooldown, giving the freshly-activated gate a clean baseline.

4. option_proposal_outcomes veto_class backfill (per-profile DBs):
   every option_spread_risk veto is re-keyed 'invalid_input' (all 78
   were "missing strike/premium data" verdicts on fully-priced spreads
   — the render bug, not risk judgments; verified row by row in the
   2026-07-15 investigation); every other vetoed row is keyed 'risk'.
   invalid_input rows are excluded from P(veto), veto-quality, and the
   would-be-P&L resolver.

5. user_segments defensive sweep: any fraction-scale
   ai_confidence_threshold in the legacy segment config → schema
   default 25.
"""
from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
from contextlib import closing
from datetime import datetime

import config
from create_experiment_profiles import PROFILES

VOID_SUFFIX = (
    " [VOIDED 2026-07-15 scale repair: the proposal was clamped against "
    "a fraction-scale anchor (seed 0.6/0.65 on the 0-100 gate) and the "
    "int cast wrote 0 to trading_profiles while this row recorded the "
    "fraction — see CHANGELOG 2026-07-15]"
)


def _strip_confidence_keys(node) -> bool:
    """Recursively remove 'ai_confidence_threshold' keys from an
    override JSON structure (dict of regimes/symbols/TODs → param
    dicts). Returns True when anything was removed."""
    changed = False
    if isinstance(node, dict):
        if "ai_confidence_threshold" in node:
            del node["ai_confidence_threshold"]
            changed = True
        for v in node.values():
            changed = _strip_confidence_keys(v) or changed
    elif isinstance(node, list):
        for v in node:
            changed = _strip_confidence_keys(v) or changed
    return changed


def _manifest_seeds():
    """name -> intended 0-100 int seed, from the single source of truth."""
    out = {}
    for prof in PROFILES:
        if "ai_confidence_threshold" in prof:
            v = prof["ai_confidence_threshold"]
            assert isinstance(v, int) and 1 <= v <= 100, (
                f"manifest seed {prof['name']}={v!r} is not 0-100 int — "
                f"fix create_experiment_profiles.py first")
            out[prof["name"]] = v
    return out


def repair_master(apply: bool) -> list:
    findings = []
    seeds = _manifest_seeds()
    now = datetime.utcnow().isoformat()
    with closing(sqlite3.connect(config.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row

        # ── 1. thresholds by manifest name ──────────────────────────
        rows = conn.execute(
            "SELECT id, name, user_id, ai_confidence_threshold "
            "FROM trading_profiles").fetchall()
        for r in rows:
            target = seeds.get(r["name"])
            cur = r["ai_confidence_threshold"]
            if target is None:
                # not in the manifest (controls / legacy) — repair only
                # scale corruption (fractions), never redesign.
                try:
                    curf = float(cur)
                except (TypeError, ValueError):
                    curf = None
                if curf is not None and 0 < curf < 1:
                    findings.append(
                        f"profile {r['id']} ({r['name']}): fraction "
                        f"{cur!r} -> 25 (schema default)")
                    if apply:
                        conn.execute(
                            "UPDATE trading_profiles SET "
                            "ai_confidence_threshold=25 WHERE id=?",
                            (r["id"],))
                continue
            if cur != target:
                findings.append(
                    f"profile {r['id']} ({r['name']}): "
                    f"{cur!r} -> {target}")
                if apply:
                    conn.execute(
                        "UPDATE trading_profiles SET "
                        "ai_confidence_threshold=? WHERE id=?",
                        (target, r["id"]))
                    # corrective ledger row — immediately TERMINAL on
                    # every axis: outcome 'n/a' (never reviewed → never
                    # auto-reversed), expired_at + reviewed_at stamped
                    # (never an auto-expiry candidate — review finding
                    # 2026-07-15 #1: without these stamps the expiry
                    # optimizer treated this row as a tightening and
                    # walked the repaired threshold back down 14 days
                    # out, permanently).
                    cur2 = conn.execute(
                        "INSERT INTO tuning_history (profile_id, user_id, "
                        "timestamp, adjustment_type, parameter_name, "
                        "old_value, new_value, reason, outcome_after, "
                        "expired_at, reviewed_at) "
                        "VALUES (?, ?, ?, 'operator_scale_repair', "
                        "'ai_confidence_threshold', ?, ?, ?, 'n/a', ?, ?)",
                        (r["id"], r["user_id"] or 1, now, str(cur),
                         str(target),
                         "Operator scale repair: restored the manifest's "
                         "intended 0-100 integer threshold. The fraction "
                         "seed made the confidence gate a no-op and "
                         "poisoned the tuner's clamp anchors; the live "
                         "gate (STEP 4.85) activates at this value.",
                         now, now))
                    findings.append(
                        f"  corrective ledger row id={cur2.lastrowid}")

        # ── 2. fraction-scale reference anchors ─────────────────────
        refs = conn.execute(
            "SELECT profile_id, reference_value FROM param_references "
            "WHERE parameter_name='ai_confidence_threshold'").fetchall()
        for r in refs:
            findings.append(
                f"param_reference profile {r['profile_id']} "
                f"value {r['reference_value']!r} -> DELETED")
        if apply and refs:
            conn.execute(
                "DELETE FROM param_references "
                "WHERE parameter_name='ai_confidence_threshold'")

        # ── 3. void the fraction-era confidence ledger rows ─────────
        # EVERY pre-repair confidence row on this cohort is fraction-
        # scale — voiding only 'pending' ones (the first cut) left any
        # already-reviewed row ('unchanged'/'worsened') eligible for
        # auto-expiry/auto-reversal replay of its dead-scale values
        # (review finding 2026-07-15 #1 aggravator).
        led = conn.execute(
            "SELECT id, profile_id, old_value, new_value FROM "
            "tuning_history WHERE parameter_name='ai_confidence_threshold'"
            " AND adjustment_type != 'operator_scale_repair'"
            " AND COALESCE(outcome_after,'pending') NOT LIKE 'voided%'"
        ).fetchall()
        for r in led:
            findings.append(
                f"tuning_history id {r['id']} (profile {r['profile_id']}, "
                f"{r['old_value']} -> {r['new_value']}) -> voided")
        if apply and led:
            conn.execute(
                "UPDATE tuning_history SET "
                "outcome_after='voided_scale_repair', expired_at=?, "
                "reviewed_at=COALESCE(reviewed_at, ?), reason=reason || ? "
                "WHERE id IN (%s)" % ",".join(
                    str(r["id"]) for r in led),
                (now, now, VOID_SUFFIX))

        # ── 4. fraction-era override JSON sweep ─────────────────────
        # The STEP 4.85 gate resolves through the per-symbol/regime/TOD
        # override chain, and any override minted during the fraction
        # era outranks the repaired global. Prod holds zero override
        # rows today — this sweep is the guarantee, not an assumption.
        for col in ("regime_overrides", "symbol_overrides",
                    "tod_overrides"):
            try:
                rows2 = conn.execute(
                    f"SELECT id, {col} FROM trading_profiles "
                    f"WHERE {col} IS NOT NULL AND {col} != '' "
                    f"AND {col} LIKE '%ai_confidence_threshold%'"
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            import json as _json
            for r in rows2:
                try:
                    data = _json.loads(r[col])
                except (ValueError, TypeError):
                    continue
                changed = _strip_confidence_keys(data)
                if not changed:
                    continue
                findings.append(
                    f"profile {r['id']} {col}: removed fraction-era "
                    f"ai_confidence_threshold override(s)")
                if apply:
                    conn.execute(
                        f"UPDATE trading_profiles SET {col}=? WHERE id=?",
                        (_json.dumps(data), r["id"]))

        # ── 5. legacy segment configs ────────────────────────────────
        try:
            segs = conn.execute(
                "SELECT id, ai_confidence_threshold FROM user_segments "
                "WHERE ai_confidence_threshold > 0 "
                "AND ai_confidence_threshold < 1").fetchall()
        except sqlite3.OperationalError:
            segs = []
        for r in segs:
            findings.append(
                f"user_segments id {r['id']}: fraction "
                f"{r['ai_confidence_threshold']!r} -> 25")
        if apply and segs:
            conn.execute(
                "UPDATE user_segments SET ai_confidence_threshold=25 "
                "WHERE ai_confidence_threshold > 0 "
                "AND ai_confidence_threshold < 1")

        if apply:
            conn.commit()
    return findings


def repair_profile_dbs(apply: bool) -> list:
    findings = []
    for path in sorted(glob.glob("quantopsai_profile_*.db")):
        if ".deleted" in path:
            continue
        try:
            with closing(sqlite3.connect(path)) as conn:
                conn.row_factory = sqlite3.Row
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "option_proposal_outcomes" not in tables:
                    continue
                cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(option_proposal_outcomes)")}
                has_vc = "veto_class" in cols
                if not has_vc:
                    findings.append(f"{path}: ADD COLUMN veto_class")
                    if apply:
                        conn.execute(
                            "ALTER TABLE option_proposal_outcomes "
                            "ADD COLUMN veto_class TEXT")
                        has_vc = True
                # Dry-run must PREVIEW the re-key even when the column
                # doesn't exist yet (run before deploy/restart) — the
                # veto_reason-only variants below reference no missing
                # column (review finding 2026-07-15 #3: the first cut
                # errored on every profile DB in exactly that ordering).
                # The osr match uses the recorder's two STRUCTURED
                # formats only ("vetoed_by: reason" prefix / parsed-log
                # "VETO (name)" tag) — a reason merely MENTIONING the
                # specialist must never re-key (hack-hunt #3), and only
                # class-less rows are candidates on a re-run.
                _osr_match = (
                    "(veto_reason LIKE 'option_spread_risk:%' OR "
                    "veto_reason LIKE '%VETO (option_spread_risk)%')")
                if has_vc:
                    n_osr = conn.execute(
                        "SELECT COUNT(*) FROM option_proposal_outcomes "
                        "WHERE vetoed=1 AND veto_class IS NULL AND "
                        + _osr_match).fetchone()[0]
                    n_risk = conn.execute(
                        "SELECT COUNT(*) FROM option_proposal_outcomes "
                        "WHERE vetoed=1 AND veto_class IS NULL AND NOT "
                        + _osr_match).fetchone()[0]
                else:
                    n_osr = conn.execute(
                        "SELECT COUNT(*) FROM option_proposal_outcomes "
                        "WHERE vetoed=1 AND " + _osr_match
                    ).fetchone()[0]
                    n_risk = conn.execute(
                        "SELECT COUNT(*) FROM option_proposal_outcomes "
                        "WHERE vetoed=1 AND NOT " + _osr_match
                    ).fetchone()[0]
                if n_osr or n_risk:
                    findings.append(
                        f"{path}: {n_osr} option_spread_risk veto(s) -> "
                        f"invalid_input; {n_risk} other veto(s) -> risk")
                if apply:
                    # RE-RUN SAFETY (hack-hunt #3): only rows with NO
                    # class yet are ever re-keyed — post-fix live rows
                    # carry veto_class at insert, and a re-run must
                    # never reclassify a genuine 'risk' judgment. The
                    # match is the recorder's two STRUCTURED formats
                    # ("vetoed_by: reason" prefix / the parsed-log
                    # "VETO (name)" tag), never a bare substring that a
                    # reason merely MENTIONING the specialist would hit.
                    conn.execute(
                        "UPDATE option_proposal_outcomes SET "
                        "veto_class='invalid_input' WHERE vetoed=1 "
                        "AND veto_class IS NULL AND " + _osr_match)
                    conn.execute(
                        "UPDATE option_proposal_outcomes SET "
                        "veto_class='risk' WHERE vetoed=1 AND "
                        "veto_class IS NULL")
                    conn.commit()
        except sqlite3.Error as exc:
            findings.append(f"{path}: ERROR {type(exc).__name__}: {exc}")
    return findings


def verify() -> int:
    """Post-apply invariants. Returns count of violations (0 = clean)."""
    bad = 0
    seeds = _manifest_seeds()
    with closing(sqlite3.connect(config.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
                "SELECT id, name, ai_confidence_threshold "
                "FROM trading_profiles"):
            v = r["ai_confidence_threshold"]
            try:
                vf = float(v)
            except (TypeError, ValueError):
                print(f"VIOLATION: profile {r['id']} threshold {v!r} "
                      f"non-numeric")
                bad += 1
                continue
            if 0 < vf < 1:
                print(f"VIOLATION: profile {r['id']} threshold {v!r} "
                      f"still fraction-scale")
                bad += 1
            if r["name"] in seeds and int(vf) != seeds[r["name"]]:
                print(f"VIOLATION: profile {r['id']} ({r['name']}) "
                      f"threshold {v!r} != manifest {seeds[r['name']]}")
                bad += 1
        n_refs = conn.execute(
            "SELECT COUNT(*) FROM param_references "
            "WHERE parameter_name='ai_confidence_threshold'").fetchone()[0]
        if n_refs:
            print(f"VIOLATION: {n_refs} fraction-era param_references "
                  f"row(s) survive")
            bad += 1
        n_unvoided = conn.execute(
            "SELECT COUNT(*) FROM tuning_history "
            "WHERE parameter_name='ai_confidence_threshold' "
            "AND adjustment_type != 'operator_scale_repair' "
            "AND COALESCE(outcome_after,'pending') NOT LIKE 'voided%'"
        ).fetchone()[0]
        if n_unvoided:
            print(f"VIOLATION: {n_unvoided} un-voided fraction-era "
                  f"confidence ledger row(s) survive (any outcome)")
            bad += 1
        n_bad_corrective = conn.execute(
            "SELECT COUNT(*) FROM tuning_history "
            "WHERE adjustment_type='operator_scale_repair' "
            "AND (expired_at IS NULL OR reviewed_at IS NULL "
            "     OR COALESCE(outcome_after,'') != 'n/a')").fetchone()[0]
        if n_bad_corrective:
            print(f"VIOLATION: {n_bad_corrective} corrective row(s) not "
                  f"terminally stamped (auto-expiry/review could replay "
                  f"the repair)")
            bad += 1
        for col in ("regime_overrides", "symbol_overrides",
                    "tod_overrides"):
            try:
                n_ovr = conn.execute(
                    f"SELECT COUNT(*) FROM trading_profiles "
                    f"WHERE {col} LIKE '%ai_confidence_threshold%'"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            if n_ovr:
                print(f"VIOLATION: {n_ovr} profile(s) still carry an "
                      f"ai_confidence_threshold override in {col} — the "
                      f"gate's resolve chain would outrank the repair")
                bad += 1
    for path in sorted(glob.glob("quantopsai_profile_*.db")):
        if ".deleted" in path:
            continue
        try:
            with closing(sqlite3.connect(path)) as conn:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "option_proposal_outcomes" not in tables:
                    continue
                n = conn.execute(
                    "SELECT COUNT(*) FROM option_proposal_outcomes "
                    "WHERE vetoed=1 AND veto_class IS NULL AND "
                    "(veto_reason LIKE 'option_spread_risk:%' OR "
                    "veto_reason LIKE '%VETO (option_spread_risk)%')"
                ).fetchone()[0]
                if n:
                    print(f"VIOLATION: {path} has {n} un-keyed "
                          f"option_spread_risk veto(s)")
                    bad += 1
        except sqlite3.Error as exc:
            print(f"VIOLATION: {path} unreadable: {exc}")
            bad += 1
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== confidence-scale repair 2026-07-15 [{mode}] ===")
    findings = repair_master(args.apply)
    findings += repair_profile_dbs(args.apply)
    for f in findings:
        print(" ", f)
    if not findings:
        print("  nothing to repair")
    if args.apply:
        print("--- verification ---")
        bad = verify()
        print("CLEAN" if bad == 0 else f"{bad} VIOLATION(S)")
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
