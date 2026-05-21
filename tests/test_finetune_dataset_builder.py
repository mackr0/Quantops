"""Fine-tune dataset builder + model registry (Phase 4B1, 2026-05-21).

The dataset builder turns the system's own resolved predictions into
a hindsight-relabeled OpenAI training corpus. These tests pin the
high-stakes correctness properties:

  1. hindsight_label — every relabel case (win/loss directional,
     HOLD flat / missed-up / missed-down, gray-zone skip, option
     skip, allow_short gating).
  2. LOOK-AHEAD-BIAS GUARD — the load-bearing invariant: a label
     whose outcome resolved at/before the decision time is rejected.
     docs/20 §11 flags this as the one Critical-impact risk.
  3. _is_training_quality — the filter (resolved, prompt present,
     parseable response, canonical outcome, not tainted, not option).
  4. build_example — full transform + HOLD strips sizing.
  5. build_dataset — end-to-end pooling, dedup, split, JSONL shape.
  6. model_registry — lifecycle CRUD + latest_live_model.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# 1. hindsight_label
# ---------------------------------------------------------------------------

class TestHindsightLabel:
    def test_winning_buy_keeps_buy(self):
        from finetune.dataset_builder import hindsight_label
        assert hindsight_label("BUY", "win", 8.0) == "BUY"
        assert hindsight_label("STRONG_BUY", "win", 6.0) == "BUY"

    def test_winning_short_keeps_short(self):
        from finetune.dataset_builder import hindsight_label
        assert hindsight_label("SHORT", "win", -7.0) == "SHORT"

    def test_losing_directional_inverts_to_hold(self):
        from finetune.dataset_builder import hindsight_label
        assert hindsight_label("BUY", "loss", -6.0) == "HOLD"
        assert hindsight_label("SHORT", "loss", 6.0) == "HOLD"

    def test_hold_on_flat_stays_hold(self):
        from finetune.dataset_builder import hindsight_label
        assert hindsight_label("HOLD", "win", 0.5) == "HOLD"
        assert hindsight_label("HOLD", "neutral", -1.9) == "HOLD"

    def test_hold_missed_upside_becomes_buy(self):
        from finetune.dataset_builder import hindsight_label
        assert hindsight_label("HOLD", "loss", 7.0) == "BUY"

    def test_hold_missed_downside_becomes_short_when_allowed(self):
        from finetune.dataset_builder import hindsight_label
        assert hindsight_label("HOLD", "loss", -8.0, allow_short=True) == "SHORT"

    def test_hold_missed_downside_stays_hold_when_short_disallowed(self):
        from finetune.dataset_builder import hindsight_label
        assert hindsight_label("HOLD", "loss", -8.0, allow_short=False) == "HOLD"

    def test_gray_zone_skipped(self):
        from finetune.dataset_builder import hindsight_label
        # HOLD with a 2-5% move is ambiguous → skip
        assert hindsight_label("HOLD", "loss", 3.0) is None
        assert hindsight_label("HOLD", "loss", -4.0) is None

    def test_option_actions_skipped(self):
        from finetune.dataset_builder import hindsight_label
        for sig in ("MULTILEG_OPEN", "OPTIONS", "OPTION_EXERCISE",
                    "PAIR_TRADE"):
            assert hindsight_label(sig, "win", 10.0) is None

    def test_unparseable_return_skipped(self):
        from finetune.dataset_builder import hindsight_label
        assert hindsight_label("BUY", "win", None) is None
        assert hindsight_label("BUY", "win", "n/a") is None


# ---------------------------------------------------------------------------
# 2. LOOK-AHEAD-BIAS GUARD — the load-bearing invariant
# ---------------------------------------------------------------------------

class TestLookAheadGuard:
    def test_resolved_after_decision_passes(self):
        from finetune.dataset_builder import assert_no_lookahead
        assert_no_lookahead({
            "id": 1,
            "timestamp": "2026-05-19T10:00:00",
            "resolved_at": "2026-05-21T10:00:00",
        })  # no raise

    def test_resolved_before_decision_raises(self):
        from finetune.dataset_builder import assert_no_lookahead
        with pytest.raises(AssertionError, match="LOOK-AHEAD BIAS"):
            assert_no_lookahead({
                "id": 2,
                "timestamp": "2026-05-21T10:00:00",
                "resolved_at": "2026-05-19T10:00:00",  # BEFORE decision
            })

    def test_resolved_equal_decision_raises(self):
        from finetune.dataset_builder import assert_no_lookahead
        with pytest.raises(AssertionError, match="LOOK-AHEAD BIAS"):
            assert_no_lookahead({
                "id": 3,
                "timestamp": "2026-05-21T10:00:00",
                "resolved_at": "2026-05-21T10:00:00",  # SAME instant
            })

    def test_missing_resolved_at_raises(self):
        from finetune.dataset_builder import assert_no_lookahead
        with pytest.raises(AssertionError, match="no parseable resolved_at"):
            assert_no_lookahead({
                "id": 4,
                "timestamp": "2026-05-21T10:00:00",
                "resolved_at": None,
            })

    def test_build_example_enforces_guard(self):
        """build_example must NOT emit a leaking row even if it's
        otherwise training-quality."""
        from finetune.dataset_builder import build_example
        leaking = _quality_row(
            timestamp="2026-05-21T10:00:00",
            resolved_at="2026-05-20T10:00:00",  # before
        )
        with pytest.raises(AssertionError, match="LOOK-AHEAD BIAS"):
            build_example(leaking)


# ---------------------------------------------------------------------------
# Shared row factory
# ---------------------------------------------------------------------------

def _quality_row(**overrides):
    """A row that passes _is_training_quality by default."""
    row = {
        "id": 100,
        "status": "resolved",
        "symbol": "AAPL",
        "predicted_signal": "BUY",
        "actual_outcome": "win",
        "actual_return_pct": 8.0,
        "actual_return_pct_net": 7.6,
        "data_quality": None,
        "occ_symbol": None,
        "timestamp": "2026-05-19T10:00:00",
        "resolved_at": "2026-05-21T10:00:00",
        "prompt_text": "You are a portfolio manager.\n" + ("x" * 200)
                       + "\nPORTFOLIO STATE:\n  Equity: $100,000\n"
                       "CANDIDATES:\n  AAPL ...",
        "raw_response_json": json.dumps({"trades": [
            {"symbol": "AAPL", "action": "BUY", "size_pct": 5.0,
             "confidence": 75, "stop_loss_pct": 5.0,
             "take_profit_pct": 10.0, "reasoning": "strong"},
        ]}),
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 3. _is_training_quality
# ---------------------------------------------------------------------------

class TestTrainingQualityFilter:
    def test_quality_row_passes(self):
        from finetune.dataset_builder import _is_training_quality
        assert _is_training_quality(_quality_row()) is True

    def test_unresolved_rejected(self):
        from finetune.dataset_builder import _is_training_quality
        assert not _is_training_quality(_quality_row(status="pending"))

    def test_short_prompt_rejected(self):
        from finetune.dataset_builder import _is_training_quality
        assert not _is_training_quality(_quality_row(prompt_text="hi"))

    def test_missing_response_rejected(self):
        from finetune.dataset_builder import _is_training_quality
        assert not _is_training_quality(_quality_row(raw_response_json=None))

    def test_tainted_rejected(self):
        from finetune.dataset_builder import _is_training_quality
        assert not _is_training_quality(
            _quality_row(data_quality="tainted_equity_2026_05_21"))

    def test_option_row_rejected(self):
        from finetune.dataset_builder import _is_training_quality
        assert not _is_training_quality(
            _quality_row(predicted_signal="MULTILEG_OPEN"))
        assert not _is_training_quality(
            _quality_row(occ_symbol="AAPL260101C00200000"))

    def test_noncanonical_outcome_rejected(self):
        from finetune.dataset_builder import _is_training_quality
        assert not _is_training_quality(_quality_row(actual_outcome="pending"))


# ---------------------------------------------------------------------------
# 4. build_example
# ---------------------------------------------------------------------------

class TestBuildExample:
    def test_winning_buy_example_shape(self):
        from finetune.dataset_builder import build_example
        ex = build_example(_quality_row())
        assert ex is not None
        roles = [m["role"] for m in ex["messages"]]
        assert roles == ["system", "user", "assistant"]
        # System prefix split off the PORTFOLIO STATE delimiter
        assert "portfolio manager" in ex["messages"][0]["content"].lower()
        assert "PORTFOLIO STATE:" in ex["messages"][1]["content"]
        assistant = json.loads(ex["messages"][2]["content"])
        assert assistant["trades"][0]["action"] == "BUY"
        assert assistant["trades"][0]["symbol"] == "AAPL"

    def test_losing_buy_relabels_to_hold_and_strips_sizing(self):
        from finetune.dataset_builder import build_example
        ex = build_example(_quality_row(
            actual_outcome="loss", actual_return_pct=-6.0,
            actual_return_pct_net=-6.4))
        assistant = json.loads(ex["messages"][2]["content"])
        trade = assistant["trades"][0]
        assert trade["action"] == "HOLD"
        assert trade.get("size_pct") == 0
        assert "stop_loss_pct" not in trade
        assert "take_profit_pct" not in trade

    def test_gray_zone_returns_none(self):
        from finetune.dataset_builder import build_example
        assert build_example(_quality_row(
            predicted_signal="HOLD", actual_outcome="loss",
            actual_return_pct=3.0, actual_return_pct_net=2.8)) is None

    def test_prefers_net_return_for_label(self):
        """Gross is +6% (would be a missed-upside BUY) but net is
        +1.5% (flat after costs) → label should be HOLD on a HOLD row.
        Confirms the builder uses return_pct_net when present."""
        from finetune.dataset_builder import build_example
        ex = build_example(_quality_row(
            predicted_signal="HOLD", actual_outcome="win",
            actual_return_pct=6.0, actual_return_pct_net=1.5))
        # net 1.5% < gray-zone floor → HOLD (not BUY)
        assistant = json.loads(ex["messages"][2]["content"])
        assert assistant["trades"][0]["action"] == "HOLD"


# ---------------------------------------------------------------------------
# 5. build_dataset end-to-end
# ---------------------------------------------------------------------------

class TestBuildDataset:
    def _make_profile_db(self, path, rows):
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY, status TEXT, symbol TEXT,
                predicted_signal TEXT, actual_outcome TEXT,
                actual_return_pct REAL, actual_return_pct_net REAL,
                data_quality TEXT, occ_symbol TEXT, timestamp TEXT,
                resolved_at TEXT, prompt_text TEXT, raw_response_json TEXT
            )
        """)
        for r in rows:
            cols = ",".join(r.keys())
            qs = ",".join("?" * len(r))
            conn.execute(f"INSERT INTO ai_predictions ({cols}) VALUES ({qs})",
                         tuple(r.values()))
        conn.commit()
        conn.close()

    def test_builds_split_and_jsonl(self, tmp_path):
        from finetune.dataset_builder import build_dataset
        db = str(tmp_path / "p.db")
        rows = []
        for i in range(30):
            rows.append(_quality_row(
                id=i, symbol=f"SYM{i}",
                timestamp=f"2026-05-{(i % 28) + 1:02d}T10:00:00",
                resolved_at=f"2026-06-{(i % 28) + 1:02d}T10:00:00"))
        self._make_profile_db(db, rows)
        out = str(tmp_path / "corpus")
        manifest = build_dataset([db], out, archive_root=None,
                                  eval_holdout=5, val_fraction=0.2)
        assert manifest["total_examples"] == 30
        assert manifest["eval"] == 5
        # Remaining 25 split 20/80 → val 5, train 20
        assert manifest["val"] == 5
        assert manifest["train"] == 20
        # JSONL files exist and every line is valid OpenAI shape
        for name in ("train", "val", "eval"):
            with open(manifest["paths"][name]) as fh:
                for line in fh:
                    obj = json.loads(line)
                    assert "messages" in obj
                    assert "_meta" not in obj  # meta stripped from vendor file
                    assert [m["role"] for m in obj["messages"]] == \
                        ["system", "user", "assistant"]

    def test_dedup_across_live_and_archive(self, tmp_path):
        """A prediction id present in BOTH a live DB and the archive
        is counted once."""
        from finetune.dataset_builder import build_dataset
        db = str(tmp_path / "p.db")
        self._make_profile_db(db, [_quality_row(id=1, symbol="AAPL")])
        # Archive with the SAME id=1
        arch = tmp_path / "archive" / "16" / "20260519_000000"
        arch.mkdir(parents=True)
        with open(arch / "predictions.jsonl", "w") as fh:
            fh.write(json.dumps(_quality_row(id=1, symbol="AAPL")) + "\n")
        out = str(tmp_path / "corpus")
        manifest = build_dataset([db], out,
                                  archive_root=str(tmp_path / "archive"),
                                  eval_holdout=0, val_fraction=0.0)
        assert manifest["total_examples"] == 1


# ---------------------------------------------------------------------------
# 6. model_registry
# ---------------------------------------------------------------------------

class TestModelRegistry:
    def test_register_and_latest_live(self, tmp_path):
        from finetune import model_registry as mr
        db = str(tmp_path / "master.db")
        mr.register_model("ft:m-w1", training_window_start="2026-05-12",
                          db_path=db)
        mr.register_model("ft:m-w2", parent_model_id="ft:m-w1",
                          db_path=db)
        # Nothing promoted yet
        assert mr.latest_live_model(db_path=db) is None
        mr.promote_to_shadow("ft:m-w2", db_path=db)
        assert mr.latest_live_model(db_path=db) is None  # shadow != live
        mr.promote_to_live("ft:m-w2", db_path=db)
        assert mr.latest_live_model(db_path=db) == "ft:m-w2"

    def test_retire_falls_back(self, tmp_path):
        from finetune import model_registry as mr
        db = str(tmp_path / "master.db")
        mr.register_model("ft:m-live", db_path=db)
        mr.promote_to_live("ft:m-live", db_path=db)
        assert mr.latest_live_model(db_path=db) == "ft:m-live"
        mr.retire_model("ft:m-live", "win-rate regressed", db_path=db)
        assert mr.latest_live_model(db_path=db) is None

    def test_register_idempotent(self, tmp_path):
        from finetune import model_registry as mr
        db = str(tmp_path / "master.db")
        id1 = mr.register_model("ft:dup", db_path=db)
        id2 = mr.register_model("ft:dup", db_path=db)
        assert id1 == id2
        assert len(mr.list_models(db_path=db)) == 1
