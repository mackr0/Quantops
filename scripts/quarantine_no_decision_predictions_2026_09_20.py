"""Quarantine the HOLD "predictions" journaled for AI calls that FAILED.

2026-09-20 — when the apex batch call failed (provider 429/5xx,
unparseable output) or was cost-capped, `ai_analyst.ai_select_trades`
returned a stand-in with an empty trade list, and the pipeline's
"record a prediction for every candidate the AI analyzed" loop read
that as HOLD on every candidate. Over Experiment 2's first four weeks
that journaled 8,187 HOLD predictions no model ever made (the Gemini
arms lost ~21% of their cycles to quota errors); they resolved and
were graded like real ones. The pipeline no longer writes them
(`ai_analyst.is_no_decision`); this script deals with the ones already
there.

WHY MOVE, NOT TAG. More than thirty modules read `ai_predictions`
(the meta-model, Kelly sizing, alpha decay, specialist calibration,
the case-file RAG, the tuner, the scoreboard, ...) and most know
nothing of `data_quality`. A tag protects only the readers that check
it. Physically separating the rows protects every reader, present and
future, with no code change — the project's standing rule for
learning data.

WHAT MOVES. Each fabricated prediction, plus the rows that hang off it
in `ai_prediction_outcomes` and `specialist_outcomes` (on these cycles
the specialists failed too — measured 99.5-100% ABSTAIN — so they carry
no calibration information), into sibling `*_no_decision` tables in
the SAME journal, with when and why. Nothing is deleted from the
database; `--restore` moves everything back. The `ai_cycles` row stays
where it is: it is the true record that the cycle happened and failed.

A row is fabricated only if BOTH hold: its cycle's stored response is
the failure stand-in (recognised by shape, not by a loose text match —
`finetune.dataset_builder._is_failed_call`), AND the row itself is a
HOLD. Anything else on such a cycle is reported and left alone.

DRY-RUN by default; --apply to write. One transaction per journal.
Idempotent. Run from the repo root; journals are the active profiles'
(never a hardcoded id range).
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir)))

REASON = "ai_call_failed_or_cost_capped"
# (live table, quarantine table, column holding the prediction id)
DEPENDENTS = (
    ("ai_prediction_outcomes", "ai_prediction_outcomes_no_decision",
     "prediction_id"),
    ("specialist_outcomes", "specialist_outcomes_no_decision",
     "prediction_id"),
)
MAIN = ("ai_predictions", "ai_predictions_no_decision", "id")


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _ensure_quarantine(conn, live, quarantine):
    """A quarantine table with the live table's columns plus when/why.
    Columns the live table has gained since are added, so a later
    restore never loses a field."""
    live_cols = conn.execute(f"PRAGMA table_info({live})").fetchall()
    if quarantine not in _tables(conn):
        defs = ", ".join(f'"{c[1]}" {c[2]}' for c in live_cols)
        conn.execute(
            f"CREATE TABLE {quarantine} ({defs}, "
            "quarantined_at TEXT, quarantine_reason TEXT)")
        return
    have = set(_columns(conn, quarantine))
    for c in live_cols:
        if c[1] not in have:
            conn.execute(
                f'ALTER TABLE {quarantine} ADD COLUMN "{c[1]}" {c[2]}')


def find_fabricated(conn):
    """(prediction ids to quarantine, {signal: n} of non-HOLD rows on
    failed cycles that are reported and left alone)."""
    from finetune.dataset_builder import _is_failed_call
    tables = _tables(conn)
    if "ai_predictions" not in tables or "ai_cycles" not in tables:
        return [], {}
    failed = [cid for cid, raw in conn.execute(
        "SELECT cycle_id, raw_response_json FROM ai_cycles "
        "WHERE raw_response_json LIKE '%AI call failed%' "
        "   OR raw_response_json LIKE '%Cost cap reached%'")
        if _is_failed_call(raw)]
    ids, left = [], {}
    for i in range(0, len(failed), 500):
        chunk = failed[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for pid, sig in conn.execute(
                "SELECT id, UPPER(COALESCE(predicted_signal, '')) "
                f"FROM ai_predictions WHERE cycle_id IN ({marks})", chunk):
            if sig == "HOLD":
                ids.append(pid)
            else:
                left[sig] = left.get(sig, 0) + 1
    return ids, left


def _move(conn, src, dst, col, ids, extra_cols=(), extra_vals=()):
    """Move rows whose `col` is in `ids` from src to dst; returns the
    number moved. Column lists are explicit — never SELECT *."""
    if src not in _tables(conn) or not ids:
        return 0
    cols = [c for c in _columns(conn, src) if c in set(_columns(conn, dst))]
    quoted = ", ".join(f'"{c}"' for c in cols)
    moved = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        extra_c = "".join(f', "{c}"' for c in extra_cols)
        extra_q = "".join(", ?" for _ in extra_cols)
        cur = conn.execute(
            f"INSERT INTO {dst} ({quoted}{extra_c}) "
            f"SELECT {quoted}{extra_q} FROM {src} WHERE {col} IN ({marks})",
            [*extra_vals, *chunk])
        moved += cur.rowcount
        conn.execute(f"DELETE FROM {src} WHERE {col} IN ({marks})", chunk)
    return moved


def quarantine(db, apply):
    conn = sqlite3.connect(db)
    try:
        ids, left = find_fabricated(conn)
        counts = {"predictions": len(ids), "left_alone": left}
        for live, _q, col in DEPENDENTS:
            n = 0
            if live in _tables(conn):
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    n += conn.execute(
                        f"SELECT COUNT(*) FROM {live} WHERE {col} IN "
                        f"({marks})", chunk).fetchone()[0]
            counts[live] = n
        if not apply or not ids:
            return counts
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with conn:                       # one transaction per journal
            for live, q, col in DEPENDENTS:
                if live in _tables(conn):
                    _ensure_quarantine(conn, live, q)
                    moved = _move(conn, live, q, col, ids,
                                  ("quarantined_at", "quarantine_reason"),
                                  (stamp, REASON))
                    assert moved == counts[live], (live, moved, counts[live])
            _ensure_quarantine(conn, MAIN[0], MAIN[1])
            moved = _move(conn, MAIN[0], MAIN[1], MAIN[2], ids,
                          ("quarantined_at", "quarantine_reason"),
                          (stamp, REASON))
            assert moved == len(ids), (moved, len(ids))
        return counts
    finally:
        conn.close()


def restore(db, apply):
    """Move everything in the quarantine tables back."""
    conn = sqlite3.connect(db)
    try:
        counts = {}
        for live, q, col in (MAIN,) + DEPENDENTS:
            if q not in _tables(conn):
                counts[live] = 0
                continue
            ids = [r[0] for r in conn.execute(f"SELECT {col} FROM {q}")]
            counts[live] = len(ids)
        if not apply:
            return counts
        with conn:
            for live, q, col in (MAIN,) + DEPENDENTS:
                if q in _tables(conn):
                    ids = sorted({r[0] for r in conn.execute(
                        f"SELECT {col} FROM {q}")})
                    _move(conn, q, live, col, ids)
        return counts
    finally:
        conn.close()


def _journals():
    from models import get_active_profile_ids
    return [(pid, f"quantopsai_profile_{pid}.db")
            for pid in get_active_profile_ids()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write (default: dry run, reads only)")
    ap.add_argument("--restore", action="store_true",
                    help="move quarantined rows back")
    ap.add_argument("--db", action="append",
                    help="a journal path (repeatable); default: every "
                         "active profile's journal")
    args = ap.parse_args(argv)
    journals = ([(os.path.basename(p), p) for p in args.db] if args.db
                else _journals())
    mode = ("RESTORE" if args.restore else "QUARANTINE") + (
        "" if args.apply else " (dry run — nothing written)")
    print(mode)
    total = {}
    for name, db in journals:
        if not os.path.exists(db):
            print(f"  {name}: journal not found at {db} — SKIPPED")
            continue
        counts = (restore if args.restore else quarantine)(db, args.apply)
        print(f"  {name}: {counts}")
        for k, v in counts.items():
            if isinstance(v, int):
                total[k] = total.get(k, 0) + v
    print("TOTAL:", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
