"""Normalization helpers — value parsing, CUSIP → ticker mapping.

Pure functions, no I/O. Easy to test.
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

def parse_value_dollars(raw: Optional[str]) -> Optional[int]:
    """13F values are reported in DOLLARS (not thousands). Returns
    an integer count of dollars, or None if unparseable.

    Historical note: pre-2022 filings reported values in thousands,
    but SEC rule FR-86 (effective 2022) changed this to actual dollars.
    Every currently-fetched filing is post-change, so we treat the
    value string as actual dollars. If we ever backfill pre-2022
    filings we'll need a date-conditional parser.

    Verified against a real Berkshire 13F: Ally Financial
    (value=576074081, 12.7M shares) = $576M ≈ 12.7M × $45 stock price.
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


# Back-compat alias — existing callers may have imported the old name
parse_value_thousands = parse_value_dollars


def parse_shares(raw: Optional[str]) -> Optional[int]:
    """Shares count — no scaling. Integer."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Put/Call + Discretion normalization
# ---------------------------------------------------------------------------

def normalize_put_call(raw: Optional[str]) -> Optional[str]:
    """Normalize to 'PUT' | 'CALL' | None."""
    if not raw:
        return None
    s = raw.strip().upper()
    if s in ("PUT", "CALL"):
        return s
    return None


def normalize_discretion(raw: Optional[str]) -> Optional[str]:
    """Investment discretion: 'SOLE' | 'SHARED' | 'DFND' | None.

    Filings commonly use 'DFND' = defined (neither sole nor shared).
    """
    if not raw:
        return None
    s = raw.strip().upper()
    if s in ("SOLE", "SHARED", "DFND"):
        return s
    # Some use full words
    if s.startswith("SOL"):
        return "SOLE"
    if s.startswith("SH"):
        return "SHARED"
    if s.startswith("DFN") or s == "DEFINED":
        return "DFND"
    return None


# ---------------------------------------------------------------------------
# CUSIP validation
# ---------------------------------------------------------------------------

_CUSIP_RE = re.compile(r"^[A-Z0-9]{9}$")


def is_valid_cusip_shape(cusip: Optional[str]) -> bool:
    """Returns True iff the string is a 9-char alphanumeric (ignoring
    checksum). We don't verify the checksum — that's overkill for scraping."""
    if not cusip:
        return False
    return bool(_CUSIP_RE.match(cusip.strip().upper()))


def normalize_cusip(cusip: Optional[str]) -> Optional[str]:
    if not cusip:
        return None
    s = cusip.strip().upper()
    return s if is_valid_cusip_shape(s) else None


# ---------------------------------------------------------------------------
# Ticker mapping — seed with well-known CUSIPs; grow over time
# ---------------------------------------------------------------------------

# Hand-curated seed of CUSIPs → tickers. Extended as real filings surface
# new mappings that matter. This is the primary "name resolution" path
# because 13F filings don't include tickers.
_CUSIP_TO_TICKER: dict = {
    # Mega-cap tech
    "037833100": "AAPL",   # Apple
    "594918104": "MSFT",   # Microsoft
    "02079K305": "GOOGL",  # Alphabet Class A
    "02079K107": "GOOG",   # Alphabet Class C
    "023135106": "AMZN",   # Amazon
    "30303M102": "META",   # Meta Platforms
    "67066G104": "NVDA",   # Nvidia
    "88160R101": "TSLA",   # Tesla
    # Banks / finance
    "46625H100": "JPM",    # JPMorgan Chase
    "060505104": "BAC",    # Bank of America
    "949746101": "WFC",    # Wells Fargo
    "38141G104": "GS",     # Goldman Sachs
    "617446448": "MS",     # Morgan Stanley
    "808513105": "SCHW",   # Charles Schwab
    "92826C839": "V",      # Visa
    "57636Q104": "MA",     # Mastercard
    "084670702": "BRK.B",  # Berkshire Hathaway Class B
    # Healthcare
    "478160104": "JNJ",    # Johnson & Johnson
    "717081103": "PFE",    # Pfizer
    "58933Y105": "MRK",    # Merck
    "00287Y109": "ABBV",   # AbbVie
    "532457108": "LLY",    # Eli Lilly
    "91324P102": "UNH",    # UnitedHealth
    # Energy
    "30231G102": "XOM",    # Exxon Mobil
    "166764100": "CVX",    # Chevron
    # Consumer staples
    "931142103": "WMT",    # Walmart
    "742718109": "PG",     # P&G
    "191216100": "KO",     # Coca-Cola
    "713448108": "PEP",    # PepsiCo
    # Industrial
    "097023105": "BA",     # Boeing
    "149123101": "CAT",    # Caterpillar
    # Popular ETFs
    "78462F103": "SPY",    # SPDR S&P 500
    "46090E103": "QQQ",    # Invesco QQQ
    "922908363": "VTI",    # Vanguard Total Stock
    "46428Q109": "IWM",    # iShares Russell 2000
}


def cusip_to_ticker(cusip: Optional[str]) -> Optional[str]:
    """Map a CUSIP to ticker. Returns None when unknown — caller stores
    the CUSIP + company_name so we can query even without ticker."""
    norm = normalize_cusip(cusip)
    if norm is None:
        return None
    return _CUSIP_TO_TICKER.get(norm)
