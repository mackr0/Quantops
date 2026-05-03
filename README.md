# QuantOpsAI

QuantOpsAI is an AI-first autonomous trading platform. It runs a multi-strategy paper-trading book on Alpaca with a Claude / GPT / Gemini AI in the portfolio-manager seat. The system surfaces every candidate's full feature context — technicals, alternative data, options state, factor exposures, portfolio-level risk, and its own track record — to the AI on every cycle, captures every decision and resolves it against price action, and feeds the resolved outcomes back into a two-layer meta-model, a five-specialist calibrated ensemble, a twelve-layer self-tuning stack, and a Barra-style portfolio risk model. The platform tests ten or more strategies in parallel inside three free Alpaca paper accounts via a virtual-account reconciliation layer, and is wired with guardrail tests that prevent hidden levers, untracked features, and untested code from shipping.

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
| [`CHANGELOG.md`](CHANGELOG.md) | Everyone | Chronological history of every behavior change. |
| [`OPEN_ITEMS.md`](OPEN_ITEMS.md) | Everyone | Single source of truth for what is still pending. |
| [`docs/archive/`](docs/archive/) | Archaeology | Pre-rewrite documentation. Frozen for traceability. |

## Quick starts by role

- **Curious investor / non-technical reader:** start with `01_EXECUTIVE_SUMMARY.md`, glance at `08_RISK_CONTROLS.md`.
- **Quant researcher evaluating the methodology:** read `10_METHODOLOGY.md`, then `02_AI_SYSTEM.md`, then `03_TRADING_STRATEGY.md`. The data dictionary (`05`) is the reference you keep open while reading.
- **Engineer joining the project:** read `04_TECHNICAL_REFERENCE.md`, `07_OPERATIONS.md`, then `11_INTEGRATION_GUIDE.md` before changing anything.
- **End user setting up profiles:** start with `06_USER_GUIDE.md`. Reference `08_RISK_CONTROLS.md` to understand what each safety toggle does.

## Status

- **Mode:** paper trading on Alpaca, three live accounts virtualized into ten or more profiles.
- **Capital:** simulated $10K per profile (configurable per virtual account).
- **Test suite:** 1,914 tests, zero skipped.
- **Guardrails:** snake_case leakage, hidden-lever, scheduled-feature-toggle, meta-feature UI coverage, schema migration safety.
- **Deploy:** continuous deployment via `sync.sh` to a single droplet.

## License & ownership

Personal project of Mack Smith (`mack@mackenziesmith.com`). Not currently licensed for redistribution.
