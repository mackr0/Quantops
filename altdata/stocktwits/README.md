# stocktwits

Local cache of StockTwits messages and daily-rollup sentiment for a
custom watchlist. Companion to `congresstrades`, `edgar13f`, and
`biotechevents` — same engineering pattern.

Reddit alternative for now while the Reddit API key application is
pending. StockTwits' free tier is generous (200 req/hour) and signal
overlap is meaningful for the active-trader retail names.

## Quickstart

```bash
cd /Users/mackr0/stocktwits
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./scripts/install-hooks.sh

# Smoke test: 3 tickers
python -m stocktwits.cli daily --max-tickers 3

# Full daily routine — 37 tickers, ~13 min at 20s/req
python -m stocktwits.cli daily

# Pull one ticker on demand
python -m stocktwits.cli ticker NVDA

# Current trending list
python -m stocktwits.cli trending

# Query
python -m stocktwits.cli show --ticker NVDA --sentiment bullish
python -m stocktwits.cli sentiment --since 2026-04-20
```

## Watchlist customization

Default watchlist is ~37 mega-cap tech, financials, biotech, etc.
(see `cli.DEFAULT_WATCHLIST`). To override, write one ticker per line
to `~/stocktwits_watchlist.txt`:

```
AAPL
NVDA
TSLA
PLTR
# Lines starting with # are ignored
```

## Architecture

```
stocktwits/
├── stocktwits/
│   ├── store.py          messages / daily / trending / raw_responses
│   ├── scrape.py         REST client + parsers
│   └── cli.py
├── tests/
├── hooks/pre-commit
└── data/stocktwits.db    output (gitignored)
```

## Rate limit

Free StockTwits API: 200 req/hour unauthenticated. We default to 20s
between requests (180/hour effective), well under the cap. Each ticker
fetch is one request returning ~30 recent messages.

If you register a free StockTwits app you can go to 400/hour. Drop
`REQUEST_DELAY_SEC` in `scrape.py` accordingly.

## Daily aggregation

Every time we fetch messages for a ticker, we recompute that ticker's
`ticker_sentiment_daily` row for any date the messages touched. This
keeps the daily-rollup view current without separate aggregation jobs.

## What we capture

| Field | Why |
|---|---|
| `messages.body` | Raw post text — can re-analyze later |
| `messages.sentiment` | StockTwits' bullish/bearish/none tag (user-supplied) |
| `messages.user_name` | For tracking high-rep traders |
| `messages.like_count` | Engagement — high-like messages are the conversation drivers |
| `ticker_sentiment_daily.net_sentiment` | (bullish − bearish) / total — main signal |
| `trending_snapshots` | What retail is talking about at any moment |

## Engineering pattern

Same as siblings — `raw_responses` cache before parse, `parser_version`
on every row, idempotent migrations, contract tests, pre-commit
CHANGELOG enforcement.
