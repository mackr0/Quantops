# /shadow — Calculation Register

Source of computation: `shadow_metrics.py`
(`collect_fleet_metrics`), fed by `ai_shadow_calls` (one row per
shadowed primary call per arm) and `ai_predictions` (the primary's
per-symbol decisions with resolved outcomes). View wiring:
`views.py shadow_page`, template `templates/shadow.html`.

Verification pass: 2026-08-13 at commit `70ffbe2`, cross-checked
against live fleet data during the 08-05→08-11 shadow-page rebuild.

**Time scope (2026-08-21):** every metric on the page is computed over
the FULL shadow history — `collect_fleet_metrics` runs with no date
cutoff. Until 2026-08-21 the page used a rolling 30-day window; it
never showed because the arms started 07-23, but it was about to roll
the earliest scored decisions off the back, weakening settled verdicts
while nothing was wrong. Two deliberate exceptions remain windowed:
the standings' **Cost (30d)** column (spend is a run-rate question)
and the daily-trend table (display-capped at the latest 30 days; the
cap is labeled on the page and touches no aggregate). Pinned by
`tests/test_shadow_alltime_2026_08_21.py`, including a structural
check that the view can't quietly pass a window again.

---

## Standings sentence

**Standings vs the primary** — arms partitioned by their pairwise
verdict (`_standings`): `shadow_better` arms listed best-edge-first,
`primary_better` arms worst-first, everything else "not enough
evidence to place" and never ranked. *Why:* each row is a separate
head-to-head against the SAME primary; the ordering is the transitive
chain through that common opponent, and the sentence names the
limitation (not a direct arm-vs-arm trial) because each arm is scored
on its own disagreement set. **VERIFIED**

## Bottom line table

**Verdict** — issued only at ≥30 scored decisions AND bootstrap
p < 0.05 on the mean edge; otherwise "Not enough evidence." *Why:* a
card that always names a winner names one from noise — the 2026-07-30
"27–5 rout" was pure measurement artifact. Refusal is the designed
default. **VERIFIED**

**Edge / decision, Total** — return points banked by following the
arm instead of the primary, per resolved disagreement: forecasters
earn the move (long = +move, short = −move, neutral = 0); VETO-gates
earn the taken trade's P&L when they allowed and 0 when they blocked.
Collapsed to ONE figure per (profile, symbol) unit so nine re-reviews
of one name are one bet. Cells color by sign. *Why points not
dollars:* position size isn't recorded on a shadow row; a dollar
figure would be an invented constant times a real number.
**VERIFIED**

**Decisions (n / 30)** — count of scored units; the /30 shows the
verdict floor. **VERIFIED**

**Win rate (aW/bL)** — unit-level win/loss split, displayed beside
the money verdict because they can disagree (an arm can lose most
decisions and win on points by catching large moves); the money test
decides, the split stays visible. **VERIFIED**

**p (money)** — seeded bootstrap (deterministic across refreshes) on
the mean unit edge; computed only at ≥30 units. A sign-test p is
computed alongside for the win-count question. *Why bootstrap:*
per-decision returns are heavy-tailed at these sample sizes; a t-test
would overclaim. **VERIFIED**

**Cost (30d)** — Σ `cost_usd` over the arm's shadow rows of the last
30 days (a true run-rate, accumulated separately from the all-history
metrics; the per-model summary shows lifetime cost as "Cost (total)").
**VERIFIED**

**Evidence funnel line** — live per-arm counts: calls → graded
(agreement computable) → disagreements → distinct units → moot →
scored, plus outcome-match method: **N by decision id (exact)** vs
**M by time proximity** (rows predating the id plumbing). *Why:*
the gap between "19,926 graded" and "~130 scored" must be visible
arithmetic, not trust. **VERIFIED**

## Matching machinery (feeds everything above)

**Decision matching** — a shadow row joins its outcome by
`decision_id` (identity, minted per candidate in `run_ensemble`,
stamped on both the shadow row and the prediction row) with
time-window matching (±1800s, nearest ANY-status prediction = the
review's own decision; pending stays pending) strictly as fallback
for pre-2026-08-06 rows. History: nearest-RESOLVED matching once let
a neighbor cycle's HOLD hijack a pending decision (p210 GOOGL,
2026-08-05). **VERIFIED**

**Moot** — a VETO-authority disagreement whose matched decision took
no trade (`predicted_signal` HOLD): no position existed, credits
nobody. *Why:* scoring gates against untraded candidates manufactured
a 31–2 phantom result pre-2026-07-30. **VERIFIED**

**Noise band** — |move| < 1.5% grades neither side on direction; a
HOLD is graded like any call (right inside the band, wrong on a real
move). **VERIFIED**

## Prompt-variant process card

**Compared calls / Arm blocks % / Primary blocks % / Difference** —
on gate-purpose rows where both sides map to allow/block: block-rate
of each side over the SAME calls. Renders "unmeasured" (never 0%)
when an arm saw no gate calls; the card header always renders with an
explicit empty state. *Why:* the variant's experiment is behavioral
(veto less, with evidence); money verdicts for a gate arm accrue too
slowly to be its readout. **VERIFIED**

## Per-model summary

Calls / Graded / Agreement% / Disagree / Resolved / Units /
Shadow won / Primary won / Both / Neither / Moot / Ungradable /
Pending / Errors / Quota / Throttled / Cost / Latency / Scope —
direct counters per arm; "Shadow won"/"Primary won" count resolved
disagreements where exactly ONE side matched (reading a single side's
total is explicitly warned against in page prose); comparative
coloring (side with more wins green, other red, ties neutral);
error taxonomy separates billing-quota and cost-cap throttles from
real errors; Scope = decision categories the arm covers (a variant
deliberately covers 1). **VERIFIED**

## Category cuts (by purpose / by primary action)

Same counters cut by `purpose` and by what the primary did. Gate
specialists group under `gate: allow` / `gate: block` / `gate: exit
advice` rather than a direction — a veto is not a bearish forecast
(2026-08-07 grouping fix). **VERIFIED**

## Daily agreement trend / Recent disagreements

Daily = graded and agreement% per (day, arm). Recent = newest-first,
up to 20 rows per arm merged and capped at 60 (pre-2026-08-11 the
slice was insertion-ordered and volume-crowded; low-volume arms never
appeared); Profile column shows operator display names. **VERIFIED**

---

## Open items for this page

None currently. The page's known measurement hazards (always-name-a-
winner, per-leg vs per-decision, neighbor-cycle matching, moot
scoring, unmeasured-as-0%) each have a structural test pinning the
fix; see `tests/test_shadow_*` and
`tests/test_calc_verification_register_2026_08_13.py`.
