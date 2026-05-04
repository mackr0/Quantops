"""Tests for the House PDF parser — specifically the table-extraction
logic in _parse_table. Fixture-free: we feed synthetic "table" matrices
(list-of-lists of strings) matching the shape pdfplumber returns, so we
don't need actual PDFs in tests.
"""

from congresstrades.scrape_house import _parse_table


def _header():
    return ["#", "Asset", "Transaction Type", "Date", "Notification Date", "Amount"]


class TestHeaderDetection:
    def test_finds_header_in_first_row(self):
        table = [
            _header(),
            ["1", "Apple Inc. (AAPL)", "P", "1/15/2025", "2/10/2025",
             "$1,001 - $15,000"],
        ]
        trades = _parse_table(table)
        assert len(trades) == 1
        assert trades[0]["asset_description"] == "Apple Inc. (AAPL)"

    def test_finds_header_in_second_row(self):
        # Some PTRs have a title row before the header
        table = [
            ["PTR Transactions", "", "", "", "", ""],
            _header(),
            ["1", "JPM", "P", "1/1/2025", "", "$15,001 - $50,000"],
        ]
        trades = _parse_table(table)
        assert len(trades) == 1

    def test_no_header_returns_empty(self):
        table = [
            ["random stuff", "", ""],
            ["more random stuff", "", ""],
        ]
        assert _parse_table(table) == []

    def test_empty_table(self):
        assert _parse_table([]) == []
        assert _parse_table([["only one row"]]) == []


# ---------------------------------------------------------------------------
# Continuation-row merge (the 2026-04-24 fix)
# ---------------------------------------------------------------------------

class TestContinuationRowMerge:
    """The 2026-04-24 fix: when pdfplumber splits a logical trade across
    two rows (because the asset name wrapped in the PDF), merge the
    continuation row's text into the previous trade's asset_description."""

    def test_continuation_merges_into_previous(self):
        table = [
            _header(),
            ["1", "Vanguard Total Bond", "P", "1/15/2025", "2/10/2025",
             "$1,001 - $15,000"],
            # Continuation: asset text, no type/date/amount
            ["", "Market Index Fund Admiral Shares", "", "", "", ""],
            ["2", "Apple Inc. (AAPL)", "S (Partial)", "2/1/2025", "",
             "$15,001 - $50,000"],
        ]
        trades = _parse_table(table)
        # Two trades, NOT three — continuation merged
        assert len(trades) == 2
        assert "Vanguard Total Bond" in trades[0]["asset_description"]
        assert "Admiral Shares" in trades[0]["asset_description"]
        assert trades[1]["asset_description"] == "Apple Inc. (AAPL)"

    def test_row_with_amount_is_NOT_continuation(self):
        # If a row has asset text AND any trade field, it's a real trade
        table = [
            _header(),
            ["1", "Real First Trade", "P", "1/1/2025", "", "$1 - $2"],
            # Second row has an amount — NOT a continuation, real trade
            ["2", "continuation-looking text", "", "", "", "$100 - $200"],
        ]
        trades = _parse_table(table)
        assert len(trades) == 2
        assert trades[1]["asset_description"] == "continuation-looking text"

    def test_multiple_continuations_all_merge_into_one(self):
        table = [
            _header(),
            ["1", "First part of asset", "P", "1/1/2025", "", "$1 - $2"],
            ["", "second part", "", "", "", ""],
            ["", "third part", "", "", "", ""],
            ["2", "Next real trade", "S", "1/2/2025", "", "$100 - $200"],
        ]
        trades = _parse_table(table)
        assert len(trades) == 2
        assert "First part" in trades[0]["asset_description"]
        assert "second part" in trades[0]["asset_description"]
        assert "third part" in trades[0]["asset_description"]

    def test_continuation_without_previous_is_dropped(self):
        # Leading continuation row (no previous trade to merge into)
        # should just be skipped, not crash. The fix ensures a
        # continuation-shaped row never becomes its own trade with
        # empty Type/Date/Amount — that was the ghost-row bug.
        table = [
            _header(),
            ["", "orphan continuation text", "", "", "", ""],
            ["1", "Real Trade", "P", "1/1/2025", "", "$1 - $2"],
        ]
        trades = _parse_table(table)
        assert len(trades) == 1
        assert trades[0]["asset_description"] == "Real Trade"

    def test_empty_rows_skipped(self):
        table = [
            _header(),
            ["1", "Apple Inc", "P", "1/1/2025", "", "$1 - $2"],
            [None, None, None, None, None, None],   # all empty
            ["", "", "", "", "", ""],                 # all blank strings
            ["2", "Microsoft Corp", "S", "1/2/2025", "", "$5 - $10"],
        ]
        trades = _parse_table(table)
        assert len(trades) == 2


# ---------------------------------------------------------------------------
# Short-asset noise row filtering
# ---------------------------------------------------------------------------

class TestNoiseFiltering:
    def test_short_asset_text_skipped(self):
        table = [
            _header(),
            ["1", "A", "P", "1/1/2025", "", "$1 - $2"],
            # len("A") < 3 → skipped (legit trades never have 1-2 char assets)
        ]
        # The "1" row has asset="A" (<3 chars) so it's filtered
        assert _parse_table(table) == []

    def test_noise_row_doesnt_break_subsequent_rows(self):
        table = [
            _header(),
            ["", "A", "", "", "", ""],  # short noise, filtered
            ["2", "Apple Inc. (AAPL)", "P", "1/1/2025", "", "$1 - $2"],
        ]
        trades = _parse_table(table)
        assert len(trades) == 1
        assert trades[0]["asset_description"] == "Apple Inc. (AAPL)"
