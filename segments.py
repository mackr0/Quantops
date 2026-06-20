"""Market segment definitions for multi-account trading.

Two segments now: `stocks` (every Alpaca-tradable US equity, gated only
by per-profile price/volume thresholds) and `crypto` (separate by data
source — Alpaca uses 'BTC/USD' format and the crypto-data endpoint).

Pre-2026-05-20 this module shipped four stock segments by price bracket
(micro/small/midcap/largecap). That cap-tier grouping was vestigial from
an earlier system design; per-profile asset-class flags (`enable_stocks`,
`enable_options`, `enable_crypto`) plus per-profile min/max-price columns
now do what the cap tiers used to do. See `docs/22_UNIFIED_STOCK_UNIVERSE.md`.
"""

import os
from dotenv import load_dotenv

load_dotenv()

ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# ---------------------------------------------------------------------------
# Universes
# ---------------------------------------------------------------------------

# Stock universe — outage fallback only.
#
# The screener's primary path (`screen_dynamic_universe` in screener.py)
# pulls every Alpaca-tradable US equity (~8,000 symbols) each cycle.
# This curated list is used as:
#   (a) the safety-net return value when Alpaca's `list_assets` is
#       unreachable or returns suspiciously few entries
#   (b) the `alive_fallback` dedup-merge into the dynamic sample so
#       hand-picked names are guaranteed to be considered
#
# It is the deduplicated union of the four pre-2026-05-20 cap-tier
# lists (micro + small + midcap + largecap), kept here as a known-good
# set of liquid US names. Free to update as needed; not the source of
# truth for what the system can trade.
STOCK_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    # Semiconductors
    "AMD", "INTC", "QCOM", "AVGO", "TXN", "MU", "ADI", "NXPI", "KLAC",
    "LRCX", "AMAT", "ASML", "ON", "WOLF", "LSCC", "SMCI", "IONQ", "RGTI",
    "ACLS", "AEHR", "MRVL", "SWKS", "QRVO", "MPWR", "QUBT",
    # Software / cloud / SaaS
    "CRM", "ORCL", "ADBE", "NOW", "INTU", "WDAY", "TEAM", "ZM",
    "CDNS", "SNPS", "PTC", "FICO", "CPRT", "CSGP", "VRSK",
    "NET", "RBLX", "PINS", "TTD", "ZS", "BILL", "HUBS", "TWLO", "MDB",
    "GTLB", "DOCN", "MNDY", "APP", "ESTC", "CFLT", "PCOR", "BRZE",
    "CWAN", "ASAN", "FROG", "ZI", "VEEV", "PAYC", "PCTY", "WK", "APPF",
    "BSY", "AGYS", "EVBG", "SEMR", "COUR",
    "SHOP", "WISH", "POSH", "REAL", "RVLV", "PATH", "AI", "BBAI", "SOUN",
    # Cybersecurity
    "CRWD", "OKTA", "S", "TENB", "RPD", "VRNS", "CYBR",
    "FTNT", "PANW",
    # Internet / media / streaming
    "NFLX", "DIS", "CMCSA", "UBER", "BKNG",
    "SNAP", "WBD", "PARA", "LYV", "ROKU",
    "GENI", "IRNT", "IQ", "WB", "EBON", "GPRO", "VUZI",
    # Networking / IT services
    "CSCO", "IBM", "ACN", "DELL", "HPQ",
    # Payments / fintech
    "V", "MA", "PYPL", "FIS", "FISV", "GPN", "ADP", "PAYX",
    "COF", "AXP",
    "SOFI", "HOOD", "AFRM", "UPST", "LC", "ALLY", "AXOS", "ML", "LMND",
    "VNET", "SLM", "NAVI", "CACC", "TOST",
    "NU", "STNE", "PAGS", "DLOCAL",
    # Banks / brokers / asset managers
    "JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "TFC",
    "SCHW", "BLK",
    # Aerospace / defense / industrials
    "BA", "RTX", "LMT", "NOC", "GD", "GE", "HON",
    "CAT", "DE", "MMM", "ITW", "EMR", "ETN", "ROK",
    "XPO", "SAIA", "ODFL", "WERN", "JBHT", "KNX",
    "GXO", "LSTR", "ARCB", "SNDR",
    # Healthcare / devices / diagnostics
    "ISRG", "DXCM", "ALGN", "IDXX", "ZBH", "SYK", "MDT", "ABT",
    "BSX", "EW",
    "TMO", "DHR", "A", "BIO", "IQV",
    "INSP", "NUVB", "TWST", "PACB", "CERT", "NTRA", "GH",
    "DOCS", "TDOC", "AMWL", "GDRX", "OSCR", "HIMS",
    # Biotech / pharma
    "VRTX", "REGN", "AMGN", "GILD", "BIIB", "BMY",
    "LLY", "PFE", "MRK", "JNJ", "ABBV",
    "DNA", "ADMA", "WVE", "OLPX", "RVMD", "EXAS", "MRNA",
    "BNTX", "CRSP", "NTLA", "BEAM", "EDIT", "VERV", "VIR", "FOLD",
    "APLS", "FATE", "ACAD", "TGTX", "CERE", "ALNY", "SMMT", "IONS",
    "RXRX", "VKTX", "LUNG", "KALA", "TBIO", "ABCL", "OPK",
    "BNGO", "SAVA",
    # Health insurance / managed care / services
    "UNH", "CI", "HUM", "CNC", "MOH", "ELV",
    "HCA", "THC", "UHS", "DVA",
    # Consumer staples
    "KO", "PEP", "MDLZ", "KHC", "HSY", "GIS", "SJM", "CAG",
    "HRL", "TSN", "MKC", "CLX", "CL", "PG", "KMB", "CHD", "EL",
    # Retail / consumer brands
    "COST", "WMT", "TGT", "HD", "LOW", "TJX", "ROST", "BURL",
    "FIVE", "OLLI", "DKS", "BBWI", "VSCO", "GPS", "PVH",
    "RL", "TPR", "CPRI", "LEVI", "BJ",
    "SKX", "DECK", "BIRK",
    "DBI", "ANF", "URBN", "AEO",
    "CHWY", "W", "CVNA", "LYFT", "SQ", "ETSY",
    # Restaurants / food service
    "SBUX", "MCD", "YUM", "CMG", "DPZ", "QSR",
    "CAVA", "BROS", "WING", "SHAK", "CELH", "MNST",
    "ELF", "CROX", "DUOL", "LULU", "DIN",
    "COTY", "FIZZ", "PRPL", "IRBT", "LL", "PLBY",
    # Apparel / lifestyle
    "NKE",
    # Airlines / cruise / travel
    "LUV", "DAL", "UAL", "AAL",
    "JBLU", "SAVE", "NCLH", "CCL", "RCL", "TRIP", "ABNB",
    "HTHT", "LTH", "DASH", "EXPE",
    "ERJ", "AZUL", "CPA",
    # Hotels / travel infrastructure
    "MAR", "HLT",
    # Telecom / media
    "T", "VZ", "TMUS", "SIRI", "IRDM", "LUMN", "GSAT",
    "MGNI", "PUBM", "IAS", "DV", "CARG",
    # Gaming / entertainment
    "PENN", "DKNG", "U", "SKLZ",
    # EVs / autos / mobility
    "RIVN", "LCID", "NIO", "XPEV", "LI", "FSR", "GOEV", "WKHS", "NKLA",
    "MVST", "QS", "CHPT", "BLNK", "EVGO", "REE",
    # Clean energy / hydrogen
    "PLUG", "FCEL", "BE", "RUN", "NOVA", "ARRY", "STEM", "OPAL",
    "MAXN", "JKS", "DQ",
    # Oil & gas / energy
    "RIG", "ET", "AM", "AR", "CNX", "BTU", "SWN", "KOS", "TELL", "BTE",
    "CEIX", "NEXT", "SD", "HPK", "CPE", "SM", "CRGY", "VET",
    "CTRA", "OVV", "CRK",
    "NOG", "VTLE", "CHRD", "MTDR",
    # Mining / metals / materials
    "GOLD", "HL", "CDE", "AG", "PAAS", "SVM", "FSM", "MAG", "AUY",
    "MUX", "GPL", "EXK", "SILV", "GATO", "USAS",
    "CLF", "X", "AA", "VALE", "PBR", "ITUB", "SID", "BBD",
    # REITs / real estate
    "AGNC", "NLY", "TWO", "MFA", "IVR", "NYMT", "CIM", "MITT",
    "RC", "BRMK",
    # Space / aerospace startups
    "RKLB", "ASTS", "LUNR", "JOBY", "ACHR", "LILM",
    "SPCE", "ASTR", "RDW", "BKSY",
    # LiDAR / sensors / quantum
    "LAZR", "MVIS", "LIDR", "OUST", "AEVA", "INVZ",
    # Cannabis (penny territory)
    "SNDL", "ACB", "HEXO", "OGI", "CGC", "TLRY",
    # Crypto miners / blockchain-adjacent
    "MARA", "RIOT", "HUT", "BITF", "CIFR", "CLSK", "IREN", "WULF", "COIN",
    # International / ADRs
    "UMC", "ASX", "QFIN", "VIPS", "JD", "BABA", "BIDU",
    "TAL", "EDU", "FUTU",
    "GRAB", "SE", "CPNG", "MELI",
    "VTRS", "TEVA", "PRGO",
    # Other liquid names
    "PLTR", "F", "PCG", "BB", "NOK",
    "CRSR", "LOGI", "HEAR",
    "PSFE", "CLOV", "OPEN",
    "APPH", "HYLN", "PTRA", "GBS", "VG", "ME",
    "SWI", "JAMF",
]


# Crypto universe — Alpaca uses "BTC/USD" format, yfinance uses "BTC-USD".
# Stored in Alpaca format; screener/market_data converts for yfinance.
CRYPTO_UNIVERSE = [
    "BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD",
    "ADA/USD", "AVAX/USD", "DOT/USD", "LINK/USD", "LTC/USD",
    "UNI/USD", "AAVE/USD", "FIL/USD", "GRT/USD", "RENDER/USD",
    "ARB/USD", "ONDO/USD", "PEPE/USD", "SHIB/USD", "BONK/USD",
    "WIF/USD", "TRUMP/USD", "HYPE/USD", "BCH/USD", "SUSHI/USD",
    "CRV/USD", "BAT/USD", "LDO/USD", "POL/USD", "XTZ/USD",
    "YFI/USD", "SKY/USD", "PAXG/USD",
]


# ---------------------------------------------------------------------------
# Segment definitions
# ---------------------------------------------------------------------------
#
# Two segments now. The dict values are SEEDS for new profile rows: the
# `min_price` / `max_price` / `min_volume` / `stop_loss_pct` / etc. fields
# are copied into the profile row at creation time and are then editable
# per-profile thereafter. The `universe` field is the outage fallback for
# the screener — primary candidate discovery goes through Alpaca's full
# tradable asset list via `screen_dynamic_universe`.
#
# Alpaca credentials no longer live here. They are stored per-profile via
# `alpaca_account_id` FK (see feedback memory "No master key — Alpaca
# creds live in alpaca_accounts only"). The `alpaca_key` / `alpaca_secret`
# fields on segment dicts are intentionally empty and read by no code path.

SEGMENTS = {
    "stocks": {
        "name": "Stocks",
        "alpaca_key": "",
        "alpaca_secret": "",
        "db_path": "quantopsai_stocks.db",
        "min_price": 1.0,
        "max_price": 10000.0,
        "min_volume": 100_000,
        "max_position_pct": 0.07,
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.15,
        "universe": STOCK_UNIVERSE,
    },
    "crypto": {
        "name": "Crypto",
        "alpaca_key": os.getenv("CRYPTO_ALPACA_KEY", ""),
        "alpaca_secret": os.getenv("CRYPTO_ALPACA_SECRET", ""),
        "db_path": "quantopsai_crypto.db",
        "min_price": 0.0,         # Crypto can be fractions of a cent
        "max_price": 200000.0,     # BTC
        "min_volume": 0,           # Volume filtering done differently for crypto
        "max_position_pct": 0.10,
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.15,
        "universe": CRYPTO_UNIVERSE,
        "is_crypto": True,
        "market_hours": "24/7",
    },
}

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def list_segments():
    """Return list of segment names."""
    return list(SEGMENTS.keys())


def get_segment(name):
    """Return the segment dict for *name*, or raise KeyError."""
    if name not in SEGMENTS:
        raise KeyError(f"Unknown segment: {name!r}. Available: {list_segments()}")
    return SEGMENTS[name]


def get_live_universe(name, ctx=None):
    """Return the live trading universe for segment `name`.

    Default behavior (`USE_DYNAMIC_UNIVERSE` unset or "false"): returns
    the hand-curated list from `SEGMENTS[name]["universe"]`. This is
    the outage-safe path.

    When `USE_DYNAMIC_UNIVERSE=true` in the env: returns the intersection
    of (a) Alpaca's currently-active US-equity asset set (cached daily
    via `screener.get_active_alpaca_symbols`) and (b) the curated list.
    Dead/renamed/delisted symbols are silently dropped while the curated
    set still bounds eligibility.

    Crypto bypasses the dynamic path — its universe is small and stable,
    and Alpaca's crypto asset list semantics differ.

    Zero new API calls: `get_active_alpaca_symbols` is the same helper
    the screener already calls daily and caches in-process.
    """
    seg = get_segment(name)
    base = list(seg.get("universe", []))
    if name == "crypto":
        return base
    if os.getenv("USE_DYNAMIC_UNIVERSE", "false").lower() != "true":
        return base
    try:
        from screener import get_active_alpaca_symbols
        active = get_active_alpaca_symbols(ctx)
        if not active:
            # Cold cache + Alpaca unreachable: don't blow up the
            # caller — return the static base. Self-healing on next
            # successful Alpaca call.
            return base
        return [s for s in base if s in active]
    except Exception:
        return base


def get_segment_api(name):
    """Return an Alpaca REST client configured for the given segment.

    Note: per-profile credentials are the primary path now (via
    `alpaca_account_id` on `trading_profiles`). This helper is kept for
    the crypto segment where there's still a segment-level credential
    fallback; for stocks the segment dict's keys are empty and callers
    should resolve via the per-profile path.
    """
    import alpaca_trade_api as tradeapi

    seg = get_segment(name)
    client = tradeapi.REST(
        key_id=seg["alpaca_key"],
        secret_key=seg["alpaca_secret"],
        base_url=ALPACA_BASE_URL,
        api_version="v2",
    )
    # Wrap in the oversell door (no per-segment journal ctx → submit_order
    # refuses). This is a data/credential-fallback path with no product
    # order callers, but it must not be a latent unguarded broker door.
    from order_guard import guarded_api
    return guarded_api(client, None)
