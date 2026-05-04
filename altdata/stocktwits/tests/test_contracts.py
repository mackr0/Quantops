"""Source-inspection contract tests."""

import inspect

import stocktwits.scrape as sc
import stocktwits.store as st


class TestScraperContracts:
    def test_user_agent_has_email(self):
        assert "@" in sc.USER_AGENT

    def test_has_rate_limit_detection(self):
        src = inspect.getsource(sc)
        assert "429" in src and "403" in src
        assert "RateLimitedError" in src

    def test_politeness_delay_at_least_15s(self):
        """StockTwits free tier: 200 req/hour = 1 req/18 sec.
        We use 20s as a polite buffer. Anything below 15s risks throttle."""
        assert sc.REQUEST_DELAY_SEC >= 15.0, (
            "StockTwits delay must be >= 15s. Free-tier 200 req/hour = "
            "~18s/req minimum; we target 20s for safety. Below this "
            "puts us close to the throttle ceiling."
        )

    def test_raw_response_stored(self):
        """raw_response cached so parser changes don't require re-scraping."""
        src = inspect.getsource(sc.fetch_messages_for_ticker)
        assert "insert_raw_response" in src

    def test_parser_version_tagged(self):
        assert hasattr(sc, "PARSER_VERSION")

    def test_per_ticker_commit(self):
        """Daily watchlist is sequential — commit per ticker so a
        rate-limit halfway through preserves what we already pulled."""
        src = inspect.getsource(sc.fetch_messages_for_ticker)
        assert "db_conn.commit()" in src

    def test_aggregates_recomputed_per_fetch(self):
        """Daily sentiment must be recomputed every time we ingest new
        messages — otherwise the aggregate goes stale."""
        src = inspect.getsource(sc.fetch_messages_for_ticker)
        assert "upsert_daily_sentiment" in src


class TestStoreContracts:
    def test_raw_responses_table_exists(self):
        assert "CREATE TABLE IF NOT EXISTS raw_responses" in st.SCHEMA

    def test_messages_msg_id_is_primary_key(self):
        """StockTwits msg_ids are unique + immutable. Using them as PK
        gives us free dedup on re-pulls."""
        assert "msg_id          INTEGER PRIMARY KEY" in st.SCHEMA

    def test_daily_sentiment_pk_is_ticker_date(self):
        """One row per (ticker, date) — ON CONFLICT DO UPDATE handles re-runs."""
        assert "PRIMARY KEY (ticker, date)" in st.SCHEMA

    def test_migrations_idempotent(self):
        src = inspect.getsource(st._apply_migrations)
        assert "duplicate column" in src.lower()

    def test_parser_version_column_in_messages(self):
        assert "parser_version" in st.SCHEMA.split("messages")[1][:1500]
