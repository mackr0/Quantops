"""Market segment definitions for multi-account trading.

Each segment has its own Alpaca credentials, database, universe, and risk
parameters.  The single-account scheduler (scheduler.py) is unaffected;
this module is consumed by multi_scheduler.py.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Alpaca credentials per segment (loaded from environment variables)
# ---------------------------------------------------------------------------
_SMALLCAP_KEY = os.getenv("SMALLCAP_ALPACA_KEY", "")
_SMALLCAP_SECRET = os.getenv("SMALLCAP_ALPACA_SECRET", "")

_MIDCAP_KEY = os.getenv("MIDCAP_ALPACA_KEY", "")
_MIDCAP_SECRET = os.getenv("MIDCAP_ALPACA_SECRET", "")

_LARGECAP_KEY = os.getenv("LARGECAP_ALPACA_KEY", "")
_LARGECAP_SECRET = os.getenv("LARGECAP_ALPACA_SECRET", "")

ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# ---------------------------------------------------------------------------
# Universes
# ---------------------------------------------------------------------------

# Micro Cap universe -- stocks typically under $5
MICRO_CAP_UNIVERSE = [
    # Cannabis (penny territory)
    "SNDL", "ACB", "HEXO", "OGI", "CGC", "TLRY",
    # Biotech / health (low price)
    "DNA", "BNGO", "KALA", "TBIO", "ABCL", "OPK",
    # Tech / social (low price)
    "IQ", "WISH", "BB", "NOK", "EBON", "VUZI", "GPRO",
    # Clean energy (low price)
    "FCEL", "PLUG", "STEM", "OPAL", "MAXN",
    # EV / mobility (low price)
    "FSR", "GOEV", "WKHS", "NKLA", "REE", "MVST",
    # Telecom / other (low price)
    "GSAT", "TELL", "LUMN", "SIRI",
    # LiDAR / sensors (low price)
    "LAZR", "MVIS", "LIDR", "OUST", "AEVA", "INVZ",
    # Other micro-caps
    "APPH", "HYLN", "PTRA", "GBS", "VG", "ME",
    "SKLZ", "PRPL", "IRBT", "LL", "PLBY",
    "GPL", "EXK", "SILV", "GATO", "USAS", "MUX",
    "TWO", "MFA", "IVR", "NYMT", "MITT", "BRMK",
    "SPCE", "ASTR", "RDW", "BKSY",
    "SD", "HPK", "BTE", "GOL",
    "PSFE", "CLOV", "OPEN",
    "CHPT", "BLNK", "EVGO",
]

# Small Cap universe -- stocks typically $5-$20
SMALL_CAP_UNIVERSE = [
    # Fintech / finance
    "SOFI", "HOOD", "AFRM", "UPST", "ML", "LMND",
    "VNET", "SLM", "NAVI", "CACC",
    # EVs / autos / mobility
    "RIVN", "LCID", "NIO", "XPEV", "LI", "QS",
    # Social / tech / software
    "SNAP", "PATH", "GENI", "IRNT", "WB",
    "ZI", "AI", "BBAI", "SOUN", "RKLB",
    # Crypto / blockchain / miners
    "MARA", "RIOT", "HUT", "BITF", "CIFR", "CLSK", "IREN", "WULF",
    # Clean energy / hydrogen / fuel cells
    "BE", "RUN", "NOVA", "ARRY", "JKS", "DQ",
    # Oil & gas / energy
    "RIG", "ET", "AM", "AR", "CNX", "BTU", "SWN", "KOS",
    "CEIX", "NEXT", "CPE", "SM", "CRGY", "VET",
    "CTRA", "OVV", "CRK",
    # Airlines / cruise / travel
    "JBLU", "AAL", "SAVE", "NCLH", "CCL", "RCL", "TRIP", "ABNB",
    "HTHT", "LTH",
    # Biotech / pharma / health
    "ADMA", "WVE", "OLPX", "HIMS", "RVMD", "EXAS", "MRNA",
    "BNTX", "CRSP", "NTLA", "BEAM", "EDIT", "VERV", "VIR", "FOLD",
    "APLS", "FATE", "ACAD", "TGTX", "CERE", "ALNY", "SMMT", "IONS",
    "RXRX", "GILD", "VKTX", "LUNG",
    # Consumer / retail / food
    "LULU", "CAVA", "DIN", "SHAK", "BROS", "MNST", "COTY", "ELF",
    "DBI", "ANF", "URBN", "AEO", "FIZZ", "CELH",
    # Mining / metals / materials
    "GOLD", "HL", "CDE", "AG", "PAAS", "SVM", "FSM", "MAG", "AUY",
    # REITs / real estate
    "AGNC", "NLY", "CIM", "RC",
    # Industrials / aerospace / defense
    "JOBY", "ACHR", "LILM", "ASTS", "LUNR",
    # Telecom / media
    "IRDM", "WBD", "PARA", "LYV",
    # Other popular small caps
    "PLTR", "F", "PCG", "T", "VZ",
    "SAVA", "DKNG", "PENN",
    "CRSR", "LOGI", "HEAR",
    # Additional liquid names in the $5-$30 range
    "CLF", "X", "AA", "VALE", "PBR", "ITUB", "SID", "BBD",
    "UMC", "ASX", "QFIN", "VIPS", "JD", "BABA", "BIDU",
    "TAL", "EDU", "FUTU",
    "GRAB", "SE", "CPNG", "MELI",
    "NU", "STNE", "PAGS",
    "VTRS", "TEVA", "PRGO",
    "NOG", "VTLE", "CHRD", "MTDR",
    "ERJ", "AZUL", "CPA",
    "SWI", "JAMF", "TENB", "RPD", "S", "CRWD",
]

MID_CAP_UNIVERSE = [
    # Tech / SaaS / cloud
    "DKNG", "ROKU", "COIN", "NET", "RBLX", "PINS", "TTD", "ZS",
    "BILL", "HUBS", "TWLO", "MDB", "GTLB", "DOCN", "MNDY", "APP",
    "ESTC", "CFLT", "PCOR", "BRZE", "CWAN", "ASAN", "FROG",
    "ZI", "VEEV", "PAYC", "PCTY", "WK", "APPF", "BSY",
    "AGYS", "EVBG", "SEMR", "COUR",
    # Cybersecurity
    "CRWD", "OKTA", "S", "TENB", "RPD", "VRNS", "CYBR",
    "FTNT", "PANW", "ZS",
    # E-commerce / consumer internet
    "ETSY", "CHWY", "W", "CVNA", "LYFT", "SQ", "AFRM",
    "SHOP", "WISH", "POSH", "REAL", "RVLV",
    # Fintech / digital finance
    "HOOD", "SOFI", "LC", "ALLY", "AXOS", "UPST", "LMND",
    "AFRM", "TOST", "BILL",
    # Food / restaurants / consumer brands
    "CAVA", "BROS", "WING", "SHAK", "DPZ", "CELH", "MNST",
    "ELF", "CROX", "DUOL", "LULU",
    # Retail / apparel
    "FIVE", "OLLI", "DKS", "BBWI", "VSCO", "GPS", "PVH",
    "RL", "TPR", "CPRI", "LEVI", "BURL", "ROST", "BJ",
    "SKX", "DECK", "BIRK",
    # Gaming / entertainment
    "PENN", "DKNG", "RBLX", "U", "GENI", "SKLZ",
    # Semiconductors / hardware
    "ON", "WOLF", "LSCC", "SMCI", "IONQ", "RGTI",
    "ACLS", "AEHR", "MRVL", "SWKS", "QRVO", "MPWR",
    # Healthcare / biotech
    "HIMS", "DOCS", "TDOC", "AMWL", "GDRX", "OSCR",
    "INSP", "NUVB", "RXRX", "TWST", "PACB", "CERT",
    "RVMD", "TGTX", "EXAS", "NTRA", "GH",
    # Space / aerospace
    "RKLB", "ASTS", "LUNR", "JOBY", "ACHR", "LILM",
    # Quantum / AI
    "IONQ", "RGTI", "QUBT", "AI", "BBAI", "SOUN", "PATH",
    # EV / clean energy
    "RIVN", "LCID", "QS", "CHPT", "BLNK", "EVGO",
    "RUN", "NOVA", "ARRY", "BE", "PLUG",
    # Industrials / specialty
    "XPO", "SAIA", "ODFL", "WERN", "JBHT", "KNX",
    "GXO", "LSTR", "ARCB", "SNDR",
    # Media / ad tech
    "MGNI", "PUBM", "TTD", "IAS", "DV", "CARG",
    # Travel / leisure
    "ABNB", "DASH", "NCLH", "CCL", "RCL", "LTH",
    "TRIP", "EXPE",
    # Latin America / international
    "NU", "MELI", "SE", "GRAB", "CPNG", "STNE", "PAGS",
    "DLOCAL",
    # Crypto-adjacent
    "COIN", "MARA", "RIOT", "CLSK", "HUT", "CIFR",
]

LARGE_CAP_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    # Semiconductors
    "AMD", "INTC", "QCOM", "AVGO", "TXN", "MU", "ADI", "NXPI", "KLAC",
    "LRCX", "AMAT", "ASML",
    # Software / cloud
    "CRM", "ORCL", "ADBE", "NOW", "INTU", "WDAY", "TEAM", "ZM",
    "CDNS", "SNPS", "ANSS", "PTC", "FICO", "CPRT", "CSGP", "VRSK",
    # Internet / media
    "NFLX", "DIS", "CMCSA", "UBER", "BKNG",
    # Networking / IT
    "CSCO", "IBM", "ACN", "DELL", "HPQ",
    # Payments / fintech
    "V", "MA", "PYPL", "FIS", "FISV", "GPN", "ADP", "PAYX",
    "SQ", "COF", "AXP",
    # Banks / financial services
    "JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "TFC",
    "SCHW", "BLK",
    # Aerospace / defense
    "BA", "RTX", "LMT", "NOC", "GD", "GE", "HON",
    # Industrials
    "CAT", "DE", "MMM", "ITW", "EMR", "ETN", "ROK",
    # Healthcare / devices / diagnostics
    "ISRG", "DXCM", "ALGN", "IDXX", "ZBH", "SYK", "MDT", "ABT",
    "BSX", "EW",
    # Life sciences / tools
    "TMO", "DHR", "A", "BIO", "IQV",
    # Biotech / pharma
    "VRTX", "REGN", "AMGN", "GILD", "BIIB", "BMY",
    "LLY", "PFE", "MRK", "JNJ", "ABBV",
    # Health insurance / managed care
    "UNH", "CI", "HUM", "CNC", "MOH", "ELV",
    # Hospitals / healthcare services
    "HCA", "THC", "UHS", "DVA",
    # Consumer staples
    "KO", "PEP", "MDLZ", "KHC", "HSY", "GIS", "SJM", "CAG",
    "HRL", "TSN", "MKC", "CLX", "CL", "PG", "KMB", "CHD", "EL",
    # Retail
    "COST", "WMT", "TGT", "HD", "LOW", "TJX", "ROST", "BURL",
    # Restaurants / food service
    "SBUX", "MCD", "YUM", "CMG", "DPZ", "QSR",
    # Apparel / lifestyle
    "NKE", "LULU", "ANF", "AEO", "GPS",
    # Airlines
    "LUV", "DAL", "UAL", "AAL",
    # Hotels / travel
    "MAR", "HLT", "ABNB",
    # Telecom
    "T", "VZ", "TMUS",
]

# ---------------------------------------------------------------------------
# Crypto universe — Alpaca uses "BTC/USD" format, yfinance uses "BTC-USD"
# We store the Alpaca format; screener/market_data converts for yfinance
# ---------------------------------------------------------------------------
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

SEGMENTS = {
    "micro": {
        "name": "Micro Cap",
        "alpaca_key": _SMALLCAP_KEY,
        "alpaca_secret": _SMALLCAP_SECRET,
        "db_path": "quantopsai_micro.db",
        "min_price": 1.0,
        "max_price": 5.0,
        "min_volume": 100_000,
        "max_position_pct": 0.05,
        "stop_loss_pct": 0.10,
        "take_profit_pct": 0.15,
        "universe": MICRO_CAP_UNIVERSE,
    },
    "small": {
        "name": "Small Cap",
        "alpaca_key": _SMALLCAP_KEY,
        "alpaca_secret": _SMALLCAP_SECRET,
        "db_path": "quantopsai_small.db",
        "min_price": 5.0,
        "max_price": 20.0,
        "min_volume": 300_000,
        "max_position_pct": 0.08,
        "stop_loss_pct": 0.06,
        "take_profit_pct": 0.08,
        "universe": SMALL_CAP_UNIVERSE,
    },
    "midcap": {
        "name": "Mid Cap",
        "alpaca_key": _MIDCAP_KEY,
        "alpaca_secret": _MIDCAP_SECRET,
        "db_path": "quantopsai_midcap.db",
        "min_price": 20.0,
        "max_price": 100.0,
        "min_volume": 300_000,
        "max_position_pct": 0.08,
        "stop_loss_pct": 0.04,
        "take_profit_pct": 0.12,
        "universe": MID_CAP_UNIVERSE,
    },
    "largecap": {
        "name": "Large Cap",
        "alpaca_key": _LARGECAP_KEY,
        "alpaca_secret": _LARGECAP_SECRET,
        "db_path": "quantopsai_largecap.db",
        "min_price": 50.0,
        "max_price": 500.0,
        "min_volume": 1_000_000,
        "max_position_pct": 0.07,
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.15,
        "universe": LARGE_CAP_UNIVERSE,
    },
    "crypto": {
        "name": "Crypto",
        "alpaca_key": os.getenv("CRYPTO_ALPACA_KEY", ""),
        "alpaca_secret": os.getenv("CRYPTO_ALPACA_SECRET", ""),
        "db_path": "quantopsai_crypto.db",
        "min_price": 0.0,       # Crypto can be fractions of a cent
        "max_price": 200000.0,   # BTC
        "min_volume": 0,         # Volume filtering done differently for crypto
        "max_position_pct": 0.10,
        "stop_loss_pct": 0.05,   # 5% — crypto is volatile
        "take_profit_pct": 0.15, # 15%
        "universe": CRYPTO_UNIVERSE,
        "is_crypto": True,       # Flag for special handling
        "market_hours": "24/7",  # Crypto never closes
    },
}

# Backward compatibility: "microsmall" maps to "small"
SEGMENTS["microsmall"] = SEGMENTS["small"]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def list_segments():
    """Return list of segment names (excludes backward-compat aliases)."""
    return [k for k in SEGMENTS.keys() if k != "microsmall"]


def get_segment(name):
    """Return the segment dict for *name*, or raise KeyError."""
    if name not in SEGMENTS:
        raise KeyError(f"Unknown segment: {name!r}. Available: {list_segments()}")
    return SEGMENTS[name]


def get_segment_api(name):
    """Return an Alpaca REST client configured for the given segment."""
    import alpaca_trade_api as tradeapi

    seg = get_segment(name)
    return tradeapi.REST(
        key_id=seg["alpaca_key"],
        secret_key=seg["alpaca_secret"],
        base_url=ALPACA_BASE_URL,
        api_version="v2",
    )
