"""Phase 4B1 — incremental fine-tune pipeline.

Builds a training corpus from the system's own resolved predictions,
submits weekly incremental fine-tune jobs, tracks the model registry,
and routes inference per-profile behind a soak-then-promote flag.

Full design: docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md.

Build order (data-independent pieces first, since the first
meaningful training run is gated on ~4-8 weeks of post-B1 data
accumulation — see docs/20 §17):
  1. dataset_builder.py — corpus builder + hindsight relabel +
     look-ahead-bias guard. THE high-stakes piece. (this commit)
  2. model_registry.py — finetune_models table CRUD. (this commit)
  3. training_runner.py / job_monitor.py / evaluator.py /
     inference.py — vendor integration, built once a vendor path
     (4b.1 OpenAI vs 4b.2 local LoRA) is chosen.
"""
