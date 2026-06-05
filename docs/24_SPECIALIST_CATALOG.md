# 24 — Specialist Catalog

**Audience:** quants evaluating coverage of the ensemble; financial analysts / forensic accountants / VC reviewers asking "what does the system actually check for before it trades?"; engineers adding a new specialist.
**Purpose:** the canonical enumeration of every specialist running in production today — 8 LLM-narrative specialists in `specialists/` and 179 deterministic rule checkers in `deterministic_specialists/`. **187 specialists total.**
**Last updated:** 2026-06-04 (audit reconciliation — see `docs/AUDIT_2026_06_04_DOC_RECONCILIATION.md`).

---

## Why this catalog exists

The system's value-prop story is encoded here. The platform achieves high accuracy at low cost by putting *hundreds* of zero-API-cost rule checkers in front of *one* batched LLM call:

- **179 deterministic rules** (the catalog in §2) cost nothing per cycle — they're pure-Python pattern matchers. Most decisions short-circuit cleanly through this layer.
- **8 LLM-narrative specialists** (the catalog in §1) spend the per-call AI tokens, but six of the eight were re-scoped 2026-05-18 (Phase 3 of `docs/17`) to *synthesize* from the deterministic panel rather than re-derive facts. Each LLM specialist now reads `RULES: [V]name [C]name ...` in each candidate's render and writes the narrative thesis on top.
- **Result:** observed steady-state AI spend on the 13-profile experiment fleet is ~$0.27/day at the `gemini-2.5-flash-lite` rate. Adding the 100th deterministic specialist costs $0; adding the 9th LLM specialist would meaningfully bump per-cycle cost.

Each deterministic rule carries a severity:
- **VETO** — high-confidence block. The candidate is structurally unsafe; ensemble drops it regardless of other verdicts. **10 VETO rules** in production.
- **CAUTION** — yellow flag. The rule fires a warning the LLM reads; the LLM decides whether to override. **92 CAUTION rules**.
- **CONFIRM** — pattern supports the signal. Adds conviction; the LLM weighs the confirm-vs-caution mix per candidate. **77 CONFIRM rules**.

Routing — a rule fires only when its `APPLIES_TO_SIGNALS` overlaps the candidate's signal. For stocks the match is direct (`BUY` → long-only rules); for options the router classifies the candidate's direction via `signal_direction(candidate)` and routes OPTIONS / MULTILEG_OPEN to the same-direction stock rule set. See `deterministic_specialists/__init__.py:run_panel`.

---

## 1. LLM-narrative specialists (8 total, `specialists/`)

Each specialist exposes `NAME`, `DESCRIPTION`, `HAS_VETO_AUTHORITY`, `APPLIES_TO_PIPELINES`, and `build_prompt(candidates, ctx)`. Auto-discovered at load time by `specialists/__init__.py`.

| Specialist | Role | Veto authority | Pipelines |
|---|---|---|---|
| `adversarial_reviewer` | Red-team reviewer — hunts failure modes pre-execution (book-level correlation, mandate violations, novel scenarios the rule library can't encode) | **Yes** | stock + option |
| `earnings_analyst` | Synthesizes earnings trajectory from earnings-cluster rule verdicts (beat-and-raise vs deteriorating vs event-priced) | No | stock + option |
| `gamma_pin_specialist` | Reads dealer GEX + max-pain strike for pinning (stability) vs negative-gamma (instability) regimes | No | option only |
| `iv_skew_specialist` | Reads put/call IV skew for premium-side bias; consumes options-rule verdicts | No | option only |
| `option_spread_risk` | Option-aware risk gatekeeper — IV crush, gamma exposure, max-loss budget violations | **Yes** | option only |
| `pattern_recognizer` | Synthesizes a coherent technical thesis from the deterministic technical rule verdicts | No | stock only |
| `risk_assessor` | Synthesizes a worst-plausible-outcome scenario from the risk-cluster rule verdicts | **Yes** | stock + option |
| `sentiment_narrative` | Synthesizes the narrative — who is positioning and why — from smart-money + sentiment rule verdicts | No | stock + option |

Six of the eight (`adversarial_reviewer`, `earnings_analyst`, `iv_skew_specialist`, `pattern_recognizer`, `risk_assessor`, `sentiment_narrative`) were re-scoped 2026-05-18 to synthesize from the deterministic panel rather than re-derive facts. Two (`gamma_pin_specialist`, `option_spread_risk`) cover unique territory the rule library structurally can't subsume and remain as-is.

Veto authority: `risk_assessor`, `adversarial_reviewer`, and `option_spread_risk` can drop a candidate regardless of other verdicts. The ensemble synthesizer (`ensemble.run_ensemble`) applies vetoes before confidence-weighted aggregation of the remaining specialists.

---

## 2. Deterministic specialists (179 total, `deterministic_specialists/`)

Each rule is a pure function `(candidate, ctx) → Optional[{severity, reasoning}]`. Severities: VETO / CAUTION / CONFIRM. Zero API cost per rule. Each is gated by `APPLIES_TO_SIGNALS`; a rule fires only when its signal set overlaps the candidate's signal (or, for options, its direction).

**Total: 179** (10 VETO + 92 CAUTION + 77 CONFIRM)

Organized by theme. Within each theme, sorted alphabetically by rule name.

### 2.1 Technical / momentum patterns (36)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CONFIRM` | `above_vwap_long_confirm` | LONG when price 0.1%-2.0% above session VWAP | Long |
| `CAUTION` | `bearish_divergence` | LONG when RSI ≥ 70 but StochRSI ≤ 50 (momentum tail rolling over) | Long |
| `CAUTION` | `below_vwap_long_caution` | LONG when price < 0 below session VWAP | Long |
| `CAUTION` | `below_vwap_short_extended` | SHORT when price > 3% below session VWAP | Short |
| `CONFIRM` | `bollinger_walk_down` | SHORT on Bollinger-walk-down signature | Short |
| `CONFIRM` | `bollinger_walk_up` | LONG on Bollinger-walk-up signature (RSI>60, ADX>25, above VWAP) | Long |
| `CONFIRM` | `cmf_accumulation_long` | LONG when CMF > +0.10 (institutional accumulation) | Long |
| `CAUTION` | `cmf_distribution_long` | LONG when CMF < -0.10 (institutional distribution) | Long |
| `CAUTION` | `cmf_neutral_low_signal` | when CMF in neutral zone (no flow conviction) | Long |
| `CAUTION` | `extended_above_vwap` | LONG when price > 3% above session VWAP | Long |
| `CAUTION` | `high_vol_caution` | when ATR% > 5% (high-vol regime — size down) | Long |
| `CONFIRM` | `high_volume_confirmation` | signal when volume_ratio ≥ 3× (institutional surge) | Long |
| `CAUTION` | `low_adx_no_trade` | on directional signal when ADX < 15 (no trend regime) | Long |
| `CAUTION` | `low_atr_breakout` | on breakout when ATR < 1% of price (very compressed range) | Long |
| `CONFIRM` | `low_vol_factor` | LONG when ATR% < 2% (low-vol factor exposure) | Long |
| `CAUTION` | `mfi_overbought_caution` | LONG when MFI ≥ 80 (volume-weighted overbought) | Long |
| `CONFIRM` | `mfi_oversold_confirm` | LONG when MFI ≤ 20 (volume-weighted oversold) | Long |
| `CAUTION` | `momentum_5d_negative_long` | LONG when ROC10 < -3% (negative momentum factor) | Long |
| `CONFIRM` | `momentum_5d_strong_positive` | LONG when ROC10 > 5% (momentum factor) | Long |
| `CONFIRM` | `near_fib_support` | LONG when within 1% of a Fibonacci support level | Long |
| `CONFIRM` | `orb_breakout` | directional signal on opening-range breakout | Long |
| `VETO` | `parabolic_blow_off` | LONG on parabolic blow-off (ROC10>15% AND RSI>85) | Long |
| `CONFIRM` | `rsi_bear_short_confirm` | SHORT when RSI < 45 (bearish backdrop) | Short |
| `CAUTION` | `rsi_bull_short_caution` | SHORT when RSI > 55 (shorting into bullish backdrop) | Short |
| `CAUTION` | `rsi_midline_bear` | LONG when RSI < 45 (below midline = bearish backdrop) | Long |
| `CONFIRM` | `rsi_midline_bull` | LONG when RSI > 50 (above midline = bullish backdrop) | Long |
| `VETO` | `rsi_overbought_late_stage` | LONG when RSI>80 AND price within 2% of 52-week high | Long |
| `CONFIRM` | `rsi_oversold_uptrend` | LONG when RSI<30 in an uptrend (positive 5-day return) | Long |
| `CAUTION` | `sentiment_divergence` | LONG when price rising but retail sentiment bearish | Long |
| `CAUTION` | `stoch_overbought` | LONG when Stochastic RSI > 80 | Long |
| `CONFIRM` | `stoch_oversold` | LONG on StochRSI < 20 in an uptrend | Long |
| `CONFIRM` | `strong_adx_trend_confirm` | signal when ADX > 30 (strong trend backdrop) | Long |
| `CONFIRM` | `strong_uptrend_pullback` | LONG on pullback (RSI 40-50) in strong uptrend (ADX≥25) | Long |
| `VETO` | `triple_overbought` | LONG when RSI ≥ 75 + StochRSI ≥ 80 + MFI ≥ 80 | Long |
| `CONFIRM` | `triple_oversold` | LONG when RSI ≤ 30 + StochRSI ≤ 20 + MFI ≤ 20 | Long |
| `CAUTION` | `weak_adx_breakout` | on breakouts when ADX < 20 (no trend backdrop) | Long |

### 2.2 Breakout / gap quality (4)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `extreme_gap_news` | on | Long |
| `CONFIRM` | `gap_down_capitulation` | LONG on gap < -3% AND RSI < 35 (capitulation bounce) | Long |
| `CAUTION` | `gap_into_resistance` | LONG when gap up >2% AND near 52-week high | Long |
| `VETO` | `volume_dry_breakout` | LONG when | Long |

### 2.3 Volume / liquidity (6)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CONFIRM` | `finra_short_volume_collapsed` | LONG when FINRA short-vol ratio < 0.20 (shorts covered) | Long |
| `CAUTION` | `finra_short_volume_elevated` | LONG when FINRA short volume ratio is elevated | Long |
| `CAUTION` | `news_volume_spike` | on news cluster (3+ items) without parsed SEC catalyst | Long |
| `CAUTION` | `sector_high_short_volume` | LONG when stock RS positive but short-vol elevated | Long |
| `CONFIRM` | `squeeze_release_with_volume_short` | SHORT on squeeze release with volume + strong ADX | Short |
| `CONFIRM` | `strong_volume_late_session` | signal when volume >= 2x in the afternoon session | Long |

### 2.4 Candlestick patterns (16)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `candle_bearish_engulfing` | LONG on bearish-engulfing 2-bar pattern | Long |
| `CONFIRM` | `candle_bullish_engulfing` | LONG on bullish-engulfing 2-bar pattern | Long |
| `CAUTION` | `candle_dark_cloud_cover` | LONG on dark-cloud-cover (partial bearish engulfing) | Long |
| `CAUTION` | `candle_doji` | on doji bar (body < 10% of range — indecision) | Long |
| `CAUTION` | `candle_evening_star` | LONG on evening-star 3-bar bearish reversal | Long |
| `CONFIRM` | `candle_hammer` | LONG on hammer candle (small upper body + long lower wick) | Long |
| `CAUTION` | `candle_hanging_man` | LONG on hanging-man (hammer shape after positive ROC) | Long |
| `CAUTION` | `candle_inside_day` | on inside-day entries (consolidation; direction unconfirmed) | Long |
| `CONFIRM` | `candle_marubozu_long` | LONG on green marubozu (body >= 80% of range) | Long |
| `CONFIRM` | `candle_marubozu_short` | SHORT on red marubozu (body >= 80% of range) | Short |
| `CONFIRM` | `candle_morning_star` | LONG on morning-star 3-bar reversal | Long |
| `CONFIRM` | `candle_outside_day` | signal on outside-day (range expansion bar) | Long |
| `CONFIRM` | `candle_piercing_pattern` | LONG on piercing pattern (partial bullish engulfing) | Long |
| `CAUTION` | `candle_shooting_star` | LONG on shooting-star candle (long upper wick + small bottom body) | Long |
| `CAUTION` | `candle_three_black_crows` | LONG on 3 consecutive lower-close red bars | Long |
| `CONFIRM` | `candle_three_white_soldiers` | LONG on 3 consecutive higher-close green bars | Long |

### 2.5 Smart money — insider / 13F / activist / dark pool (12)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CONFIRM` | `activist_13d_filed` | LONG on recent Schedule 13D filing (activist arrival) | Long |
| `CONFIRM` | `congressional_buying` | LONG when members of Congress net-bought recently | Long |
| `CONFIRM` | `dark_pool_accumulation` | signal when meaningful dark-pool ATS volume present | Long |
| `CONFIRM` | `insider_buying_near_earnings` | LONG when insiders bought near earnings | Long |
| `CONFIRM` | `insider_cluster_buying` | LONG on insider buying cluster (academic-strong signal) | Long |
| `CONFIRM` | `insider_cluster_with_options` | LONG when insider cluster + bullish UOA stack | Long |
| `CONFIRM` | `insider_recent_buys_meaningful` | LONG when 3+ recent insider buys (meaningful net activity) | Long |
| `CAUTION` | `insider_selling_near_earnings` | LONG when insiders sold near earnings | Long |
| `CAUTION` | `insider_sold_recently` | LONG when insiders net-sold with ≥3 transactions in last 30 days | Long |
| `CONFIRM` | `insider_track_record_strong` | LONG when insider buyer has strong historical track record | Long |
| `CAUTION` | `insider_track_record_weak` | LONG when insider buyer has poor historical track record | Long |
| `CONFIRM` | `star_manager_holding` | LONG when a star manager holds the name | Long |

### 2.6 Short-interest / borrow / squeeze (8)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `borrow_cost_high_short` | SHORT when borrow is HTB / high-cost | Short |
| `CAUTION` | `high_short_interest_long` | LONG when short interest > 20% of float | Long |
| `CONFIRM` | `short_squeeze_setup` | LONG on short-squeeze setup (high SI + MED/HIGH squeeze_risk) | Long |
| `CONFIRM` | `squeeze_release_setup` | directional signal when TTM-squeeze indicator fires | Long |
| `VETO` | `squeeze_risk_short` | SHORT when squeeze risk is HIGH | Short |
| `CONFIRM` | `squeeze_then_release_buy` | LONG when squeeze fires WITH volume surge AND strengthening trend | Long |
| `CAUTION` | `squeeze_unreleased` | when squeeze fires but volume_ratio < 1.2 (no release yet) | Long |
| `CONFIRM` | `squeeze_with_consensus` | LONG on squeeze + ensemble score ≥ 3 | Long |

### 2.7 Earnings / analyst / fundamentals / valuation (11)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `earnings_miss_streak` | LONG when 3+ quarters of earnings misses | Long |
| `CONFIRM` | `earnings_surprise_streak` | LONG when 4+ quarters of positive earnings surprises | Long |
| `CAUTION` | `earnings_within_window` | when days_to_earnings within profile.avoid_earnings_days | Long |
| `CONFIRM` | `expensive_short_confirm` | SHORT when PE > 50 | Short |
| `CAUTION` | `negative_earnings_revisions` | LONG when EPS revision direction is DOWN | Long |
| `CAUTION` | `pe_extreme_high` | LONG when PE > 50 (extreme valuation) | Long |
| `CONFIRM` | `pe_value_zone` | LONG when PE 5-15 AND profitable | Long |
| `CONFIRM` | `positive_earnings_revisions` | LONG when EPS revision direction is UP/higher | Long |
| `CONFIRM` | `quality_factor_long` | LONG on quality factor (positive revisions + sensible PE + positive ROC) | Long |
| `CAUTION` | `recent_8k_earnings_release` | on recent 8-K Item 2.02 (earnings release) | Long |
| `CAUTION` | `value_short_warning` | SHORT when PE in value zone (asymmetric upside risk) | Short |

### 2.8 Sentiment / attention (12)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `app_store_ranking_drop` | LONG on app-store ranking drop | Long |
| `CONFIRM` | `app_store_ranking_jump` | LONG on app-store ranking jump | Long |
| `CAUTION` | `google_trends_spike` | LONG when Google Trends spike (attention-driven entry) | Long |
| `CAUTION` | `no_news_low_attention` | on directional entry with no news + no catalyst signal | Long |
| `VETO` | `retail_euphoria_overbought` | LONG on RSI>75 + retail sentiment very bullish | Long |
| `CONFIRM` | `retail_panic_oversold` | LONG on RSI<30 + retail sentiment bearish | Long |
| `CAUTION` | `stocktwits_data_absent` | LONG on small-cap when StockTwits chatter is absent | Long |
| `CONFIRM` | `stocktwits_extreme_bearish` | LONG when StockTwits 7d sentiment < -0.50 (capitulation) | Long |
| `CAUTION` | `stocktwits_extreme_bullish` | LONG when StockTwits 7d sentiment > +0.70 (retail euphoria) | Long |
| `CAUTION` | `transcript_sentiment_bearish` | LONG on bearish transcript tone | Long |
| `CONFIRM` | `transcript_sentiment_bullish` | LONG on bullish transcript tone | Long |
| `CAUTION` | `wikipedia_attention_surge` | LONG on wikipedia pageview surge | Long |

### 2.9 Regulatory / corporate-event (14)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `biotech_milestone_upcoming` | on biotech catalyst upcoming (PDUFA/readout) | Long |
| `CAUTION` | `epa_osha_violations_present` | LONG on recent EPA/OSHA violations | Long |
| `CAUTION` | `fda_inspection_warning` | LONG when recent FDA inspection citations present | Long |
| `CAUTION` | `macro_event_imminent` | on entries within 1 day of FOMC / CPI / NFP | Long |
| `VETO` | `multiple_negative_catalysts` | LONG when 2+ negative catalysts stack | Long |
| `CONFIRM` | `multiple_positive_catalysts` | LONG when 2+ positive catalysts stack | Long |
| `CAUTION` | `nhtsa_recall_active` | LONG when auto OEM has recent NHTSA recalls | Long |
| `CONFIRM` | `patent_velocity_strong` | LONG when patent filing velocity is accelerating | Long |
| `CAUTION` | `recent_8k_acquisition` | on recent 8-K Item 1.01 (material definitive agreement) | Long |
| `CAUTION` | `recent_8k_exec_departure` | LONG on recent 8-K Item 5.02 (exec departure/appointment) | Long |
| `VETO` | `recent_8k_negative_event` | LONG on recent 8-K Items 1.03 / 4.02 / 2.06 | Long |
| `CAUTION` | `recent_8k_regulation_fd` | on recent 8-K Item 7.01 (Regulation FD disclosure) | Long |
| `CAUTION` | `risk_factor_diff_added` | LONG when 10-K/Q added new risk factors | Long |
| `VETO` | `sec_alert_high_severity` | LONG on HIGH/CRITICAL SEC filing alert | Long |

### 2.10 Macro / regime / market context (31)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `cboe_skew_complacent` | LONG when CBOE SKEW is LOW (complacency) | Long |
| `CAUTION` | `cboe_skew_extreme` | when CBOE SKEW signals elevated tail-risk pricing | Long |
| `CAUTION` | `crisis_state_long_caution` | LONG when crisis state is active | Long |
| `CONFIRM` | `crisis_state_short_confirm` | SHORT when crisis state is active | Short |
| `CAUTION` | `macro_gold_vol_high` | when GVZ (gold vol) is in high regime | Long |
| `CONFIRM` | `macro_low_vol_riskon` | LONG when cross-asset vol is broadly low (risk-on) | Long |
| `CAUTION` | `macro_oil_vol_high` | LONG when OVX (oil vol) is in high regime | Long |
| `CAUTION` | `macro_risk_off_cross_asset_vol` | when cross-asset vol (MOVE/OVX/GVZ) is high | Long |
| `CONFIRM` | `macro_treasury_low_riskon` | LONG when MOVE in low regime (rate vol contained) | Long |
| `CAUTION` | `macro_treasury_vol_high` | LONG when MOVE (treasury vol) is in high regime | Long |
| `CONFIRM` | `macro_yield_curve_steepening` | LONG on yield-curve steepening (reflation regime) | Long |
| `CAUTION` | `regime_bearish_long_caution` | LONG when market regime is bearish | Long |
| `CONFIRM` | `regime_bearish_short_confirm` | SHORT when market regime is bearish | Short |
| `CONFIRM` | `regime_bullish_long_confirm` | LONG when market regime is bullish | Long |
| `CAUTION` | `regime_bullish_short_caution` | SHORT when market regime is bullish | Short |
| `CAUTION` | `regime_volatile_caution` | on entries in volatile / crisis regimes (size down) | Long |
| `CAUTION` | `sector_downtrend_long` | LONG when sector is in a clear downtrend | Long |
| `CONFIRM` | `sector_relative_strength_confirm` | LONG when stock 5d ≥ sector 5d + 3pp | Long |
| `CAUTION` | `sector_rotation_bottom_loser` | LONG when candidate | Long |
| `CONFIRM` | `sector_rotation_top_winner` | LONG when candidate | Long |
| `CAUTION` | `sector_sector_rotation_signal` | LONG when sector trending up but stock RS < -5% | Long |
| `CONFIRM` | `sector_strength_aligned` | LONG when sector trending up AND stock RS positive | Long |
| `CAUTION` | `sector_weakness_caution` | LONG when stock 5d ≤ sector 5d - 3pp | Long |
| `CAUTION` | `spy_downtrend_long_caution` | LONG when SPY is in a downtrend | Long |
| `CONFIRM` | `spy_downtrend_short_confirm` | SHORT when SPY is in a downtrend | Short |
| `CONFIRM` | `spy_uptrend_long_confirm` | LONG when SPY is in an uptrend | Long |
| `CAUTION` | `vix_extreme_complacency` | LONG when VIX < 11 (complacency, mean-reversion risk) | Long |
| `VETO` | `vix_extreme_panic` | LONG when VIX > 35 (acute panic regime) | Long |
| `CAUTION` | `vix_high_caution` | LONG when VIX > 25 (elevated broad fear) | Long |
| `CONFIRM` | `vix_low_riskon` | LONG when VIX < 18 (low-vol risk-on regime) | Long |
| `CAUTION` | `yield_curve_inverted` | LONG sizing when yield curve is inverted | Long |

### 2.11 Options-specific (10)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CONFIRM` | `options_iv_cheap_for_buyers` | option-buy strategy when IV rank < 25 | Long |
| `CAUTION` | `options_iv_extreme_high` | when IV rank ≥ 75 (extreme premium / catalyst priced in) | Long |
| `CONFIRM` | `options_iv_normal_zone` | directional bet when IV rank in normal zone (25-60) | Long |
| `CONFIRM` | `options_iv_rich_for_sellers` | option-sell strategy when IV rank > 60 | Long |
| `CAUTION` | `options_pcr_complacent` | LONG when put/call ratio < 0.5 (complacency) | Long |
| `CONFIRM` | `options_pcr_panic` | LONG when put/call ratio > 1.5 (retail panic) | Long |
| `CONFIRM` | `options_unusual_calls` | LONG when unusual options flow is call-heavy (P/C < 0.6) | Long |
| `CAUTION` | `options_unusual_puts` | LONG when unusual options flow is put-heavy (P/C > 1.5) | Long |
| `CONFIRM` | `unusual_options_activity` | when unusual options flow aligns with signal direction | Long |
| `CAUTION` | `wide_spread_caution` | when slippage estimate > 0.15% (wide-spread proxy) | Long |

### 2.12 Calendar / time-of-day (3)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CONFIRM` | `end_of_quarter_window` | LONG in last 3 trading days of a quarter (window dressing) | Long |
| `CONFIRM` | `turn_of_month_strength` | LONG in turn-of-month window (positive seasonal bias) | Long |
| `CONFIRM` | `wednesday_strength` | LONG on Wednesday entries (weekday effect) | Long |

### 2.13 Execution / friction / liquidity (6)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `extreme_high_price_caution` | when price > $1000 (sizing/fill quality concerns) | Long |
| `CONFIRM` | `intraday_pattern_aligned` | signal when intraday pattern aligns with direction | Long |
| `CAUTION` | `intraday_pattern_opposed` | when intraday pattern opposes signal direction | Long |
| `CAUTION` | `penny_stock_caution` | on any entry when price < $5 | Long |
| `CAUTION` | `slippage_high_caution` | when slippage estimate > 0.3% (friction may eat edge) | Long |
| `CAUTION` | `wash_cycle_recent` | when reason text mentions recent wash cycle | Long |

### 2.14 Portfolio / holdings context (3)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `portfolio_already_long` | LONG when portfolio already long this name | Long |
| `CAUTION` | `portfolio_already_short` | SHORT when portfolio already short this name | Short |
| `CAUTION` | `portfolio_high_drawdown` | on entries when portfolio drawdown > 5% | Long |

### 2.15 Signal-ensemble quality (7)

| Severity | Rule | Purpose | Direction |
|---|---|---|---|
| `CAUTION` | `crowded_long` | LONG when short_vol_ratio < 0.15 AND analyst consensus is bullish | Long |
| `CAUTION` | `divergent_signals_caution` | when score is low but reason text claims a strong pattern | Long |
| `CAUTION` | `low_conviction_score` | when ensemble score ≤ 1 (low cross-screen agreement) | Long |
| `CAUTION` | `multi_alt_data_silent` | when alt-data is silent across all sources (pure-technical entry) | Long |
| `CONFIRM` | `multi_signal_consensus` | when ensemble score ≥ 3 (3+ underlying screens agree) | Long |
| `CAUTION` | `round_number_resistance` | LONG when price within 1% of a round-number level | Long |
| `CONFIRM` | `round_number_support` | LONG when price within 1% above a round-number level | Long |

---

## 3. How a candidate flows through the ensemble

On every cycle, every candidate that survives the meta-model pre-gate runs through both layers:

1. **Deterministic panel** (`run_panel(candidate, ctx)`) — every rule whose `APPLIES_TO_SIGNALS` matches the candidate fires its `evaluate(candidate, ctx)`. The fired verdicts are aggregated into a `DETERMINISTIC RULE PANEL` block injected into the apex AI prompt for that candidate, AND rendered as a compact `RULES: [V]name [C]name ...` suffix on each LLM specialist's candidate render.
2. **LLM specialist ensemble** (`ensemble.run_ensemble`) — each of the 8 specialists makes a separate call. The six re-scoped specialists consume the RULES suffix and write narrative theses on top; the two untouched specialists (`gamma_pin_specialist`, `option_spread_risk`) read raw candidate features.
3. **Veto authority** — if `risk_assessor`, `adversarial_reviewer`, `option_spread_risk`, or any deterministic VETO rule returns VETO, the candidate is dropped from the apex AI's consideration. The apex AI never sees a vetoed candidate.
4. **Synthesizer** — surviving specialists' verdicts are calibrated via per-specialist Platt scaling (`specialist_calibration.fit_platt_scaler`) then confidence-weighted into an aggregate ensemble verdict.
5. **Apex AI prompt** — every surviving candidate's full feature payload + RULE PANEL + RAG case-files + specialist verdicts + portfolio context lands in the single batched LLM call that picks the trades.

Each deterministic rule's exception is isolated — one bad rule logs at DEBUG and is skipped; the rest of the panel continues. Per-rule fire behavior is pinned by `tests/test_deterministic_specialists_2026_05_18.py` `_FIRE_CASES`.

## 4. Adding a new specialist

- **Deterministic** (preferred for fact-pattern checks): see `docs/10_METHODOLOGY.md` §4.5b. Drop a module under `deterministic_specialists/<name>.py` exposing `NAME`, `DESCRIPTION`, `APPLIES_TO_SIGNALS`, `evaluate(candidate, ctx)`. Add to `RULE_MODULES`. Add a positive-fixture row to `_FIRE_CASES`. Zero per-rule API cost; zero calibration overhead.
- **LLM-narrative** (reserve for synthesis work): see `docs/11_INTEGRATION_GUIDE.md` §5. A new LLM specialist multiplies per-cycle AI cost; justify the synthesis role.

## See also

- `docs/02_AI_SYSTEM.md` §4 — the two-layer ensemble architecture.
- `docs/17_SELF_TUNER_GUARDRAILS_AND_RAG.md` Phase 3 — the specialist library expansion history (8 → 187 in a single day, 2026-05-18).
- `docs/04_TECHNICAL_REFERENCE.md` §3b — module map.
- `tests/test_deterministic_specialists_2026_05_18.py` — per-rule fire-behavior pin.
