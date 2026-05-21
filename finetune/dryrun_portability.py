#!/usr/bin/env python3
"""Phase 4B1 portability dry run (docs/20 §18) — PROVE the local-train
path produces a model that runs ANYWHERE, before investing in a real
corpus.

What it proves, end to end:
  1. Train a tiny LoRA on the M2 Max via HuggingFace PEFT + MPS,
     using the dataset_builder's EXACT JSONL shape.
  2. Merge the adapter into the base and save as HF safetensors.
  3. RELOAD the merged model on a FRESH code path on plain CPU
     (i.e. as if on a different machine / a hosted endpoint) and run
     inference.

If step 3 works, the artifact is not welded to your laptop or to
MPS — it's a standard HF model that vLLM / Together / Fireworks /
HF Endpoints / Ollama (after GGUF convert) can all serve. That's the
operator's lock-in concern, retired.

This is an OPERATOR-RUN script, not part of the pytest suite — it
needs heavy deps + a model download + Apple Silicon. Run it once.

──────────────────────────────────────────────────────────────────
RUNBOOK (on the M2 Max)
──────────────────────────────────────────────────────────────────
  # 1. Fresh venv (keep it separate from the prod venv)
  python3 -m venv ~/ft-dryrun-venv
  source ~/ft-dryrun-venv/bin/activate

  # 2. Deps. torch ships with MPS support on Apple Silicon wheels.
  pip install --upgrade pip
  pip install "torch>=2.2" transformers peft datasets accelerate safetensors

  # 3. Run the dry run (downloads ~1GB base model the first time)
  python finetune/dryrun_portability.py

  # Expected tail:
  #   [3/3] reload on CPU + infer ...
  #   PORTABILITY PROOF: PASS
  #   merged model is HF safetensors at /tmp/ft_dryrun_xxx/merged
  #   → runs on plain CPU transformers (not tied to MPS/this machine)

Base model: Qwen2.5-0.5B-Instruct — open weights, no HF gate, ~1GB,
fast. It's a STAND-IN to prove the chain; the real run uses an 8B
(Llama-3.1-8B-Instruct / Qwen-2.5-7B-Instruct). The portability
mechanics are identical regardless of size.

If you'd rather prove it on the real target size, set
  QUANTOPS_DRYRUN_MODEL="Qwen/Qwen2.5-7B-Instruct"
(needs ~20GB; fits in 64GB; slower download + train).
"""
from __future__ import annotations

import glob
import json
import os
import sys
import tempfile


BASE_MODEL = os.environ.get(
    "QUANTOPS_DRYRUN_MODEL", "Qwen/Qwen2.5-0.5B-Instruct",
)


def _require(mod: str):
    try:
        return __import__(mod)
    except ImportError:
        sys.exit(
            f"Missing dependency '{mod}'. See the RUNBOOK at the top of "
            f"this file:\n  pip install \"torch>=2.2\" transformers peft "
            f"datasets accelerate safetensors"
        )


def make_synthetic_corpus(path: str, n: int = 20) -> None:
    """Write n examples in the dataset_builder JSONL shape so we prove
    the REAL corpus format trains, not a toy format."""
    actions = ["BUY", "SHORT", "HOLD"]
    with open(path, "w") as fh:
        for i in range(n):
            act = actions[i % 3]
            ex = {
                "messages": [
                    {"role": "system",
                     "content": "You are the apex portfolio-manager AI "
                                "for an automated trading system. Decide "
                                "the candidate's action."},
                    {"role": "user",
                     "content": f"PORTFOLIO STATE:\n  Equity: $100,000\n"
                                f"CANDIDATE: SYM{i} price ${100 + i}, "
                                f"RSI {30 + i}, regime bull.\n"
                                f"Respond with the trade decision JSON."},
                    {"role": "assistant",
                     "content": json.dumps({"trades": [
                         {"symbol": f"SYM{i}", "action": act,
                          "size_pct": 0 if act == "HOLD" else 5.0}]})},
                ]
            }
            fh.write(json.dumps(ex) + "\n")


def train_lora(corpus_path: str, adapter_out: str):
    torch = _require("torch")
    _require("transformers")
    _require("peft")
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import LoraConfig, get_peft_model

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"[1/3] train LoRA on device={device}, base={BASE_MODEL}")
    if device == "cpu":
        print("    (MPS not available — running on CPU. On the M2 Max "
              "this should say 'mps'. CPU still proves the chain.)")

    tok = AutoTokenizer.from_pretrained(BASE_MODEL)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    # float32 on MPS avoids the float16 op-coverage gaps; the model is
    # tiny so memory is a non-issue for the dry run.
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, torch_dtype=torch.float32,
    ).to(device)

    cfg = LoraConfig(
        r=8, lora_alpha=16, lora_dropout=0.0,
        target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, cfg)
    model.train()
    opt = torch.optim.AdamW(
        (p for p in model.parameters() if p.requires_grad), lr=1e-4)

    examples = [json.loads(l) for l in open(corpus_path)]
    steps = 12
    for step in range(steps):
        ex = examples[step % len(examples)]
        text = tok.apply_chat_template(ex["messages"], tokenize=False)
        enc = tok(text, return_tensors="pt", truncation=True,
                  max_length=512).to(device)
        out = model(input_ids=enc["input_ids"],
                    attention_mask=enc["attention_mask"],
                    labels=enc["input_ids"])
        out.loss.backward()
        opt.step()
        opt.zero_grad()
        if step % 4 == 0:
            print(f"    step {step}/{steps} loss={out.loss.item():.4f}")

    # PEFT adapter saved as safetensors (default safe_serialization)
    model.save_pretrained(adapter_out)
    tok.save_pretrained(adapter_out)
    print(f"    adapter saved → {adapter_out}")


def merge_and_save(adapter_out: str, merged_out: str) -> None:
    torch = _require("torch")
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel

    print("[2/3] merge adapter → base, export HF safetensors")
    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, torch_dtype=torch.float32)
    merged = PeftModel.from_pretrained(base, adapter_out).merge_and_unload()
    # safe_serialization=True → *.safetensors, NOT pickle .bin
    merged.save_pretrained(merged_out, safe_serialization=True)
    AutoTokenizer.from_pretrained(adapter_out).save_pretrained(merged_out)
    sft = glob.glob(os.path.join(merged_out, "*.safetensors"))
    print(f"    merged saved → {merged_out}")
    print(f"    safetensors files: {[os.path.basename(p) for p in sft]}")
    if not sft:
        sys.exit("FAIL: merged model has no *.safetensors — not portable.")


def reload_and_infer(merged_out: str) -> str:
    """The portability proof: load the merged model on a FRESH path on
    plain CPU (no MPS, no PEFT, as a hosted endpoint would) and run a
    generation. If this works, the artifact isn't tied to this
    machine."""
    _require("torch")
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print("[3/3] reload on CPU + infer (proves not tied to MPS/this box)")
    t2 = AutoTokenizer.from_pretrained(merged_out)
    m2 = AutoModelForCausalLM.from_pretrained(merged_out)  # default = CPU
    msgs = [
        {"role": "system", "content": "You are the apex portfolio-manager AI."},
        {"role": "user", "content": "PORTFOLIO STATE:\n  Equity: $100,000\n"
                                     "CANDIDATE: SYM1 price $101, RSI 31, "
                                     "regime bull.\nRespond with the trade "
                                     "decision JSON."},
    ]
    text = t2.apply_chat_template(msgs, tokenize=False,
                                  add_generation_prompt=True)
    enc = t2(text, return_tensors="pt")
    out = m2.generate(**enc, max_new_tokens=40, do_sample=False)
    return t2.decode(out[0][enc["input_ids"].shape[1]:],
                     skip_special_tokens=True)


def main() -> int:
    work = tempfile.mkdtemp(prefix="ft_dryrun_")
    corpus = os.path.join(work, "corpus.jsonl")
    adapter = os.path.join(work, "adapter")
    merged = os.path.join(work, "merged")

    print(f"=== Phase 4B1 portability dry run (work dir: {work}) ===\n")
    make_synthetic_corpus(corpus, n=20)
    train_lora(corpus, adapter)
    merge_and_save(adapter, merged)
    sample = reload_and_infer(merged)

    print()
    print("    sample generation:", repr(sample[:120]))
    print()
    print("PORTABILITY PROOF: PASS")
    print(f"  merged model is HF safetensors at {merged}")
    print("  → trained on MPS (or CPU), reloaded on plain CPU "
          "transformers")
    print("  → this artifact runs on vLLM / Together / Fireworks / HF "
          "Endpoints; convert to GGUF for Ollama. Not welded to this "
          "machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
