"""Source-inspection contract tests — guard architectural invariants."""

import inspect

import edgar13f.scrape as sc
import edgar13f.store as st


class TestScraperContracts:
    def test_uses_sec_approved_user_agent(self):
        """SEC's EDGAR terms require a User-Agent with a contact email.
        Regression: missing/generic UA can result in 403 blocks from SEC."""
        assert "@" in sc.USER_AGENT, (
            "USER_AGENT must include a contact email per SEC EDGAR terms: "
            "https://www.sec.gov/os/accessing-edgar-data"
        )

    def test_has_rate_limit_detection(self):
        src = inspect.getsource(sc)
        assert "429" in src and "403" in src
        assert "RateLimitedError" in src

    def test_politeness_delay_present(self):
        assert sc.REQUEST_DELAY_SEC >= 1.0, (
            "EDGAR delay must be >= 1s. SEC's published limit is 10 req/sec "
            "but we target 1 req/sec to stay well below throttle thresholds."
        )

    def test_raw_stored_before_parse(self):
        """Architectural invariant: raw filing must persist BEFORE the
        parser runs. If the parser crashes, we still have the raw XML
        and can re-parse later without re-fetching."""
        src = inspect.getsource(sc.scrape_filer)
        raw_idx = src.find("insert_raw_filing")
        parse_idx = src.find("parse_information_table")
        assert raw_idx > 0
        assert parse_idx > raw_idx, (
            "insert_raw_filing must be called BEFORE parse_information_table — "
            "otherwise a parser crash loses the raw XML."
        )

    def test_parser_version_tag_present(self):
        assert hasattr(sc, "PARSER_VERSION")
        src = inspect.getsource(sc.scrape_filer)
        assert "parser_version" in src

    def test_commits_per_filing(self):
        """Per-filing commit so mid-run failures don't lose progress."""
        src = inspect.getsource(sc.scrape_filer)
        assert "db_conn.commit()" in src

    def test_filers_registry_is_non_empty(self):
        assert len(sc.FILERS) > 5, (
            "Starter filer list should include at least the major names "
            "(Berkshire, Renaissance, Bridgewater, etc.)."
        )

    def test_known_filer_berkshire_present(self):
        """Berkshire Hathaway's CIK 0001067983 is the canonical test filer."""
        assert "0001067983" in sc.FILERS


class TestStoreContracts:
    def test_raw_filings_table_exists_in_schema(self):
        assert "CREATE TABLE IF NOT EXISTS raw_filings" in st.SCHEMA

    def test_parser_version_column_in_holdings_schema(self):
        """We tag every parsed row with parser_version. Missing column
        would silently drop this capability."""
        assert "parser_version" in st.SCHEMA.split("holdings")[1][:600]

    def test_unique_constraint_allows_multiple_share_classes(self):
        """Berkshire's Apple position is actually THREE rows (different
        share classes). UNIQUE must include class_title so they don't
        collapse into one."""
        assert "UNIQUE (accession_number, cusip, class_title, put_call)" in st.SCHEMA

    def test_migrations_are_idempotent(self):
        """`duplicate column` must be swallowed so re-runs work."""
        src = inspect.getsource(st._apply_migrations)
        assert "duplicate column" in src.lower()
