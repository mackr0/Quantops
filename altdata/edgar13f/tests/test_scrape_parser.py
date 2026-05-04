"""Parser tests using a real Berkshire 13F XML fixture.

The fixture is a snippet of the actual informationTable.xml we fetched
from SEC during initial development. If SEC changes the XML format, the
fixture goes stale — we update both fixture and parser together.
"""

from pathlib import Path

from edgar13f.scrape import parse_information_table


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


class TestParseInformationTable:
    def test_real_xml_returns_rows(self):
        """Sanity: the real Berkshire fixture has Ally Financial as the
        first entry. Must come through cleanly."""
        xml = _load("berkshire_infotable_sample.xml")
        rows = parse_information_table(xml)
        assert len(rows) > 0, (
            "Namespace-stripping regex regression? The parser returned "
            "zero rows on a real 13F XML — almost certainly a regex bug."
        )
        first = rows[0]
        assert first["cusip"] == "02005N100"   # Ally Financial
        assert "ALLY" in first["company_name"].upper()
        assert first["shares"] == 12_719_675
        # Post-2022 rule: value is in dollars, not thousands
        assert first["value_usd"] == 576_074_081

    def test_handles_xml_with_both_namespace_declarations(self):
        """Regression guard: 13F XML has BOTH xmlns:xsi AND default xmlns.
        Previous bug: regex stripped only the first → parser returned 0 rows.
        """
        xml = _load("berkshire_infotable_sample.xml")
        # Confirm the fixture has both namespaces
        assert "xmlns:xsi" in xml
        assert 'xmlns="http' in xml
        rows = parse_information_table(xml)
        assert len(rows) > 0

    def test_extracts_class_title(self):
        xml = _load("berkshire_infotable_sample.xml")
        rows = parse_information_table(xml)
        # All Berkshire positions use "COM" as title
        assert any(r["class_title"] == "COM" for r in rows)

    def test_unknown_cusips_are_skipped_cleanly(self):
        """A row with no CUSIP gets dropped rather than raising."""
        xml = """<informationTable xmlns="http://sec">
                   <infoTable>
                     <nameOfIssuer>NO CUSIP</nameOfIssuer>
                     <cusip></cusip>
                     <value>100</value>
                   </infoTable>
                 </informationTable>"""
        rows = parse_information_table(xml)
        assert rows == []

    def test_empty_xml_returns_empty(self):
        xml = '<informationTable xmlns="http://sec"></informationTable>'
        assert parse_information_table(xml) == []

    def test_malformed_xml_raises(self):
        """Malformed XML SHOULD raise — we rely on this so the scraper
        loop can catch, mark raw_filing as parse_error, and continue."""
        import pytest
        with pytest.raises(Exception):
            parse_information_table("<broken<<")
