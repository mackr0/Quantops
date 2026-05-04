"""Tests for the Senate HTML parser using a saved real PTR fixture.

Fixture: tests/fixtures/senate_ptr_james_banks.html (James Banks PTR
filed 2026-04-20, one SBUX sale). The real HTML — layout as Senate
currently renders it. If they change the layout and our parser breaks,
these tests will fail immediately.
"""

from pathlib import Path

from congresstrades.scrape_senate import (
    parse_electronic_ptr,
    parse_search_results,
    _clean_member_name,
)


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_html(name: str) -> str:
    with open(FIXTURE_DIR / name) as f:
        return f.read()


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------

class TestElectronicPtrParser:
    def test_extracts_member_name(self):
        html = _load_html("senate_ptr_james_banks.html")
        result = parse_electronic_ptr(html)
        # Header is "The Honorable James Banks (Banks, James E.)"
        assert "James" in result["member"]
        assert "Banks" in result["member"]
        # Honorific stripped
        assert "Honorable" not in result["member"]
        # Trailing "(Last, First)" stripped
        assert "(" not in result["member"]

    def test_extracts_trades(self):
        html = _load_html("senate_ptr_james_banks.html")
        result = parse_electronic_ptr(html)
        assert len(result["trades"]) == 1
        trade = result["trades"][0]
        assert trade["ticker_raw"] == "SBUX"
        assert "Starbucks" in trade["asset"]
        assert trade["asset_type"] == "Stock"
        assert "Sale" in trade["tx_type"]
        assert "$1,001" in trade["amount"]
        assert trade["txn_date"] == "04/15/2026"
        assert trade["owner"] == "Self"

    def test_handles_empty_html(self):
        result = parse_electronic_ptr("<html><body><p>nothing here</p></body></html>")
        assert result["member"] == ""
        assert result["trades"] == []

    def test_handles_malformed_html(self):
        # Must not raise on garbage
        result = parse_electronic_ptr("<<<broken<html")
        assert "trades" in result


class TestCleanMemberName:
    def test_strips_the_honorable(self):
        assert _clean_member_name("The Honorable Katie Britt") == "Katie Britt"

    def test_strips_trailing_alphabetic_index(self):
        assert _clean_member_name(
            "The Honorable James Banks (Banks, James E.)"
        ) == "James Banks"

    def test_collapses_whitespace(self):
        assert _clean_member_name("  The  Honorable   Jane    Doe  ") == "Jane Doe"

    def test_preserves_existing_clean_name(self):
        assert _clean_member_name("Rand Paul") == "Rand Paul"


# ---------------------------------------------------------------------------
# Search-results parser (DataTables JSON row → filing dict)
# ---------------------------------------------------------------------------

class TestSearchResultsParser:
    def test_electronic_filing_row(self):
        data = {
            "data": [
                ["James", "Banks", "Banks, James E. (Senator)",
                 '<a href="/search/view/ptr/680da3d8-5f81-43a3-a658-0493c0070378/" target="_blank">Periodic Transaction Report for 04/20/2026</a>',
                 "04/20/2026"],
            ]
        }
        rows = parse_search_results(data)
        assert len(rows) == 1
        r = rows[0]
        assert r["doc_id"] == "680da3d8-5f81-43a3-a658-0493c0070378"
        assert r["filing_type"] == "electronic"
        assert r["filing_date"] == "2026-04-20"
        assert r["first_name"] == "James"
        assert r["last_name"] == "Banks"

    def test_paper_filing_row(self):
        data = {
            "data": [
                ["Jane", "Doe", "Doe, Jane (Senator)",
                 '<a href="/search/view/paper/abc-123/" target="_blank">Paper Filing</a>',
                 "01/15/2025"],
            ]
        }
        rows = parse_search_results(data)
        assert rows[0]["filing_type"] == "paper"

    def test_empty_data(self):
        assert parse_search_results({"data": []}) == []
        assert parse_search_results({}) == []

    def test_malformed_row_skipped(self):
        # Row with fewer than 5 cols — skipped silently, not crash
        rows = parse_search_results({"data": [["X", "Y"]]})
        assert rows == []

    def test_row_without_link_skipped(self):
        # Missing href — can't extract doc_id, skip
        rows = parse_search_results({
            "data": [["A", "B", "A, B (Senator)", "plain text no link",
                      "01/01/2025"]]
        })
        assert rows == []
