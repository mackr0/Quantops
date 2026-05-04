"""Tests for normalize.py — ticker extraction, amount parsing, tx-type canon.

These are pure-function tests with no I/O. Fast, hermetic.
"""

from congresstrades.normalize import (
    extract_ticker,
    normalize_transaction_type,
    parse_amount_range,
    _NON_TICKER_WORDS,
)


# ---------------------------------------------------------------------------
# Amount range
# ---------------------------------------------------------------------------

class TestParseAmountRange:
    def test_dollar_range_standard(self):
        assert parse_amount_range("$1,001 - $15,000") == (1001, 15000)

    def test_dollar_range_en_dash(self):
        # Senate uses en-dash in some renders
        assert parse_amount_range("$15,001 – $50,000") == (15001, 50000)

    def test_dollar_range_em_dash(self):
        assert parse_amount_range("$50,001 — $100,000") == (50001, 100000)

    def test_no_dollar_signs(self):
        assert parse_amount_range("1001 - 15000") == (1001, 15000)

    def test_to_keyword(self):
        assert parse_amount_range("$1,001 to $15,000") == (1001, 15000)

    def test_over_clause(self):
        low, high = parse_amount_range("Over $50,000,000")
        assert low == 50_000_001 and high is None

    def test_under_clause(self):
        low, high = parse_amount_range("Under $1,001")
        assert low == 0 and high == 1000

    def test_empty(self):
        assert parse_amount_range(None) == (None, None)
        assert parse_amount_range("") == (None, None)

    def test_garbage(self):
        # Unparseable shouldn't throw
        assert parse_amount_range("redacted") == (None, None)


# ---------------------------------------------------------------------------
# Transaction type
# ---------------------------------------------------------------------------

class TestTransactionType:
    def test_house_single_letter_codes(self):
        assert normalize_transaction_type("P") == "buy"
        assert normalize_transaction_type("S") == "sell"
        assert normalize_transaction_type("E") == "exchange"

    def test_partial_variants(self):
        assert normalize_transaction_type("S (partial)") == "partial_sale"
        assert normalize_transaction_type("Sale (Partial)") == "partial_sale"
        assert normalize_transaction_type("partial sale") == "partial_sale"

    def test_full_sale(self):
        assert normalize_transaction_type("S (full)") == "sell"
        assert normalize_transaction_type("Sale (Full)") == "sell"

    def test_senate_verbose_forms(self):
        assert normalize_transaction_type("Purchase") == "buy"
        assert normalize_transaction_type("Sale") == "sell"
        assert normalize_transaction_type("Exchange") == "exchange"

    def test_unknown_falls_to_other(self):
        assert normalize_transaction_type("some weird thing") == "other"

    def test_empty(self):
        assert normalize_transaction_type(None) is None
        assert normalize_transaction_type("") is None


# ---------------------------------------------------------------------------
# Ticker extraction
# ---------------------------------------------------------------------------

class TestExtractTicker:
    """The ticker extractor has three strategies in order:
    1. Parenthesized ticker (highest confidence)
    2. Name-to-ticker map lookup
    3. Bare uppercase word (noisy fallback)
    """

    def test_parens_aapl(self):
        assert extract_ticker("Apple Inc. (AAPL)") == "AAPL"

    def test_parens_class_ticker(self):
        assert extract_ticker("Berkshire Hathaway (BRK.B)") == "BRK.B"

    def test_name_to_ticker_apple(self):
        assert extract_ticker("Apple Inc. Common Stock") == "AAPL"

    def test_name_to_ticker_jpm(self):
        assert extract_ticker("JP Morgan Chase & Co.") == "JPM"

    def test_name_to_ticker_case_insensitive(self):
        assert extract_ticker("MICROSOFT CORPORATION") == "MSFT"

    def test_bare_ticker_accepted_when_unambiguous(self):
        # A lone 3-letter all-caps token that isn't a blocklisted word
        # falls through to the bare-regex strategy
        assert extract_ticker("ZZZ Corp") == "ZZZ"  # noise token but still returns

    def test_returns_none_for_treasury(self):
        # "U.S. Treasury Bill" — blocklist should prevent extraction
        assert extract_ticker("U.S. Treasury Bill") is None

    def test_does_not_emit_ira_false_positive(self):
        # The 2026-04-24 fix: IRA is an account type, not a ticker
        assert "IRA" in _NON_TICKER_WORDS
        assert extract_ticker("Vanguard IRA holding cash") is None

    def test_does_not_emit_crt_false_positive(self):
        assert "CRT" in _NON_TICKER_WORDS
        assert extract_ticker("Charitable Remainder Trust CRT #123") is None

    def test_does_not_emit_reit_false_positive(self):
        assert "REIT" in _NON_TICKER_WORDS

    def test_empty_input(self):
        assert extract_ticker("") is None
        assert extract_ticker(None) is None

    def test_prefers_parens_over_name(self):
        # If both signals exist, the paren ticker wins
        assert extract_ticker("Apple Inc. (XYZ)") == "XYZ"
