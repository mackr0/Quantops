# Experiment profile mapping (2026-05-19 snapshot)

Reference for re-creating the 13-profile experiment after a fresh-slate restart.

## Common across all 13 profiles

| Field | Value |
|---|---|
| `market_type` | `largecap` |
| `is_virtual` | `1` (true — shared paper accounts) |
| `ai_provider` | `google` |
| `ai_model` | `gemini-2.5-flash-lite` |
| `enable_stocks` | `1` (true) |
| `enable_crypto` | `0` (false — no crypto strategies yet) |

## Account 4 — Group A1 (Baselines + Full System)

Total capital on this Alpaca paper account: **$1,000,000**

| profile_id | name | initial_capital | strategy_type | enable_options | enable_short_selling |
|---|---|---|---|---|---|
| 12 | EXP-A1-BuyHoldSPY | 250000 | buy_hold | 0 | 0 |
| 13 | EXP-A1-RandomA | 250000 | random | 0 | 0 |
| 14 | EXP-A1-RandomB | 250000 | random | 0 | 0 |
| 15 | EXP-A1-FullSystemStandard | 250000 | ai | 1 | 1 |

## Account 5 — Group A2 (Component ablations)

Total capital on this Alpaca paper account: **$1,000,000**

| profile_id | name | initial_capital | strategy_type | enable_options | enable_short_selling |
|---|---|---|---|---|---|
| 16 | EXP-A2-NoAltData | 200000 | ai | 1 | 1 |
| 17 | EXP-A2-NoMetaModel | 200000 | ai | 1 | 1 |
| 18 | EXP-A2-NoSelfTuning | 200000 | ai | 1 | 1 |
| 19 | EXP-A2-NoOptions | 200000 | ai | **0** (ablation) | 1 |
| 20 | EXP-A2-NoAltData-NoMetaModel | 200000 | ai | 1 | 1 |

## Account 6 — Group A3 (Capital scaling)

Total capital on this Alpaca paper account: **$1,000,000**

| profile_id | name | initial_capital | strategy_type | enable_options | enable_short_selling |
|---|---|---|---|---|---|
| 21 | EXP-A3-25K-Candidate | 25000 | ai | 1 | 0 |
| 22 | EXP-A3-25K-Replica | 25000 | ai | 1 | 0 |
| 23 | EXP-A3-250K-ConservativeScale | 250000 | ai | 1 | 0 |
| 24 | EXP-A3-700K-AggressiveFree | 700000 | ai | 1 | 1 |

## Grand totals

| | Profiles | Virtual capital |
|---|---|---|
| Account 4 | 4 | $1,000,000 |
| Account 5 | 5 | $1,000,000 |
| Account 6 | 4 | $1,000,000 |
| **Total** | **13** | **$3,000,000** |

## Setup order when restarting

1. In Alpaca dashboard, create 3 paper accounts (or rotate keys on existing ones).
2. In QuantOpsAI Settings → Alpaca Accounts, add each new key pair as its own row. Each row gets its own ID — note which Alpaca account each maps to.
3. Create 13 profiles via Settings → Create New Profile, matching the chart above. The `alpaca_account_id` selector picks the row from step 2.
4. Verify by visiting `/settings` and confirming each profile's "Alpaca Account" matches the intended group (A1 / A2 / A3).
5. Do NOT add any Alpaca keys to `/opt/quantopsai/.env` — the code doesn't read them from there. Settings UI is the only place.
