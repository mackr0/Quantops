"""Tests for the Form 4 XML parser. Fixture-driven; the SEC schema
is stable but the parser needs to handle:
  - The full happy path (issuer + reportingOwner + nonDerivative txn)
  - Multiple reporting owners on one filing
  - Multiple transactions on one filing
  - Malformed XML → None (not a crash)
  - Form 4/A amendments (same shape, different filename)
"""
from __future__ import annotations

import pytest

from edgar_form4.normalize import parse_form4_xml


# ── Sample Form 4 XML (real shape, synthetic values) ────────────

SAMPLE_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
    <schemaVersion>X0306</schemaVersion>
    <documentType>4</documentType>
    <periodOfReport>2026-05-15</periodOfReport>
    <issuer>
        <issuerCik>0000320193</issuerCik>
        <issuerName>Apple Inc</issuerName>
        <issuerTradingSymbol>AAPL</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0001214156</rptOwnerCik>
            <rptOwnerName>COOK TIMOTHY D</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector>
            <isOfficer>1</isOfficer>
            <isTenPercentOwner>0</isTenPercentOwner>
            <officerTitle>CEO</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-05-15</value></transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>P</transactionCode>
                <equitySwapInvolved>0</equitySwapInvolved>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>10000</value></transactionShares>
                <transactionPricePerShare><value>175.50</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction><value>3300000</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
            </ownershipNature>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
"""

MULTI_TXN_XML = """<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <issuer>
        <issuerCik>0001318605</issuerCik>
        <issuerName>Tesla Inc</issuerName>
        <issuerTradingSymbol>TSLA</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerName>MUSK ELON</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector>
            <isOfficer>1</isOfficer>
            <officerTitle>CEO</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-05-12</value></transactionDate>
            <transactionCoding>
                <transactionCode>S</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>500</value></transactionShares>
                <transactionPricePerShare><value>250.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <transactionDate><value>2026-05-13</value></transactionDate>
            <transactionCoding>
                <transactionCode>S</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>200</value></transactionShares>
                <transactionPricePerShare><value>252.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
"""


class TestParseHappyPath:
    def test_extracts_issuer(self):
        parsed = parse_form4_xml(SAMPLE_FORM4_XML)
        assert parsed is not None
        assert parsed["issuer_cik"] == "0000320193"
        assert parsed["issuer_name"] == "Apple Inc"
        assert parsed["issuer_ticker"] == "AAPL"

    def test_extracts_reporting_owner(self):
        parsed = parse_form4_xml(SAMPLE_FORM4_XML)
        owners = parsed["reporting_owners"]
        assert len(owners) == 1
        assert owners[0]["name"] == "COOK TIMOTHY D"
        assert owners[0]["is_officer"] is True
        assert owners[0]["is_director"] is True
        assert owners[0]["is_ten_percent"] is False
        assert owners[0]["officer_title"] == "CEO"

    def test_extracts_transaction(self):
        parsed = parse_form4_xml(SAMPLE_FORM4_XML)
        txns = parsed["non_derivative_transactions"]
        assert len(txns) == 1
        t = txns[0]
        assert t["transaction_date"] == "2026-05-15"
        assert t["txn_code"] == "P"
        assert t["shares"] == 10000.0
        assert t["price_per_share"] == 175.50
        assert t["value_usd"] == 1755000.0  # 10000 * 175.50
        assert t["acquired_disposed"] == "A"
        assert t["direct_indirect"] == "D"
        # Owner attribution
        assert t["rpt_owner_name"] == "COOK TIMOTHY D"
        assert t["is_officer"] is True
        assert t["officer_title"] == "CEO"


class TestMultipleTransactions:
    def test_two_txns_in_one_filing(self):
        parsed = parse_form4_xml(MULTI_TXN_XML)
        assert parsed is not None
        txns = parsed["non_derivative_transactions"]
        assert len(txns) == 2
        assert txns[0]["txn_code"] == "S"
        assert txns[0]["shares"] == 500.0
        assert txns[0]["value_usd"] == 125000.0
        assert txns[1]["shares"] == 200.0
        assert txns[1]["value_usd"] == 50400.0


class TestMalformedXml:
    def test_garbage_returns_none(self):
        assert parse_form4_xml("this is not xml") is None

    def test_empty_returns_none(self):
        assert parse_form4_xml("") is None

    def test_xml_without_issuer_returns_none(self):
        bad = """<?xml version="1.0"?><ownershipDocument></ownershipDocument>"""
        assert parse_form4_xml(bad) is None
