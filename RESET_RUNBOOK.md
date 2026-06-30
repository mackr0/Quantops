# Fresh-Start Reset Runbook

Canonical procedure for a full fresh-start of the EXP-A* experiment. Supersedes
the pasted instruction list. Last validated: 2026-06-17 (two resets — the
morning run surfaced the `ENCRYPTION_KEY` footgun now fixed in Step 3).

**Code baseline (2026-06-30):** this reset lands on the broker/journal
divergence-class fix (per-cycle freshness invariant + durable journaling, CHANGELOG
2026-06-23), the institutional-universe floors — `$10` min price + `$5M` min
dollar-ADV, operator-only / never auto-tuned (CHANGELOG 2026-06-26/27) — **and**
the 2026-06-30 options-path + isolation fixes (CHANGELOG 2026-06-30, *Standing
state* below): the cross-profile Greeks leak in `option_spread_risk` closed
(own-book), the options candidate path made own-book concentration-aware (no more
~97% redundant-veto storm), option Greeks priced off the underlying spot (not the
premium), the cross-profile concentration cap removed, the fast-exits two-pass
scheduler, and the cost cuts (2 advisory specialists off + 10-min scan floor). A
reset inherits all of these automatically. Make sure Step 2 ships them (prod
`HEAD` matches your pushed commit) so the fresh experiment runs on the fixed code
and the institutional universe from day one.

> **What a fresh-start does:** deletes every profile + per-profile DB outright,
> rebuilds the 13 EXP-A* profiles from the manifest, swaps in new Alpaca paper
> accounts, wipes AI learning state + caches + audit alerts + runtime/altdata
> logs + journald. This is the **TRUE fresh-start** path
> (`clean_orphaned_profiles` + `create_experiment_profiles`), **not** the
> mid-day-restart variant that preserves AI learning state — the learning state
> is exactly what we're resetting.
>
> **Preserved:** all altdata / world-data DBs, master-DB world caches, universe
> state, `users` / segment configs / migration markers.

---

## 0. Prerequisites (operator, at Alpaca.com)

- [ ] **Delete the OLD paper accounts** at Alpaca.com.
- [ ] **Create 3 NEW paper accounts**, each funded **$1,000,000** with **0 open
      positions** (the default is $100K — you must set $1M, or use the
      dashboard **Reset** to restore an account to its configured $1M / 0).
- [ ] Have the three `api_key  secret` pairs ready to paste.
- [ ] Decide the Google AI key: **carry over** (default) or **rotate**.

---

## 1. Prepare the dated reset script

Clone the **most recent** reset script (it carries the full lineage — funding
guard, RC1–RC11, gap fixes). As of 2026-06-30 that is
`full_fresh_start_2026_06_29.py` (the 2026-06-30 reset cloned it to
`full_fresh_start_2026_06_30.py`).

**Naming the target** (the filename MUST end `_YYYY_MM_DD.py` for the
test-exemption; **never** a numeric suffix):
- First reset of the day → `full_fresh_start_<YYYY_MM_DD>.py`
- Second same-day reset → `full_fresh_start_pm_<YYYY_MM_DD>.py` (the `pm_`
  prefix just disambiguates same-date files)

Then edit the clone:
- [ ] Paste the 3 new keys into `NEW_KEYS` (`(name, label, api_key, secret)`).
- [ ] Update the docstring date/rationale, the `Run:` filenames, and the
      `main()` banner to the new filename/date.
- [ ] **Carry over the Google key:** do nothing — `step1b` snapshots it from the
      current profiles and `step5b` restores it (this works *only if the script
      runs clean — see Step 3*). **To rotate instead:**
      `export RESET_NEW_GOOGLE_AI_KEY='AIza...'` before `--apply` (step5c installs it).

---

## 2. Deploy the script (+ any pending code fixes)

```bash
# local
git add full_fresh_start_<date>.py && git commit && git push origin main
# prod — prod git MUST track deployed code; this also ships any pending fixes
ssh root@67.205.155.63 'cd /opt/quantopsai && git fetch origin && git reset --hard origin/main'
```

Confirm prod `HEAD` matches your pushed commit.

---

## 3. ⚠️ CRITICAL — load the prod env before anything destructive

```bash
ssh root@67.205.155.63
cd /opt/quantopsai && set -a && . ./.env && set +a
```

**Why this is mandatory:** `step3` encrypts the new account keys and needs
`ENCRYPTION_KEY`, which lives in `/opt/quantopsai/.env`. systemd loads it for the
service, but a **bare ssh shell does not**. If it's missing, the script crashes
at `step3` **after `step2` has already wiped all 13 profiles**, and the in-memory
`step1b` Google-key snapshot dies with the process. (This happened on the morning
2026-06-17 reset; recovery is possible from the 05:00 master backup — see
*Recovery* — but don't rely on it. Load `.env`.)

---

## 4. Dry-run (no writes)

```bash
venv/bin/python full_fresh_start_<date>.py
```

- [ ] **STEP 1** — all three accounts show `equity=$1,000,000.00  positions=0`.
      A `WARNING: equity != $1M` aborts the run **before any writes** — fix the
      account funding at Alpaca and re-run.
- [ ] **MANIFEST DRIFT** — look for: `drift report: live profile settings match
      the rebuild manifest — nothing will be reverted`. If it instead lists
      drift, fold anything intentional into `create_experiment_profiles.PROFILES`
      first (this is how the 999 position caps were lost on 06-09; SPY=1 and
      Randoms=5 and the caps now live in the manifest and survive automatically).
- [ ] **STEP 4** — `manifest verified: 13 profiles totaling $3,000,000` with the
      expected splits (A1 4×$250K, A2 5×$200K, A3 $25K/$25K/$250K/$700K).

---

## 5. Apply

```bash
# stop the scheduler first if the market is open, so it can't race the DB wipe
systemctl stop quantopsai
venv/bin/python full_fresh_start_<date>.py --apply
```

Confirm in the output:
- [ ] STEP 1 keys verified ($1M / 0).
- [ ] **STEP 5b** restores `google/gemini-2.5-flash-lite key=164B` on all 13
      profiles (the carry-over worked). If `step5c` says `NEW_GOOGLE_AI_KEY not
      set — skipping`, that's correct for carry-over.
- [ ] **STEP 6** `OK: 3 accounts, 13 EXP-Ax- profiles linked correctly`.
- [ ] `APPLIED — done`.

---

## 6. Restart + certify

```bash
systemctl restart quantopsai quantopsai-web
cd /opt/quantopsai && set -a && . ./.env && set +a
venv/bin/python certify_books.py --since-hours 168
```

Require **`CERTIFIED CLEAN`** — five checks in one command:
`0 BROKER FUNDING` ($1M each), `1 BROKER DRIFT` (zero per account),
`2 RECONCILE` (zero dry-run actions), `3 DECOMPOSITION` (±$100 per profile),
`4 ISSUES` (empty over 7 days). If any check FAILS, find the missed source and
**fold the fix into the next dated script** — don't hand-patch prod.

Then confirm the rebuilt profiles inherited the institutional universe floors
(they come from the `stocks` segment automatically — this just proves it):

```bash
sqlite3 quantopsai.db "SELECT MIN(min_price),MAX(min_price),MIN(min_adv),MAX(min_adv) FROM trading_profiles WHERE enabled=1;"
```
Expect `10.0|10.0|5000000.0|5000000.0` (all 13 at $10 min price / $5M min ADV). If
they came up at `1.0` / penny floors, the segment baseline regressed — fix
`segments.py` before trading, don't hand-edit the rows.

---

## 7. Watch the first live cycles (necessary — certify CLEAN is not sufficient)

`CERTIFIED CLEAN` right after a reset passes trivially (zero trades). Real
problems surface only in **live trading**. On 2026-06-17 the books certified
clean, then ~15 min later ICCM (a hard-to-borrow micro-cap) caused naked
positions, rejected entries, and reconciler halts.

Watch the first ~3 cycles / ~20–30 min and confirm:
- [ ] no profile halted; broker drift stays 0;
- [ ] every new stock entry arms a protective stop (no naked positions);
- [ ] options closes book P&L (no `pnl=NULL` on closed legs), no orphans;
- [ ] no repeating tracebacks / `insufficient` / `only day orders are allowed`;
- [ ] the `Freshen To Broker` task runs first in each cycle's exits phase, and
      `reconcile_state` accrues per-symbol epochs (the freshness invariant is
      live). An occasional `OversellGuardError ... freshness reconcile failed`
      during a real Alpaca hiccup is the gate **failing closed** (refusing a
      sell on a possibly-stale book) — that is correct, not a bug; it clears
      the next cycle once the broker is reachable. A *flood* of them means the
      broker is unreachable — investigate connectivity, not the gate.

---

## Gotchas & recovery

- **Benign:** `db_integrity: skipping orphan profile DB ...` warnings for old
  reset IDs (e.g. 45–141). Non-blocking — the wipe only removes DBs for
  currently-listed profiles, so prior generations' files linger harmlessly.
- **Recovery if `step3` ever crashes post-wipe** (i.e. you forgot Step 3): the
  Google key (uniform across all 13, encrypted in `trading_profiles.ai_api_key_enc`)
  is recoverable from the daily 05:00 master backup
  `backups/quantopsai.db.YYYYMMDD-0500` — decrypt one `ai_api_key_enc` with
  `crypto.decrypt`, `export RESET_NEW_GOOGLE_AI_KEY=<value>`, re-run `--apply`
  (step2 is idempotent once profiles are gone; step5c installs the key).
  `clean_orphaned_profiles` backs up per-profile DBs to `backups/pre-orphan-cleanup-*Z/`
  but **not** the master DB — the 05:00 snapshot is the master fallback.

## Standing state

- **Broker/journal freshness invariant + durable journaling (2026-06-24):** no
  order is submitted for a (profile, symbol) unless that symbol's journal has
  been reconciled to *this profile's own* broker truth this cycle. Enforced at
  the oversell door (`order_guard`) for stocks/declared-shorts and at
  `options_multileg._submit_alpaca_order_raw` for options; **fail-closed** if
  the broker is unreachable (a refused sell is the gate working, not a bug). A
  process-wide `cycle_epoch` (wall-clock-seeded, monotonic across restarts)
  drives staleness; the per-profile `reconcile_state` and `submitted_orders`
  tables are **auto-created by `init_db`** and **start empty after a wipe** — no
  reset step needed. This is the class that repeatedly forced restarts (p166
  PLUG oversell, the phantom-equity incidents); resets inherit the fix
  automatically. Profiles remain **fully independent** — they share only the
  brokerage conduit; nothing coordinates across them.
- **Universe is institutionally aligned (2026-06-17 `6bad94e`; floors 2026-06-27
  `20ea3a0`):** the screener and `execute_trade` exclude `easy_to_borrow=False`
  (hard-to-borrow / non-shortable) names, AND the `stocks` segment baseline is now
  **min price `$10` + min dollar-ADV `$5,000,000`** (avg daily $ volume = price ×
  20-day mean share volume) — the institutional liquid universe that excludes the
  sub-$10 penny/meme tier and the thin-but-cheap names whose wide spreads whipsaw
  the stops. These four floors (`min_price`/`max_price`/`min_volume`/`min_adv`) are
  **operator-only — never auto-tuned** (`self_tuning._OPERATOR_ONLY_PARAMS`; the
  tuner optimizes HOW to trade, the operator owns WHAT is eligible). The floors
  live in `segments.py` (`get_segment("stocks")`), which `create_trading_profile`
  copies into every profile, so **resets inherit `$10`/`$5M` automatically** — no
  per-profile or manifest step. Rationale: ~80% of the prior cohort's realized
  losses came from sub-$10 names this universe now excludes (HTZ alone, a ~$2.50
  stock, was 60%). Operator can still narrow/widen any profile from Settings.
- **Option-path isolation + own-book concentration (2026-06-30):** the option
  risk specialist (`option_spread_risk`, a VETO specialist) reads its book-Greeks
  context from the profile's OWN book (`client.get_positions(ctx=ctx)`), NEVER the
  shared Alpaca conduit aggregate (`api.list_positions()`) — an AST test pins that
  no specialist calls `list_positions()` directly. The options candidate generator
  is own-book concentration-aware: it skips spreads on underlyings the profile
  already holds, so the AI no longer re-proposes redundant spreads that the
  reviewer auto-vetoes (the ~97% option-veto storm). Option-book Greeks are priced
  off the underlying spot (`market_data.get_bars`), not the option premium. All
  inherited automatically by a reset (code-level); profiles remain fully
  independent — they share only the brokerage conduit.
- **`ENCRYPTION_KEY` pre-flight (folded in 2026-06-30):** `step1_verify_keys`
  now aborts *before* `step2` if `ENCRYPTION_KEY` is unset, turning the Step-3
  footgun into a clean no-op (it returns False → `main()` aborts before any
  destructive write). Step 3 above is still the operative instruction — load the
  env — but a forgotten load now fails safe instead of crashing post-wipe. Carry
  this gate forward when cloning the next dated script.
