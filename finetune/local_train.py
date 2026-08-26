"""Local LoRA training driver — docs/20 §16.1 Phase 4b.2, activated by
docs/25 step 4.5.

Runs on the operator's M2 Max, NEVER on prod. Three subcommands form
the weekly batch loop (train locally in batches so the artifact is
ours and vendor-independent — operator decision 2026-08-23):

    build-corpus   dataset_builder over a local snapshot of the prod
                   archive (+ optional profile-journal copies) →
                   train/valid/eval chat JSONL (mlx-lm layout).
    train          mlx_lm LoRA fine-tune of an open-weights base on
                   that corpus; resumable batch-over-batch via
                   --resume-adapter.
    eval           self-scored held-out eval: the adapter (and,
                   separately, the bare base model) answer the exact
                   prompts the production AI saw; answers are parsed
                   with the production trade-dict expectations and
                   scored against the hindsight labels. The adapter
                   earns promotion consideration ONLY by beating the
                   base here — same evidence-first doctrine as every
                   other arm (docs/20 §7).

Base model: any mlx-compatible instruct model. Default is
Qwen2.5-7B-Instruct because its weights are UNGATED (a run needs no
account or token). Llama-3.1-8B-Instruct (the docs/20 §16.1 table's
first row) is gated behind a HuggingFace license acceptance — pass
--model meta-llama/Llama-3.1-8B-Instruct once the operator's HF token
is configured (`huggingface-cli login`). The corpus, driver, and eval
are identical either way — the base is a config value, not a design
commitment.

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
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_WORKDIR = os.path.expanduser("~/Quantops-finetune")
DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"  # ungated; see module doc
_BULLISH = frozenset({"BUY", "STRONG_BUY", "WEAK_BUY"})
_BEARISH = frozenset({"SHORT", "STRONG_SELL", "SELL"})


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------
# build-corpus
# ---------------------------------------------------------------------------

def cmd_build_corpus(args) -> int:
    from finetune.dataset_builder import build_dataset
    workdir = Path(args.workdir)
    out_dir = workdir / "data" / _stamp()
    profile_dbs: List[str] = []
    snap = workdir / "corpus" / "profile_dbs"
    if snap.is_dir():
        profile_dbs = [str(p) for p in sorted(snap.glob("*.db"))]
    archive = workdir / "corpus" / "predictions_archive"
    if not archive.is_dir():
        print(f"ERROR: no archive snapshot at {archive} — rsync "
              "backups/predictions_archive from prod first.")
        return 2
    manifest = build_dataset(
        profile_dbs, str(out_dir),
        archive_root=str(archive),
        eval_holdout=args.eval_holdout,
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
                        max_seq_length: int = 8192) -> List[str]:
    """The exact mlx_lm.lora invocation — pure so tests can pin it.

    max_seq_length matters: the production prompts run ~9-10K tokens
    and mlx-lm's 2048 default would silently truncate away the
    candidate table the decision depends on."""
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
    ]
    if resume_adapter:
        cmd += ["--resume-adapter-file",
                os.path.join(resume_adapter, "adapters.safetensors")]
    return cmd


def cmd_train(args) -> int:
    workdir = Path(args.workdir)
    data_dir = Path(args.data)
    if not (data_dir / "train.jsonl").exists():
        print(f"ERROR: {data_dir}/train.jsonl missing — run build-corpus.")
        return 2
    adapter_path = str(workdir / "adapters" / _stamp())
    cmd = build_train_command(
        sys.executable, args.model, str(data_dir), adapter_path,
        args.iters, args.batch_size, args.num_layers,
        args.resume_adapter, max_seq_length=args.max_seq_length,
    )
    print("running:", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"adapter: {adapter_path}")
        (Path(adapter_path) / "train_run.json").write_text(json.dumps({
            "model": args.model, "data": str(data_dir),
            "iters": args.iters, "batch_size": args.batch_size,
            "resume_adapter": args.resume_adapter,
            "finished_utc": _stamp(),
        }, indent=2))
    return rc


# ---------------------------------------------------------------------------
# eval — self-scored, adapter vs bare base
# ---------------------------------------------------------------------------

def parse_decision(text: str) -> Optional[str]:
    """Extract the decision action from a generated completion, using
    the production expectation (a JSON object with trades[0].action).
    Falls back to a bare action token scan. None = unparseable (scored
    as wrong — an unparseable answer is a wrong answer live too)."""
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
                        trades = obj.get("trades") or []
                        if trades and isinstance(trades, list):
                            act = str(
                                (trades[0] or {}).get("action", "")
                            ).upper().strip()
                            return act or None
                        return None
                    except (json.JSONDecodeError, AttributeError):
                        break
    for token in ("STRONG_BUY", "WEAK_BUY", "STRONG_SELL", "BUY",
                  "SELL", "SHORT", "HOLD"):
        if token in s.upper():
            return token
    return None


def direction_bucket(action: Optional[str]) -> str:
    if action in _BULLISH:
        return "bullish"
    if action in _BEARISH:
        return "bearish"
    if action == "HOLD":
        return "hold"
    return "unparseable"


def score_examples(labels: List[str], answers: List[Optional[str]]
                   ) -> Dict[str, Any]:
    """Directional accuracy of parsed answers vs hindsight labels —
    pure, so tests can pin the scoring."""
    n = len(labels)
    hits = 0
    by_label: Dict[str, Dict[str, int]] = {}
    unparseable = 0
    for lbl, ans in zip(labels, answers):
        want = direction_bucket(lbl)
        got = direction_bucket(ans)
        d = by_label.setdefault(want, {"n": 0, "hit": 0})
        d["n"] += 1
        if got == "unparseable":
            unparseable += 1
        if got == want:
            hits += 1
            d["hit"] += 1
    return {
        "n": n,
        "accuracy": round(hits / n, 4) if n else None,
        "unparseable": unparseable,
        "by_label": by_label,
    }


def _generate_answers(model_path: str, adapter: Optional[str],
                      eval_rows: List[Dict[str, Any]],
                      max_tokens: int) -> List[Optional[str]]:
    from mlx_lm import load, generate
    model, tokenizer = load(model_path, adapter_path=adapter)
    answers: List[Optional[str]] = []
    for i, row in enumerate(eval_rows):
        msgs = [m for m in row["messages"] if m.get("role") != "assistant"]
        prompt = tokenizer.apply_chat_template(
            msgs, add_generation_prompt=True, tokenize=False)
        text = generate(model, tokenizer, prompt=prompt,
                        max_tokens=max_tokens, verbose=False)
        answers.append(parse_decision(text))
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(eval_rows)} generated")
    return answers


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
    labels = [r.get("label", "?") for r in _load_jsonl(meta_path)]
    if args.limit:
        eval_rows = eval_rows[:args.limit]
        labels = labels[:args.limit]
    report: Dict[str, Any] = {
        "model": args.model, "adapter": args.adapter,
        "data": str(data_dir), "limit": args.limit,
    }
    for name, adapter in (("base", None), ("adapter", args.adapter)):
        if name == "adapter" and not args.adapter:
            continue
        print(f"generating with {name} …")
        answers = _generate_answers(args.model, adapter, eval_rows,
                                    args.max_tokens)
        report[name] = score_examples(labels, answers)
        print(name, json.dumps(report[name], indent=2))
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
    b.set_defaults(fn=cmd_build_corpus)

    t = sub.add_parser("train")
    t.add_argument("--data", required=True)
    t.add_argument("--model", default=DEFAULT_MODEL)
    t.add_argument("--iters", type=int, default=600)
    t.add_argument("--batch-size", type=int, default=2)
    t.add_argument("--num-layers", type=int, default=16)
    t.add_argument("--max-seq-length", type=int, default=8192)
    t.add_argument("--resume-adapter", default=None)
    t.set_defaults(fn=cmd_train)

    e = sub.add_parser("eval")
    e.add_argument("--data", required=True)
    e.add_argument("--model", default=DEFAULT_MODEL)
    e.add_argument("--adapter", default=None)
    e.add_argument("--limit", type=int, default=0)
    e.add_argument("--max-tokens", type=int, default=300)
    e.set_defaults(fn=cmd_eval)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
