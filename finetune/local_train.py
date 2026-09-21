"""Local LoRA training driver — docs/20 §16.1 Phase 4b.2, activated by
docs/25 step 4.5.

Runs on the operator's M2 Max, NEVER on prod. Three subcommands form
the weekly batch loop (train locally in batches so the artifact is
ours and vendor-independent — operator decision 2026-08-23):

    build-corpus   dataset_builder over a local snapshot of the prod
                   archive (+ profile-journal copies) → train/valid/
                   eval chat JSONL (mlx-lm layout). Every example is
                   measured with the base model's own tokenizer and
                   made to fit the training window; the train split
                   is rebalanced; the manifest reports every stage.
    train          mlx_lm LoRA fine-tune on that corpus. Prompt
                   masking is always on (the loss covers the answer
                   only), the learning rate warms up then decays on a
                   cosine, checkpoints and validation losses land
                   every 100 steps, and the run REFUSES to start if
                   any example would have its answer cut off.
    eval           self-scored held-out exam: the untrained base
                   answers once, then one adapter — or a sweep of a
                   run's best checkpoints — answers the same prompts.
                   Scored against the hindsight labels beside three
                   guessing baselines, with a paired significance
                   test and an explicit promotion bar. An adapter
                   earns promotion consideration ONLY by clearing
                   that bar (docs/28 §4.4).

Base model: any mlx-compatible instruct model. Default is the 4-bit
MLX build of Qwen2.5-7B-Instruct — UNGATED (a run needs no account or
token) and the base every batch has trained against. Llama-3.1-8B-
Instruct is gated behind a HuggingFace license acceptance — pass
--model once the operator's HF token is configured. The corpus,
driver, and eval are identical either way — the base is a config
value, not a design commitment.

PERFECT-DATA notes: the corpus inherits dataset_builder's no-look-ahead
invariant (labels only from outcomes resolved strictly after the
decision); eval examples are the most RECENT predictions, held out of
training entirely; nothing here reads prod live — it consumes an
explicit local snapshot directory.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_WORKDIR = os.path.expanduser("~/Quantops-finetune")
# The 4-bit MLX build of Qwen2.5-7B-Instruct — the base EVERY batch has
# actually trained and been examined against (each run passed it by
# flag while this default named the unquantized repo, so a run without
# --model would have silently trained a different, ~15GB base).
DEFAULT_MODEL = "mlx-community/Qwen2.5-7B-Instruct-4bit"
DEFAULT_MAX_SEQ_LENGTH = 8192
_BULLISH = frozenset({"BUY", "STRONG_BUY", "WEAK_BUY"})
_BEARISH = frozenset({"SHORT", "STRONG_SELL", "SELL"})
# Option decisions (2026-08-27): labeled by premium outcome; graded as
# their own bucket — proposing ANY option structure on the labeled
# symbol counts as the option call.
_OPTION = frozenset({"OPTIONS", "MULTILEG_OPEN", "OPTION_EXERCISE",
                     "PAIR_TRADE"})


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------
# build-corpus
# ---------------------------------------------------------------------------

class TokenizerCounter:
    """Exact token lengths from the base model's own tokenizer, counted
    the way mlx-lm's chat dataset counts them (`apply_chat_template`
    over the full message list)."""

    def __init__(self, tokenizer, model: str):
        self._tok = tokenizer
        self.method = f"exact:{model}"

    def text(self, s: str) -> int:
        return len(self._tok(s or "", add_special_tokens=False)["input_ids"])

    def messages(self, messages: List[Dict[str, str]]) -> int:
        return len(self._tok.apply_chat_template(
            messages, tokenize=True, return_dict=False))


def make_token_counter(model: str):
    """The exact counter when a tokenizer library is installed (the
    Mac's training venv); None otherwise, and the builder falls back to
    its conservative character estimate — which over-splits but can
    never under-count (see dataset_builder._CHARS_PER_TOKEN). Either
    way the manifest records which method measured the corpus, and
    `train` re-verifies every example with the real tokenizer before
    it starts."""
    try:
        from transformers import AutoTokenizer
    except ImportError:
        print("NOTE: no tokenizer library here — measuring example "
              "lengths with the conservative character estimate.")
        return None
    return TokenizerCounter(AutoTokenizer.from_pretrained(model), model)


def check_profile_snapshots(paths: List[str]) -> List[str]:
    """Problems with the journal copies, as plain sentences (empty =
    all sound). The builder itself only WARNS on an unreadable journal
    and moves on, so a torn or half-copied snapshot would silently
    shrink the corpus by a whole profile — the build refuses instead."""
    import sqlite3
    problems: List[str] = []
    for path in paths:
        name = os.path.basename(path)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
                if verdict != "ok":
                    problems.append(f"{name}: integrity check said "
                                    f"{verdict!r}")
                    continue
                n = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE status = 'resolved'").fetchone()[0]
                if not n:
                    problems.append(f"{name}: no resolved predictions")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            problems.append(f"{name}: unreadable ({exc})")
    return problems


def cmd_build_corpus(args) -> int:
    from finetune.dataset_builder import build_dataset
    workdir = Path(args.workdir)
    out_dir = workdir / "data" / _stamp()
    profile_dbs: List[str] = []
    snap = workdir / "corpus" / "profile_dbs"
    if snap.is_dir():
        profile_dbs = [str(p) for p in sorted(snap.glob("*.db"))]
    problems = check_profile_snapshots(profile_dbs)
    if problems:
        print("ERROR: journal snapshot(s) are not usable — re-copy them "
              "before building:")
        for p in problems:
            print(f"  {p}")
        return 2
    archive = workdir / "corpus" / "predictions_archive"
    if not archive.is_dir():
        print(f"ERROR: no archive snapshot at {archive} — rsync "
              "backups/predictions_archive from prod first.")
        return 2
    manifest = build_dataset(
        profile_dbs, str(out_dir),
        archive_root=str(archive),
        eval_holdout=args.eval_holdout,
        eval_days=args.eval_days,
        prune_unlabeled_candidates=not args.keep_unlabeled,
        val_fraction=args.val_fraction,
        max_tokens=args.max_tokens,
        token_counter=make_token_counter(args.model),
        rebalance=not args.no_rebalance,
        max_empty_fraction=args.max_empty_fraction,
        direction_ratio=args.direction_ratio,
        missed_move_cap=args.missed_move_cap,
    )
    # mlx-lm expects valid.jsonl alongside train.jsonl.
    val = out_dir / "val.jsonl"
    if val.exists():
        shutil.copyfile(val, out_dir / "valid.jsonl")
    manifest["out_dir"] = str(out_dir)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({k: v for k, v in manifest.items() if k != "paths"},
                     indent=2))
    print(f"corpus: {out_dir}")
    return 0


# ---------------------------------------------------------------------------
# train
# ---------------------------------------------------------------------------

def build_train_command(python_bin: str, model: str, data_dir: str,
                        adapter_path: str, iters: int,
                        batch_size: int, num_layers: int,
                        resume_adapter: Optional[str],
                        max_seq_length: int = DEFAULT_MAX_SEQ_LENGTH,
                        config_path: Optional[str] = None) -> List[str]:
    """The exact mlx_lm.lora invocation — pure so tests can pin it.

    --mask-prompt is ALWAYS on: the loss is computed on the assistant
    answer only. Without it mlx-lm averages over every token, and with
    ~7,000-token prompts and ~20-token answers the answer was 0.4-0.7%
    of the loss in batches 1-3 — those runs mostly learned to
    re-predict the prompt.

    max_seq_length is the training window. The corpus builder makes
    every example fit it and `cmd_train` refuses to start otherwise;
    mlx-lm itself would silently cut the END of a long example — the
    answer.

    `config_path` carries what mlx-lm only accepts from a config file
    (the learning-rate schedule) — see `build_train_config`."""
    cmd = [
        python_bin, "-m", "mlx_lm", "lora",
        "--model", model,
        "--train",
        "--data", data_dir,
        "--adapter-path", adapter_path,
        "--iters", str(iters),
        "--batch-size", str(batch_size),
        "--num-layers", str(num_layers),
        "--max-seq-length", str(max_seq_length),
        # Always on: recomputes activations instead of holding them.
        # Without it, 7B-class LoRA at 8K context OOMs Metal on the
        # 64GB M2 Max (batch-1 crash, 2026-08-26).
        "--grad-checkpoint",
        "--mask-prompt",
    ]
    if config_path:
        cmd += ["--config", config_path]
    if resume_adapter:
        cmd += ["--resume-adapter-file",
                os.path.join(resume_adapter, "adapters.safetensors")]
    return cmd


def build_train_config(iters: int, *, peak_lr: float = 1e-5,
                       end_lr: float = 1e-7,
                       warmup_fraction: float = 0.05,
                       save_every: int = 100,
                       steps_per_eval: int = 100,
                       val_batches: int = 100) -> Dict[str, Any]:
    """The settings mlx-lm reads from its config file — pure.

    Learning rate: linear warmup to `peak_lr`, then cosine decay to
    `end_lr` at the final iteration. Batch 3's constant 1e-5 was fine
    for 600 steps and blew validation loss from 0.82 to 7.3 past
    ~800. mlx-lm JOINS the warmup and the decay end to end, so the
    cosine's step count is the iterations left after the warmup.

    save_every / steps_per_eval line up so every saved checkpoint has
    a validation loss measured at the same step (the sweep selects on
    it). val_batches is 100, not mlx-lm's 25: with masking on, a
    validation pass scores only answer tokens — a few dozen per
    example — and 25 examples was too few to rank checkpoints."""
    warmup = max(1, int(round(iters * warmup_fraction)))
    return {
        "lr_schedule": {
            "name": "cosine_decay",
            "warmup": warmup,
            "warmup_init": 0.0,
            "arguments": [peak_lr, max(1, iters - warmup), end_lr],
        },
        "save_every": save_every,
        "steps_per_eval": steps_per_eval,
        "val_batches": val_batches,
    }


def _yaml_number(x) -> str:
    """Plain decimal, never exponent form: stock YAML reads `1e-05` as
    a STRING (its float pattern requires a dot), which would reach the
    optimizer as text."""
    if isinstance(x, int):
        return str(x)
    s = f"{x:.12f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def render_train_config(cfg: Dict[str, Any]) -> str:
    sched = cfg["lr_schedule"]
    return (
        "# Written by finetune/local_train.py — do not edit by hand.\n"
        "lr_schedule:\n"
        f"  name: {sched['name']}\n"
        f"  warmup: {_yaml_number(sched['warmup'])}\n"
        f"  warmup_init: {_yaml_number(sched['warmup_init'])}\n"
        "  arguments: ["
        + ", ".join(_yaml_number(a) for a in sched["arguments"]) + "]\n"
        f"save_every: {_yaml_number(cfg['save_every'])}\n"
        f"steps_per_eval: {_yaml_number(cfg['steps_per_eval'])}\n"
        f"val_batches: {_yaml_number(cfg['val_batches'])}\n"
    )


def check_corpus_lengths(rows: List[Dict[str, Any]], count_full,
                         count_prompt, max_seq_length: int
                         ) -> List[Dict[str, Any]]:
    """Every example the trainer would damage — pure; the two counters
    are injected (`cmd_train` builds them on the real tokenizer).

    An example is a violation when its full length exceeds the window
    (mlx-lm keeps only the first `max_seq_length` tokens, cutting the
    answer) or when no answer token lies inside the window (with
    prompt masking that is ZERO loss tokens, and mlx-lm's loss divides
    by that count — a NaN that destroys the adapter at batch size 1)."""
    bad: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        msgs = row["messages"]
        full = count_full(msgs)
        prompt = count_prompt([m for m in msgs
                               if m.get("role") != "assistant"])
        if full > max_seq_length or prompt >= min(full, max_seq_length):
            bad.append({"index": i, "tokens": full,
                        "prompt_tokens": prompt})
    return bad


def _verify_corpus_fits(data_dir: Path, model: str,
                        max_seq_length: int) -> int:
    """Refuse-to-train guard: tokenizes train + valid exactly as the
    trainer will. Returns the number of violations (0 = safe)."""
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(model)

    def _full(msgs):
        return len(tok.apply_chat_template(
            msgs, tokenize=True, return_dict=False))

    def _prompt(msgs):
        return len(tok.apply_chat_template(
            msgs, tokenize=True, add_generation_prompt=True,
            return_dict=False))

    total = 0
    for name in ("train.jsonl", "valid.jsonl"):
        path = data_dir / name
        if not path.exists():
            print(f"ERROR: {path} missing — run build-corpus.")
            return -1
        rows = []
        for i, line in enumerate(path.read_text().splitlines()):
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, ValueError) as exc:
                # Fail LOUD and closed: a corrupt corpus file must not
                # be trained on, or half-checked.
                print(f"ERROR: {path}:{i + 1} is not valid JSON ({exc}) "
                      "— rebuild the corpus before training.")
                return -1
        bad = check_corpus_lengths(rows, _full, _prompt, max_seq_length)
        print(f"length guard: {name}: {len(rows)} examples, "
              f"{len(bad)} over the {max_seq_length}-token window")
        for b in bad[:5]:
            print(f"  example {b['index']}: {b['tokens']} tokens "
                  f"(prompt {b['prompt_tokens']})")
        total += len(bad)
    return total


def cmd_train(args) -> int:
    workdir = Path(args.workdir)
    data_dir = Path(args.data)
    if not (data_dir / "train.jsonl").exists():
        print(f"ERROR: {data_dir}/train.jsonl missing — run build-corpus.")
        return 2
    violations = _verify_corpus_fits(data_dir, args.model,
                                     args.max_seq_length)
    if violations:
        print("REFUSING TO TRAIN: the corpus has examples the trainer "
              "would cut the answer off of. Rebuild it with "
              f"`build-corpus --max-tokens {args.max_seq_length}` "
              "(or train with the window the corpus was built for).")
        return 2
    adapter_dir = workdir / "adapters" / _stamp()
    adapter_dir.mkdir(parents=True, exist_ok=True)
    cfg = build_train_config(
        args.iters, peak_lr=args.learning_rate, end_lr=args.end_lr,
        warmup_fraction=args.warmup_fraction,
        val_batches=args.val_batches)
    config_path = adapter_dir / "train_config.yaml"
    config_path.write_text(render_train_config(cfg))
    # Prove the file says what we meant BEFORE hours of training: read
    # it back the way a YAML reader will and compare, numbers as
    # numbers.
    import yaml
    if yaml.safe_load(config_path.read_text()) != cfg:
        print(f"ERROR: {config_path} does not read back as the intended "
              "settings — refusing to train on a misparsed config.")
        return 2
    cmd = build_train_command(
        sys.executable, args.model, str(data_dir), str(adapter_dir),
        args.iters, args.batch_size, args.num_layers,
        args.resume_adapter, max_seq_length=args.max_seq_length,
        config_path=str(config_path),
    )
    print("running:", " ".join(cmd))
    # Tee the trainer's output into the adapter directory: the
    # checkpoint sweep reads each step's validation loss from it.
    log_path = adapter_dir / "train.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
        rc = proc.wait()
    if rc == 0:
        print(f"adapter: {adapter_dir}")
        (adapter_dir / "train_run.json").write_text(json.dumps({
            "model": args.model, "data": str(data_dir),
            "iters": args.iters, "batch_size": args.batch_size,
            "max_seq_length": args.max_seq_length,
            "mask_prompt": True, "config": cfg,
            "resume_adapter": args.resume_adapter,
            "finished_utc": _stamp(),
        }, indent=2))
    else:
        print(f"ERROR: trainer exited {rc} — see {log_path}")
    return rc


# ---------------------------------------------------------------------------
# eval — self-scored, adapter vs bare base
# ---------------------------------------------------------------------------

def parse_decision(text: str, symbol: Optional[str] = None
                   ) -> Optional[str]:
    """Extract the decision for `symbol` from a generated completion,
    using PRODUCTION semantics (2026-08-27 scorer fix): the prompt asks
    for a batch of candidates and non-actionable names are OMITTED, so
    a parsed trades list that doesn't mention the labeled symbol IS a
    HOLD on it — and the labeled symbol's own entry is what gets
    graded, never trades[0] (which is an arbitrary other candidate).
    The first eval scored 0/15 on every HOLD and graded the base on
    random candidates because of exactly those two errors.

    Falls back to a bare action token scan when no JSON object parses.
    None = unparseable (scored as wrong — it would be wrong live too).
    """
    if not text:
        return None
    s = text.strip()
    start = s.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start:i + 1])
                    except (json.JSONDecodeError, AttributeError):
                        break
                    trades = obj.get("trades") if isinstance(
                        obj, dict) else None
                    if not isinstance(trades, list):
                        return None
                    if symbol:
                        for t in trades:
                            if (isinstance(t, dict)
                                    and str(t.get("symbol", "")
                                            ).upper() == symbol.upper()):
                                act = str(t.get("action", "")
                                          ).upper().strip()
                                return act or None
                        return "HOLD"  # omitted from the batch = no action
                    if trades and isinstance(trades[0], dict):
                        act = str(trades[0].get("action", "")
                                  ).upper().strip()
                        return act or None
                    return "HOLD" if trades == [] else None
    for token in ("MULTILEG_OPEN", "OPTIONS", "STRONG_BUY", "WEAK_BUY",
                  "STRONG_SELL", "BUY", "SELL", "SHORT", "HOLD"):
        if token in s.upper():
            return token
    return None


def direction_bucket(action: Optional[str]) -> str:
    if action in _BULLISH:
        return "bullish"
    if action in _BEARISH:
        return "bearish"
    if action in _OPTION:
        return "option"
    if action == "HOLD":
        return "hold"
    return "unparseable"


def score_examples(labels: List[str], answers: List[Optional[str]],
                   origins: Optional[List[Optional[str]]] = None
                   ) -> Dict[str, Any]:
    """Directional accuracy of parsed answers vs hindsight labels —
    pure, so tests can pin the scoring. Reports the ANSWER MIX every
    time (a model that wins overall by answering HOLD is batch 2
    again) and, when label origins are supplied, accuracy per origin
    (is it right about its own entries, or only about easy flats?)."""
    n = len(labels)
    hits = 0
    by_label: Dict[str, Dict[str, int]] = {}
    by_origin: Dict[str, Dict[str, int]] = {}
    answer_mix: Dict[str, int] = {}
    unparseable = 0
    for i, (lbl, ans) in enumerate(zip(labels, answers)):
        want = direction_bucket(lbl)
        got = direction_bucket(ans)
        answer_mix[got] = answer_mix.get(got, 0) + 1
        d = by_label.setdefault(want, {"n": 0, "hit": 0})
        d["n"] += 1
        o = None
        if origins is not None and origins[i]:
            o = by_origin.setdefault(origins[i], {"n": 0, "hit": 0})
            o["n"] += 1
        if got == "unparseable":
            unparseable += 1
        if got == want:
            hits += 1
            d["hit"] += 1
            if o is not None:
                o["hit"] += 1
    out = {
        "n": n,
        "accuracy": round(hits / n, 4) if n else None,
        "unparseable": unparseable,
        "by_label": by_label,
        "answer_mix": answer_mix,
    }
    if by_origin:
        out["by_origin"] = by_origin
    return out


def baseline_scores(labels: List[str], *, n_draws: int = 1000,
                    seed: int = 1729) -> Dict[str, Any]:
    """What guessing scores on THIS exam — pure. "Beats the base" can
    hide behind class priors, so every exam reports:

      frequency_matched  a guesser drawing each answer from the exam's
                         own label frequencies; mean over `n_draws`
                         seeded draws with the 5th-95th percentile
                         band (closed-form expectation: Σ pᵢ²)
      always_hold        the share of decisions labeled HOLD
      majority_class     always answering the most common label
    """
    import random
    buckets = [direction_bucket(lbl) for lbl in labels]
    n = len(buckets)
    if not n:
        return {"n": 0}
    freq: Dict[str, int] = {}
    for b in buckets:
        freq[b] = freq.get(b, 0) + 1
    names = sorted(freq)
    weights = [freq[k] for k in names]
    rng = random.Random(seed)
    accs = []
    for _ in range(n_draws):
        guesses = rng.choices(names, weights=weights, k=n)
        accs.append(sum(g == b for g, b in zip(guesses, buckets)) / n)
    accs.sort()
    majority = max(names, key=lambda k: freq[k])
    return {
        "n": n,
        "label_frequencies": {k: round(freq[k] / n, 4) for k in names},
        "frequency_matched": {
            "mean": round(sum(accs) / n_draws, 4),
            "p05": round(accs[int(0.05 * (n_draws - 1))], 4),
            "p95": round(accs[int(0.95 * (n_draws - 1))], 4),
            "closed_form": round(sum((w / n) ** 2 for w in weights), 4),
            "draws": n_draws,
        },
        "always_hold": round(freq.get("hold", 0) / n, 4),
        "majority_class": {"label": majority,
                           "accuracy": round(freq[majority] / n, 4)},
    }


def _sign_test(wins: int, losses: int) -> float:
    """Exact two-sided sign-test p-value."""
    from math import comb
    m = wins + losses
    if m == 0:
        return 1.0
    k = min(wins, losses)
    return min(1.0, 2 * sum(comb(m, i) for i in range(k + 1)) / 2 ** m)


def paired_comparison(labels: List[str], base: List[Optional[str]],
                      adapter: List[Optional[str]],
                      clusters: Optional[List[Any]] = None
                      ) -> Dict[str, Any]:
    """Same decisions, two answerers: how many did ONLY the adapter get
    right vs ONLY the base, and the exact two-sided sign-test p-value
    that a split that lopsided is chance. Pure. Overall accuracy alone
    cannot tell a real win from noise (batch 2's 38.6% vs 37.3%).

    `clusters` (one key per decision — the exam passes stock + decision
    date) makes the test honest about INDEPENDENCE: twelve replicate
    profiles judge the same stock on the same day against the same
    outcome, so those rows are one piece of evidence, not twelve. Each
    cluster votes once — for whichever answerer got more of its
    decisions right — and `clustered_p_value` is the sign test over
    those votes. It is the number `promotion_bar` trusts."""
    only_adapter = only_base = 0
    per_cluster: Dict[Any, List[int]] = {}
    for i, (lbl, b, a) in enumerate(zip(labels, base, adapter)):
        want = direction_bucket(lbl)
        b_ok = direction_bucket(b) == want
        a_ok = direction_bucket(a) == want
        only_adapter += a_ok and not b_ok
        only_base += b_ok and not a_ok
        if clusters is not None:
            tally = per_cluster.setdefault(clusters[i], [0, 0])
            tally[0] += a_ok
            tally[1] += b_ok
    out = {"only_adapter_right": only_adapter,
           "only_base_right": only_base,
           "p_value": round(_sign_test(only_adapter, only_base), 5)}
    if clusters is not None:
        wins = sum(1 for a, b in per_cluster.values() if a > b)
        losses = sum(1 for a, b in per_cluster.values() if b > a)
        out.update({"independent_clusters": len(per_cluster),
                    "clusters_adapter_better": wins,
                    "clusters_base_better": losses,
                    "clustered_p_value": round(_sign_test(wins, losses),
                                               5)})
    return out


def promotion_bar(base: Dict[str, Any], adapter: Dict[str, Any],
                  baselines: Dict[str, Any],
                  paired: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Any]:
    """The bar a checkpoint must clear before hosting, a shadow seat or
    any spend is even discussed (docs/28 §4.4) — pure. ALL of:

      1. beats the untrained base on overall accuracy;
      2. beats the 95th percentile of frequency-matched guessing;
      3. is NOT worse than the base on BOTH directional classes (a
         model that wins overall by answering HOLD is batch 2 again).

    `clear_win` additionally requires the paired sign test — counted
    per independent stock-and-day, not per row — to put the win beyond
    chance (p < 0.05). Reported beside `passed` so the operator sees
    how solid a pass is."""
    reasons: List[str] = []
    a_acc, b_acc = adapter.get("accuracy"), base.get("accuracy")
    p95 = (baselines.get("frequency_matched") or {}).get("p95")
    if a_acc is None or b_acc is None or p95 is None:
        return {"passed": False, "clear_win": False,
                "reasons": ["exam is empty — nothing to judge"]}
    if not a_acc > b_acc:
        reasons.append(f"does not beat the untrained base "
                       f"({a_acc:.1%} vs {b_acc:.1%})")
    if not a_acc > p95:
        reasons.append(f"does not beat frequency-matched guessing "
                       f"({a_acc:.1%} vs 95th percentile {p95:.1%})")

    def _rate(score, bucket):
        d = (score.get("by_label") or {}).get(bucket)
        return (d["hit"] / d["n"]) if d and d["n"] else None
    worse = []
    for bucket in ("bullish", "bearish"):
        a, b = _rate(adapter, bucket), _rate(base, bucket)
        if a is not None and b is not None and a < b:
            worse.append(bucket)
    if len(worse) == 2:
        reasons.append("worse than the base on BOTH bullish and bearish "
                       "calls — an overall win here is a HOLD prior, "
                       "not discrimination")
    passed = not reasons
    # The clustered p-value when the exam supplied clusters (it always
    # does); the row-level one only as a fallback for callers without.
    p = 1.0
    if paired:
        p = paired.get("clustered_p_value", paired.get("p_value", 1.0))
    clear = bool(passed and p < 0.05)
    return {"passed": passed, "clear_win": clear, "reasons": reasons}


# --- checkpoints -----------------------------------------------------------

_CHECKPOINT_RE = re.compile(r"^(\d{7})_adapters\.safetensors$")
_VAL_LOSS_RE = re.compile(r"^Iter (\d+): Val loss ([0-9.]+|nan|inf)",
                          re.M | re.I)


def discover_checkpoints(adapter_dir: str) -> Dict[int, str]:
    """{step: path} for the numbered checkpoint files mlx-lm writes
    beside adapters.safetensors."""
    out: Dict[int, str] = {}
    for name in sorted(os.listdir(adapter_dir)):
        m = _CHECKPOINT_RE.match(name)
        if m:
            out[int(m.group(1))] = os.path.join(adapter_dir, name)
    return out


def parse_val_losses(log_text: str) -> Dict[int, float]:
    """{step: validation loss} from a trainer log. A NaN/inf loss is
    kept as inf so a blown-up checkpoint sorts last, never first."""
    out: Dict[int, float] = {}
    for step, loss in _VAL_LOSS_RE.findall(log_text):
        try:
            v = float(loss)
        except ValueError:
            v = float("inf")
        out[int(step)] = v if v == v else float("inf")
    return out


def select_checkpoints(checkpoints: Dict[int, str],
                       val_losses: Dict[int, float],
                       top_k: int = 3) -> List[int]:
    """Steps to examine: the `top_k` saved checkpoints with the lowest
    validation loss, plus the final one. Validation loss bottomed
    mid-run in every batch; examining only the last step threw the
    best model away."""
    if not checkpoints:
        return []
    scored = sorted((s for s in checkpoints if s in val_losses),
                    key=lambda s: (val_losses[s], s))
    chosen = scored[:top_k]
    final = max(checkpoints)
    if final not in chosen:
        chosen.append(final)
    return sorted(chosen)


def stage_checkpoint(adapter_dir: str, step: int, dest_root: str) -> str:
    """mlx-lm's loader reads `adapters.safetensors` + the adapter
    config from ONE directory, so each numbered checkpoint is staged
    into its own."""
    src = discover_checkpoints(adapter_dir)[step]
    dest = os.path.join(dest_root, f"step_{step:07d}")
    os.makedirs(dest, exist_ok=True)
    shutil.copyfile(src, os.path.join(dest, "adapters.safetensors"))
    shutil.copyfile(os.path.join(adapter_dir, "adapter_config.json"),
                    os.path.join(dest, "adapter_config.json"))
    return dest


def generation_cache_path(data_dir: str, eval_rows: List[Dict[str, Any]],
                          model: str, candidate: str, max_tokens: int
                          ) -> str:
    """Where one answerer's generations for one exam are kept — pure.

    The name carries a digest of everything that determines the
    answers' meaning (the exam's prompts, the base model, the
    generation cap), so a file is only ever reused for the SAME exam
    under the SAME settings; rebuild the corpus and the old answers
    are simply never looked at again. `candidate` is "base" or a
    checkpoint label that includes the training run."""
    import hashlib
    h = hashlib.sha1()
    for row in eval_rows:
        for m in row["messages"]:
            if m.get("role") != "assistant":
                h.update((m.get("content") or "").encode())
        h.update(b"\x00")
    h.update(f"|{model}|{max_tokens}".encode())
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", candidate)
    return os.path.join(data_dir, "generations",
                        f"{safe}__{h.hexdigest()[:12]}.jsonl")


def _load_generations(path: str) -> Dict[int, str]:
    """{prompt index: text} already on disk. A torn final line (the
    process died mid-write) is dropped; that prompt is regenerated."""
    done: Dict[int, str] = {}
    if not os.path.exists(path):
        return done
    with open(path) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
                done[int(rec["i"])] = rec["text"]
            except (ValueError, KeyError, TypeError):
                logger.warning("generation cache %s: dropped an "
                               "unreadable line (regenerating it)", path)
    return done


def _generate_answers(model_path: str, adapter: Optional[str],
                      eval_rows: List[Dict[str, Any]],
                      max_tokens: int,
                      cache_path: Optional[str] = None) -> List[str]:
    """Raw completions, one per eval row — parsing/scoring happens in
    the caller so the report can keep the generations for forensics
    (the first eval discarded them and 7 'unparseable' answers could
    not be diagnosed).

    RESUMABLE. An exam is ~half a day of GPU time, and macOS can kill
    a long Metal job at any moment ("Impacting Interactivity" ended
    batch 4's training run at step ~630). With `cache_path`, every
    answer is appended to disk the moment it is produced and a rerun
    generates only what is missing — so a crash costs one answer, not
    eleven hours, and the untrained base's answers are generated once
    per exam no matter how many sweeps follow."""
    done = _load_generations(cache_path) if cache_path else {}
    todo = [i for i in range(len(eval_rows)) if i not in done]
    if done:
        print(f"  resuming: {len(done)} answers already on disk, "
              f"{len(todo)} to generate")
    if todo:
        from mlx_lm import load, generate
        model, tokenizer = load(model_path, adapter_path=adapter)
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        for n, i in enumerate(todo, 1):
            msgs = [m for m in eval_rows[i]["messages"]
                    if m.get("role") != "assistant"]
            prompt = tokenizer.apply_chat_template(
                msgs, add_generation_prompt=True, tokenize=False)
            text = generate(model, tokenizer, prompt=prompt,
                            max_tokens=max_tokens, verbose=False)
            done[i] = text
            if cache_path:
                with open(cache_path, "a") as fh:
                    fh.write(json.dumps({"i": i, "text": text}) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            if n % 25 == 0:
                print(f"  {n}/{len(todo)} generated")
    return [done[i] for i in range(len(eval_rows))]


def cmd_eval(args) -> int:
    data_dir = Path(args.data)
    eval_path = data_dir / "eval.jsonl"
    meta_path = data_dir / "eval_meta.jsonl"
    if not eval_path.exists() or not meta_path.exists():
        print(f"ERROR: eval.jsonl/eval_meta.jsonl missing in {data_dir}")
        return 2
    def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for i, line in enumerate(path.read_text().splitlines()):
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, ValueError) as exc:
                # Fail LOUD and closed: a truncated/corrupt eval file
                # scored partially would misreport the model.
                raise SystemExit(
                    f"ERROR: {path}:{i + 1} is not valid JSON ({exc}) "
                    "— rebuild the corpus before evaluating.") from exc
        return rows

    eval_rows = _load_jsonl(eval_path)
    metas = _load_jsonl(meta_path)
    # Cycle-grouped metas carry a {symbol: label} map (2026-08-27);
    # legacy per-row metas carry a single symbol/label pair.
    label_maps: List[Dict[str, str]] = []
    origin_maps: List[Dict[str, str]] = []
    for m in metas:
        if isinstance(m.get("labels"), dict):
            label_maps.append(m["labels"])
        else:
            label_maps.append({str(m.get("symbol")): m.get("label", "?")})
        origin_maps.append(m.get("origins") or {})
    if args.limit:
        eval_rows = eval_rows[:args.limit]
        label_maps = label_maps[:args.limit]
        origin_maps = origin_maps[:args.limit]
    flat_labels = [lbl for lm in label_maps for lbl in lm.values()]
    flat_origins = [om.get(sym) for lm, om in zip(label_maps, origin_maps)
                    for sym in lm]
    # One cluster per (stock, decision date): replicate profiles judge
    # the same stock on the same day against the same outcome.
    flat_clusters = [(sym, str(m.get("timestamp") or "")[:10])
                     for lm, m in zip(label_maps, metas) for sym in lm]
    n_graded = len(flat_labels)
    print(f"{len(eval_rows)} eval prompts, {n_graded} graded decisions, "
          f"{len(set(flat_clusters))} distinct stock-days")

    # Which adapters sit the exam: one directory (--adapter alone), or
    # a sweep of that run's checkpoints (--sweep / --steps).
    candidates: List[Tuple[str, Optional[str]]] = []
    staged_root: Optional[str] = None
    selection: Dict[str, Any] = {}
    if args.adapter and (args.sweep or args.steps):
        checkpoints = discover_checkpoints(args.adapter)
        log_path = Path(args.adapter) / "train.log"
        val_losses = (parse_val_losses(log_path.read_text())
                      if log_path.exists() else {})
        if args.steps:
            steps = sorted({int(s) for s in args.steps.split(",")})
            missing = [s for s in steps if s not in checkpoints]
            if missing:
                print(f"ERROR: no checkpoint file for step(s) {missing} "
                      f"in {args.adapter}")
                return 2
        else:
            if not val_losses:
                print(f"ERROR: --sweep needs {log_path} to rank "
                      "checkpoints by validation loss; pass --steps to "
                      "name them instead.")
                return 2
            steps = select_checkpoints(checkpoints, val_losses,
                                       args.sweep_top)
        if not steps:
            print(f"ERROR: no numbered checkpoints in {args.adapter}")
            return 2
        import tempfile
        staged_root = tempfile.mkdtemp(prefix="qo_ckpt_")
        for s in steps:
            candidates.append((f"step_{s}",
                               stage_checkpoint(args.adapter, s,
                                                staged_root)))
        selection = {"steps": steps,
                     "val_losses": {str(s): val_losses.get(s)
                                    for s in steps},
                     "lowest_val_loss_step": (
                         min((s for s in steps if s in val_losses),
                             key=lambda s: val_losses[s], default=None))}
        print(f"sweeping checkpoints {steps} (val loss: "
              f"{selection['val_losses']})")
    elif args.adapter:
        candidates.append(("adapter", args.adapter))

    baselines = baseline_scores(flat_labels)
    report: Dict[str, Any] = {
        "model": args.model, "adapter": args.adapter,
        "data": str(data_dir), "limit": args.limit,
        "eval_prompts": len(eval_rows), "graded_decisions": n_graded,
        "distinct_stock_days": len(set(flat_clusters)),
        "baselines": baselines, "checkpoint_selection": selection,
        "candidates": {},
    }
    print("baselines", json.dumps(baselines, indent=2))

    # Answers are kept per (exam, model, answerer): the base's under
    # "base", a checkpoint's under its step AND its training run, so
    # two runs' step-500 checkpoints never share a file.
    run_tag = (os.path.basename(os.path.normpath(args.adapter))
               if args.adapter else "")

    def _sit_exam(name: str, adapter: Optional[str]) -> List[Optional[str]]:
        print(f"generating with {name} …")
        label = "base" if name == "base" else f"{name}@{run_tag}"
        texts = _generate_answers(
            args.model, adapter, eval_rows, args.max_tokens,
            cache_path=generation_cache_path(
                str(data_dir), eval_rows, args.model, label,
                args.max_tokens))
        answers: List[Optional[str]] = []
        gens: List[Dict[str, Any]] = []
        for text, lmap in zip(texts, label_maps):
            parsed = {sym: parse_decision(text, sym) for sym in lmap}
            answers.extend(parsed.values())
            gens.append({"labels": lmap, "parsed": parsed, "text": text})
        score = score_examples(flat_labels, answers, flat_origins)
        score["generations"] = gens
        if name == "base":
            report["base"] = score
        else:
            report["candidates"][name] = score
        print(name, json.dumps({k: v for k, v in score.items()
                                if k != "generations"}, indent=2))
        return answers

    try:
        # The base answers ONCE; every checkpoint is compared to the
        # same generations.
        base_answers = _sit_exam("base", None)
        for name, path in candidates:
            answers = _sit_exam(name, path)
            score = report["candidates"][name]
            score["paired_vs_base"] = paired_comparison(
                flat_labels, base_answers, answers, flat_clusters)
            score["promotion_bar"] = promotion_bar(
                report["base"], score, baselines, score["paired_vs_base"])
            print(name, "promotion bar:", json.dumps(
                score["promotion_bar"]), json.dumps(score["paired_vs_base"]))
    finally:
        if staged_root:
            shutil.rmtree(staged_root, ignore_errors=True)

    if report["candidates"]:
        winner = max(report["candidates"],
                     key=lambda k: report["candidates"][k]["accuracy"] or 0)
        report["winner"] = {
            "name": winner,
            "accuracy": report["candidates"][winner]["accuracy"],
            "promotion_bar": report["candidates"][winner]["promotion_bar"],
            # Picking the best of several checkpoints ON the exam
            # flatters the winner a little; the lowest-validation-loss
            # step is the choice made without looking at the exam.
            "chosen_on_exam_from": len(report["candidates"]),
        }
        print("winner:", json.dumps(report["winner"], indent=2))
    out = data_dir / f"eval_report_{_stamp()}.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"report: {out}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build-corpus")
    b.add_argument("--eval-holdout", type=int, default=200)
    # Outcomes take 5-8 days to resolve, so "the 200 most recent
    # resolved cycles" is ONE trading day. The exam is sampled evenly
    # across the last three decision dates instead.
    b.add_argument("--eval-days", type=int, default=3)
    # The validation block is the slice of time just before the exam
    # period, and training is purged a full outcome horizon before it
    # — so every validation cycle costs training data. 1% is ~160
    # cycles (~250 examples once split), comfortably more than the 100
    # a validation pass scores.
    b.add_argument("--val-fraction", type=float, default=0.01)
    b.add_argument("--model", default=DEFAULT_MODEL,
                   help="whose tokenizer measures example lengths")
    b.add_argument("--max-tokens", type=int,
                   default=DEFAULT_MAX_SEQ_LENGTH,
                   help="training window; nothing longer is written")
    b.add_argument("--keep-unlabeled", action="store_true",
                   help="leave candidates with no known answer in the "
                        "train/val prompts (teaches them as 'no trade' "
                        "by omission — off by default)")
    b.add_argument("--no-rebalance", action="store_true",
                   help="leave the train split at its natural mix")
    b.add_argument("--max-empty-fraction", type=float, default=0.10,
                   help="cap on train examples whose answer is 'no "
                        "trades' (natural: ~21%%)")
    b.add_argument("--direction-ratio", type=float, default=1.25,
                   help="largest allowed BUY:SHORT example imbalance")
    b.add_argument("--missed-move-cap", type=float, default=None,
                   help="cap the share of directional labels that are "
                        "hindsight 'missed moves' (default: off)")
    b.set_defaults(fn=cmd_build_corpus)

    t = sub.add_parser("train")
    t.add_argument("--data", required=True)
    t.add_argument("--model", default=DEFAULT_MODEL)
    t.add_argument("--iters", type=int, default=1200)
    # 1 is what every batch has actually run at (each passed it by
    # flag; peak memory 23.8GB of 64GB at 8K context).
    t.add_argument("--batch-size", type=int, default=1)
    t.add_argument("--num-layers", type=int, default=16)
    t.add_argument("--max-seq-length", type=int,
                   default=DEFAULT_MAX_SEQ_LENGTH)
    t.add_argument("--learning-rate", type=float, default=1e-5,
                   help="peak of the warmup + cosine schedule")
    t.add_argument("--end-lr", type=float, default=1e-7)
    t.add_argument("--warmup-fraction", type=float, default=0.05)
    t.add_argument("--val-batches", type=int, default=100)
    t.add_argument("--resume-adapter", default=None)
    t.set_defaults(fn=cmd_train)

    e = sub.add_parser("eval")
    e.add_argument("--data", required=True)
    e.add_argument("--model", default=DEFAULT_MODEL)
    e.add_argument("--adapter", default=None,
                   help="a training run's adapter directory")
    e.add_argument("--sweep", action="store_true",
                   help="examine that run's best checkpoints by "
                        "validation loss, plus the final one")
    e.add_argument("--sweep-top", type=int, default=3)
    e.add_argument("--steps", default=None,
                   help="comma-separated checkpoint steps to examine "
                        "instead of ranking by validation loss")
    e.add_argument("--limit", type=int, default=0)
    # Batch answers run long: ~40 candidates × ~40 tokens each. 300
    # truncated the base model's JSON mid-list in batch 1 and degraded
    # its parsing to token-scan noise.
    e.add_argument("--max-tokens", type=int, default=2000)
    e.set_defaults(fn=cmd_eval)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
