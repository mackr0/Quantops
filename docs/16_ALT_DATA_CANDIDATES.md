# Alt-Data Catalog and Candidates (2026-05-17)

## Why this document exists

The single canonical entry point for every alt-data signal the AI sees is `alternative_data.get_all_alternative_data(symbol)`. Adding a new signal = adding a key to the dict that function returns. Auditing the inventory = reading that function.

This file lists:
1. Every signal currently implemented (by source).
2. Every freely-available signal NOT yet implemented, ranked by expected signal-per-dev-hour.
3. Signals that were considered and rejected (with rationale, so we don't relitigate).

Update this file every time a signal is added, deprecated, or moved between tiers. It is the canonical inventory.

---

## Implemented signals (29 per-symbol + 1 macro block with 5-6 sub-keys ≈ 30 total)

`alternative_data.get_all_alternative_data(symbol)` returns a dict whose top-level keys are listed below. The exact key count drifts as alt-data fetchers ship; the canonical inventory at any moment is whatever `get_all_alternative_data` actually returns (see `alternative_data.py:2421-2486`).

### Per-symbol signals — `get_all_alternative_data(symbol)` returns

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
| `recent_8k_events` | SEC EDGAR (`sec_8k_broad.py`) | Cycle-fresh: daily scan of NEW 8-K filings parsed by Item type (1.01 M&A, 2.02 earnings, 5.02 officer change, 8.01 other material). Built 2026-05-17 per Tier 1. |
| `activist_13dg` | SEC EDGAR (`sec_13dg_activist.py`) | Real-time activist >5% positions (different from quarterly 13F). Built 2026-05-17. |
| `github_activity` | `altdata_tier2_corporate.get_github_activity` | Commits / stars / active-30d for 26 tech tickers. Built 2026-05-17. |
| `fda_inspections` | `get_fda_inspections` | FDA inspection citations for 17 pharma tickers. Built 2026-05-17. |
| `nhtsa_recalls` | `get_nhtsa_recalls` | NHTSA recall database for 12 auto/EV tickers. Built 2026-05-17. |
| `sam_gov_contracts` | `get_sam_gov_contracts` | SAM.gov / USASpending contracts for 11 defense/govtech tickers. Built 2026-05-17. |
| `risk_factor_diff` | `altdata_tier3.get_risk_factor_diff` | SEC 10-K YoY risk-factor diff — counts NEW risk sentences vs prior year. Built 2026-05-17. |
| `epa_osha_violations` | `get_epa_osha_violations` | EPA ECHO + OSHA (via Cloudflare Worker proxy) for 25 heavy-industrial tickers. Built 2026-05-17. |
| `bls_jobless_claims` | `altdata_tier3.get_bls_jobless_claims` | Weekly Thursday jobless claims (FRED ICSA series). Built 2026-05-17. |
| `wikipedia_edits` | `get_wikipedia_edits` | Article EDIT counts — controversy precursor (distinct from pageviews). Built 2026-05-17. |
| `uspto_patents` | `get_uspto_patents` | USPTO last-365d applications for 13 tech tickers. Built 2026-05-17. |
| `job_postings` | `get_job_postings_count` | Greenhouse public-board API for 13 tickers. Built 2026-05-17. |
| `insider_track_records` | derived from `edgar_form4.db` | CEO / insider personal track records. Built 2026-05-17. |
| `star_manager_holdings` | derived from `edgar13f.db` | Berkshire / Pershing / Greenlight / Third Point hand-curated. Built 2026-05-17. |

### Symbol-agnostic macro signals — `macro` key, cached at module level

| Sub-key | Source | What it measures |
|---|---|---|
| `yield_curve` | FRED API | 2Y / 10Y treasury yields + curve slope |
| `fred_macro` | FRED API | Unemployment claims, CPI, consumer sentiment |
| `cboe_skew` | yfinance (`^SKEW`) | CBOE Skew Index — tail-risk pricing |
| `etf_flows` | yfinance / ETF.com | Sector ETF flows (XLF/XLK/XLV/...) |
| `cross_asset_vol` | `macro_data.get_cross_asset_vol` (1h cache) | MOVE / OVX / GVZ — bond / oil / gold vol (extends VIX). Built 2026-05-17 per Tier 1. |
| `sector_flow_diff` | `altdata_tier2_macro.get_sector_flow_differentials` | Sector ETF flow differentials (derived from `etf_flows`). Built 2026-05-17 per Tier 2. |

Symbol-targeted SEC filings (10-K, 10-Q, 8-K diffs for held + shortlist symbols only) handled separately by `sec_filings.monitor_symbol` via `_task_sec_filings`. Not in the per-cycle alt-data dict because it produces alerts on demand.

---

## Candidates — not yet implemented

### Tier 1 — High signal, broad applicability (✅ 3 of 4 built 2026-05-17; Reddit awaiting API access)

| # | Source | Status | Module / wiring | Why |
|---|---|---|---|---|
| 1 | **SEC 8-K broad discovery** | ✅ Built 2026-05-17 | `sec_8k_broad.py` + `altdata/edgar_8k/` cron shim; wired into `alternative_data.get_all_alternative_data` as `recent_8k_events` (cycle-fresh, not cached — 8:30am ET filings matter) | Daily scan of ALL new 8-K filings across the universe, parse Item type (1.01 M&A, 2.02 earnings, 5.02 officer change, 8.01 other material). Surfaces NEW opportunities, not just monitors known watchlist. The single highest-signal SEC filing type. |
| 2 | **SEC 13D/G activist filings** | ✅ Built 2026-05-17 | `sec_13dg_activist.py` + `altdata/edgar_13dg/` cron shim; wired as `activist_13dg` | Real-time activist >5% positions (different from 13F which is quarterly + late). Strong directional signal for the named symbol. |
| 3 | **MOVE / OVX / GVZ vol indices** | ✅ Built 2026-05-17 | `macro_data.get_cross_asset_vol` (1h cache); surfaced under `alt_data["macro"]["cross_asset_vol"]` | Bond / oil / gold volatility (extends VIX). Differentiates "equity vol spike" from cross-asset stress. |
| 4 | **Reddit /r/wallstreetbets sentiment** | ⏸ Awaiting Reddit API access | (estimate ~1 day after access; would land alongside `stocktwits_sentiment`) | Retail-driven name detection (NVDA, TSLA, PLTR, GME-class). Distinct from StockTwits. |

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

### Tier 3 — Specialized / lower frequency / harder (✅ 9 of 9 functional 2026-05-17; FAA dropped — see Rejected table)

| # | Source | Status | Notes |
|---|---|---|---|
| 12 | SEC 10-K YoY risk-factor diff | ✅ Built | `altdata_tier3.get_risk_factor_diff` — counts NEW risk sentences vs prior year |
| 13 | EPA + OSHA violations | ✅ Built (both live) | `get_epa_osha_violations` for 25 heavy-industrial tickers. EPA via ECHO direct (CV/SV/inspections/$ penalties). OSHA via Cloudflare Worker (`osha_proxy/`) because OSHA's CloudFront WAF hard-403s our DigitalOcean IP — the Worker runs from a Cloudflare IP that OSHA allows, parses the establishment.search HTML, returns JSON aggregates (inspections_5y, violations_5y). Worker is token-gated (`OSHA_PROXY_TOKEN`) and edge-cached 24h. Sample live: CVX = $29M EPA + 20 OSHA viols; US Steel = $14.9M EPA + 18 OSHA viols. |
| 14 | FAA accident database | ❌ Dropped 2026-05-17 | See Rejected table — ~95% of NTSB records are general-aviation, catastrophic airline events already captured by SEC 8-K Item 8.01 in real time. |
| 15 | BLS weekly jobless claims | ✅ Built | Reuses FRED ICSA series (`altdata_tier3.get_bls_jobless_claims`) |
| 16 | Wikipedia article EDITS | ✅ Built | `get_wikipedia_edits` — controversy precursor distinct from pageviews |
| 17 | USPTO patent applications | ✅ Built | `get_uspto_patents` — USPTO Open Data Portal (`api.uspto.gov`) — last-365d applications for 13 tech tickers; requires `USPTO_API_KEY` env var (free) |
| 18 | Job postings | ✅ Built | `get_job_postings_count` — Greenhouse public-board API for 13 tickers (HOOD, ABNB, MDB, NET, DDOG, PINS, LYFT, DBX, TWLO, SQ, RBLX, CPNG, ASAN); LinkedIn/Indeed remain blocked, Lever has no public-ticker hits |
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
| PatentsView v1 | API deprecated by USPTO Q4 2024 | 2024 — replacement #17 above (USPTO Open Data Portal) |
| Lever public boards | Tested 6 known boards (Plaid/Brex/Coinbase/Figma/Canva/GitLab) — all are private companies, zero match our ticker universe | 2026-05-17 |
| FAA / NTSB accident database | ~95% of NTSB records are general-aviation events (private pilots, Cessnas) — irrelevant to the 10 listed airlines we'd map. Catastrophic events that move airline stocks are already captured in real time by the SEC 8-K broad-discovery scraper (Item 8.01 "Other material events"). Building the NTSB AIDS CSV-ETL would be plumbing for redundant signal. | 2026-05-17 |
| Direct OSHA scrape from prod | OSHA's CloudFront WAF hard-403s our DigitalOcean prod IP regardless of UA / header massage. SOLVED via the Cloudflare Worker proxy in `osha_proxy/` — see signal #13 above. | 2026-05-17 |

---

## Status

All Tier 1, Tier 2, and Tier 3 candidates are wired in production except Reddit /r/wallstreetbets (awaiting API access). Per-source module map:

- **Tier 1** — `sec_8k_broad.py` (broad 8-K discovery), `sec_13dg_activist.py` (activist filings), `macro_data.get_cross_asset_vol` (MOVE / OVX / GVZ). Reddit/WSB remains blocked on API access.
- **Tier 2** — `altdata_tier2_corporate.py` (GitHub, FDA, NHTSA, SAM.gov) and `altdata_tier2_macro.py` (USDA, EIA, CFTC, sector flow differentials).
- **Tier 3** — `altdata_tier3.py` covers all slots; some sources require an API key and degrade gracefully to a no-op when one isn't configured.

---

## How to add a new signal (operator checklist)

1. Implement a `get_<signal_name>(symbol)` function in `alternative_data.py` (or in a separate scraper module if it's a daily-cron scrape into a per-source SQLite). It must return a dict that's `{}` on no-data / failure — never raise.
2. Add the key to the `get_all_alternative_data` return dict.
3. Update this file's "Implemented signals" table.
4. Add the source to `morning_health_check.sh` §H2's `EXPECTED` list so the daily check verifies it.
5. (If using a new persistent-store DB) place it at `altdata/<source>/data/<name>.db` so the §H1 glob auto-picks it up for freshness checks.

The audit chain is the safety net: any new signal that breaks `get_all_alternative_data` for AAPL will trip §H2 within 24 hours.
