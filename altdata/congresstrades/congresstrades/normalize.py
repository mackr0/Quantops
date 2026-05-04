"""Turn free-text disclosure fields into structured data.

Disclosure forms give us messy inputs:
  - "Apple Inc. Common Stock"     → ticker AAPL
  - "aapl"                        → ticker AAPL
  - "APPLE INC - COMMON"          → ticker AAPL
  - "JP Morgan Chase & Co."       → ticker JPM
  - "Vanguard Total Stock ETF"    → ticker VTI (heuristic — could be VT or VTSAX)
  - "U.S. Treasury Bill"          → ticker None (not equity)
  - "$1,001 - $15,000"            → amount_low=1001, amount_high=15000

This module handles both, cleanly. MVP quality — can iterate on the
ticker map as failures are surfaced.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Amount-range parser
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(
    r"\$?([0-9,]+)\s*(?:-|to|–|—)\s*\$?([0-9,]+)",
    re.IGNORECASE,
)


def parse_amount_range(text: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """Parse '$1,001 - $15,000' into (1001, 15000).

    Returns (None, None) if unparseable. Handles common variants:
      '$1,001 - $15,000'
      '1001 to 15000'
      '$15,001–$50,000'  (en dash)
      'Over $50,000,000'   → (50_000_001, None)
    """
    if not text:
        return (None, None)

    text = text.strip()
    m = _AMOUNT_RE.search(text)
    if m:
        lo = int(m.group(1).replace(",", ""))
        hi = int(m.group(2).replace(",", ""))
        return (lo, hi)

    # "Over $50,000,000"
    over = re.search(r"over\s+\$?([0-9,]+)", text, re.IGNORECASE)
    if over:
        return (int(over.group(1).replace(",", "")) + 1, None)

    # "Under $1,001"
    under = re.search(r"under\s+\$?([0-9,]+)", text, re.IGNORECASE)
    if under:
        return (0, int(under.group(1).replace(",", "")) - 1)

    return (None, None)


# ---------------------------------------------------------------------------
# Transaction type normalizer
# ---------------------------------------------------------------------------

_TX_TYPE_MAP = {
    "p":  "buy",
    "purchase": "buy",
    "buy": "buy",
    "bought": "buy",
    "acquired": "buy",
    "s":  "sell",
    "sale": "sell",
    "sold": "sell",
    "s (partial)": "partial_sale",
    "sale (partial)": "partial_sale",
    "partial sale": "partial_sale",
    "s (full)": "sell",
    "sale (full)": "sell",
    "e": "exchange",
    "exchange": "exchange",
}


def normalize_transaction_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    key = raw.strip().lower()
    return _TX_TYPE_MAP.get(key, "other")


# ---------------------------------------------------------------------------
# Ticker extraction
# ---------------------------------------------------------------------------

# Hand-curated starter map of common company names → tickers.
# We expand this organically as real disclosures come in. For MVP,
# the ~80 most-traded US large-caps covers probably 60-70% of what
# congressional members actually trade (they trade boring stuff mostly).
_NAME_TO_TICKER = {
    # Mega-cap tech
    "apple": "AAPL", "apple inc": "AAPL", "apple computer": "AAPL",
    "microsoft": "MSFT", "microsoft corp": "MSFT", "microsoft corporation": "MSFT",
    "alphabet": "GOOGL", "alphabet inc": "GOOGL", "google": "GOOGL",
    "amazon": "AMZN", "amazon.com": "AMZN", "amazon com": "AMZN",
    "meta": "META", "meta platforms": "META", "facebook": "META",
    "nvidia": "NVDA", "nvidia corp": "NVDA", "nvidia corporation": "NVDA",
    "tesla": "TSLA", "tesla inc": "TSLA", "tesla motors": "TSLA",
    "netflix": "NFLX", "netflix inc": "NFLX",
    # Banks / finance
    "jp morgan chase": "JPM", "jpmorgan chase": "JPM", "jpmorgan": "JPM",
    "j p morgan": "JPM", "jp morgan": "JPM",
    "bank of america": "BAC", "bank of america corp": "BAC",
    "wells fargo": "WFC", "wells fargo & co": "WFC",
    "goldman sachs": "GS", "goldman sachs group": "GS",
    "morgan stanley": "MS",
    "charles schwab": "SCHW", "schwab": "SCHW",
    "blackrock": "BLK",
    "visa": "V", "visa inc": "V",
    "mastercard": "MA", "mastercard inc": "MA", "mastercard international": "MA",
    "american express": "AXP",
    "paypal": "PYPL",
    "berkshire hathaway": "BRK.B", "berkshire": "BRK.B",
    # Industrials / energy
    "exxon": "XOM", "exxon mobil": "XOM", "exxonmobil": "XOM",
    "chevron": "CVX", "chevron corp": "CVX",
    "conocophillips": "COP",
    "boeing": "BA", "boeing co": "BA",
    "lockheed martin": "LMT",
    "raytheon": "RTX", "raytheon technologies": "RTX", "rtx corp": "RTX",
    "caterpillar": "CAT",
    "general electric": "GE", "ge aerospace": "GE",
    "3m": "MMM",
    "honeywell": "HON", "honeywell international": "HON",
    # Healthcare
    "johnson & johnson": "JNJ", "johnson and johnson": "JNJ",
    "pfizer": "PFE", "pfizer inc": "PFE",
    "merck": "MRK", "merck & co": "MRK",
    "abbvie": "ABBV",
    "eli lilly": "LLY", "lilly": "LLY",
    "unitedhealth": "UNH", "unitedhealth group": "UNH",
    "cvs": "CVS", "cvs health": "CVS",
    "walgreens": "WBA", "walgreens boots alliance": "WBA",
    # Consumer
    "walmart": "WMT", "wal-mart": "WMT",
    "costco": "COST",
    "home depot": "HD", "the home depot": "HD",
    "target": "TGT",
    "nike": "NKE", "nike inc": "NKE",
    "coca-cola": "KO", "coca cola": "KO",
    "pepsi": "PEP", "pepsico": "PEP",
    "mcdonald": "MCD", "mcdonald's": "MCD",
    "starbucks": "SBUX",
    "procter & gamble": "PG", "procter and gamble": "PG", "p&g": "PG",
    "philip morris": "PM",
    # Semis
    "intel": "INTC", "intel corp": "INTC",
    "amd": "AMD", "advanced micro devices": "AMD",
    "qualcomm": "QCOM",
    "broadcom": "AVGO",
    "taiwan semiconductor": "TSM", "tsmc": "TSM",
    # Airlines / travel
    "delta air lines": "DAL", "delta airlines": "DAL",
    "american airlines": "AAL",
    "united airlines": "UAL",
    "southwest airlines": "LUV",
    # Crypto / brokerage
    "coinbase": "COIN", "coinbase global": "COIN",
    "robinhood": "HOOD", "robinhood markets": "HOOD",
    # ETFs common in disclosures
    "spdr s&p 500": "SPY", "spdr s and p 500": "SPY", "spy": "SPY",
    "vanguard total stock": "VTI",
    "vanguard s&p 500": "VOO", "vanguard 500": "VOO",
    "invesco qqq": "QQQ", "qqq": "QQQ",
    "ishares core s&p 500": "IVV",
    "vanguard growth": "VUG",
    # Cable / telecom / media
    "verizon": "VZ", "verizon communications": "VZ",
    "at&t": "T", "at and t": "T",
    "t-mobile": "TMUS", "t mobile": "TMUS",
    "comcast": "CMCSA",
    "disney": "DIS", "walt disney": "DIS", "walt disney co": "DIS",
}

# Ticker inside parentheses or brackets: common format on disclosures
# like "Apple Inc. (AAPL)" or "[AAPL]"
_TICKER_IN_PARENS_RE = re.compile(r"[\(\[\{]\s*([A-Z]{1,5}(?:\.[A-Z])?)\s*[\)\]\}]")

# Plain ticker match (uppercase, 1-5 letters, word boundary).
# False-positive-prone (e.g. "INC" matches), used as last resort only.
_BARE_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")

# Known non-ticker all-caps words that show up in disclosures
_NON_TICKER_WORDS = {
    # Corporate structure
    "INC", "CORP", "LLC", "LP", "LLP", "CO", "THE", "AND", "FUND",
    # Asset-class descriptors
    "ETF", "BOND", "INDEX", "NOTE", "NOTES", "BILL", "BILLS",
    "TREASURY", "MUNICIPAL", "MUNI", "CERTIFICATE", "CERTIFICATES",
    "STOCK", "COMMON", "PREFERRED", "CLASS", "SERIES", "SHARES",
    "US", "USA", "OPTION", "OPTIONS", "CALL", "PUT",
    # Account-type abbreviations (surfaced in 2025 data as false positives)
    "IRA", "ROTH", "SEP", "HSA", "CRT", "GRAT", "GRT",
    # Transaction-type abbreviations
    "OT", "PUR", "SALE", "BUY", "SELL", "DIV", "INT",
    # Other common non-ticker acronyms seen in disclosures
    "LTD", "TRUST", "ACCT", "JT", "SPS", "DC", "DEP",
    "CD", "CDS", "MMF", "REIT", "FHA", "GNMA", "FNMA",
}


def extract_ticker(asset_description: str) -> Optional[str]:
    """Best-effort ticker extraction. Returns None when genuinely uncertain
    — we'd rather store NULL than wrong.

    Strategy:
      1. Try ticker-in-parens pattern — highest confidence.
      2. Try name-to-ticker map — reliable for common names.
      3. Try bare all-caps word — noisy, only if nothing else.
    """
    if not asset_description:
        return None

    text = asset_description.strip()

    # Strategy 1: ticker in parens
    m = _TICKER_IN_PARENS_RE.search(text)
    if m:
        candidate = m.group(1)
        if candidate not in _NON_TICKER_WORDS and len(candidate) <= 5:
            return candidate.upper()

    # Strategy 2: name lookup (case-insensitive, partial match)
    lowered = text.lower()
    # Sort by length descending so longer names match first ("apple inc" before "apple")
    for name in sorted(_NAME_TO_TICKER.keys(), key=len, reverse=True):
        if name in lowered:
            return _NAME_TO_TICKER[name]

    # Strategy 3: bare uppercase token — last resort, low confidence.
    # Only accept if exactly one non-noise token, 3-5 chars
    all_caps = _BARE_TICKER_RE.findall(text)
    legit = [t for t in all_caps if t not in _NON_TICKER_WORDS and 2 <= len(t) <= 5]
    if len(legit) == 1:
        return legit[0].upper()

    return None
