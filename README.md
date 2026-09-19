# QuantOpsAI

QuantOpsAI is an AI-first autonomous trading platform. It runs a multi-strategy paper-trading book on Alpaca with a Claude / GPT / Gemini AI in the portfolio-manager seat. The system surfaces every candidate's full feature context — technicals, alternative data, options state, factor exposures, portfolio-level risk, and its own track record — to the AI on every cycle, captures every decision and resolves it against price action, and feeds the resolved outcomes back into a two-layer meta-model (GBM batch + SGD freshness), a **two-layer calibrated specialist ensemble** (179 deterministic rule-checkers + 8 LLM-narrative specialists, six enabled), a self-tuning stack (12 original layers, cut to ten evidence-backed levers for Experiment 2, + 5 deterministic guardrails), a shadow-evaluation layer that A/B-tests competing models on identical prompts, and a Barra-style portfolio risk model. The platform runs **12 profiles in parallel inside 3 Alpaca paper accounts** (Experiment 2: four model arms × three replicates, profiles 229–240) via a virtual-account reconciliation layer, and is wired with guardrail tests that prevent hidden levers, untracked features, and untested code from shipping. The deterministic-vs-narrative architecture is the cost story: hundreds of zero-API-cost rule checkers handle structurally-checkable patterns so the single batched LLM call only spends tokens on synthesis — observed operational AI spend is ≈ $1.86/day primary + ≈ $1.70/day shadow evaluation across the twelve-profile fleet (measured 2026-09-12→18 on the four Experiment-2 arms: `gpt-4.1-nano`, `gpt-5.6-luna`, `gemini-3.5-flash-lite`, `gemini-3.7-flash`).

## Documentation

Read in this order — each doc is written for a specific audience.

| Doc | Audience | Read this when… |
|---|---|---|
| [`docs/01_EXECUTIVE_SUMMARY.md`](docs/01_EXECUTIVE_SUMMARY.md) | Investors, executives, anyone non-technical | …you want to understand what this is, why it might be valuable, and what the honest risks are. |
| [`docs/02_AI_SYSTEM.md`](docs/02_AI_SYSTEM.md) | Quants, ML researchers, anyone who builds prediction systems | …you want a peer-quality description of the meta-model, ensemble, calibration, online learning, and self-tuning. |
| [`docs/03_TRADING_STRATEGY.md`](docs/03_TRADING_STRATEGY.md) | Finance professionals, strategy researchers | …you want to know what it actually trades, how it sizes, and how it manages risk. |
| [`docs/04_TECHNICAL_REFERENCE.md`](docs/04_TECHNICAL_REFERENCE.md) | Software engineers | …you need to understand the system architecture, modules, schema, and deploy flow. |
| [`docs/05_DATA_DICTIONARY.md`](docs/05_DATA_DICTIONARY.md) | Quants and engineers | …you need the canonical reference for every column, signal, feature, and tunable knob. |
| [`docs/06_USER_GUIDE.md`](docs/06_USER_GUIDE.md) | End users, operators | …you're using the platform and need to know what every setting does. |
| [`docs/07_OPERATIONS.md`](docs/07_OPERATIONS.md) | SRE, ops engineers | …you're running it on infrastructure and need monitoring, deployment, and incident response. |
| [`docs/08_RISK_CONTROLS.md`](docs/08_RISK_CONTROLS.md) | Risk and compliance | …you need to enumerate every kill switch, gate, and safety override. |
| [`docs/09_GLOSSARY.md`](docs/09_GLOSSARY.md) | Cross-audience | …you encounter a domain term in any of the other docs. |
| [`docs/10_METHODOLOGY.md`](docs/10_METHODOLOGY.md) | Anyone extending or reviewing the system | …you want to understand how decisions are made, not just what was built. |
| [`docs/11_INTEGRATION_GUIDE.md`](docs/11_INTEGRATION_GUIDE.md) | Developers adding new strategies, signals, or specialists | …you're extending the platform. |
| [`docs/12_SCALING_AND_GRADUATION.md`](docs/12_SCALING_AND_GRADUATION.md) | Operators planning capital deployment | …you want to know what changes at $10K, $50K, $250K, $1M+. |
| [`docs/24_SPECIALIST_CATALOG.md`](docs/24_SPECIALIST_CATALOG.md) | Quants, financial analysts, VC reviewers | …you want the canonical enumeration of all 187 specialists (8 LLM + 179 deterministic) with what each one checks. The value-prop story made concrete. |
| [`docs/17_SELF_TUNER_GUARDRAILS_AND_RAG.md`](docs/17_SELF_TUNER_GUARDRAILS_AND_RAG.md) | Quants, engineers | …you want the self-tuner's guardrails, evidence mode, the case-file RAG layer, and the status of prompt-variant and fine-tune work. |
| [`docs/25_MODEL_SELECTION_AND_LEARNING_PLAN.md`](docs/25_MODEL_SELECTION_AND_LEARNING_PLAN.md) | Operator, anyone judging the learning claims | …you want the plan, decisions and progress log behind Experiment 2: which model should run the book, and is the system actually learning. |
| [`docs/26_EXPERIMENTS.md`](docs/26_EXPERIMENTS.md) | Everyone quoting a result | …you need the experiments register — what each experiment asked, how it ran, what it taught, and the measurement-validity notes to read before citing any number. |
| [`docs/27_FINETUNE_TRAINING_LOG.md`](docs/27_FINETUNE_TRAINING_LOG.md) | Operator, ML engineers | …you want the batch-by-batch record of training the owned model, and when the next batch should run. |
| [`docs/28_FINETUNE_BATCH4_BUILD_SPEC.md`](docs/28_FINETUNE_BATCH4_BUILD_SPEC.md) | Whoever builds batch 4 | …you are picking up the fine-tune work: the five mandatory changes, where the code lives, the tests, and the Mac runbook. (`docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md` is the original design.) |
| [`CHANGELOG.md`](CHANGELOG.md) | Everyone | Chronological history of every behavior change. |
| [`OPEN_ITEMS.md`](OPEN_ITEMS.md) | Everyone | Single source of truth for what is still pending. |
| [`docs/archive/`](docs/archive/) | Archaeology | Pre-rewrite documentation. Frozen for traceability. |

## Quick starts by role

- **Curious investor / non-technical reader:** start with `01_EXECUTIVE_SUMMARY.md`, glance at `08_RISK_CONTROLS.md`.
- **Quant researcher evaluating the methodology:** read `10_METHODOLOGY.md`, then `02_AI_SYSTEM.md`, then `03_TRADING_STRATEGY.md`. The data dictionary (`05`) is the reference you keep open while reading.
- **Engineer joining the project:** read `04_TECHNICAL_REFERENCE.md`, `07_OPERATIONS.md`, then `11_INTEGRATION_GUIDE.md` before changing anything.
- **End user setting up profiles:** start with `06_USER_GUIDE.md`. Reference `08_RISK_CONTROLS.md` to understand what each safety toggle does.

## Status

- **Mode:** paper trading on Alpaca, three accounts virtualized into 12 profiles (229–240) via the FIFO journal layer.
- **Capital:** $3M total virtual ($1M per Alpaca paper-account cap × 3 accounts), a flat $250K per profile — four model arms × three replicates, one replicate of each arm per account — per `docs/26_EXPERIMENTS.md`. The controls (Buy-Hold-SPY and ten random books) are broker-free virtual benchmarks, not profiles. (Experiment 1's baseline + ablation + capital-scaling design, `docs/15_EXPERIMENT_DESIGN_2026_05_17.md`, was retired 2026-08-23.)
- **Test suite:** 7,036 tests, zero skipped, zero failed (574 test files; ~11 min on the droplet with temp files on tmpfs — see `DROPLET_DEV.md`). Zero-fail / zero-skip is a merge gate, not an aspiration.
- **Guardrails:** snake_case leakage, hidden-lever, scheduled-feature-toggle, meta-feature UI coverage, schema migration safety, no silent except: pass, no unguarded json.loads, every option submit passes position_intent, every mutating endpoint admin-required.
- **Deploy:** `./sync.sh` from the Mac or `./droplet-sync.sh` on the droplet — the same stages and pre-flight gate (clean tree, pushed HEAD, content-sha verification, restart decision from the changed set); both self-detach and log to `deploy_logs/`. See `DROPLET_DEV.md`.

## License & ownership

Personal project of MacKenzie Smith (`mack@mackenziesmith.com`). Not currently licensed for redistribution.
