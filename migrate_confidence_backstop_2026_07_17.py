"""One-shot prod reseed for the 2026-07-17 confidence-gate backstop.

Run ON THE DROPLET, after deploying the code (the app's schema
migration adds trade_drops.pred_id on import; this script also adds it
defensively so ordering doesn't matter):

    cd /opt/quantopsai && venv/bin/python \
        migrate_confidence_backstop_2026_07_17.py            # dry-run
    cd /opt/quantopsai && venv/bin/python \
        migrate_confidence_backstop_2026_07_17.py --apply

WHY: the gate went live 2026-07-15 at the arm bars (60/65/55). Two
live days proved those bars sit at/above the AI's HONEST confidence
scale (pre-gate median 62; the meta-model blend then discounts stated
confidence BEFORE the gate reads it) — so the "backstop" became the
allocator: 132 blocks vs 39 executed buys in half a day (07-17),
books at 70-92% cash. Operator design rule: gates INFORM the AI; the
hard floor fires rarely. 45 is backstop level — below the honest
distribution's bulk, above genuine no-conviction tail ideas.

WHAT IT DOES (idempotent — safe to re-run):

1. trading_profiles.ai_confidence_threshold → the manifest's 45
   backstop seeds, matched BY NAME against
   create_experiment_profiles.PROFILES (never a hardcoded id range).
   Controls (BuyHoldSPY / Randoms, not in the manifest map) are
   untouched. One 'operator_backstop_reseed' ledger row per changed
   profile — immediately TERMINAL on every axis (outcome 'n/a',
   expired_at + reviewed_at stamped) so review/auto-reversal/
   auto-expiry can never replay it. Deliberate side effect: the row
   holds the 3-day tuner cooldown, giving the 45 floor a clean
   baseline window.

2. param_references rows for ai_confidence_threshold → DELETED. Any
   anchor recorded during the 60/65/55 era would clamp future tuner
   proposals to a ±window around the old bar. The next tuner touch
   re-records the anchor at the backstop scale.

3. tuning_history ai_confidence_threshold rows that are NOT operator
   rows and NOT already voided → terminally voided
   ('voided_backstop_reseed' + expired_at + reviewed_at + reason
   suffix). Prod holds zero such rows today (the 07-15 repair's
   cooldown ran through 07-18) — this is the guarantee, not an
   assumption: an auto-expiry replay of a 60-era value would
   silently overwrite the reseed (the 2026-07-15 review-finding #1
   class).

4. Override JSON re-sweep (regime/symbol/TOD): any
   ai_confidence_threshold override outranks the reseeded global in
   the gate's resolve chain → stripped. Same defensive guarantee as
   #3.

5. Per-profile DBs: ADD COLUMN trade_drops.pred_id if missing, then
   BACKFILL the link for existing CONFIDENCE_GATE drops via exact
   (cycle_id, symbol) join to ai_predictions (both written in the
   same cycle; the prediction is recorded pre-gate with a non-HOLD
   signal). This retro-links the 07-15..07-17 drops so their
   counterfactual outcomes count as evidence too.
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
    " [VOIDED 2026-07-17 backstop reseed: this row's values belong to "
    "the 60/65/55 allocator-era bars; replaying them would overwrite "
    "the operator's 45 backstop floor — see CHANGELOG 2026-07-17]"
)

#: The ONE definition of "this CONFIDENCE_GATE drop is safely
#: backfill-linkable" — shared by the preview count, the apply UPDATE,
#: and verify(), so the three can never disagree about which rows are
#: in scope. {T} is the trade_drops table alias (SQLite UPDATE can't
#: alias its target). The NOT EXISTS guard skips drops where the same
#: symbol ALSO executed an entry within ±10 min (one cycle): ranked
#: alternates are gate-checked but carry no prediction row, and
#: without the guard a blocked alternate's drop would link to the
#: executed primary's prediction — a real trade's outcome counted as
#: "refused" (review 2026-07-17 M3). Skipping forfeits at most one
#: counterfactual row; a mis-link poisons a band. The LIVE path needs
#: no guard: pred_id is stamped only on primaries at recording time.
LINKABLE_WHERE_TPL = (
    "{T}.drop_code='CONFIDENCE_GATE' "
    "AND {T}.cycle_id IS NOT NULL "
    "AND EXISTS (SELECT 1 FROM ai_predictions p "
    "  WHERE p.cycle_id = {T}.cycle_id "
    "  AND p.symbol = {T}.symbol "
    "  AND UPPER(COALESCE(p.predicted_signal,'')) != 'HOLD') "
    "AND NOT EXISTS (SELECT 1 FROM trades t "
    "  WHERE t.symbol = {T}.symbol "
    "  AND t.side IN ('buy', 'short') "
    "  AND COALESCE(t.status,'open') NOT IN "
    "      ('pending_protective', 'canceled', 'expired', "
    "       'rejected', 'done_for_day', "
    "       'auto_reconciled_phantom_close') "
    "  AND ABS(strftime('%s', t.timestamp) "
    "      - strftime('%s', {T}.timestamp)) <= 600)"
)


def _strip_confidence_keys(node) -> bool:
    """Recursively remove 'ai_confidence_threshold' keys from an
    override JSON structure. Returns True when anything was removed."""
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
    """name -> intended backstop seed, from the single source of truth."""
    out = {}
    for prof in PROFILES:
        if "ai_confidence_threshold" in prof:
            v = prof["ai_confidence_threshold"]
            assert isinstance(v, int) and 1 <= v <= 100, (
                f"manifest seed {prof['name']}={v!r} is not 0-100 int — "
                f"fix create_experiment_profiles.py first")
            out[prof["name"]] = v
    return out


def reseed_master(apply: bool) -> list:
    findings = []
    seeds = _manifest_seeds()
    now = datetime.utcnow().isoformat()
    with closing(sqlite3.connect(config.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row

        # ── 1. thresholds by manifest name ──────────────────────────
        rows = conn.execute(
            "SELECT id, name, user_id, ai_confidence_threshold "
            "FROM trading_profiles").fetchall()
        _matched = set()
        for r in rows:
            target = seeds.get(r["name"])
            if target is None:
                continue  # controls / legacy — never redesigned here
            _matched.add(r["name"])
            cur = r["ai_confidence_threshold"]
            if cur == target:
                continue
            findings.append(
                f"profile {r['id']} ({r['name']}): {cur!r} -> {target}")
            if apply:
                conn.execute(
                    "UPDATE trading_profiles SET "
                    "ai_confidence_threshold=? WHERE id=?",
                    (target, r["id"]))
                cur2 = conn.execute(
                    "INSERT INTO tuning_history (profile_id, user_id, "
                    "timestamp, adjustment_type, parameter_name, "
                    "old_value, new_value, reason, outcome_after, "
                    "expired_at, reviewed_at) "
                    "VALUES (?, ?, ?, 'operator_backstop_reseed', "
                    "'ai_confidence_threshold', ?, ?, ?, 'n/a', ?, ?)",
                    (r["id"], r["user_id"] or 1, now, str(cur),
                     str(target),
                     "Operator backstop reseed: the 60/65/55 bars sat "
                     "at/above the AI's honest confidence median (62) "
                     "and the meta-model blend discounts stated "
                     "confidence before the gate reads it, so the "
                     "'backstop' blocked most entry flow (132 blocks "
                     "vs 39 buys, 2026-07-17 AM) and books stockpiled "
                     "cash. 45 restores the design: prompt informs, "
                     "floor fires rarely. Gate drops are now "
                     "counterfactually scored (trade_drops.pred_id) "
                     "so future moves of this bar are evidence-based.",
                     now, now))
                findings.append(
                    f"  reseed ledger row id={cur2.lastrowid}")

        # Name-drift surfaced in the DRY-RUN too, not only at verify():
        # an unmatched manifest seed means that profile silently keeps
        # its allocator-era bar (review 2026-07-17 M4).
        _unmatched = sorted(set(seeds) - _matched)
        if _unmatched:
            findings.append(
                f"WARNING: {len(_unmatched)} manifest seed(s) matched "
                f"no profile by name — those keep their current bars: "
                f"{', '.join(_unmatched)}")

        # ── 2. old-bar reference anchors ────────────────────────────
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

        # ── 3. void any replay-eligible allocator-era ledger rows ───
        led = conn.execute(
            "SELECT id, profile_id, old_value, new_value FROM "
            "tuning_history WHERE parameter_name='ai_confidence_threshold'"
            " AND adjustment_type NOT IN "
            "     ('operator_scale_repair', 'operator_backstop_reseed')"
            " AND COALESCE(outcome_after,'pending') NOT LIKE 'voided%'"
        ).fetchall()
        for r in led:
            findings.append(
                f"tuning_history id {r['id']} (profile {r['profile_id']}, "
                f"{r['old_value']} -> {r['new_value']}) -> voided")
        if apply and led:
            conn.execute(
                "UPDATE tuning_history SET "
                "outcome_after='voided_backstop_reseed', expired_at=?, "
                "reviewed_at=COALESCE(reviewed_at, ?), "
                # COALESCE: `NULL || suffix` is NULL in SQLite — a
                # NULL-reason row would void correctly but silently
                # lose its audit annotation (review 2026-07-17 L4).
                "reason=COALESCE(reason, '') || ? "
                "WHERE id IN (%s)" % ",".join(
                    str(r["id"]) for r in led),
                (now, now, VOID_SUFFIX))

        # ── 4. override JSON re-sweep ────────────────────────────────
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
                if not _strip_confidence_keys(data):
                    continue
                findings.append(
                    f"profile {r['id']} {col}: removed allocator-era "
                    f"ai_confidence_threshold override(s)")
                if apply:
                    conn.execute(
                        f"UPDATE trading_profiles SET {col}=? WHERE id=?",
                        (_json.dumps(data), r["id"]))

        if apply:
            conn.commit()
    return findings


def link_profile_dbs(apply: bool) -> list:
    """ADD trade_drops.pred_id where missing + backfill existing
    CONFIDENCE_GATE drops via exact (cycle_id, symbol) join."""
    findings = []
    for path in sorted(glob.glob("quantopsai_profile_*.db")):
        if ".deleted" in path:
            continue
        try:
            with closing(sqlite3.connect(path)) as conn:
                conn.row_factory = sqlite3.Row
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "trade_drops" not in tables:
                    continue
                cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(trade_drops)")}
                has_col = "pred_id" in cols
                if not has_col:
                    findings.append(f"{path}: ADD COLUMN pred_id")
                    if apply:
                        conn.execute(
                            "ALTER TABLE trade_drops "
                            "ADD COLUMN pred_id INTEGER")
                        has_col = True
                # Dry-run previews the linkable count even before the
                # column exists (run-before-deploy ordering, the
                # 2026-07-15 review-finding #3 lesson). The join is
                # EXACT: same cycle_id + same symbol + a non-HOLD
                # prediction (the pre-gate recording of the same
                # idea). One guard on top (review 2026-07-17 M3):
                # ranked ALTERNATES are gate-checked too but carry no
                # prediction row — if the same symbol ALSO executed as
                # a primary in that cycle, a blocked alternate's drop
                # would link to the EXECUTED primary's prediction and
                # count a real trade's outcome as "refused". Any
                # entry-side trades row for the symbol within ±10 min
                # of the drop (a cycle runs in minutes) marks that
                # case; skipping it forfeits at most one counterfactual
                # row, while a mis-link poisons a band — perfect-data
                # rule says skip. The live path needs no guard: it
                # stamps pred_id only on primaries at recording time.
                if has_col:
                    n_link = conn.execute(
                        "SELECT COUNT(*) FROM trade_drops d WHERE "
                        "d.pred_id IS NULL AND "
                        + LINKABLE_WHERE_TPL.format(T="d")
                    ).fetchone()[0]
                else:
                    n_link = conn.execute(
                        "SELECT COUNT(*) FROM trade_drops d WHERE "
                        + LINKABLE_WHERE_TPL.format(T="d")).fetchone()[0]
                if n_link:
                    findings.append(
                        f"{path}: {n_link} CONFIDENCE_GATE drop(s) "
                        f"-> linked to their prediction rows")
                if apply and has_col:
                    conn.execute(
                        "UPDATE trade_drops SET pred_id = ("
                        "  SELECT p.id FROM ai_predictions p "
                        "  WHERE p.cycle_id = trade_drops.cycle_id "
                        "  AND p.symbol = trade_drops.symbol "
                        "  AND UPPER(COALESCE(p.predicted_signal,'')) "
                        "      != 'HOLD' "
                        "  ORDER BY p.id LIMIT 1) "
                        "WHERE pred_id IS NULL AND "
                        + LINKABLE_WHERE_TPL.format(T="trade_drops"))
                    conn.commit()
        except sqlite3.Error as exc:
            findings.append(f"{path}: ERROR {type(exc).__name__}: {exc}")
    return findings


def verify() -> int:
    """Post-apply invariants. Returns count of violations (0 = clean)."""
    bad = 0
    seeds = _manifest_seeds()
    matched = set()
    with closing(sqlite3.connect(config.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
                "SELECT id, name, ai_confidence_threshold "
                "FROM trading_profiles"):
            if r["name"] in seeds:
                matched.add(r["name"])
                if int(float(r["ai_confidence_threshold"])) \
                        != seeds[r["name"]]:
                    print(f"VIOLATION: profile {r['id']} ({r['name']}) "
                          f"threshold {r['ai_confidence_threshold']!r} != "
                          f"manifest backstop {seeds[r['name']]}")
                    bad += 1
        # Every manifest seed must have found its prod row — a renamed
        # profile would otherwise keep its allocator-era bar while this
        # script exits 0 with "CLEAN" (review 2026-07-17 M4: the
        # name-join fails SILENT, and a dry-run against a drifted DB is
        # indistinguishable from a no-op).
        unmatched = sorted(set(seeds) - matched)
        if unmatched:
            print(f"VIOLATION: {len(unmatched)} manifest seed(s) "
                  f"matched no prod profile by name — those profiles "
                  f"keep their old bars: {', '.join(unmatched)}")
            bad += 1
        n_refs = conn.execute(
            "SELECT COUNT(*) FROM param_references "
            "WHERE parameter_name='ai_confidence_threshold'").fetchone()[0]
        if n_refs:
            print(f"VIOLATION: {n_refs} old-bar param_references "
                  f"row(s) survive")
            bad += 1
        n_replayable = conn.execute(
            "SELECT COUNT(*) FROM tuning_history "
            "WHERE parameter_name='ai_confidence_threshold' "
            "AND adjustment_type NOT IN "
            "    ('operator_scale_repair', 'operator_backstop_reseed') "
            "AND COALESCE(outcome_after,'pending') NOT LIKE 'voided%'"
        ).fetchone()[0]
        if n_replayable:
            print(f"VIOLATION: {n_replayable} replay-eligible "
                  f"allocator-era ledger row(s) survive")
            bad += 1
        n_bad_reseed = conn.execute(
            "SELECT COUNT(*) FROM tuning_history "
            "WHERE adjustment_type='operator_backstop_reseed' "
            "AND (expired_at IS NULL OR reviewed_at IS NULL "
            "     OR COALESCE(outcome_after,'') != 'n/a')").fetchone()[0]
        if n_bad_reseed:
            print(f"VIOLATION: {n_bad_reseed} reseed row(s) not "
                  f"terminally stamped (auto-expiry/review could "
                  f"replay the reseed)")
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
                      f"ai_confidence_threshold override in {col}")
                bad += 1
    for path in sorted(glob.glob("quantopsai_profile_*.db")):
        if ".deleted" in path:
            continue
        try:
            with closing(sqlite3.connect(path)) as conn:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "trade_drops" not in tables:
                    continue
                cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(trade_drops)")}
                if "pred_id" not in cols:
                    print(f"VIOLATION: {path} trade_drops has no "
                          f"pred_id column")
                    bad += 1
                    continue
                n_unlinked = conn.execute(
                    "SELECT COUNT(*) FROM trade_drops d "
                    "WHERE d.pred_id IS NULL AND "
                    + LINKABLE_WHERE_TPL.format(T="d")).fetchone()[0]
                if n_unlinked:
                    print(f"VIOLATION: {path} has {n_unlinked} "
                          f"linkable-but-unlinked gate drop(s)")
                    bad += 1
        except sqlite3.Error as exc:
            print(f"VIOLATION: {path} unreadable: {exc}")
            bad += 1
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default: dry-run)")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== confidence backstop reseed 2026-07-17 [{mode}] ===")
    findings = reseed_master(args.apply)
    findings += link_profile_dbs(args.apply)
    for f in findings:
        print("  " + f)
    if not findings:
        print("  nothing to do")
    if args.apply:
        bad = verify()
        print(f"=== verification: "
              f"{'CLEAN' if bad == 0 else f'{bad} VIOLATION(S)'} ===")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
