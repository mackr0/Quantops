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

    def test_pre_2013_filings_filtered_at_list_time(self):
        """SEC mandated structured XML for 13F-HR effective 2013-06-30.
        Filings before that date have only HTML/text infotables which
        our XML parser can't read. The list function must filter them
        out so we don't generate spurious 'No infotable.xml' WARNs
        on /issues (300+/day pre-2026-05-16)."""
        from unittest.mock import MagicMock
        from edgar13f.scrape import list_13f_filings_for_filer
        session = MagicMock()
        session.get.return_value.json.return_value = {
            "filings": {
                "recent": {
                    "form": ["13F-HR", "13F-HR", "13F-HR"],
                    "accessionNumber": ["modern-1", "old-1", "ancient-1"],
                    "periodOfReport": ["2024-09-30", "2010-09-30",
                                       "2006-09-30"],
                    "filingDate": ["2024-11-15", "2010-11-15",
                                   "2006-11-15"],
                    "primaryDocument": ["primary.xml", "primary.htm",
                                        "primary.htm"],
                }
            }
        }
        out = list_13f_filings_for_filer(session, "0000123456")
        assert len(out) == 1, (
            "Only the 2024 filing should pass the date filter; "
            "2010 and 2006 are pre-2013-06-30 and lack XML"
        )
        assert out[0]["accession_number"] == "modern-1"

    def test_namespaced_attributes_are_handled(self):
        """Pre-2026-05-16 the regex stripped tag prefixes (`<ns:foo>`)
        but NOT attribute prefixes (`<foo xsi:type="...">`). With the
        xmlns declaration already removed, an attribute like
        `xsi:type="ns1:USD"` raised "unbound prefix: line 2, column 0".
        Result: 17+/day silent parse_error rows for one specific
        filer since 2018."""
        xml = '''<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xmlns:ns1="http://www.sec.gov/edgar/common">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass xsi:type="ns1:STRING">COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>1000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>100</sshPrnamt>
      <sshPrnamtType xsi:type="ns1:STRING">SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion xsi:type="ns1:STRING">SOLE</investmentDiscretion>
  </infoTable>
</informationTable>'''
        # Pre-fix this raised
        # `xml.etree.ElementTree.ParseError: unbound prefix:
        #  line 2, column 0`.
        rows = parse_information_table(xml)
        assert len(rows) == 1, (
            "Namespaced attributes (xsi:type) broke the parser "
            "pre-2026-05-16. Expected 1 row from the fixture; got "
            f"{len(rows)}."
        )
        assert rows[0]["cusip"] == "037833100"
        assert rows[0]["company_name"] == "APPLE INC"
