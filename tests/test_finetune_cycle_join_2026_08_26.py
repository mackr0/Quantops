"""2026-08-26 — dataset builder joins predictions to their CYCLE's
prompt/raw response.

Since 2026-07-02 the prompt and raw response live once per cycle on
`ai_cycles` (6.15x dedup) and the prediction row carries only
cycle_id. The builder was written against the pre-07-02 per-row shape
and silently rejected 100% of post-move rows as "stub" — the
zero-example corpus found when docs/25 step 4.5 was activated (46,583
archived resolved predictions, all prompt-less at row level, all
100.0% joinable through their dump's cycles.jsonl). These tests pin
the join on both ingest paths and the row-level-wins precedence.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from finetune.dataset_builder import build_dataset

_PROMPT = (
    "You are the apex portfolio-manager AI. PORTFOLIO STATE: cash "
    "$100,000; positions none. CANDIDATES: AAPL momentum 3.2 rsi 41 "
    "news-sent +0.4 … decide each candidate's action as JSON."
)
_RAW = json.dumps({"trades": [{"symbol": "AAPL", "action": "BUY",
                               "size_pct": 5, "confidence": 71}]})


def _pred_row(pid=1, cycle_id="cyc-1", prompt=None, raw=None):
    return {
        "id": pid, "symbol": "AAPL", "predicted_signal": "BUY",
        "status": "resolved", "actual_outcome": "win",
        "actual_return_pct": 6.0, "actual_return_pct_net": 5.6,
        "timestamp": "2026-08-01T14:00:00",
        "resolved_at": "2026-08-04T14:00:00",
        "cycle_id": cycle_id, "prompt_text": prompt,
        "raw_response_json": raw, "data_quality": None,
        "occ_symbol": None, "confidence": 71,
    }


def _archive(tmp_path, preds, cycles):
    d = tmp_path / "archive" / "210" / "exp_test"
    d.mkdir(parents=True)
    (d / "predictions.jsonl").write_text(
        "\n".join(json.dumps(p) for p in preds) + "\n")
    if cycles is not None:
        (d / "cycles.jsonl").write_text(
            "\n".join(json.dumps(c) for c in cycles) + "\n")
    return str(tmp_path / "archive")


class TestArchiveCycleJoin:
    def test_post_0702_rows_join_to_cycle_prompt(self, tmp_path):
        """THE incident shape: prompt-less prediction + cycles.jsonl
        carrying the prompt → a full training example."""
        root = _archive(
            tmp_path, [_pred_row()],
            [{"cycle_id": "cyc-1", "prompt_text": _PROMPT,
              "raw_response_json": _RAW}])
        out = tmp_path / "out"
        m = build_dataset([], str(out), archive_root=root,
                          eval_holdout=0)
        assert m["total_examples"] == 1
        line = json.loads(
            (out / "train.jsonl").read_text().splitlines()[0])
        joined = json.dumps(line["messages"])
        assert "PORTFOLIO STATE" in joined
        target = json.loads(line["messages"][-1]["content"])
        assert target["trades"][0]["action"] == "BUY"

    def test_missing_cycles_file_filters_not_crashes(self, tmp_path):
        root = _archive(tmp_path, [_pred_row()], cycles=None)
        m = build_dataset([], str(tmp_path / "out"), archive_root=root,
                          eval_holdout=0)
        assert m["total_examples"] == 0

    def test_row_level_prompt_wins_over_cycle(self, tmp_path):
        """Pre-07-02 rows carry their own prompt — enrichment must
        never overwrite it."""
        row_prompt = _PROMPT.replace("AAPL", "ROWLEVEL-AAPL")
        root = _archive(
            tmp_path, [_pred_row(prompt=row_prompt, raw=_RAW)],
            [{"cycle_id": "cyc-1",
              "prompt_text": _PROMPT.replace("AAPL", "CYCLE-AAPL"),
              "raw_response_json": _RAW}])
        out = tmp_path / "out"
        m = build_dataset([], str(out), archive_root=root,
                          eval_holdout=0)
        assert m["total_examples"] == 1
        line = (out / "train.jsonl").read_text()
        assert "ROWLEVEL-AAPL" in line
        assert "CYCLE-AAPL" not in line


class TestCycleGroupedExamples:
    """2026-08-27 — production-shape targets: one example per cycle,
    all labeled candidates in one answer, HOLDs by omission. Batch 1
    taught a one-pick convention with per-row targets; this pins the
    refinement that fixes it."""

    def test_three_candidates_one_example_holds_omitted(self, tmp_path):
        preds = [
            _pred_row(pid=1),  # BUY winner → labeled BUY
            dict(_pred_row(pid=2), symbol="TGT",
                 predicted_signal="SHORT", actual_outcome="win",
                 actual_return_pct=-6.0, actual_return_pct_net=-6.2),
            dict(_pred_row(pid=3), symbol="VLO",
                 predicted_signal="BUY", actual_outcome="loss",
                 actual_return_pct=-4.0, actual_return_pct_net=-4.2),
        ]
        root = _archive(
            tmp_path, preds,
            [{"cycle_id": "cyc-1", "prompt_text": _PROMPT,
              "raw_response_json": _RAW}])
        out = tmp_path / "out"
        m = build_dataset([], str(out), archive_root=root,
                          eval_holdout=0)
        assert m["total_examples"] == 1          # one CYCLE example
        assert m["labeled_rows"] == 3
        assert m["label_distribution"] == {
            "BUY": 1, "SHORT": 1, "HOLD": 1}
        line = json.loads(
            (out / "train.jsonl").read_text().splitlines()[0])
        target = json.loads(line["messages"][-1]["content"])
        by_sym = {t["symbol"]: t["action"] for t in target["trades"]}
        assert by_sym == {"AAPL": "BUY", "TGT": "SHORT"}, (
            "the losing BUY relabels to HOLD and must be OMITTED "
            "from the trades list (production semantics)")

    def test_eval_meta_carries_labels_map(self, tmp_path):
        preds = [_pred_row(pid=1),
                 dict(_pred_row(pid=2), symbol="TGT",
                      predicted_signal="SHORT", actual_outcome="win",
                      actual_return_pct=-6.0,
                      actual_return_pct_net=-6.2)]
        root = _archive(
            tmp_path, preds,
            [{"cycle_id": "cyc-1", "prompt_text": _PROMPT,
              "raw_response_json": _RAW}])
        out = tmp_path / "out"
        build_dataset([], str(out), archive_root=root, eval_holdout=1)
        meta = json.loads(
            (out / "eval_meta.jsonl").read_text().splitlines()[0])
        assert meta["labels"] == {"AAPL": "BUY", "TGT": "SHORT"}

    def test_same_cycle_id_different_prompts_never_merge(self, tmp_path):
        """Two profiles can mint the same cycle_id string; the prompt
        identity sub-key keeps their rows apart."""
        preds = [_pred_row(pid=1, prompt=_PROMPT + " variant-A",
                           raw=_RAW),
                 dict(_pred_row(pid=2, prompt=_PROMPT + " variant-B",
                                raw=_RAW), symbol="TGT",
                      predicted_signal="SHORT", actual_outcome="win",
                      actual_return_pct=-6.0,
                      actual_return_pct_net=-6.2)]
        root = _archive(tmp_path, preds, [])
        m = build_dataset([], str(tmp_path / "out"), archive_root=root,
                          eval_holdout=0)
        assert m["total_examples"] == 2


class TestLiveCycleJoin:
    def _db(self, tmp_path, with_cycles=True):
        path = str(tmp_path / "quantopsai_profile_5.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE ai_predictions ("
            " id INTEGER PRIMARY KEY, symbol TEXT,"
            " predicted_signal TEXT, status TEXT, actual_outcome TEXT,"
            " actual_return_pct REAL, actual_return_pct_net REAL,"
            " timestamp TEXT, resolved_at TEXT, cycle_id TEXT,"
            " prompt_text TEXT, raw_response_json TEXT,"
            " data_quality TEXT, occ_symbol TEXT, confidence REAL)")
        r = _pred_row()
        conn.execute(
            "INSERT INTO ai_predictions (id, symbol, predicted_signal,"
            " status, actual_outcome, actual_return_pct,"
            " actual_return_pct_net, timestamp, resolved_at, cycle_id,"
            " prompt_text, raw_response_json, data_quality, occ_symbol,"
            " confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r["symbol"], r["predicted_signal"], r["status"],
             r["actual_outcome"], r["actual_return_pct"],
             r["actual_return_pct_net"], r["timestamp"],
             r["resolved_at"], r["cycle_id"], None, None, None, None,
             r["confidence"]))
        if with_cycles:
            conn.execute(
                "CREATE TABLE ai_cycles (cycle_id TEXT PRIMARY KEY,"
                " profile_id INTEGER, prompt_text TEXT,"
                " raw_response_json TEXT)")
            conn.execute(
                "INSERT INTO ai_cycles (cycle_id, profile_id,"
                " prompt_text, raw_response_json) VALUES (?,?,?,?)",
                ("cyc-1", 5, _PROMPT, _RAW))
        conn.commit()
        conn.close()
        return path

    def test_live_rows_join_ai_cycles(self, tmp_path):
        db = self._db(tmp_path, with_cycles=True)
        out = tmp_path / "out"
        m = build_dataset([db], str(out), archive_root=None,
                          eval_holdout=0)
        assert m["total_examples"] == 1
        assert "PORTFOLIO STATE" in (out / "train.jsonl").read_text()

    def test_journal_without_ai_cycles_table_survives(self, tmp_path):
        """Pre-07-02 journal shape: no ai_cycles table — the fallback
        query runs and prompt-less rows are filtered, not crashed on."""
        db = self._db(tmp_path, with_cycles=False)
        m = build_dataset([db], str(tmp_path / "out"),
                          archive_root=None, eval_holdout=0)
        assert m["total_examples"] == 0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
