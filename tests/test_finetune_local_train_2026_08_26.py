"""2026-08-26 — local LoRA training driver (docs/20 §16.1, docs/25
step 4.5): the pure parts, pinned.

The mlx invocation shape (a wrong flag silently truncates 9-10K-token
production prompts to mlx's 2048 default), the completion parser (an
unparseable answer scores as WRONG — live it would be one too), and
the eval scorer.
"""
from __future__ import annotations

import pytest

from finetune.local_train import (
    build_train_command, direction_bucket, parse_decision,
    score_examples,
)


class TestTrainCommand:
    def test_shape_and_max_seq_length(self):
        cmd = build_train_command(
            "python", "Qwen/Qwen2.5-7B-Instruct", "/d", "/a",
            iters=600, batch_size=1, num_layers=16,
            resume_adapter=None)
        assert cmd[:4] == ["python", "-m", "mlx_lm", "lora"]
        assert "--train" in cmd
        i = cmd.index("--max-seq-length")
        assert cmd[i + 1] == "8192", (
            "without an explicit max-seq-length, mlx truncates the "
            "production prompts to 2048 tokens and the model never "
            "sees the candidate table")
        assert "--grad-checkpoint" in cmd, (
            "without gradient checkpointing, 7B LoRA at 8K context "
            "OOMs Metal on the 64GB M2 Max")
        assert "--resume-adapter-file" not in cmd

    def test_resume_points_at_adapter_file(self):
        cmd = build_train_command(
            "python", "m", "/d", "/a", iters=1, batch_size=1,
            num_layers=8, resume_adapter="/adapters/prev")
        i = cmd.index("--resume-adapter-file")
        assert cmd[i + 1] == "/adapters/prev/adapters.safetensors"


class TestParseDecision:
    def test_production_trade_dict(self):
        text = '{"trades":[{"symbol":"AAPL","action":"BUY","size_pct":5}]}'
        assert parse_decision(text) == "BUY"

    def test_json_with_surrounding_prose(self):
        text = ('Sure — here is my decision:\n'
                '{"trades": [{"symbol": "TGT", "action": "SHORT"}]}\n'
                'done.')
        assert parse_decision(text) == "SHORT"

    def test_bare_token_fallback(self):
        assert parse_decision("I would HOLD here.") == "HOLD"

    def test_garbage_is_none(self):
        assert parse_decision("") is None
        assert parse_decision("no decision at all 42") is None

    def test_symbol_selects_its_own_trade_never_first(self):
        """2026-08-27 scorer fix: production answers are BATCHES;
        grading trades[0] scored the base model on arbitrary other
        candidates."""
        text = ('{"trades":[{"symbol":"NVDA","action":"BUY"},'
                '{"symbol":"TGT","action":"SHORT"}]}')
        assert parse_decision(text, symbol="TGT") == "SHORT"
        assert parse_decision(text, symbol="nvda") == "BUY"

    def test_symbol_omitted_from_batch_is_hold(self):
        """Production semantics: non-actionable names are OMITTED from
        the trades list — absence IS the HOLD signal. The first eval
        scored 0/15 on every HOLD by treating omission as a miss."""
        text = '{"trades":[{"symbol":"NVDA","action":"BUY"}]}'
        assert parse_decision(text, symbol="TGT") == "HOLD"
        assert parse_decision('{"trades": []}', symbol="TGT") == "HOLD"

    def test_empty_trades_without_symbol_is_hold(self):
        assert parse_decision('{"trades": []}') == "HOLD"


class TestScoring:
    def test_buckets(self):
        assert direction_bucket("STRONG_BUY") == "bullish"
        assert direction_bucket("SHORT") == "bearish"
        assert direction_bucket("HOLD") == "hold"
        assert direction_bucket(None) == "unparseable"

    def test_accuracy_and_unparseable_count(self):
        labels = ["BUY", "HOLD", "SHORT", "BUY"]
        answers = ["WEAK_BUY", "HOLD", "BUY", None]
        r = score_examples(labels, answers)
        assert r["n"] == 4
        assert r["accuracy"] == pytest.approx(0.5)  # bullish+hold hit
        assert r["unparseable"] == 1
        assert r["by_label"]["bullish"] == {"n": 2, "hit": 1}

    def test_empty_set(self):
        r = score_examples([], [])
        assert r["n"] == 0 and r["accuracy"] is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
