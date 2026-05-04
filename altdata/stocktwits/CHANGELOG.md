# Changelog

Every meaningful code change. Newest at the top. Pre-commit hook
blocks `.py` changes without a same-day entry.

---

## 2026-04-25 — Initial release: StockTwits cache + daily sentiment rollup (Severity: feature)

Local cache of StockTwits messages and per-ticker daily sentiment
aggregates. Companion to `congresstrades`, `edgar13f`, and
`biotechevents` — same engineering pattern.

**What ships:**

- StockTwits REST API client polling `/streams/symbol/{TICKER}.json`
  and `/trending/symbols.json`. Free, no API key. 20s/req politeness
  to stay well under the 200/hour unauthenticated limit.
- `messages` table — one row per StockTwits message, dedup by `msg_id`
  (unique + immutable per the API).
- `ticker_sentiment_daily` table — daily rollup recomputed every
  fetch, no separate aggregation jobs.
- `trending_snapshots` table — top-N trending tickers per snapshot
  with rank preserved.
- `raw_responses` table — every API response cached so parser changes
  don't require re-scraping.
- CLI: `daily`, `ticker`, `trending`, `show`, `sentiment`, `runs`.
- Customizable watchlist via `~/stocktwits_watchlist.txt`; falls
  back to a default of ~37 mega-cap names when missing.

**Engineering invariants enforced by contract tests:**

- User-Agent with contact email
- 429/403 → `RateLimitedError`
- `REQUEST_DELAY_SEC >= 15s` (we target 20s; below 15s risks throttle
  on the 200/hour free-tier ceiling)
- Raw response cached on every fetch (parse-resilient)
- Parser version tagged on every row
- Per-ticker commit so mid-watchlist rate-limit preserves progress
- Daily aggregates recomputed when new messages land (always fresh)
- Idempotent migrations
- `msg_id` is the PK so re-pulls dedup automatically

**Sentiment classification follows StockTwits' user-supplied tags
(Bullish / Bearish), with unknown values normalized to None. We
preserve raw bodies for future re-classification (e.g. via a custom
sentiment model trained on the corpus).

This fills the role Reddit was supposed to play in the QuantOpsAI
signal stack while the Reddit API key remains pending. StockTwits is
not a Reddit replacement long-term — but for retail-name sentiment
on heavily-tracked tickers, the overlap is meaningful.
