"""Source-inspection contract tests.

These verify invariants that aren't easily expressed as unit tests:
  - the House scraper uses the corrected PTR_PDF_URL path
  - rate limiting + 429/403 handling are present
  - the Senate scraper initializes a session before searching
  - parser_version is tagged consistently

When these fail, something architecturally important changed. Read
the failure message, then decide if the test should adapt or the
code should revert.
"""

from __future__ import annotations

import inspect
import re

import congresstrades.scrape_house as sh
import congresstrades.scrape_senate as ss
import congresstrades.store as store


# ---------------------------------------------------------------------------
# House scraper contracts
# ---------------------------------------------------------------------------

class TestHouseScraperContracts:
    def test_ptr_pdfs_url_is_correct_path(self):
        """Discovered 2026-04-24: PTR PDFs live at ptr-pdfs/ not financial-pdfs/."""
        src = inspect.getsource(sh)
        assert "ptr-pdfs" in src, (
            "House scraper must use /public_disc/ptr-pdfs/ for PTR PDFs — "
            "not /public_disc/financial-pdfs/ which hosts the yearly ZIP index."
        )

    def test_has_rate_limit_detection(self):
        src = inspect.getsource(sh)
        assert "429" in src and "403" in src, (
            "House scraper must detect HTTP 429 and 403 rate-limit signals."
        )
        assert "RateLimitedError" in src, (
            "Rate-limit handling must raise RateLimitedError so callers can "
            "abort the run cleanly rather than silently skip hundreds of PDFs."
        )

    def test_has_politeness_delay(self):
        src = inspect.getsource(sh._download_pdf)
        assert "time.sleep" in src, (
            "House scraper must delay between PDF downloads — without a "
            "throttle, the scraper is one bad retry loop away from getting "
            "IP-blocked."
        )
        # Numeric delay > 0
        assert re.search(r"_PDF_REQUEST_DELAY_SEC\s*=\s*[0-9.]+", inspect.getsource(sh)), (
            "Delay must be a named module-level constant, not a magic number."
        )

    def test_continuation_row_merge_present(self):
        """The 2026-04-24 fix for pdfplumber's line-wrap ghost rows."""
        src = inspect.getsource(sh._parse_table)
        assert "continuation" in src.lower(), (
            "Parser must handle pdfplumber continuation rows — without this, "
            "wrapped asset names produce phantom trades with empty fields."
        )

    def test_commits_per_filing(self):
        """Per-filing commit so a mid-run failure doesn't lose progress."""
        src = inspect.getsource(sh.scrape_year)
        assert "db_conn.commit()" in src, (
            "scrape_year must commit per filing. Single end-of-run commit "
            "risks losing all progress on a mid-run crash."
        )


# ---------------------------------------------------------------------------
# Senate scraper contracts
# ---------------------------------------------------------------------------

class TestSenateScraperContracts:
    def test_session_initialize_exists(self):
        assert hasattr(ss, "SenateSession")
        assert hasattr(ss.SenateSession, "initialize")

    def test_search_requires_initialized_session(self):
        """search_ptrs without initialize() must fail clearly."""
        src = inspect.getsource(ss.SenateSession.search_ptrs)
        assert "search_csrf" in src, (
            "search_ptrs must use the search-page CSRF (captured during "
            "initialize()) — the home-page CSRF is rejected by the search "
            "endpoint."
        )

    def test_has_rate_limit_detection(self):
        src = inspect.getsource(ss)
        assert "429" in src and "403" in src
        assert "RateLimitedError" in src

    def test_has_politeness_delay(self):
        assert ss.REQUEST_DELAY_SEC >= 1.0, (
            "Senate delay must be >= 1 sec — Senate is pickier than House "
            "and killed previous scrapers that went too fast."
        )

    def test_agreement_form_posted(self):
        """Must POST the prohibition_agreement checkbox."""
        src = inspect.getsource(ss.SenateSession.initialize)
        assert "prohibition_agreement" in src, (
            "Senate session setup must POST prohibition_agreement=1 to the "
            "agreement form — otherwise subsequent queries are rejected."
        )

    def test_raw_filing_stored_before_parsing(self):
        """Future-proof design: raw HTML must be saved before parse runs,
        so a parser change in the future can re-parse historical docs
        without re-scraping."""
        src = inspect.getsource(ss.scrape_year)
        raw_idx = src.find("insert_raw_filing")
        parse_idx = src.find("parse_electronic_ptr")
        assert raw_idx > 0, "scrape_year must persist raw filings"
        assert parse_idx > raw_idx, (
            "raw filing persistence must happen BEFORE parsing — "
            "otherwise a parser crash loses the raw document and we'd "
            "need to re-scrape."
        )

    def test_parser_version_tag_present(self):
        assert hasattr(ss, "PARSER_VERSION")
        src = inspect.getsource(ss.scrape_year)
        assert "parser_version" in src, (
            "Every trade row must be tagged with parser_version so a "
            "future parser improvement can identify which rows to re-parse."
        )

    def test_senate_paginates_monthly(self):
        """The 100-row DataTables cap means we must split by time window.
        Monthly is the granularity we chose."""
        src = inspect.getsource(ss.scrape_year)
        assert "range(1, 13)" in src or "month" in src.lower(), (
            "scrape_year must iterate by month to stay under the Senate "
            "DataTables 100-row pagination cap."
        )


# ---------------------------------------------------------------------------
# Cross-module: storage contract
# ---------------------------------------------------------------------------

class TestStorageContract:
    def test_init_db_creates_raw_filings(self):
        """Both scrapers depend on raw_filings existing. A refactor that
        removes it breaks both."""
        src = inspect.getsource(store)
        assert "CREATE TABLE IF NOT EXISTS raw_filings" in src

    def test_migrations_are_idempotent_by_design(self):
        """_apply_migrations must swallow 'duplicate column' to survive
        re-runs against already-migrated DBs."""
        src = inspect.getsource(store._apply_migrations)
        assert "duplicate column" in src.lower()

    def test_insert_trade_parser_version_column(self):
        """Contract: insert_trade must persist parser_version in its column list."""
        src = inspect.getsource(store.insert_trade)
        assert "parser_version" in src


# ---------------------------------------------------------------------------
# Normalizer contracts (no-guessing style)
# ---------------------------------------------------------------------------

class TestNormalizerContracts:
    def test_non_ticker_words_has_real_world_false_positives(self):
        """The 2026-04-24 fix found these specific false positives in the
        actual 2025 House disclosure data. Losing them would re-introduce
        hundreds of bogus 'IRA' and 'CRT' ticker rows."""
        from congresstrades.normalize import _NON_TICKER_WORDS
        # From 2026-04-24 incident — do not remove without reading the changelog
        for w in ("IRA", "CRT", "OT", "ROTH", "SEP", "HSA"):
            assert w in _NON_TICKER_WORDS, (
                f"{w} must stay in _NON_TICKER_WORDS — "
                f"removing it reintroduces the 2026-04-24 bug where "
                f"account-type acronyms got emitted as stock tickers."
            )
