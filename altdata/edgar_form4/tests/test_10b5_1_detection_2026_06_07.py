"""TODO `10b5-1 insider planned-sale tracking` — Form 4 footnote
parsing for the 10b5-1 plan indicator.

SEC Form 4 stores "this trade was made pursuant to a Rule 10b5-1
trading plan" as free text inside a `<footnote>` element referenced
by `<footnoteId id="F1"/>` tags inside the transaction's element
tree. A 10b5-1 sale is mechanical (the plan locked the trade in
months earlier) and carries far weaker bearish-signal weight than
a discretionary sale.

Contract pinned:

  Parser
  - footnote text matching `\\b10b5[\\s-]?1\\b` (case-insensitive,
    dash-variant-tolerant) marks the transaction `is_10b5_1_plan=True`.
  - Multiple footnoteId references → ANY match flips the flag.
  - No footnotes / no matching footnote → `is_10b5_1_plan=False`.

  Persistence
  - Round-trip through SQLite: the boolean stored as 0/1.

  Aggregation
  - `get_recent_insider_activity` splits buys/sells into
    discretionary vs planned. `net_direction` is computed from
    discretionary counts ONLY — a 10b5-1 sale wave doesn't flip
    the call. `cluster_count` excludes plan-driven buys.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT / "altdata" / "edgar_form4"))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _xml_with_footnote(footnote_text: str = None) -> str:
    """Form-4 XML with one sale transaction that references F1.
    If `footnote_text` is provided, the F1 footnote carries that
    text. Otherwise the footnote element is absent (regression case
    where no footnote → no plan flag)."""
    footnote_block = (
        f"<footnotes><footnote id=\"F1\">{footnote_text}</footnote>"
        f"</footnotes>"
    ) if footnote_text is not None else ""
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0001045810</issuerCik>
    <issuerName>NVIDIA CORP</issuerName>
    <issuerTradingSymbol>NVDA</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001234567</rptOwnerCik>
      <rptOwnerName>HUANG JENSEN</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <isDirector>0</isDirector>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-06-01</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>S</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
        <footnoteId id="F1"/>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>500.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  {footnote_block}
</ownershipDocument>
"""


class TestParserDetects10b5_1:

    def test_canonical_phrasing_flags_transaction(self):
        from edgar_form4.normalize import parse_form4_xml
        xml = _xml_with_footnote(
            "This transaction was effected pursuant to a Rule 10b5-1 "
            "trading plan adopted on March 15, 2024."
        )
        parsed = parse_form4_xml(xml)
        assert parsed is not None
        txns = parsed["non_derivative_transactions"]
        assert len(txns) == 1
        assert txns[0]["is_10b5_1_plan"] is True, (
            "Canonical 'Rule 10b5-1 trading plan' phrasing in the "
            "footnote MUST flag the transaction. Without this every "
            "downstream consumer treats plan-driven sales as "
            "discretionary bearish signals."
        )

    def test_case_insensitive_and_dash_variants(self):
        from edgar_form4.normalize import parse_form4_xml
        for variant in [
            "10b5-1 plan",         # canonical
            "10B5-1 plan",         # uppercase
            "10b5 1 plan",         # space instead of dash
            "Rule 10b5-1 plan",    # "Rule" prefix
            "10b5−1 plan",         # en-dash unicode
        ]:
            xml = _xml_with_footnote(f"Trade pursuant to {variant}.")
            parsed = parse_form4_xml(xml)
            assert parsed["non_derivative_transactions"][0][
                "is_10b5_1_plan"] is True, (
                f"Variant {variant!r} not detected — operator-visible "
                f"signal will mis-weight this transaction"
            )

    def test_unrelated_footnote_does_not_flag(self):
        from edgar_form4.normalize import parse_form4_xml
        xml = _xml_with_footnote(
            "Shares acquired through the company's ESPP at a 15% "
            "discount to FMV."
        )
        parsed = parse_form4_xml(xml)
        assert parsed["non_derivative_transactions"][0][
            "is_10b5_1_plan"] is False, (
            "Footnote about an unrelated topic must not flag the "
            "transaction as 10b5-1"
        )

    def test_no_footnote_at_all_does_not_flag(self):
        from edgar_form4.normalize import parse_form4_xml
        # Strip the footnoteId reference too so we test the bare
        # no-footnotes case
        xml = _xml_with_footnote(None)
        # Remove the <footnoteId/> tag too — simulating a fully
        # discretionary trade with no annotation
        xml = xml.replace('<footnoteId id="F1"/>', "")
        parsed = parse_form4_xml(xml)
        assert parsed["non_derivative_transactions"][0][
            "is_10b5_1_plan"] is False

    def test_dangling_footnoteid_without_footnote_text_does_not_flag(self):
        """The <footnoteId id="F1"/> reference exists but no
        matching <footnote id="F1">…</footnote> body — defensive
        against malformed filings. Must NOT raise; must NOT flag."""
        from edgar_form4.normalize import parse_form4_xml
        xml = _xml_with_footnote(None)  # has the footnoteId ref, no body
        parsed = parse_form4_xml(xml)
        assert parsed["non_derivative_transactions"][0][
            "is_10b5_1_plan"] is False


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

class TestPersistenceRoundTrip:

    def test_insert_and_read_back_with_plan_flag(self, tmp_path):
        from edgar_form4.store import init_db, insert_txn, upsert_company
        db = str(tmp_path / "form4.db")
        init_db(db)
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            upsert_company(conn, cik="0001045810",
                            name="NVIDIA", ticker="NVDA")
            ok = insert_txn(
                conn, accession_number="0001-23-456789",
                cik="0001045810",
                rpt_owner_name="HUANG JENSEN",
                transaction_date="2026-06-01", txn_code="S",
                shares=1000, price_per_share=500.0, value_usd=500000,
                is_officer=True, officer_title="CEO",
                is_10b5_1_plan=True,
            )
            assert ok
            row = conn.execute(
                "SELECT is_10b5_1_plan FROM insider_txns LIMIT 1"
            ).fetchone()
        assert row["is_10b5_1_plan"] == 1

    def test_existing_db_gets_column_via_migration(self, tmp_path):
        """init_db on a pre-existing DB without is_10b5_1_plan must
        ALTER-ADD the column. Without the migration, prod DBs that
        existed before 2026-06-07 would fail on every insert."""
        db = str(tmp_path / "form4_old.db")
        # Create an OLD-shape table (no is_10b5_1_plan column)
        with sqlite3.connect(db) as conn:
            conn.execute("""
                CREATE TABLE insider_txns (
                    id INTEGER PRIMARY KEY,
                    accession_number TEXT, cik TEXT,
                    rpt_owner_name TEXT, transaction_date TEXT,
                    txn_code TEXT, shares REAL
                )
            """)
        # Now run init_db — must add the column
        from edgar_form4.store import init_db
        init_db(db)
        with sqlite3.connect(db) as conn:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(insider_txns)").fetchall()]
        assert "is_10b5_1_plan" in cols, (
            "init_db must add is_10b5_1_plan via ALTER TABLE for "
            "existing DBs. Without this, prod DBs predating 2026-06-07 "
            "fail on every insert_txn call."
        )


# ---------------------------------------------------------------------------
# Aggregation — discretionary vs planned split
# ---------------------------------------------------------------------------

class TestAggregationSplit:

    def _seed(self, conn, txns):
        """Seed the DB with a list of transaction tuples:
        (txn_code, shares, value, is_10b5_1, owner_suffix)."""
        from edgar_form4.store import upsert_company, insert_txn
        upsert_company(conn, cik="0000001234",
                        name="Test", ticker="TEST")
        for i, (code, shares, value, planned, owner) in enumerate(txns):
            insert_txn(
                conn, accession_number=f"acc-{i:03d}",
                cik="0000001234",
                rpt_owner_name=f"Insider {owner}",
                transaction_date="2026-06-01", txn_code=code,
                shares=shares, price_per_share=10.0, value_usd=value,
                is_10b5_1_plan=planned,
            )

    def test_planned_sales_dont_flip_net_direction_to_selling(self, tmp_path):
        """The core regression — pre-2026-06-07 a CEO's 50,000-share
        10b5-1 sale wave (plan-driven) showed as net_direction='selling'
        and tilted the AI bearish on the name. Now: discretionary
        counts only drive direction."""
        from edgar_form4.store import init_db, connect, get_recent_insider_activity
        db = str(tmp_path / "form4.db")
        init_db(db)
        with connect(db) as conn:
            # 1 discretionary buy + 5 planned (10b5-1) sales —
            # without the split, this looks like 5-vs-1 selling.
            # WITH the split: 1-vs-0 discretionary → buying.
            self._seed(conn, [
                ("P", 100, 1000, False, "A"),
                ("S", 1000, 10000, True, "B"),
                ("S", 1000, 10000, True, "C"),
                ("S", 1000, 10000, True, "D"),
                ("S", 1000, 10000, True, "E"),
                ("S", 1000, 10000, True, "F"),
            ])
            result = get_recent_insider_activity(
                conn, "TEST", lookback_days=30,
            )
        assert result["recent_buys"] == 1
        assert result["recent_sells"] == 5
        assert result["discretionary_buys"] == 1
        assert result["discretionary_sells"] == 0
        assert result["planned_10b5_1_sells"] == 5
        assert result["net_direction"] == "buying", (
            f"5 planned 10b5-1 sales must NOT flip direction to "
            f"'selling' — they're mechanical, not bearish signal. "
            f"Discretionary count is 1 buy vs 0 sells. "
            f"got: {result['net_direction']!r}"
        )

    def test_discretionary_sales_still_flip_direction(self, tmp_path):
        """Sanity flip — a wave of DISCRETIONARY sales SHOULD still
        produce net_direction='selling'."""
        from edgar_form4.store import init_db, connect, get_recent_insider_activity
        db = str(tmp_path / "form4.db")
        init_db(db)
        with connect(db) as conn:
            self._seed(conn, [
                ("P", 100, 1000, False, "A"),
                ("S", 1000, 10000, False, "B"),
                ("S", 1000, 10000, False, "C"),
                ("S", 1000, 10000, False, "D"),
            ])
            result = get_recent_insider_activity(
                conn, "TEST", lookback_days=30,
            )
        assert result["net_direction"] == "selling"
        assert result["planned_10b5_1_sells"] == 0
        assert result["discretionary_sells"] == 3

    def test_cluster_count_excludes_planned_buys(self, tmp_path):
        """A coincidence of 5 different insiders' 10b5-1 BUYS funding
        on the same day is NOT a cluster signal. cluster_count must
        only count discretionary buyers."""
        from edgar_form4.store import init_db, connect, get_recent_insider_activity
        db = str(tmp_path / "form4.db")
        init_db(db)
        with connect(db) as conn:
            # 5 distinct insiders all making planned buys + 2 making
            # discretionary buys. Cluster signal = 2, not 7.
            self._seed(conn, [
                ("P", 100, 1000, True, "A"),
                ("P", 100, 1000, True, "B"),
                ("P", 100, 1000, True, "C"),
                ("P", 100, 1000, True, "D"),
                ("P", 100, 1000, True, "E"),
                ("P", 100, 1000, False, "F"),
                ("P", 100, 1000, False, "G"),
            ])
            result = get_recent_insider_activity(
                conn, "TEST", lookback_days=14,
            )
        assert result["cluster_count"] == 2, (
            f"cluster_count must only count DISCRETIONARY buyers; "
            f"plan-driven buys are scheduling artifacts, not "
            f"genuine clusters. got: {result['cluster_count']}"
        )
