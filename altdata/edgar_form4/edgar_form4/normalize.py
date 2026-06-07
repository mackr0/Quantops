"""Form 4 XML parsing + transaction-code interpretation.

The Form 4 XML schema is published by SEC. Key elements we extract:

  <issuerName>, <issuerTradingSymbol>, <issuerCik>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>
    <reportingOwnerRelationship>
      <isOfficer>, <isDirector>, <isTenPercentOwner>
      <officerTitle>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>YYYY-MM-DD</value>
      <transactionCoding>
        <transactionCode>             # A,P,S,M,F,D,G,etc.
        <equitySwapInvolved>
      <transactionAmounts>
        <transactionShares><value>
        <transactionPricePerShare><value>
        <transactionAcquiredDisposedCode><value>A|D
      <ownershipNature>
        <directOrIndirectOwnership><value>D|I

Multiple <nonDerivativeTransaction> blocks per Form 4 are normal —
a single insider can disclose several transactions on one filing.

Transaction codes (the ones that matter for the trade pipeline):
  P — Open-market or private purchase                ← BUY signal
  S — Open-market or private sale                    ← SELL signal
  A — Grant, award or other acquisition (not a buy in trader sense)
  M — Exercise/conversion of derivative
  F — Payment of exercise price / tax via shares
  D — Sale (other than to issuer) — sometimes treated as S
  G — Bona fide gift
  I — Discretionary transaction
  X — Exercise of in-the-money or at-the-money derivative
  C — Conversion of derivative security
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET


PARSER_VERSION = "form4-xml-v1"


# Transaction codes worth surfacing as signals (per SEC Form 4 inst).
# P / S are the open-market codes — those are the codes consumers
# treat as real "insider buying/selling." A (award) / M (conversion)
# are scheduled / mechanical and excluded from the buy/sell counts.
BUY_CODES = {"P"}
SELL_CODES = {"S", "D"}  # D=Sale to issuer, included with sales


def _text(el: Optional[ET.Element], path: str) -> Optional[str]:
    """Safely extract text from an XPath relative to el."""
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _to_float(v: Optional[str]) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_form4_xml(xml_text: str) -> Optional[Dict[str, Any]]:
    """Parse a Form 4 XML document.

    Returns a dict:
        {
            issuer_cik: "0000320193",
            issuer_name: "Apple Inc",
            issuer_ticker: "AAPL",
            reporting_owners: [
                {name, is_officer, is_director, is_ten_percent,
                 officer_title}, ...
            ],
            non_derivative_transactions: [
                {transaction_date, txn_code, shares,
                 price_per_share, value_usd, acquired_disposed,
                 direct_indirect, rpt_owner_name (best-effort
                 attribution if multiple owners)},
                ...
            ],
        }
    Returns None if the XML is malformed or doesn't look like Form 4.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    issuer = root.find("issuer")
    issuer_cik = _text(issuer, "issuerCik")
    issuer_name = _text(issuer, "issuerName")
    issuer_ticker = _text(issuer, "issuerTradingSymbol")
    if issuer_cik:
        issuer_cik = issuer_cik.zfill(10)
    else:
        return None  # Not a parseable Form 4

    reporting_owners = []
    for owner_el in root.findall("reportingOwner"):
        owner_id = owner_el.find("reportingOwnerId")
        relationship = owner_el.find("reportingOwnerRelationship")
        reporting_owners.append({
            "name": _text(owner_id, "rptOwnerName") or "",
            "cik": _text(owner_id, "rptOwnerCik") or "",
            "is_officer":
                (_text(relationship, "isOfficer") or "0") in ("1", "true"),
            "is_director":
                (_text(relationship, "isDirector") or "0") in ("1", "true"),
            "is_ten_percent":
                (_text(relationship, "isTenPercentOwner") or "0")
                in ("1", "true"),
            "officer_title": _text(relationship, "officerTitle"),
        })

    # Best-effort owner attribution when multiple insiders co-file.
    # Common case is a single reportingOwner.
    primary_owner = (
        reporting_owners[0] if reporting_owners
        else {"name": "Unknown insider"}
    )

    # 2026-06-07 — capture 10b5-1 plan indicator. SEC Form 4 stores
    # the "this trade is part of a pre-arranged Rule 10b5-1 plan"
    # signal as free-text inside a <footnote> element referenced by
    # <footnoteId id="F1"/> tags inside the transaction. A 10b5-1
    # sale by an insider is mechanical (the plan locked in the
    # trade months earlier) and carries far weaker bearish-signal
    # weight than a discretionary sale. The trade pipeline needs
    # this distinction to avoid weighting plan-driven activity as
    # if it were timed selling.
    footnotes_by_id: Dict[str, str] = {}
    footnotes_el = root.find("footnotes")
    if footnotes_el is not None:
        for fn in footnotes_el.findall("footnote"):
            fid = fn.get("id")
            text = (fn.text or "").strip()
            if fid:
                footnotes_by_id[fid] = text

    non_derivative_transactions = []
    table = root.find("nonDerivativeTable")
    if table is not None:
        for txn_el in table.findall("nonDerivativeTransaction"):
            txn_date = _text(txn_el, "transactionDate/value")
            coding = txn_el.find("transactionCoding")
            txn_code = _text(coding, "transactionCode")
            amounts = txn_el.find("transactionAmounts")
            shares = _to_float(_text(amounts, "transactionShares/value"))
            price = _to_float(
                _text(amounts, "transactionPricePerShare/value"),
            )
            acquired_disposed = _text(
                amounts, "transactionAcquiredDisposedCode/value",
            )
            ownership = txn_el.find("ownershipNature")
            direct_indirect = _text(
                ownership, "directOrIndirectOwnership/value",
            )
            value_usd = None
            if shares is not None and price is not None:
                value_usd = round(shares * price, 2)

            # 2026-06-07 — 10b5-1 plan detection. Walk every
            # <footnoteId id="..."/> reference anywhere inside this
            # transaction's element tree, resolve to the footnote
            # text, and check for the rule pattern. The footnote
            # references can live under transactionCoding (whole-
            # transaction note) or under any field-level sub-element
            # (price, shares, etc.) — recursive scan is the
            # cheapest way to cover all of them.
            is_10b5_1 = False
            for fn_ref in txn_el.iter("footnoteId"):
                fid = fn_ref.get("id")
                if not fid:
                    continue
                fn_text = footnotes_by_id.get(fid, "")
                if _is_10b5_1_footnote(fn_text):
                    is_10b5_1 = True
                    break

            non_derivative_transactions.append({
                "transaction_date": txn_date,
                "txn_code": txn_code,
                "shares": shares,
                "price_per_share": price,
                "value_usd": value_usd,
                "acquired_disposed": acquired_disposed,
                "direct_indirect": direct_indirect,
                "is_10b5_1_plan": is_10b5_1,
                # Owner attribution: use primary; in multi-owner
                # filings each owner's name is on the reportingOwner
                # block, not embedded per-transaction. The same set
                # of transactions is attributed equally to each
                # owner per SEC convention (Form 4 doesn't split
                # per-owner transaction blocks).
                "rpt_owner_name": primary_owner.get("name", ""),
                "is_officer": primary_owner.get("is_officer", False),
                "is_director": primary_owner.get("is_director", False),
                "is_ten_percent": primary_owner.get("is_ten_percent", False),
                "officer_title": primary_owner.get("officer_title"),
            })

    return {
        "issuer_cik": issuer_cik,
        "issuer_name": issuer_name or "",
        "issuer_ticker": issuer_ticker or None,
        "reporting_owners": reporting_owners,
        "non_derivative_transactions": non_derivative_transactions,
        "footnotes_by_id": footnotes_by_id,
    }


# Regex tolerates spaces and ANY of the common Unicode dash glyphs
# between "10b5" and "1". SEC filers using rich-text editors emit
# en-dash (U+2013), em-dash (U+2014), hyphen-minus (U+002D), the
# Unicode hyphen (U+2010), and even minus sign (U+2212).
# Case-insensitive via the re flag.
_DASH_CHARS = "-‐‑‒–—―−"
_10B5_1_RE = re.compile(
    rf"\b10b5[\s{re.escape(_DASH_CHARS)}]?1\b",
    re.IGNORECASE,
)


def _is_10b5_1_footnote(text: str) -> bool:
    """True iff the footnote text references a Rule 10b5-1 trading
    plan. Defensively case-insensitive + dash-variant-tolerant."""
    if not text:
        return False
    return bool(_10B5_1_RE.search(text))
