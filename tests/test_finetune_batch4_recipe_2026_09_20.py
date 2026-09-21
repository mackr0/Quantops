"""2026-09-20 — the batch-4 fine-tune recipe (docs/28).

Reading the batch 1-3 artifacts on the Mac found that no batch had been
trained mainly on its answers: prompt masking was never on (the answer
was 0.4-0.7% of the tokens the loss covered) and 31-40% of training
examples were longer than the 8,192-token window, so the trainer cut
the END of them off — the answer. These tests pin the recipe that
makes batch 4 the first run that actually tests whether this system's
data improves the model:

  - nothing longer than the window is ever written (split along the
    candidate table, or dropped and counted — never truncated);
  - internal bookkeeping keys never reach a target;
  - label origin is carried in metadata and never written to a
    training file;
  - the TRAIN split alone is rebalanced, deterministically, by
    dropping whole examples — never by editing a target;
  - training masks the prompt, decays the learning rate, and REFUSES
    to start on a corpus the trainer would damage;
  - the exam sweeps checkpoints against one set of base answers, and
    reports guessing baselines, a paired test and an explicit bar.
"""
from __future__ import annotations

import json
import os

import pytest

from finetune import dataset_builder as db
from finetune import local_train as lt


# ---------------------------------------------------------------------------
# A prompt in the production grammar (ai_analyst._build_batch_prompt)
# ---------------------------------------------------------------------------

_PREAMBLE = ("You are a portfolio manager for an automated equity "
             "trading system. You see a batch of candidates.\n\n")


def _candidate(n, sym, filler=40):
    body = "\n".join(f"     Detail line {i} for {sym}: " + "x" * 60
                     for i in range(filler))
    return (f"  {n}. {sym} @ $100.00 | BUY (score 2/4)\n"
            f"     Votes: momentum=BUY\n{body}\n\n"
            f"SIMILAR PAST CASES (your own resolved trades) FOR {sym}:\n"
            f"  1. [2026-07-23] BUY {sym} in bull → WIN (+3.1% in 5d)\n"
            f"DETERMINISTIC RULE PANEL FOR {sym} (2 rule(s) fired):\n"
            f"  [CAUTION] low_conviction_score: only 1 screen agrees.")


def _prompt(longs, shorts=(), *, filler=40, pairs=True, watch=()):
    parts = []
    n = 0
    if longs:
        blocks = []
        for s in longs:
            n += 1
            blocks.append(_candidate(n, s, filler))
        parts.append("LONG CANDIDATES (ranked by technical score):\n"
                     + "\n".join(blocks))
    if shorts:
        blocks = []
        for s in shorts:
            n += 1
            blocks.append(_candidate(n, s, filler))
        parts.append("SHORT CANDIDATES (ranked by technical score):\n"
                     + "\n".join(blocks))
    else:
        parts.append("SHORT CANDIDATES: (none triggered this scan)")
    if watch:
        blocks = []
        for s in watch:
            n += 1
            blocks.append(_candidate(n, s, filler))
        parts.append(
            "DISCRETIONARY WATCH — no deterministic rule fired on these; "
            "for YOUR judgment:\n" + "\n".join(blocks)
            + "\n  NOTE: 'NEUTRAL / score 0' here means NO RULE FIRED.")
    if pairs:
        parts.append("PAIR OPPORTUNITIES (same-sector long+short — "
                     "isolates relative strength):\n"
                     "  1. tech: LONG AAA (score 3) / SHORT ZZZ (score 2)")
    return (_PREAMBLE + "PORTFOLIO STATE:\n  cash $100,000\n\n"
            "MARKET CONTEXT:\n  regime bull\n\n"
            + "\n\n".join(parts)
            + "\n\nRULES:\n- Propose every candidate where conviction is "
              "high.\n\nRespond ONLY with valid JSON.")


def _row(pid, sym, signal, outcome, ret, *, cycle="cyc-1", prompt=None,
         ts="2026-08-01T14:00:00", raw=None, resolved=None):
    # By default a label resolves 30 minutes after its decision, so the
    # time-ordered split's purge keeps it; pass `resolved` to model a
    # label that only became known later.
    resolved = resolved or (ts[:14] + "30" + ts[16:])
    return {
        "id": pid, "symbol": sym, "predicted_signal": signal,
        "status": "resolved", "actual_outcome": outcome,
        "actual_return_pct": ret, "actual_return_pct_net": ret,
        "timestamp": ts, "resolved_at": resolved,
        "cycle_id": cycle, "prompt_text": prompt,
        "raw_response_json": raw or json.dumps({"trades": []}),
        "data_quality": None, "occ_symbol": None,
    }


def _example(rows, prompt):
    group = []
    for r in rows:
        r = dict(r, prompt_text=prompt)
        label, ret = db._label_and_return(r)
        group.append((r, label, ret))
    return db.build_cycle_example(group)


def _archive(tmp_path, preds, cycles):
    d = tmp_path / "archive" / "229" / "exp_test"
    d.mkdir(parents=True)
    (d / "predictions.jsonl").write_text(
        "\n".join(json.dumps(p) for p in preds) + "\n")
    (d / "cycles.jsonl").write_text(
        "\n".join(json.dumps(c) for c in cycles) + "\n")
    return str(tmp_path / "archive")


COUNTER = db.CharEstimateCounter()


# ---------------------------------------------------------------------------
# Label origin
# ---------------------------------------------------------------------------

class TestLabelOrigin:
    @pytest.mark.parametrize("signal,outcome,ret,label,origin", [
        ("BUY", "win", 6.0, "BUY", "kept_win"),
        ("SHORT", "win", -6.0, "SHORT", "kept_win"),
        ("BUY", "loss", -4.0, "HOLD", "lost_entry"),
        ("SHORT", "loss", 4.0, "HOLD", "lost_entry"),
        ("HOLD", "neutral", 0.5, "HOLD", "flat_hold"),
        ("HOLD", "neutral", 7.0, "BUY", "missed_move"),
        ("HOLD", "neutral", -7.0, "SHORT", "missed_move"),
    ])
    def test_each_origin_from_its_inputs(self, signal, outcome, ret,
                                         label, origin):
        got = db.hindsight_label(signal, outcome, ret)
        assert got == label
        assert db.label_origin(signal, got) == origin

    def test_missed_downside_without_shorting_is_a_correct_hold(self):
        got = db.hindsight_label("HOLD", "neutral", -7.0,
                                 allow_short=False)
        assert got == "HOLD"
        assert db.label_origin("HOLD", got) == "flat_hold"

    def test_option_rows_carry_an_origin(self):
        assert db.label_origin("MULTILEG_OPEN", "MULTILEG_OPEN") == "kept_win"
        assert db.label_origin("OPTIONS", "HOLD") == "lost_entry"

    def test_origin_is_in_meta_and_never_in_a_training_file(self, tmp_path):
        prompt = _prompt(["AAPL", "MSFT"])
        preds = [_row(1, "AAPL", "BUY", "win", 6.0),
                 _row(2, "MSFT", "BUY", "loss", -4.0)]
        root = _archive(tmp_path, preds, [
            {"cycle_id": "cyc-1", "prompt_text": prompt,
             "raw_response_json": json.dumps({"trades": []})}])
        out = tmp_path / "out"
        db.build_dataset([], str(out), archive_root=root, eval_holdout=1,
                         rebalance=False)
        meta = json.loads((out / "eval_meta.jsonl").read_text())
        assert meta["origins"] == {"AAPL": "kept_win", "MSFT": "lost_entry"}
        for name in ("train.jsonl", "val.jsonl", "eval.jsonl"):
            text = (out / name).read_text()
            for origin in db.ORIGINS:
                assert origin not in text
            for line in text.splitlines():
                assert set(json.loads(line)) == {"messages"}


# ---------------------------------------------------------------------------
# Target hygiene
# ---------------------------------------------------------------------------

class TestTargetHygiene:
    def test_internal_underscore_keys_never_reach_a_target(self):
        raw = json.dumps({"trades": [{
            "symbol": "IBM", "action": "BUY", "size_pct": 5.0,
            "confidence": 73, "reasoning": "insider cluster",
            "_ledger_rar": 0.8197, "_ledger_best_rar": 0.91,
            "_ledger_best_expr": "stock", "_ledger_is_override": False}]})
        row = _row(1, "IBM", "BUY", "win", 6.0, raw=raw)
        trade = db._corrected_trade_dict(row, "BUY")
        assert not [k for k in trade if k.startswith("_")]
        assert trade["size_pct"] == 5.0 and trade["confidence"] == 73


# ---------------------------------------------------------------------------
# Over-length examples
# ---------------------------------------------------------------------------

class TestOverLength:
    SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]

    def _long_example(self):
        prompt = _prompt(self.SYMS[:4], self.SYMS[4:])
        rows = [_row(1, "AAA", "BUY", "win", 6.0),
                _row(2, "BBB", "BUY", "loss", -4.0),
                _row(3, "DDD", "HOLD", "neutral", 7.5),
                _row(4, "EEE", "SHORT", "win", -6.0),
                _row(5, "FFF", "HOLD", "neutral", 0.3)]
        return _example(rows, prompt)

    def test_parser_round_trips_the_production_grammar(self):
        user = db._split_prompt(_prompt(["AAA", "BBB"], ["CCC"],
                                        watch=["DDD"]))[1]
        table = db._parse_candidate_table(user)
        assert table is not None
        assert db._render_candidate_table(table, None) == user
        assert [s for sec in table["sections"]
                for s, _b in sec.candidates] == ["AAA", "BBB", "CCC", "DDD"]

    def test_short_example_is_untouched(self):
        ex = self._long_example()
        out, outcome = db.fit_example(ex, COUNTER, 10 ** 6)
        assert outcome == "fits" and out == [ex]

    def test_long_prompt_splits_into_fitting_parts_that_partition_it(self):
        ex = self._long_example()
        full = COUNTER.messages(ex["messages"])
        window = int(full * 0.6)
        parts, outcome = db.fit_example(ex, COUNTER, window)
        assert outcome == "split" and len(parts) >= 2
        seen_labels, seen_trades = {}, []
        for p in parts:
            assert COUNTER.messages(p["messages"]) <= window
            user = p["messages"][1]["content"]
            # full shared context survives in every part
            assert "PORTFOLIO STATE:" in user and "MARKET CONTEXT:" in user
            assert "PAIR OPPORTUNITIES (" in user and "RULES:" in user
            shown = {s for s in self.SYMS if f". {s} @ " in user}
            trades = json.loads(p["messages"][2]["content"])["trades"]
            # the target names only candidates this part shows
            assert {t["symbol"] for t in trades} <= shown
            assert set(p["_meta"]["labels"]) <= shown
            seen_labels.update(p["_meta"]["labels"])
            seen_trades += [t["symbol"] for t in trades]
        # …and together the parts are exactly the original answer
        assert seen_labels == ex["_meta"]["labels"]
        original = json.loads(ex["messages"][2]["content"])["trades"]
        assert sorted(seen_trades) == sorted(t["symbol"] for t in original)

    def test_a_part_showing_no_labeled_candidate_is_not_emitted(self):
        """An empty target there would teach "no trade" about
        candidates whose right answer is unknown."""
        prompt = _prompt(self.SYMS[:4], self.SYMS[4:])
        ex = _example([_row(1, "AAA", "BUY", "win", 6.0)], prompt)
        window = int(COUNTER.messages(ex["messages"]) * 0.6)
        parts, outcome = db.fit_example(ex, COUNTER, window)
        assert outcome == "split" and len(parts) == 1
        assert set(parts[0]["_meta"]["labels"]) == {"AAA"}

    def test_unsplittable_prompt_is_dropped_never_truncated(self):
        # No candidate table the splitter recognises.
        ex = _example([_row(1, "AAA", "BUY", "win", 6.0)],
                      _PREAMBLE + "PORTFOLIO STATE:\n" + "y" * 40000)
        out, stats = db.fit_split([ex], COUNTER, 2000)
        assert out == []
        assert stats["dropped"] == 1 and stats["labels_lost_to_drops"] == 1

    def test_unknown_top_level_line_refuses_to_split(self):
        user = db._split_prompt(_prompt(["AAA", "BBB"]))[1].replace(
            "DETERMINISTIC RULE PANEL FOR AAA",
            "BRAND NEW BLOCK NOBODY TOLD THE SPLITTER ABOUT FOR AAA")
        assert db._parse_candidate_table(user) is None

    def test_owned_block_naming_another_symbol_refuses_to_split(self):
        user = db._split_prompt(_prompt(["AAA", "BBB"]))[1].replace(
            "DETERMINISTIC RULE PANEL FOR AAA",
            "DETERMINISTIC RULE PANEL FOR QQQ")
        assert db._parse_candidate_table(user) is None

    def test_one_candidate_bigger_than_the_window_is_dropped(self):
        ex = _example([_row(1, "AAA", "BUY", "win", 6.0)],
                      _prompt(["AAA"], filler=400))
        out, outcome = db.fit_example(ex, COUNTER, 3000)
        assert out == [] and outcome == "dropped"

    def test_nothing_over_the_limit_is_ever_written(self, tmp_path):
        prompt = _prompt(self.SYMS[:4], self.SYMS[4:])
        preds, cycles = [], []
        for c in range(6):
            cid = f"cyc-{c}"
            ts = f"2026-08-{c + 1:02d}T14:00:00"
            preds += [
                _row(c * 10 + 1, "AAA", "BUY", "win", 6.0, cycle=cid, ts=ts),
                _row(c * 10 + 2, "EEE", "SHORT", "win", -6.0, cycle=cid,
                     ts=ts),
                _row(c * 10 + 3, "FFF", "HOLD", "neutral", 0.2, cycle=cid,
                     ts=ts)]
            cycles.append({"cycle_id": cid, "prompt_text": prompt + f" #{c}",
                           "raw_response_json": json.dumps({"trades": []})})
        root = _archive(tmp_path, preds, cycles)
        window = int(COUNTER.messages(
            _example(preds[:3], prompt)["messages"]) * 0.6)
        out = tmp_path / "out"
        m = db.build_dataset([], str(out), archive_root=root,
                             eval_holdout=2, max_tokens=window,
                             rebalance=False,
                             # keep all six candidates so every example
                             # is over the window and must split
                             prune_unlabeled_candidates=False)
        assert m["length"]["max_tokens"] == window
        assert m["length"]["method"].startswith("estimate:")
        for split in ("train", "val", "eval"):
            st = m["length"][split]
            assert st["max_tokens_after"] <= window
            assert st["split"] == st["in"] and st["dropped"] == 0
        for name in ("train.jsonl", "val.jsonl", "eval.jsonl"):
            for line in (out / name).read_text().splitlines():
                assert COUNTER.messages(
                    json.loads(line)["messages"]) <= window
        # eval meta stays aligned with eval prompts, part for part
        assert (len((out / "eval.jsonl").read_text().splitlines())
                == len((out / "eval_meta.jsonl").read_text().splitlines()))

    def test_character_estimate_never_undercounts_the_calibration(self):
        """2.6 chars/token sits below the measured minimum (2.68), so
        a text at the measured-densest ratio is over-counted."""
        text = "x" * 2680                     # 1,000 real tokens at 2.68
        assert COUNTER.text(text) >= 1000


# ---------------------------------------------------------------------------
# Unlabeled candidates are not taught as HOLD
# ---------------------------------------------------------------------------

class TestPruneUnlabeled:
    def _ex(self):
        # Shown: AAA BBB (long) CCC (short) DDD (watch). Labeled: AAA
        # (BUY win) and CCC (flat HOLD). BBB and DDD have no known
        # answer.
        prompt = _prompt(["AAA", "BBB"], ["CCC"], watch=["DDD"], filler=3)
        return _example([_row(1, "AAA", "BUY", "win", 6.0),
                         _row(2, "CCC", "HOLD", "neutral", 0.3)], prompt)

    def test_unlabeled_blocks_leave_the_prompt_labeled_ones_stay(self):
        ex = self._ex()
        pruned, removed = db.prune_unlabeled(ex)
        user = pruned["messages"][1]["content"]
        assert removed == 2
        assert ". AAA @ " in user and ". CCC @ " in user
        assert ". BBB @ " not in user and ". DDD @ " not in user
        # nothing owned by a removed candidate is left behind
        assert "FOR BBB" not in user and "FOR DDD" not in user
        # a section with no candidate left is dropped with its footer
        assert "DISCRETIONARY WATCH" not in user
        assert "NOTE: 'NEUTRAL / score 0'" not in user
        # shared context and the answer are untouched
        for block in ("PORTFOLIO STATE:", "MARKET CONTEXT:",
                      "PAIR OPPORTUNITIES (", "RULES:"):
            assert block in user
        assert pruned["messages"][2] == ex["messages"][2]
        assert pruned["_meta"] == ex["_meta"]

    def test_fully_labeled_and_unparseable_examples_are_left_alone(self):
        prompt = _prompt(["AAA"], filler=3)
        full = _example([_row(1, "AAA", "BUY", "win", 6.0)], prompt)
        assert db.prune_unlabeled(full) == (full, 0)
        odd = _example([_row(1, "AAA", "BUY", "win", 6.0)],
                       _PREAMBLE + "PORTFOLIO STATE:\n" + "no table " * 40)
        assert db.prune_unlabeled(odd) == (odd, -1)

    def test_train_and_val_are_pruned_the_exam_keeps_whole_prompts(
            self, tmp_path):
        prompt = _prompt(["AAA", "BBB"], filler=3)
        preds, cycles = [], []
        for d in range(1, 13):
            cid = f"cyc-{d}"
            preds.append(_row(d, "AAA", "BUY", "win", 6.0, cycle=cid,
                              ts=f"2026-08-{d:02d}T14:00:00"))
            cycles.append({"cycle_id": cid, "prompt_text": prompt + f" #{d}",
                           "raw_response_json": json.dumps({"trades": []})})
        root = _archive(tmp_path, preds, cycles)
        out = tmp_path / "out"
        m = db.build_dataset([], str(out), archive_root=root,
                             eval_holdout=2, val_fraction=0.2,
                             rebalance=False)
        assert m["prune"]["train"] == {"examples_pruned": 8,
                                       "candidates_removed": 8,
                                       "unparseable_left_whole": 0}
        assert m["prune"]["val"]["examples_pruned"] == 2
        assert ". BBB @ " not in (out / "train.jsonl").read_text()
        assert ". BBB @ " not in (out / "val.jsonl").read_text()
        assert (out / "eval.jsonl").read_text().count(". BBB @ ") == 2
        off = db.build_dataset([], str(tmp_path / "off"), archive_root=root,
                               eval_holdout=2, val_fraction=0.2,
                               rebalance=False,
                               prune_unlabeled_candidates=False)
        assert off["prune"] == {}
        assert ". BBB @ " in (tmp_path / "off" / "train.jsonl").read_text()


# ---------------------------------------------------------------------------
# Train-split rebalance
# ---------------------------------------------------------------------------

def _mini(idx, labels_origins):
    """A cycle example with given {symbol: (label, origin)}."""
    trades = [{"symbol": s, "action": l}
              for s, (l, _o) in sorted(labels_origins.items())
              if l != "HOLD"]
    return {
        "messages": [{"role": "system", "content": "s"},
                     {"role": "user", "content": f"u{idx}"},
                     {"role": "assistant",
                      "content": json.dumps({"trades": trades},
                                            separators=(",", ":"))}],
        "_meta": {"cycle_id": f"c{idx}",
                  "labels": {s: l for s, (l, _o) in labels_origins.items()},
                  "origins": {s: o for s, (_l, o) in labels_origins.items()}},
    }


def _pool():
    pool, i = [], 0
    for _ in range(40):                      # BUY-only
        pool.append(_mini(i, {"A": ("BUY", "missed_move")})); i += 1
    for _ in range(80):                      # SHORT-only
        pool.append(_mini(i, {"B": ("SHORT", "missed_move")})); i += 1
    for _ in range(10):                      # both
        pool.append(_mini(i, {"A": ("BUY", "kept_win"),
                              "B": ("SHORT", "kept_win")})); i += 1
    for _ in range(10):                      # empty, has a losing entry
        pool.append(_mini(i, {"C": ("HOLD", "lost_entry"),
                              "D": ("HOLD", "flat_hold")})); i += 1
    for _ in range(50):                      # empty, flat only
        pool.append(_mini(i, {"D": ("HOLD", "flat_hold")})); i += 1
    return pool


class TestRebalance:
    def test_empty_target_cap_is_honoured(self):
        out = db.rebalance_train(_pool(), max_empty_fraction=0.10)
        stats = db.split_stats(out)
        assert stats["empty_target_fraction"] <= 0.10
        assert stats["empty_target_fraction"] > 0   # "no trade" survives

    def test_losing_entry_empties_are_preferred_over_flat_ones(self):
        out = db.rebalance_train(_pool(), max_empty_fraction=0.10)
        empties = [e for e in out if not db._target_trades(e)]
        lost = [e for e in empties
                if "lost_entry" in e["_meta"]["origins"].values()]
        # 10 losing-entry empties exist and the cap admits >= 10: all
        # of them are kept before any flat-only one is.
        assert len(lost) == 10

    def test_direction_balance_pulls_the_majority_toward_parity(self):
        before = db.split_stats(_pool())
        assert before["examples_with_short"] > 1.25 * before[
            "examples_with_buy"]
        after = db.split_stats(db.rebalance_train(_pool()))
        assert after["examples_with_short"] <= 1.25 * after[
            "examples_with_buy"] + 1
        assert after["examples_with_buy"] == before["examples_with_buy"]

    def test_no_target_is_ever_altered_and_no_cycle_split(self):
        pool = _pool()
        originals = {e["_meta"]["cycle_id"]:
                     json.dumps(e["messages"]) for e in pool}
        out = db.rebalance_train(pool)
        ids = [e["_meta"]["cycle_id"] for e in out]
        assert len(ids) == len(set(ids))
        for e in out:
            assert json.dumps(e["messages"]) == originals[
                e["_meta"]["cycle_id"]]

    def test_deterministic_under_a_fixed_seed(self):
        a = [e["_meta"]["cycle_id"] for e in db.rebalance_train(_pool(),
                                                                seed=7)]
        b = [e["_meta"]["cycle_id"] for e in db.rebalance_train(_pool(),
                                                                seed=7)]
        c = [e["_meta"]["cycle_id"] for e in db.rebalance_train(_pool(),
                                                                seed=8)]
        assert a == b and a != c

    def test_missed_move_cap_is_off_by_default_and_works_when_set(self):
        default = db.split_stats(db.rebalance_train(_pool()))
        assert default["missed_move_share_of_directional"] > 0.5
        capped = db.split_stats(
            db.rebalance_train(_pool(), missed_move_cap=0.5))
        assert capped["missed_move_share_of_directional"] <= 0.5
        # it only ever drops all-missed-move examples: real entries stay
        assert capped["origin_distribution"]["kept_win"] == default[
            "origin_distribution"]["kept_win"]

    def test_rebalance_touches_train_only_and_manifest_audits_it(
            self, tmp_path):
        prompt = _prompt(["AAA"], ["EEE"], filler=2)
        preds, cycles = [], []
        for c in range(60):
            cid, ts = f"cyc-{c}", f"2026-07-{c % 28 + 1:02d}T{c % 24:02d}:00:00"
            if c % 3 == 0:                   # action-bearing
                preds.append(_row(c * 10 + 1, "AAA", "BUY", "win", 6.0,
                                  cycle=cid, ts=ts))
            else:                            # empty target, flat
                preds.append(_row(c * 10 + 1, "AAA", "HOLD", "neutral",
                                  0.2, cycle=cid, ts=ts))
            cycles.append({"cycle_id": cid, "prompt_text": prompt + f" #{c}",
                           "raw_response_json": json.dumps({"trades": []})})
        root = _archive(tmp_path, preds, cycles)
        nat = db.build_dataset([], str(tmp_path / "nat"), archive_root=root,
                               eval_holdout=10, rebalance=False)
        reb = db.build_dataset([], str(tmp_path / "reb"), archive_root=root,
                               eval_holdout=10)
        # eval and val are byte-identical with and without rebalancing
        for name in ("eval.jsonl", "val.jsonl", "eval_meta.jsonl"):
            assert ((tmp_path / "nat" / name).read_text()
                    == (tmp_path / "reb" / name).read_text())
        assert reb["eval_stats"] == nat["eval_stats"]
        assert reb["val_stats"] == nat["val_stats"]
        r = reb["rebalance"]
        assert r["applied"] is True and nat["rebalance"]["applied"] is False
        assert r["train_before"]["empty_target_fraction"] > 0.5
        assert r["train_after"]["empty_target_fraction"] <= 0.10
        for key in ("examples", "empty_target_fraction",
                    "mean_trades_per_target", "label_distribution",
                    "origin_distribution"):
            assert key in r["train_before"] and key in r["train_after"]
        assert reb["train"] == r["train_after"]["examples"]

    def test_look_ahead_guard_still_refuses_a_leaking_row(self, tmp_path):
        prompt = _prompt(["AAA"], filler=2)
        bad = _row(1, "AAA", "BUY", "win", 6.0)
        bad["resolved_at"] = bad["timestamp"]          # not strictly after
        root = _archive(tmp_path, [bad], [
            {"cycle_id": "cyc-1", "prompt_text": prompt,
             "raw_response_json": json.dumps({"trades": []})}])
        with pytest.raises(AssertionError, match="LOOK-AHEAD"):
            db.build_dataset([], str(tmp_path / "out"), archive_root=root,
                             eval_holdout=0)


# ---------------------------------------------------------------------------
# Time-ordered split with a purge — the exam must not leak
# ---------------------------------------------------------------------------

def _daily_corpus(tmp_path, n_days, late_resolvers=()):
    """One BUY-win cycle per day in August; cycles named in
    `late_resolvers` only learn their outcome 20 days later."""
    prompt = _prompt(["AAA"], filler=2)
    preds, cycles = [], []
    for d in range(1, n_days + 1):
        cid, ts = f"cyc-{d}", f"2026-08-{d:02d}T14:00:00"
        resolved = None
        if d in late_resolvers:
            resolved = f"2026-08-{min(d + 20, 31):02d}T14:00:00"
        preds.append(_row(d, "AAA", "BUY", "win", 6.0, cycle=cid, ts=ts,
                          resolved=resolved))
        cycles.append({"cycle_id": cid, "prompt_text": prompt + f" #{d}",
                       "raw_response_json": json.dumps({"trades": []})})
    return _archive(tmp_path, preds, cycles)


class TestTimeOrderedSplit:
    def test_blocks_are_ordered_train_then_val_then_eval(self, tmp_path):
        root = _daily_corpus(tmp_path, 20)
        m = db.build_dataset([], str(tmp_path / "out"), archive_root=root,
                             eval_holdout=4, val_fraction=0.25,
                             rebalance=False)
        s = m["split"]
        assert (s["train_last_decision"] < s["val_first_decision"]
                < s["eval_first_decision"])
        assert s["eval_first_decision"] == "2026-08-17 14:00:00"
        assert s["val_first_decision"] == "2026-08-13 14:00:00"
        assert (m["train"], m["val"], m["eval"]) == (12, 4, 4)
        assert s["train_cycles_purged"] == 0

    def test_a_label_learned_inside_a_later_block_is_purged(self, tmp_path):
        """Day 5's outcome only became known on day 25 — inside the
        exam window. Training on it would let the adapter score on the
        exam from an outcome the untrained base never saw."""
        root = _daily_corpus(tmp_path, 20, late_resolvers={5})
        out = tmp_path / "out"
        m = db.build_dataset([], str(out), archive_root=root,
                             eval_holdout=4, val_fraction=0.25,
                             rebalance=False)
        assert m["split"]["train_cycles_purged"] == 1
        assert (m["train"], m["val"], m["eval"]) == (11, 4, 4)
        train = (out / "train.jsonl").read_text()
        assert " #5\"" not in train and " #4\"" in train

    def test_validation_is_not_purged_against_the_exam(self, tmp_path):
        """Val is never trained on, and outcomes take days to resolve:
        purging val against the exam emptied it on the first real
        build. Only TRAIN is purged."""
        root = _daily_corpus(tmp_path, 20, late_resolvers={14})
        out = tmp_path / "out"
        m = db.build_dataset([], str(out), archive_root=root,
                             eval_holdout=4, val_fraction=0.25,
                             rebalance=False)
        assert m["val"] == 4 and m["split"]["train_cycles_purged"] == 0
        assert " #14\"" in (out / "val.jsonl").read_text()

    def test_exam_is_sampled_evenly_across_the_last_decision_days(
            self, tmp_path):
        """"The N most recent resolved cycles" is one trading day — one
        market regime. With eval_days the exam spans several."""
        prompt = _prompt(["AAA"], filler=2)
        preds, cycles = [], []
        for d in range(1, 11):
            for k in range(5):
                cid = f"d{d}k{k}"
                preds.append(_row(d * 10 + k, "AAA", "BUY", "win", 6.0,
                                  cycle=cid,
                                  ts=f"2026-08-{d:02d}T1{k}:00:00"))
                cycles.append({"cycle_id": cid,
                               "prompt_text": prompt + f" #{cid}",
                               "raw_response_json":
                                   json.dumps({"trades": []})})
        root = _archive(tmp_path, preds, cycles)

        def build(name, seed=1729):
            out = tmp_path / name
            m = db.build_dataset([], str(out), archive_root=root,
                                 eval_holdout=6, eval_days=3,
                                 val_fraction=0.2, rebalance=False,
                                 seed=seed)
            metas = [json.loads(line) for line in
                     (out / "eval_meta.jsonl").read_text().splitlines()]
            return m, metas
        m, metas = build("a")
        days = sorted(x["timestamp"][:10] for x in metas)
        assert days == ["2026-08-08"] * 2 + ["2026-08-09"] * 2 + [
            "2026-08-10"] * 2
        assert m["split"]["exam_period_cycles_by_day"] == {
            "2026-08-08": 5, "2026-08-09": 5, "2026-08-10": 5}
        # nothing from the exam PERIOD is trained or validated on —
        # including the period's cycles that were not sampled
        assert m["split"]["val_first_decision"] < "2026-08-08"
        assert m["train"] + m["val"] == 35
        # deterministic under a seed
        assert [x["cycle_id"] for x in build("b")[1]] == [
            x["cycle_id"] for x in metas]

    def test_the_exam_itself_is_never_purged(self, tmp_path):
        root = _daily_corpus(tmp_path, 20, late_resolvers={19})
        m = db.build_dataset([], str(tmp_path / "out"), archive_root=root,
                             eval_holdout=4, val_fraction=0.25,
                             rebalance=False)
        assert m["eval"] == 4


# ---------------------------------------------------------------------------
# Failed AI calls are not decisions
# ---------------------------------------------------------------------------

class TestFailedCallsExcluded:
    FAILED = json.dumps({
        "trades": [], "alternates": [], "pass_this_cycle": True,
        "portfolio_reasoning": "AI call failed: 429 RESOURCE_EXHAUSTED. "
                               "Your project has exceeded its monthly "
                               "spending cap."})
    CAPPED = json.dumps({
        "trades": [], "alternates": [], "pass_this_cycle": True,
        "cost_capped": True,
        "portfolio_reasoning": "Cost cap reached — no new trades."})

    def test_the_stand_in_responses_are_recognised(self):
        assert db._is_failed_call(self.FAILED)
        assert db._is_failed_call(self.CAPPED)
        assert db._is_failed_call('{"portfolio_reasoning": "AI call failed')

    def test_real_answers_are_not_mistaken_for_failures(self):
        real_pass = json.dumps({"trades": [], "portfolio_reasoning":
                                "Nothing here clears the bar; passing."})
        mentions = json.dumps({
            "trades": [{"symbol": "AAA", "action": "BUY"}],
            "portfolio_reasoning": "AI call failed earlier today per the "
                                   "news; buying AAA on the dip."})
        assert not db._is_failed_call(real_pass)
        assert not db._is_failed_call(mentions)
        assert not db._is_failed_call(None) and not db._is_failed_call("")

    def test_a_failed_cycle_never_becomes_a_hold_lesson(self, tmp_path):
        """Even if such a cycle DID store its prompt, its fabricated
        HOLD predictions must not enter the corpus."""
        prompt = _prompt(["AAA"], filler=2)
        root = _archive(
            tmp_path,
            [_row(1, "AAA", "HOLD", "neutral", 0.3, cycle="bad",
                  raw=self.FAILED),
             _row(2, "AAA", "HOLD", "neutral", 0.3, cycle="good",
                  ts="2026-08-02T14:00:00")],
            [{"cycle_id": "bad", "prompt_text": prompt + " #bad",
              "raw_response_json": self.FAILED},
             {"cycle_id": "good", "prompt_text": prompt + " #good",
              "raw_response_json": json.dumps({"trades": []})}])
        m = db.build_dataset([], str(tmp_path / "out"), archive_root=root,
                             eval_holdout=0, rebalance=False)
        assert m["labeled_rows"] == 1 and m["total_examples"] == 1
        assert "#bad" not in (tmp_path / "out" / "train.jsonl").read_text()


# ---------------------------------------------------------------------------
# Training command, config and the refuse-to-train guard
# ---------------------------------------------------------------------------

class TestTrainingRecipe:
    def test_defaults_are_what_every_batch_actually_ran(self, monkeypatch):
        """Batches 1-3 passed the 4-bit base and batch size 1 by flag
        while the defaults named the unquantized repo and 2 — a run
        without flags would have trained a different model."""
        seen = {}
        monkeypatch.setattr(lt, "cmd_train",
                            lambda args: seen.update(vars(args)) or 0)
        assert lt.main(["train", "--data", "/nowhere"]) == 0
        assert seen["model"] == "mlx-community/Qwen2.5-7B-Instruct-4bit"
        assert seen["batch_size"] == 1
        assert seen["max_seq_length"] == 8192 and seen["iters"] == 1200

    def test_command_always_masks_the_prompt_and_passes_the_config(self):
        cmd = lt.build_train_command("py", "m", "/d", "/a", 1200, 1, 16,
                                     None, config_path="/a/cfg.yaml")
        assert "--mask-prompt" in cmd and "--grad-checkpoint" in cmd
        assert cmd[cmd.index("--config") + 1] == "/a/cfg.yaml"
        assert cmd[cmd.index("--max-seq-length") + 1] == "8192"
        # pure: same inputs, same command, no filesystem
        assert cmd == lt.build_train_command(
            "py", "m", "/d", "/a", 1200, 1, 16, None,
            config_path="/a/cfg.yaml")

    def test_config_is_a_warmup_then_cosine_decay_to_the_last_step(self):
        cfg = lt.build_train_config(1200)
        s = cfg["lr_schedule"]
        assert s["name"] == "cosine_decay"
        assert s["warmup"] == 60 and s["warmup_init"] == 0.0
        assert s["arguments"] == [1e-5, 1140, 1e-7]   # warmup + decay = iters
        assert cfg["save_every"] == cfg["steps_per_eval"] == 100
        assert cfg["val_batches"] == 100

    def test_rendered_config_reads_back_as_numbers_not_strings(self):
        """Stock YAML reads `1e-05` as a STRING; the file must use
        plain decimals so the optimizer gets floats."""
        import yaml
        cfg = lt.build_train_config(1200)
        text = lt.render_train_config(cfg)
        assert "e-0" not in text
        assert "arguments: [0.00001, 1140, 0.0000001]" in text
        assert yaml.safe_load(text) == cfg

    def test_guard_flags_overlength_and_answerless_examples(self):
        def full(msgs):
            return sum(len(m["content"]) for m in msgs)

        def prompt(msgs):
            return sum(len(m["content"]) for m in msgs) + 3

        def ex(p, a):
            return {"messages": [{"role": "user", "content": "p" * p},
                                 {"role": "assistant", "content": "a" * a}]}
        rows = [ex(50, 10),      # fits
                ex(95, 10),      # 105 > window: the answer would be cut
                ex(100, 10)]     # prompt alone fills the window
        bad = lt.check_corpus_lengths(rows, full, prompt, 100)
        assert [b["index"] for b in bad] == [1, 2]
        assert lt.check_corpus_lengths(rows[:1], full, prompt, 100) == []

    def test_train_refuses_a_corpus_the_trainer_would_damage(
            self, tmp_path, monkeypatch, capsys):
        data = tmp_path / "data"
        data.mkdir()
        (data / "train.jsonl").write_text("{}\n")
        monkeypatch.setattr(lt, "_verify_corpus_fits", lambda *a: 3)
        launched = []
        monkeypatch.setattr(lt.subprocess, "Popen",
                            lambda *a, **k: launched.append(a))
        rc = lt.main(["--workdir", str(tmp_path), "train",
                      "--data", str(data)])
        assert rc == 2 and launched == []
        assert "REFUSING TO TRAIN" in capsys.readouterr().out
        assert not (tmp_path / "adapters").exists()


class TestSnapshotGuard:
    def _journal(self, path, resolved=1):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE ai_predictions (id INTEGER, status TEXT)")
        for i in range(resolved):
            conn.execute("INSERT INTO ai_predictions VALUES (?, 'resolved')",
                         (i,))
        conn.commit()
        conn.close()
        return str(path)

    def test_sound_snapshots_pass(self, tmp_path):
        p = self._journal(tmp_path / "quantopsai_profile_229.db")
        assert lt.check_profile_snapshots([p]) == []

    def test_torn_empty_and_tableless_copies_are_named(self, tmp_path):
        torn = tmp_path / "quantopsai_profile_230.db"
        torn.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
        empty = self._journal(tmp_path / "quantopsai_profile_231.db",
                              resolved=0)
        bare = tmp_path / "quantopsai_profile_232.db"
        import sqlite3
        sqlite3.connect(bare).close()
        problems = lt.check_profile_snapshots([str(torn), empty, str(bare)])
        assert len(problems) == 3
        assert "profile_230" in problems[0]
        assert "no resolved predictions" in problems[1]
        assert "profile_232" in problems[2]

    def test_build_refuses_on_a_bad_snapshot(self, tmp_path, capsys):
        (tmp_path / "corpus" / "predictions_archive").mkdir(parents=True)
        snap = tmp_path / "corpus" / "profile_dbs"
        snap.mkdir()
        (snap / "quantopsai_profile_229.db").write_bytes(b"not a database")
        rc = lt.main(["--workdir", str(tmp_path), "build-corpus"])
        assert rc == 2 and not (tmp_path / "data").exists()
        assert "not usable" in capsys.readouterr().out

    def test_manifest_counts_labeled_rows_per_source(self, tmp_path):
        prompt = _prompt(["AAA"], filler=2)
        root = _archive(tmp_path, [_row(1, "AAA", "BUY", "win", 6.0)], [
            {"cycle_id": "cyc-1", "prompt_text": prompt,
             "raw_response_json": json.dumps({"trades": []})}])
        m = db.build_dataset([], str(tmp_path / "out"), archive_root=root,
                             eval_holdout=0, rebalance=False)
        assert m["labeled_rows_by_source"] == {"229": 1}


# ---------------------------------------------------------------------------
# Checkpoint sweep
# ---------------------------------------------------------------------------

_LOG = """Iter 1: Val loss 2.078, Val took 295s
Iter 100: Val loss 1.400, Val took 1s
Iter 200: Val loss 1.027, Val took 1s
Iter 300: Val loss 0.900, Val took 1s
Iter 400: Val loss 0.822, Val took 1s
Iter 500: Val loss 0.830, Val took 1s
Iter 600: Val loss 0.816, Val took 1s
Iter 700: Val loss nan, Val took 1s
Iter 800: Val loss 7.325, Val took 1s
"""


def _adapter_dir(tmp_path, steps=(100, 200, 300, 400, 500, 600, 700, 800)):
    d = tmp_path / "adapters" / "run"
    d.mkdir(parents=True)
    for s in steps:
        (d / f"{s:07d}_adapters.safetensors").write_bytes(f"w{s}".encode())
    (d / "adapters.safetensors").write_bytes(b"final")
    (d / "adapter_config.json").write_text("{}")
    (d / "train.log").write_text(_LOG)
    return str(d)


class TestCheckpointSweep:
    def test_discovery_and_selection_by_validation_loss(self, tmp_path):
        d = _adapter_dir(tmp_path)
        ckpts = lt.discover_checkpoints(d)
        assert sorted(ckpts) == [100, 200, 300, 400, 500, 600, 700, 800]
        losses = lt.parse_val_losses(_LOG)
        assert losses[600] == 0.816 and losses[700] == float("inf")
        # three lowest validation losses, plus the final step
        assert lt.select_checkpoints(ckpts, losses, 3) == [400, 500, 600, 800]

    def test_a_blown_up_checkpoint_is_never_ranked_first(self):
        losses = lt.parse_val_losses("Iter 100: Val loss nan, Val took 1s\n"
                                     "Iter 200: Val loss 0.9, Val took 1s\n")
        assert lt.select_checkpoints({100: "a", 200: "b"}, losses, 1) == [200]

    def test_staging_gives_the_loader_the_file_name_it_reads(self, tmp_path):
        d = _adapter_dir(tmp_path)
        staged = lt.stage_checkpoint(d, 400, str(tmp_path / "stage"))
        assert open(os.path.join(staged, "adapters.safetensors"),
                    "rb").read() == b"w400"
        assert os.path.exists(os.path.join(staged, "adapter_config.json"))

    def test_sweep_generates_base_answers_once(self, tmp_path, monkeypatch):
        d = _adapter_dir(tmp_path)
        data = tmp_path / "data"
        data.mkdir()
        rows = [{"messages": [{"role": "user", "content": "p"},
                              {"role": "assistant", "content": "{}"}]}] * 4
        metas = [{"labels": {"AAA": "BUY"}, "origins": {"AAA": "kept_win"}},
                 {"labels": {"BBB": "SHORT"},
                  "origins": {"BBB": "missed_move"}},
                 {"labels": {"CCC": "HOLD"}, "origins": {"CCC": "flat_hold"}},
                 {"labels": {"DDD": "HOLD"}, "origins": {"DDD": "lost_entry"}}]
        (data / "eval.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")
        (data / "eval_meta.jsonl").write_text(
            "\n".join(json.dumps(m) for m in metas) + "\n")
        calls = []

        def fake_generate(model, adapter, eval_rows, max_tokens,
                          cache_path=None):
            calls.append(adapter)
            assert cache_path and "/generations/" in cache_path
            if adapter is None:
                return ['{"trades": []}'] * len(eval_rows)
            return ['{"trades":[{"symbol":"AAA","action":"BUY"},'
                    '{"symbol":"BBB","action":"SHORT"}]}'] * len(eval_rows)
        monkeypatch.setattr(lt, "_generate_answers", fake_generate)
        rc = lt.main(["eval", "--data", str(data), "--adapter", d,
                      "--sweep"])
        assert rc == 0
        assert calls.count(None) == 1                  # base: exactly once
        assert len(calls) == 1 + 4                     # + four checkpoints
        report = json.loads(
            next(data.glob("eval_report_*.json")).read_text())
        assert sorted(report["candidates"]) == [
            "step_400", "step_500", "step_600", "step_800"]
        assert report["checkpoint_selection"]["lowest_val_loss_step"] == 600
        for score in report["candidates"].values():
            assert score["accuracy"] == 1.0 and len(score["generations"]) == 4
            assert score["paired_vs_base"]["only_adapter_right"] == 2
            assert "clustered_p_value" in score["paired_vs_base"]
            assert "promotion_bar" in score and "by_origin" in score
        assert report["base"]["answer_mix"] == {"hold": 4}
        assert report["winner"]["chosen_on_exam_from"] == 4
        assert report["baselines"]["always_hold"] == 0.5

    def test_sweep_without_a_training_log_fails_loudly(self, tmp_path,
                                                       capsys):
        d = _adapter_dir(tmp_path)
        os.remove(os.path.join(d, "train.log"))
        data = tmp_path / "data"
        data.mkdir()
        (data / "eval.jsonl").write_text(json.dumps(
            {"messages": [{"role": "user", "content": "p"}]}) + "\n")
        (data / "eval_meta.jsonl").write_text(json.dumps(
            {"labels": {"AAA": "BUY"}}) + "\n")
        assert lt.main(["eval", "--data", str(data), "--adapter", d,
                        "--sweep"]) == 2
        assert "validation loss" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The exam survives a crash
# ---------------------------------------------------------------------------

class _FakeTokenizer:
    def apply_chat_template(self, msgs, add_generation_prompt=True,
                            tokenize=False):
        return "|".join(m["content"] for m in msgs)


def _fake_mlx(monkeypatch, crash_after=None):
    """A stand-in `mlx_lm` whose generate() echoes the prompt and can
    die after N answers, the way macOS killed batch 4's Metal job."""
    import sys
    import types
    state = {"loads": 0, "generated": []}
    mod = types.ModuleType("mlx_lm")

    def load(model_path, adapter_path=None):
        state["loads"] += 1
        return object(), _FakeTokenizer()

    def generate(model, tokenizer, prompt, max_tokens, verbose=False):
        if crash_after is not None and len(state["generated"]) >= crash_after:
            raise RuntimeError("[METAL] Command buffer execution failed: "
                               "Impacting Interactivity")
        state["generated"].append(prompt)
        return f"answer to {prompt}"
    mod.load, mod.generate = load, generate
    monkeypatch.setitem(sys.modules, "mlx_lm", mod)
    return state


def _rows(n):
    return [{"messages": [{"role": "user", "content": f"p{i}"},
                          {"role": "assistant", "content": "{}"}]}
            for i in range(n)]


class TestExamSurvivesACrash:
    def test_a_crash_costs_one_answer_not_the_whole_exam(self, tmp_path,
                                                          monkeypatch):
        rows = _rows(5)
        cache = lt.generation_cache_path(str(tmp_path), rows, "m", "base",
                                         2000)
        _fake_mlx(monkeypatch, crash_after=3)
        with pytest.raises(RuntimeError, match="Impacting Interactivity"):
            lt._generate_answers("m", None, rows, 2000, cache_path=cache)
        assert len(open(cache).read().splitlines()) == 3   # saved as made
        state = _fake_mlx(monkeypatch)                     # the rerun
        texts = lt._generate_answers("m", None, rows, 2000,
                                     cache_path=cache)
        assert state["generated"] == ["p3", "p4"]          # only the rest
        assert texts == [f"answer to p{i}" for i in range(5)]

    def test_a_finished_answerer_is_never_regenerated_or_even_loaded(
            self, tmp_path, monkeypatch):
        rows = _rows(3)
        cache = lt.generation_cache_path(str(tmp_path), rows, "m", "base",
                                         2000)
        _fake_mlx(monkeypatch)
        first = lt._generate_answers("m", None, rows, 2000, cache_path=cache)
        state = _fake_mlx(monkeypatch)
        again = lt._generate_answers("m", None, rows, 2000, cache_path=cache)
        assert again == first
        assert state["loads"] == 0 and state["generated"] == []

    def test_a_torn_last_line_is_regenerated_not_trusted(self, tmp_path,
                                                         monkeypatch):
        rows = _rows(3)
        cache = lt.generation_cache_path(str(tmp_path), rows, "m", "base",
                                         2000)
        os.makedirs(os.path.dirname(cache))
        with open(cache, "w") as fh:
            fh.write(json.dumps({"i": 0, "text": "answer to p0"}) + "\n")
            fh.write('{"i": 1, "text": "answer to')        # died mid-write
        state = _fake_mlx(monkeypatch)
        texts = lt._generate_answers("m", None, rows, 2000, cache_path=cache)
        assert state["generated"] == ["p1", "p2"]
        assert texts == [f"answer to p{i}" for i in range(3)]

    def test_answers_are_never_reused_for_a_different_exam_or_setup(
            self, tmp_path):
        rows = _rows(3)
        base = lt.generation_cache_path(str(tmp_path), rows, "m", "base", 2000)
        other_exam = list(rows)
        other_exam[1] = {"messages": [{"role": "user", "content": "CHANGED"}]}
        assert base != lt.generation_cache_path(
            str(tmp_path), other_exam, "m", "base", 2000)
        assert base != lt.generation_cache_path(
            str(tmp_path), rows, "other-model", "base", 2000)
        assert base != lt.generation_cache_path(
            str(tmp_path), rows, "m", "base", 300)
        # two training runs' step-500 checkpoints never share a file
        assert lt.generation_cache_path(
            str(tmp_path), rows, "m", "step_500@runA", 2000
        ) != lt.generation_cache_path(
            str(tmp_path), rows, "m", "step_500@runB", 2000)
        # the label (the target answer) is NOT part of the identity
        relabeled = [dict(r, messages=[r["messages"][0],
                                       {"role": "assistant",
                                        "content": "different"}])
                     for r in rows]
        assert base == lt.generation_cache_path(
            str(tmp_path), relabeled, "m", "base", 2000)


# ---------------------------------------------------------------------------
# Baselines and the promotion bar
# ---------------------------------------------------------------------------

class TestBaselinesAndBar:
    LABELS = ["HOLD"] * 50 + ["BUY"] * 30 + ["SHORT"] * 20

    def test_frequency_matched_matches_the_closed_form(self):
        b = lt.baseline_scores(self.LABELS)
        closed = 0.5 ** 2 + 0.3 ** 2 + 0.2 ** 2          # Σ pᵢ² = 0.38
        assert b["frequency_matched"]["closed_form"] == pytest.approx(closed)
        assert b["frequency_matched"]["mean"] == pytest.approx(closed,
                                                               abs=0.01)
        assert (b["frequency_matched"]["p05"]
                < b["frequency_matched"]["mean"]
                < b["frequency_matched"]["p95"])

    def test_trivial_baselines(self):
        b = lt.baseline_scores(self.LABELS)
        assert b["always_hold"] == 0.5
        assert b["majority_class"] == {"label": "hold", "accuracy": 0.5}
        assert lt.baseline_scores(["BUY"] * 6 + ["HOLD"] * 4)[
            "majority_class"] == {"label": "bullish", "accuracy": 0.6}

    def test_baselines_are_deterministic(self):
        assert lt.baseline_scores(self.LABELS) == lt.baseline_scores(
            self.LABELS)

    def test_hold_collapsed_winner_does_not_pass_the_bar(self):
        """Batch 2's shape: wins overall by answering HOLD, worse than
        the base on both directional classes."""
        labels = self.LABELS
        base_ans = (["HOLD"] * 15 + ["BUY"] * 35         # 15/50 holds
                    + ["BUY"] * 12 + ["HOLD"] * 18       # 12/30 bullish
                    + ["SHORT"] * 8 + ["HOLD"] * 12)     # 8/20 bearish
        collapsed = ["HOLD"] * 100                       # 50/50, 0, 0
        base = lt.score_examples(labels, base_ans)
        adapter = lt.score_examples(labels, collapsed)
        assert adapter["accuracy"] > base["accuracy"]    # 0.50 vs 0.35
        bar = lt.promotion_bar(base, adapter, lt.baseline_scores(labels),
                               lt.paired_comparison(labels, base_ans,
                                                    collapsed))
        assert bar["passed"] is False and bar["clear_win"] is False
        assert any("BOTH" in r for r in bar["reasons"])
        assert adapter["answer_mix"] == {"hold": 100}

    def test_losing_to_the_base_or_to_guessing_fails_with_reasons(self):
        labels = self.LABELS
        perfect = list(labels)
        base = lt.score_examples(labels, perfect)
        adapter = lt.score_examples(labels, ["BUY"] * 100)
        bar = lt.promotion_bar(base, adapter, lt.baseline_scores(labels))
        assert bar["passed"] is False and len(bar["reasons"]) >= 2

    def test_a_real_discriminating_win_passes_and_is_clear(self):
        labels = self.LABELS
        base_ans = ["HOLD"] * 100
        adapter_ans = list(labels)
        base = lt.score_examples(labels, base_ans)
        adapter = lt.score_examples(labels, adapter_ans)
        paired = lt.paired_comparison(labels, base_ans, adapter_ans)
        assert paired == {"only_adapter_right": 50, "only_base_right": 0,
                          "p_value": 0.0}
        bar = lt.promotion_bar(base, adapter, lt.baseline_scores(labels),
                               paired)
        assert bar == {"passed": True, "clear_win": True, "reasons": []}

    def test_a_narrow_pass_is_not_called_a_clear_win(self):
        labels = ["BUY"] * 10 + ["HOLD"] * 10
        base_ans = ["BUY"] * 5 + ["HOLD"] * 5 + ["HOLD"] * 10     # 15/20
        adapter_ans = ["BUY"] * 6 + ["HOLD"] * 4 + ["HOLD"] * 10  # 16/20
        paired = lt.paired_comparison(labels, base_ans, adapter_ans)
        assert paired["only_adapter_right"] == 1 and paired["p_value"] == 1.0
        bar = lt.promotion_bar(lt.score_examples(labels, base_ans),
                               lt.score_examples(labels, adapter_ans),
                               lt.baseline_scores(labels), paired)
        assert bar["passed"] is True and bar["clear_win"] is False

    def test_twelve_replicates_of_one_stock_day_are_one_piece_of_evidence(
            self):
        """Replicate profiles judge the same stock on the same day
        against the same outcome. Row-level, 12-vs-1 looks decisive;
        it is ONE stock-day for the adapter and one for the base."""
        labels = ["SHORT"] * 12 + ["BUY"]
        base_ans = ["HOLD"] * 12 + ["BUY"]
        adapter_ans = ["SHORT"] * 12 + ["HOLD"]
        clusters = [("NVDA", "2026-09-11")] * 12 + [("KO", "2026-09-10")]
        paired = lt.paired_comparison(labels, base_ans, adapter_ans,
                                      clusters)
        assert paired["only_adapter_right"] == 12
        assert paired["p_value"] < 0.01                  # row-level: fooled
        assert paired["independent_clusters"] == 2
        assert (paired["clusters_adapter_better"],
                paired["clusters_base_better"]) == (1, 1)
        assert paired["clustered_p_value"] == 1.0        # honest: nothing
        base = lt.score_examples(labels, base_ans)
        adapter = lt.score_examples(labels, adapter_ans)
        bar = lt.promotion_bar(base, adapter, lt.baseline_scores(labels),
                               paired)
        assert bar["clear_win"] is False, (
            "the bar must trust the clustered p-value, not the row one")

    def test_an_empty_exam_never_passes(self):
        bar = lt.promotion_bar(lt.score_examples([], []),
                               lt.score_examples([], []),
                               lt.baseline_scores([]))
        assert bar["passed"] is False
