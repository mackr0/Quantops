# Alt-Data Catalog and Candidates (2026-05-17)

## Why this document exists

The single canonical entry point for every alt-data signal the AI sees is `alternative_data.get_all_alternative_data(symbol)`. Adding a new signal = adding a key to the dict that function returns. Auditing the inventory = reading that function.

This file lists:
1. Every signal currently implemented (by source).
2. Every freely-available signal NOT yet implemented, ranked by expected signal-per-dev-hour.
3. Signals that were considered and rejected (with rationale, so we don't relitigate).

Update this file every time a signal is added, deprecated, or moved between tiers. It is the canonical inventory.

---

## Implemented signals (22 active + 4 macro = 26 total)

### Per-symbol signals (18) — `get_all_alternative_data(symbol)` returns

| Key | Source | What it measures |
|---|---|---|
| `insider` | yfinance | Recent insider buy/sell activity for the symbol |
| `short` | yfinance | Short interest ratio, days-to-cover |
| `fundamentals` | yfinance | P/E, P/B, profit margin, debt ratios |
| `options` | Alpaca options chain | Unusual options volume, gamma exposure |
| `intraday` | Alpaca bars | Recent intraday patterns (gap, fade, breakout) |
| `finra_short_vol` | FINRA daily CSV | Daily short volume / total volume ratio |
| `insider_cluster` | derived (Form 4 + yfinance) | Cluster-of-buys detection across insiders |
| `analyst_estimates` | yfinance | Recent EPS estimate revisions |
| `insider_earnings` | derived | Insider activity timed against earnings dates |
| `dark_pool` | derived | Dark pool volume / lit volume |
| `earnings_surprise` | yfinance | Recent EPS surprise history |
| `congressional_recent` | local DB (`altdata/congresstrades/`) | Recent Congressional disclosed trades |
| `institutional_13f` | local DB (`altdata/edgar13f/`) | Recent quarterly 13F holdings changes |
| `biotech_milestones` | local DB (`altdata/biotechevents/`) | PDUFA + AdComm events for biotechs |
| `stocktwits_sentiment` | local DB (`altdata/stocktwits/`) | Aggregated daily message sentiment |
| `google_trends` | pytrends | Search-volume momentum |
| `wikipedia_pageviews` | Wikipedia API | Pageview spike detection |
| `app_store_ranking` | scrape | Consumer app rank (for consumer-tech names) |

### Symbol-agnostic macro signals (4) — `macro` key, cached at module level

| Sub-key | Source | What it measures |
|---|---|---|
| `yield_curve` | FRED API | 2Y / 10Y treasury yields + curve slope |
| `fred_macro` | FRED API | Unemployment claims, CPI, consumer sentiment |
| `cboe_skew` | yfinance (`^SKEW`) | CBOE Skew Index — tail-risk pricing |
| `etf_flows` | yfinance / ETF.com | Sector ETF flows (XLF/XLK/XLV/...) |

Symbol-targeted SEC filings (10-K, 10-Q, 8-K diffs for held + shortlist symbols only) handled separately by `sec_filings.monitor_symbol` via `_task_sec_filings`. Not in the per-cycle alt-data dict because it produces alerts on demand.

---

## Candidates — not yet implemented

### Tier 1 — High signal, broad applicability (build NOW)

| # | Source | Free? | Why | Effort |
|---|---|---|---|---|
| 1 | **SEC 8-K broad discovery** | ✅ EDGAR | Daily scan of ALL new 8-K filings across the universe, parse Item type (1.01 M&A, 2.02 earnings, 5.02 officer change, 8.01 other material). Surfaces NEW opportunities, not just monitors known watchlist. The single highest-signal SEC filing type. | ~half day |
| 2 | **SEC 13D/G activist filings** | ✅ EDGAR | Real-time activist >5% positions (different from 13F which is quarterly + late). Strong directional signal for the named symbol. | ~half day |
| 3 | **MOVE / OVX / GVZ vol indices** | ✅ yfinance | Bond / oil / gold volatility (extends VIX). Differentiates "equity vol spike" from cross-asset stress. | ~30 min |
| 4 | **Reddit /r/wallstreetbets sentiment** | ✅ Reddit JSON API | Retail-driven name detection (NVDA, TSLA, PLTR, GME-class). Distinct from StockTwits. **Awaiting Reddit API access.** | ~1 day after access |

### Tier 2 — Sector-specific (✅ ALL BUILT 2026-05-17)

| # | Source | Status | Module |
|---|---|---|---|
| 5 | GitHub repo activity (commits/stars/active-30d) | ✅ Built | `altdata_tier2_corporate.get_github_activity` — 26 tech tickers mapped |
| 6 | FDA inspection citations | ✅ Built | `get_fda_inspections` — 17 pharma tickers mapped |
| 7 | NHTSA recall database | ✅ Built | `get_nhtsa_recalls` — 12 auto/EV tickers mapped |
| 8 | USDA crop reports | ✅ Built (requires `USDA_API_KEY` env var; graceful no-op without) | `altdata_tier2_macro.get_usda_crop_reports` |
| 9 | EIA energy data | ✅ Built (requires `EIA_API_KEY`; graceful no-op without) | `get_eia_energy_inventories` |
| 10 | CFTC Commitments of Traders | ✅ Built — Socrata public endpoint, no auth | `get_cftc_cot_positioning` |
| 11 | SAM.gov / USASpending gov contracts | ✅ Built — 11 defense/govtech tickers mapped | `get_sam_gov_contracts` |

### Tier 3 — Specialized / lower frequency / harder (✅ ALL SLOTS WIRED 2026-05-17; 6 fully functional, 4 placeholder)

| # | Source | Status | Notes |
|---|---|---|---|
| 12 | SEC 10-K YoY risk-factor diff | ✅ Built | `altdata_tier3.get_risk_factor_diff` — counts NEW risk sentences vs prior year |
| 13 | EPA / OSHA violations | 🟡 Placeholder | Slot wired; needs ticker→FRS-ID mapping table (not free) |
| 14 | FAA accident database | 🟡 Placeholder | Slot wired; FAA query API is heavy, deferred |
| 15 | BLS weekly jobless claims | ✅ Built | Reuses FRED ICSA series (`altdata_tier3.get_bls_jobless_claims`) |
| 16 | Wikipedia article EDITS | ✅ Built | `get_wikipedia_edits` — controversy precursor distinct from pageviews |
| 17 | USPTO bulk patents (re-implement) | 🟡 Placeholder | Slot wired; PatentsView v2 auth model changed Q4 2024, needs key |
| 18 | Job postings (LinkedIn/Indeed scrape) | 🟡 Placeholder | LinkedIn + Indeed actively block scrapers; would need paid data source |
| 19 | Sector ETF flow differentials | ✅ Built | Derived from existing `etf_flows`; lives in `altdata_tier2_macro.get_sector_flow_differentials` |
| 20 | CEO/insider personal track records | ✅ Built | Derived from existing `altdata/edgar_form4/data/edgar_form4.db` |
| 21 | Holdings of named star managers | ✅ Built | Derived from existing `altdata/edgar13f/data/edgar13f.db` — Berkshire / Pershing Square / Greenlight / Third Point hand-curated |

---

## Rejected candidates (don't relitigate without new data)

| Source | Why rejected | Date |
|---|---|---|
| Twitter/X sentiment | API became paywalled; can't justify cost vs StockTwits + WSB | 2025 |
| Bloomberg Terminal data | Cost prohibitive ($24K/yr/seat) for a single-operator system | 2025 |
| Glassdoor employee sentiment | Scraping is brittle + low signal for trade timing | 2025 |
| PatentsView v1 | API deprecated by USPTO Q4 2024 | 2024 — replacement #17 above |

---

## Build history

All 20 of 21 candidates landed 2026-05-17 (Reddit/WSB still blocked on API access). Tier 1 most-important first per operator request, then Tier 2 and Tier 3 in tier order:

1. ✅ **SEC 8-K broad discovery** — `sec_8k_broad.py` + `altdata/edgar_8k/` cron shim
2. ✅ **SEC 13D/G** — `sec_13dg_activist.py` + `altdata/edgar_13dg/` cron shim
3. ✅ **MOVE / OVX / GVZ** — `macro_data.get_cross_asset_vol`
4. ⏸ **Reddit /r/wallstreetbets** — awaiting Reddit API access
5-7. ✅ **GitHub / FDA / NHTSA / SAM.gov** — `altdata_tier2_corporate.py`
8-11. ✅ **USDA / EIA / CFTC / sector_flow_diff** — `altdata_tier2_macro.py`
12-21. ✅ **All Tier 3 slots wired** — `altdata_tier3.py` (6 fully functional, 4 placeholder with documented reason)

Reddit/WSB blocked on API access; revisit when access granted.

Tier 2 starts after the 13-profile experiment has produced ≥30 days of clean data — we'll know which sectors the AI actually picks from, which prioritizes sector-specific signals correctly instead of "build everything just in case."

---

## How to add a new signal (operator checklist)

1. Implement a `get_<signal_name>(symbol)` function in `alternative_data.py` (or in a separate scraper module if it's a daily-cron scrape into a per-source SQLite). It must return a dict that's `{}` on no-data / failure — never raise.
2. Add the key to the `get_all_alternative_data` return dict.
3. Update this file's "Implemented signals" table.
4. Add the source to `morning_health_check.sh` §H2's `EXPECTED` list so the daily check verifies it.
5. (If using a new persistent-store DB) place it at `altdata/<source>/data/<name>.db` so the §H1 glob auto-picks it up for freshness checks.

The audit chain is the safety net: any new signal that breaks `get_all_alternative_data` for AAPL will trip §H2 within 24 hours.
