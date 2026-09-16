"""The zombie guardrail survives full-fresh-start resets: archiving a
profile DB records which strategies had fired into it, and the zombie
test consults that record.

2026-09-16 — 14 days after the 08-24 reset the zombie guardrail fired
on 12 strategies. Six were FALSE alarms: rare-trigger strategies with
real archived firings whose lifetime evidence the reset had wiped
(the test only summed DBs on disk). Six were genuine zombies — zero
firings across the entire 4-month archive — now quarantined in
`test_no_strategy_zombies._KNOWN_ZOMBIES_2026_09_16` pending the
audit (OPEN_ITEMS "INCIDENT FOLLOW-UP 2026-09-16").

Pins the archive-awareness machinery:
  - `update_strategy_index` merges a DB's DISTINCT strategy_type
    values into the sidecar index; merge-only (a reset can only ADD
    evidence, never erase it).
  - `archive_predictions` maintains the index as part of archiving —
    the same operation that precedes the DB wipe.
  - `archived_strategy_names` is fail-closed: missing/corrupt index
    reads as empty (more alarms, never fewer).
  - The quarantine set only shrinks (self-checked inside
    test_no_strategy_zombies at run time; shape pinned here).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from predictions_archive import (  # noqa: E402
    archive_predictions, archived_strategy_names, update_strategy_index,
)


def _mk_profile_db(path, strategy_types):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ai_predictions (id INTEGER PRIMARY KEY, "
        "symbol TEXT, strategy_type TEXT, timestamp TEXT)")
    conn.executemany(
        "INSERT INTO ai_predictions (symbol, strategy_type, timestamp) "
        "VALUES ('SYM', ?, '2026-09-01 12:00:00')",
        [(s,) for s in strategy_types])
    # Tables archive_predictions dumps; empty is fine.
    for t in ("ai_cycles", "specialist_outcomes",
              "option_proposal_outcomes", "ai_shadow_calls"):
        conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
    conn.commit(); conn.close()
    return str(path)


class TestStrategyIndex:
    def test_update_merges_distinct_names(self, tmp_path):
        db = _mk_profile_db(tmp_path / "p1.db",
                            ["alpha", "beta", "alpha", "", None])
        root = str(tmp_path / "arch")
        got = update_strategy_index(db, root)
        assert got == ["alpha", "beta"]
        assert archived_strategy_names(root) == ["alpha", "beta"]

    def test_merge_only_across_resets(self, tmp_path):
        root = str(tmp_path / "arch")
        db1 = _mk_profile_db(tmp_path / "p1.db", ["alpha"])
        db2 = _mk_profile_db(tmp_path / "p2.db", ["gamma"])
        update_strategy_index(db1, root)
        update_strategy_index(db2, root)
        # alpha survives even though db2 never fired it — a later
        # reset can only ADD evidence, never erase it.
        assert archived_strategy_names(root) == ["alpha", "gamma"]

    def test_archive_predictions_maintains_the_index(self, tmp_path):
        db = _mk_profile_db(tmp_path / "p3.db", ["delta"])
        root = tmp_path / "arch"
        archive_predictions(db_path=db, profile_id=3,
                            archive_root=str(root),
                            reset_timestamp="20260916_000000")
        assert "delta" in archived_strategy_names(str(root))

    def test_missing_index_reads_empty_fail_closed(self, tmp_path):
        assert archived_strategy_names(str(tmp_path / "nowhere")) == []

    def test_corrupt_index_reads_empty_fail_closed(self, tmp_path):
        root = tmp_path / "arch"
        root.mkdir()
        (root / "strategy_index.json").write_text("{not json")
        assert archived_strategy_names(str(root)) == []
        (root / "strategy_index.json").write_text(
            json.dumps({"strategies": "not-a-list"}))
        assert archived_strategy_names(str(root)) == []

    def test_unreadable_db_never_raises(self, tmp_path):
        got = update_strategy_index(str(tmp_path / "missing.db"),
                                    str(tmp_path / "arch"))
        assert got == []


class TestQuarantineShape:
    def test_quarantine_is_dated_and_frozen(self):
        from tests import test_no_strategy_zombies as tz
        q = tz._KNOWN_ZOMBIES_2026_09_16
        assert isinstance(q, frozenset)
        # The audit shrinks this set; it must never grow past the
        # incident's own six. A NEW zombie gets a NEW dated set and a
        # NEW OPEN_ITEMS entry — never a quiet append here.
        assert q <= {
            "short_squeeze_setup", "news_sentiment_spike",
            "volume_dryup_breakout", "parabolic_exhaustion",
            "catalyst_filing_short", "iv_regime_short",
        }
