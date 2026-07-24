"""Backfill parsed_signal + agreement on historical successful shadow rows.

2026-07-24 — `shadow_eval._extract_signal` matched NONE of the house
schemas (ensemble verdict / transcript tone / batch_select trades), so
every successful shadow call ever made (543 fleet-wide) has
parsed_signal NULL and agreement NULL: the operator paid for an A/B
evaluation whose comparison never ran. The extractor is fixed; this
script re-derives both columns for the rows already paid for, from the
stored raw_response + primary_parsed (no API calls, pure local).

DRY-RUN by default; --apply to write. Only touches rows with
error IS NULL/'' AND agreement IS NULL. Idempotent.
"""
import argparse
import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from shadow_eval import (
        _try_parse_json, _extract_signal, _compute_agreement,
    )

    tot_scored = tot_signal = tot_seen = 0
    for db in sorted(glob.glob("quantopsai_profile_*.db")):
        conn = sqlite3.connect(db)
        try:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, raw_response, primary_parsed "
                    "FROM ai_shadow_calls "
                    "WHERE (error IS NULL OR error='') "
                    "  AND agreement IS NULL"
                ).fetchall()
            except sqlite3.OperationalError:
                continue   # no shadow table in this profile DB
            n_scored = n_signal = 0
            for r in rows:
                tot_seen += 1
                shadow_parsed = _try_parse_json(r["raw_response"] or "")
                try:
                    primary_parsed = (json.loads(r["primary_parsed"])
                                      if r["primary_parsed"] else None)
                except (json.JSONDecodeError, TypeError, ValueError):
                    primary_parsed = None
                sig = _extract_signal(shadow_parsed)
                agr = _compute_agreement(primary_parsed, shadow_parsed)
                if sig is not None:
                    n_signal += 1
                if agr is not None:
                    n_scored += 1
                if args.apply and (sig is not None or agr is not None):
                    conn.execute(
                        "UPDATE ai_shadow_calls "
                        "SET parsed_signal = COALESCE(?, parsed_signal), "
                        "    agreement = ? WHERE id = ?",
                        (sig, agr, r["id"]),
                    )
            if args.apply:
                conn.commit()
            if rows:
                print(f"{db}: {len(rows)} unscored rows -> "
                      f"signal {n_signal}, agreement {n_scored}"
                      f"{' (APPLIED)' if args.apply else ''}")
            tot_scored += n_scored
            tot_signal += n_signal
        finally:
            conn.close()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"{mode}: {tot_seen} rows seen, {tot_signal} gain a "
          f"parsed_signal, {tot_scored} gain an agreement verdict")
    return 0


if __name__ == "__main__":
    sys.exit(main())
