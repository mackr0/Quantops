# Strategy audit + remediation plan — 2026-05-15

Built by auditing every strategy in `strategies/` against production
prediction data from all 10 active profiles, plus live probes of every
data source the strategies depend on.

Data-source priority (your rule, applied throughout):
**Alpaca first → custom alt data second → yfinance last.**

---

## Status legend

- 🟢 **WORKING** — produces predictions in production, code looks correct, data source healthy.
- 🟡 **SHADOW BROKEN** — currently produces predictions, but data source has degraded (e.g. Alpaca options 401) or strategy is wholly dependent on a yfinance source that's eligible for migration.
- 🔴 **BROKEN — bug** — code-level bug that prevents the strategy from ever firing. Must fix.
- ⚫ **BROKEN — unreachable** — code looks correct but conditions are so strict the strategy has fired zero times across all profiles ever. Either the threshold needs relaxing or the universe is wrong; treat as broken until proven otherwise.
- 🔵 **WRAPPER** — not a real strategy, a router. Needs attribution fix, not a strategy fix.

---

## Per-strategy audit

| Strategy | Status | Lifetime preds | Data source | Diagnosis | Fix |
|---|---|---|---|---|---|
| `sector_momentum_rotation` | 🟢 WORKING | 7,317 | Alpaca bars + sector cache | Alpaca-pure. Healthy. | None. |
| `gap_reversal` | 🟢 WORKING | 1,264 | Alpaca bars only | Alpaca-pure. Healthy. | None. |
| `insider_selling_cluster` | 🟡 SHADOW BROKEN | 939 | yfinance via `get_insider_activity` | Wrapped as "alt data" but actually yfinance under the hood (`alternative_data.py:159`). | **Migrate to SEC EDGAR Form 4 custom altdata** (similar shape to `altdata/edgar13f/`). yfinance fallback if EDGAR lookup fails. |
| `max_pain_pinning` | 🟡 SHADOW BROKEN | 880 | Alpaca options chain | Alpaca chain endpoint returning **401 Unauthorized** for every symbol I tested (SPY/AAPL/TSLA/JPM/NVDA). Strategy has historical predictions but cannot fire today. | **Fix Alpaca options API key/permissions.** (Cross-cutting issue — affects 3 strategies.) |
| `insider_cluster` | 🟡 SHADOW BROKEN | 402 | yfinance via `get_insider_activity` | Same as `insider_selling_cluster`. | **SEC EDGAR Form 4 custom altdata.** |
| `vol_regime` | 🟢 WORKING | 318 | Alpaca bars only | Alpaca-pure. Healthy. | None. |
| `sector_rotation_short` | 🟢 WORKING | 70 | Alpaca bars + indicators | Alpaca-pure. Healthy. | None. |
| `relative_weakness_in_strong_sector` | 🟢 WORKING | 53 | Alpaca bars + sector cache | Alpaca-pure. Healthy. | None. |
| `earnings_disaster_short` | 🟢 WORKING | 48 | Alpaca bars (uses gap-down detection on bars, not earnings calendar) | Alpaca-pure. Healthy. | None. |
| `short_term_reversal` | 🟢 WORKING | 47 | Alpaca bars only | Alpaca-pure. Healthy. | None. |
| `breakdown_support` | 🟢 WORKING | 10 | Alpaca bars only | Alpaca-pure. Fires in only 1 of 10 profiles → **flag for threshold review**. | Audit thresholds — likely too strict for the segments it's registered in. |
| `distribution_at_highs` | 🟢 WORKING | 10 | Alpaca bars only | Alpaca-pure. Healthy. | None. |
| `relative_weakness_universe` | 🟢 WORKING | 5 | Alpaca bars only | Alpaca-pure. Healthy. | None. |
| `catalyst_filing_short` | 🟢 WORKING | 3 | Alpaca bars + SEC EDGAR via local sqlite cache | Custom alt source. Healthy. | None. |
| `market_engine` | 🔵 WRAPPER | 0 (misattributed) | Wraps `strategy_router` | **Attribution bug**: legacy router emits predictions tagged with sub-strategy names (`sector_momentum`, `pullback_support`, `index_correlation`, `dividend_yield`, `relative_strength`, `ma_alignment`, `macd_cross` — total ~1,500 lifetime preds invisible to dashboard). | Extend `get_allocation_summary` to surface legacy router sub-strategies as first-class rows. Don't show `market_engine` itself as a strategy. |
| `earnings_drift` | 🔴 BROKEN — bug | 0 | `check_earnings()` (which uses yfinance for FUTURE earnings only) | Strategy needs PAST earnings dates. `check_earnings` returns `{symbol, earnings_date, days_until}` — no `days_since_last`, returns None for past events (`earnings_calendar.py:229`). Code calls `earn.get("days_since_last", 999)` → always 999 → always skipped. | **Add a custom altdata source for past earnings** OR extend `earnings_calendar.py` to persist past dates, OR (yfinance-last) use `yf.Ticker(sym).earnings_dates`. **Recommend: extend `earnings_calendar.py`** — it already owns earnings persistence; persisting both past + future is a small change. |
| `news_sentiment_spike` | 🔴 BROKEN — bug | 0 | Alpaca News API via `get_sentiment_signal` | Field-name mismatch. Strategy reads `direction` (str) and `score` (0-100); function returns `signal` (BUY/SELL/HOLD), `sentiment_score` (-1..+1), `label`. Strategy condition `if direction not in ("bullish","bearish") or score < 70` always trips → always skipped. | **Code fix only**, no data-source change needed (already on Alpaca). Update strategy to consume real fields: `signal`, `sentiment_score`, `news_count`. |
| `analyst_upgrade_drift` | 🔴 BROKEN — bug | 0 | yfinance `Ticker.recommendations` | yfinance schema changed: now returns aggregate counts per period (`['period','strongBuy','buy','hold','sell','strongSell']`), no longer individual rating changes. Strategy reads `To Grade`/`From Grade` columns that no longer exist. | **No Alpaca alternative exists** for analyst recommendations. **No custom altdata source today.** Choices: (a) update strategy to consume new yfinance schema (period-over-period sentiment shift), (b) build a custom altdata source from a free analyst-data API (Finnhub, Polygon free tier), or (c) retire the strategy. **Recommend: (a) — yfinance is grandfathered for this since no alternative exists; document as exception.** |
| `high_iv_rank_fade` | 🔴 BROKEN — bug | 0 | Alpaca options chain via `get_options_oracle` | TWO bugs: (1) `oracle["iv_rank"]` is a dict `{rank_pct, signal, realized_vol}` not a number; strategy's `if iv_rank < 80` raises TypeError swallowed by debug-level except. (2) Even if (1) fixed, Alpaca options chain currently returns 401 for all symbols (cross-cutting). | **Code fix** (extract `iv_rank["rank_pct"]`) + **fix Alpaca options API key** (cross-cutting). |
| `iv_regime_short` | 🔴 BROKEN — bug | 0 | Alpaca options chain via `get_options_oracle` | Same two bugs as `high_iv_rank_fade`. | Same fixes. |
| `fifty_two_week_breakout` | ⚫ BROKEN — unreachable | 0 | Alpaca bars only | Live-tested on 50-megacap universe → 0 candidates. Code looks correct (new 52w high + 1.5× volume + not >15% spike day). Either thresholds too strict for the registered universes OR universe doesn't include enough stocks for 52w highs to appear. | **Audit + relax thresholds OR widen universe.** Likely 1.5× volume is the killer for liquid megacaps. |
| `macd_cross_confirmation` | ⚫ BROKEN — unreachable | 0 | Alpaca bars + indicators | Live-tested → 0 candidates. Code correct (zero-cross + RSI in 45-75 band + 1.2× volume). | **Audit thresholds.** RSI band may be too narrow. |
| `volume_dryup_breakout` | ⚫ BROKEN — unreachable | 0 | Alpaca bars only | Live-tested → 0 candidates. Code correct (5d declining volume + 2× breakout + new 10d high). Truly rare on liquid names. | **Audit + relax (e.g. 4d window, 1.5× breakout).** |
| `failed_breakout` | ⚫ BROKEN — unreachable | 0 | Alpaca bars only | Live-tested → 0 candidates. Code correct (broke 20d high in last 5 days + closed back below + 1.2× vol). | **Audit thresholds — 5-day breakout window may be too narrow.** |
| `parabolic_exhaustion` | ⚫ BROKEN — unreachable | 0 | Alpaca bars + indicators | Live-tested → 0 candidates. Code correct but conditions extreme (+25% in 10d + RSI>80 + reversal candle). Fires maybe 1-3× per year on small-caps; never on midcaps. | **Restrict APPLICABLE_MARKETS to micro/small** (currently small/midcap) OR relax run-up threshold. |
| `short_squeeze_setup` | ⚫ BROKEN — unreachable + 🟡 yfinance | 0 | yfinance via `get_short_interest` | Live-tested → 0 candidates. Threshold (`short_pct_float >= 15%`) is genuinely rare on liquid mid/large-caps; AAPL is 0.92%. Plus underlying data source is yfinance (`alternative_data.py:225`). | **Two-step**: (1) build custom altdata source for short interest from FINRA bi-monthly reports (free), (2) audit threshold appropriateness. yfinance is acceptable interim. |

---

## Cross-cutting issues uncovered

### CC-1: Alpaca options chain returning 401 Unauthorized

**Impact**: All options-dependent strategies (`max_pain_pinning`, `high_iv_rank_fade`, `iv_regime_short`) cannot fetch chains right now. Options ensemble specialists (`iv_skew_specialist`, `gamma_pin_specialist`, `option_spread_risk`) also fail silently.

**Diagnosis**: `data.alpaca.markets/v2/stocks/<sym>/snapshot` returns `401`. The Alpaca account/key being used by `options_chain_alpaca.py` may have a different permission scope than the keys used for trading. Worth checking whether options data is on a separate subscription tier.

**Action**: Investigate `options_chain_alpaca.py:fetch_chain_alpaca` — check which credentials it's using and verify the account has options market data permission. Likely need to upgrade subscription OR use the master key vs per-profile keys.

### CC-2: `alternative_data.py` is yfinance-in-disguise

**Impact**: `get_insider_activity` and `get_short_interest` are presented as "alt data" but their implementation is straight yfinance calls (lines 159, 225, 267, 317, 759, 846, 945, 1090). 4 strategies depend on them. Per the policy, these should be Alpaca or custom altdata.

**Action**: Build proper custom altdata sources to replace these:
- **Insider data** → SEC EDGAR Form 4 (similar pattern to `altdata/edgar13f/` for Form 13F). Free.
- **Short interest** → FINRA bi-monthly short interest reports. Free, downloadable CSV.
- **Fundamentals** (the remaining `info` lookups) → Alpaca Corporate Actions API for what it covers; custom altdata for the rest.

### CC-3: Missing memory rule `feedback_alpaca_first_data.md`

**Impact**: Per `docs/07_OPERATIONS.md:413` this rule was supposed to enforce Alpaca-first via auto-memory; the file does not exist in `~/.claude/projects/-Users-mackr0/memory/`. That's why yfinance has crept back in. Without the rule, the next session will repeat the same mistakes.

**Action**: Create the memory rule. Body: "All new code uses Alpaca first; if unavailable, custom altdata; if unavailable, yfinance. Document any new yfinance use as an exception in `docs/04_TECHNICAL_REFERENCE.md`."

### CC-4: Documentation overstates the yfinance restriction

**Impact**: `docs/04_TECHNICAL_REFERENCE.md:158` says "sector_classifier.py: only allowed yfinance use" — but yfinance is actually used in 6+ production modules. The docs should reflect reality OR the code should match the docs.

**Action**: Decide which is true after the migrations land. Update docs accordingly.

---

## Proposed fix order (the work plan) — REVISED 2026-05-15

User direction: **fix broken/missing first**; **currently-working yfinance code goes to a LATER phase** to evaluate replacement options.

### Phase 1 — Broken strategies that fix to Alpaca-pure or attribution-only (no new yfinance, no data-source change)

1. `news_sentiment_spike` — rewrite to consume real `get_sentiment_signal` fields. Already on Alpaca News API.
2. `high_iv_rank_fade` — extract `iv_rank["rank_pct"]` from the dict.
3. `iv_regime_short` — same fix.
4. `market_engine` attribution — extend `get_allocation_summary` to surface legacy router sub-strategies as first-class allocation rows.

### Phase 2 — Broken strategies that need yfinance because no Alpaca/altdata source exists (grandfathered, document as exception)

5. `earnings_drift` — fix to use yfinance for PAST earnings (extend `earnings_calendar.py` to persist past events). yfinance is the only source today.
6. `analyst_upgrade_drift` — fix to use new yfinance schema (period-over-period sentiment shift). yfinance is the only source today.

### Phase 3 — Cross-cutting blocker (unblocks 3 strategies in one fix)

7. **CC-1: Alpaca options 401** — investigate credentials / subscription tier. Once resolved, `max_pain_pinning` (currently degraded), `high_iv_rank_fade`, and `iv_regime_short` all start producing live signals.

### Phase 4 — Unreachable strategies — threshold audits

8. `fifty_two_week_breakout` — audit + relax volume threshold.
9. `macd_cross_confirmation` — audit + widen RSI band.
10. `volume_dryup_breakout` — audit + relax window/breakout multiplier.
11. `failed_breakout` — audit + widen breakout window.
12. `parabolic_exhaustion` — restrict APPLICABLE_MARKETS to micro/small.
13. `short_squeeze_setup` — audit threshold appropriateness (also depends on yfinance — defer that part to Phase 6).
14. `breakdown_support` — investigate why fires in only 1 of 10 profiles.

### Phase 5 — Structural enforcement + docs

15. Create `feedback_alpaca_first_data.md` memory rule (CC-3) so the discipline is enforced going forward.
16. Class-level test that flags any registered strategy with `lifetime_n=0` across all profiles for >14 days of operation. Catches the next "ship a strategy that's wired wrong" regression at test time.
17. Update `docs/04_TECHNICAL_REFERENCE.md` to reflect actual yfinance usage AND the new policy. Document earnings_drift, analyst_upgrade_drift, alternative_data.py uses as known exceptions with rationale.

### Phase 6 — DEFERRED: evaluate replacements for currently-working yfinance code

Per user direction, NOT touching currently-working code yet. Items to evaluate later:

18. `alternative_data.get_insider_activity` (powers `insider_cluster` + `insider_selling_cluster`, ~1,300 lifetime preds) — evaluate SEC EDGAR Form 4 as Alpaca-first replacement. Build only if EDGAR Form 4 is verifiably equal-or-better than yfinance for this use case.
19. `alternative_data.get_short_interest` (powers `short_squeeze_setup`) — evaluate FINRA bi-monthly short interest reports.
20. `market_data.py` `yf.Ticker(...).info` calls (lines 129, 229, 516) — audit per-call: which fields, do Alpaca Corporate Actions / Snapshots cover them?
21. `macro_data.py` `yf.Ticker("^SKEW")` — research whether Alpaca exposes ^SKEW or whether CBOE direct is feasible.
22. Remaining `alternative_data.py` yfinance uses (lines 159, 225, 267, 317, 759, 846, 945, 1090) — per-call audit of what each fetches and whether an Alpaca/custom alternative is realistic.

For each Phase 6 item: replace ONLY if a viable alternative exists. Otherwise grandfather yfinance + document as exception in the technical reference.

---

## Summary numbers

- **26 strategies registered**
- **14 currently producing predictions** (4 of those depend on yfinance via `alternative_data.py`)
- **5 broken by code bugs** (1 fixable code-only, 2 blocked by CC-1, 2 broken by yfinance schema/API drift)
- **6 unreachable** (probably correct code, conditions never met)
- **1 wrapper** (market_engine — attribution bug, ~1,500 hidden predictions)

After Phase 1 + CC-1: **~9 currently-broken strategies start producing predictions**, and the dashboard truthfully shows another ~7 legacy router sub-strategies. The system goes from fusing 5-10 signals per profile → 12-18 signals per profile.

---

## What I'm asking you to review / decide

1. **Phase ordering** — is "code bugs first → ops issue → custom altdata → threshold audits → yfinance grandfathering → structural test" the right priority? Or do you want CC-1 (options 401) first because it unblocks 3 strategies in one fix?

2. **CC-2 scope** — are SEC EDGAR Form 4 + FINRA short interest worth building right now (Phase 3), or grandfather the yfinance use until the strategies prove their edge?

3. **Phase 4 (threshold audits)** — do you want me to do this, or is this a strategy-design call you want to make? (I can audit + propose changes; the actual go/no-go on each threshold change is a strategy decision, not a code decision.)

4. **`analyst_upgrade_drift`** — yfinance grandfather, build custom, or retire?

5. **The `feedback_alpaca_first_data.md` memory rule** — should I create it now (CC-3), independently of the rest of this work?
