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

SMALL_CAP_UNIVERSE = [
    # Fintech / finance
    "SOFI", "HOOD", "AFRM", "UPST", "CLOV", "OPEN", "PSFE", "ML", "LMND",
    "VNET", "SLM", "NAVI", "CACC",
    # EVs / autos / mobility
    "RIVN", "LCID", "NIO", "XPEV", "LI", "FSR", "GOEV", "WKHS", "NKLA",
    "MVST", "QS", "CHPT", "BLNK", "EVGO", "REE",
    # Social / tech / software
    "SNAP", "PATH", "WISH", "BB", "NOK", "GENI", "IRNT", "IQ", "WB",
    "EBON", "ZI", "AI", "BBAI", "SOUN", "RKLB",
    # Cannabis
    "TLRY", "CGC", "ACB", "SNDL", "OGI", "HEXO",
    # Crypto / blockchain / miners
    "MARA", "RIOT", "HUT", "BITF", "CIFR", "CLSK", "IREN", "WULF",
    # Clean energy / hydrogen / fuel cells
    "PLUG", "FCEL", "BE", "RUN", "NOVA", "ARRY", "STEM", "OPAL",
    "MAXN", "JKS", "DQ",
    # Oil & gas / energy
    "RIG", "ET", "AM", "AR", "CNX", "BTU", "SWN", "KOS", "TELL", "BTE",
    "CEIX", "NEXT", "SD", "HPK", "CPE", "SM", "CRGY", "VET",
    "CTRA", "OVV", "CRK",
    # Airlines / cruise / travel
    "JBLU", "AAL", "SAVE", "NCLH", "CCL", "RCL", "TRIP", "ABNB",
    "HTHT", "LTH",
    # Biotech / pharma / health
    "DNA", "ADMA", "WVE", "OLPX", "HIMS", "RVMD", "EXAS", "MRNA",
    "BNTX", "CRSP", "NTLA", "BEAM", "EDIT", "VERV", "VIR", "FOLD",
    "APLS", "FATE", "ACAD", "TGTX", "CERE", "ALNY", "SMMT", "IONS",
    "RXRX", "GILD", "VKTX", "LUNG", "KALA", "TBIO", "ABCL",
    # Consumer / retail / food
    "LULU", "CAVA", "DIN", "SHAK", "BROS", "MNST", "COTY", "ELF",
    "PRPL", "IRBT", "LL", "DBI", "ANF", "URBN", "AEO", "PLBY",
    "FIZZ", "CELH",
    # Mining / metals / materials
    "GOLD", "HL", "CDE", "AG", "PAAS", "SVM", "FSM", "MAG",
    "MUX", "GPL", "EXK", "SILV", "GATO", "AUY", "USAS",
    # REITs / real estate
    "AGNC", "NLY", "TWO", "MFA", "IVR", "NYMT", "CIM", "MITT",
    "RC", "BRMK",
    # Industrials / aerospace / defense
    "JOBY", "ACHR", "LILM", "ASTS", "SPCE", "ASTR", "LUNR",
    "RDW", "BKSY",
    # Telecom / media
    "LUMN", "GSAT", "IRDM", "SIRI", "WBD", "PARA", "LYV",
    # Other popular small / micro caps
    "PLTR", "F", "PCG", "T", "VZ", "GPRO", "VUZI",
    "LAZR", "MVIS", "LIDR", "OUST", "AEVA", "INVZ",
    "APPH", "HYLN", "PTRA", "GBS", "VG",
    "ME", "BNGO", "SAVA", "SKLZ", "DKNG", "PENN",
    "CRSR", "LOGI", "HEAR",
    # Additional liquid names in the $1-$30 range
    "CLF", "X", "AA", "VALE", "PBR", "ITUB", "SID", "BBD",
    "UMC", "ASX", "QFIN", "VIPS", "JD", "BABA", "BIDU",
    "TAL", "EDU", "FUTU",
    "GRAB", "SE", "CPNG", "MELI",
    "NU", "STNE", "PAGS",
    "VTRS", "TEVA", "OPK", "PRGO",
    "NOG", "VTLE", "CHRD", "MTDR",
    "ERJ", "AZUL", "GOL", "CPA",
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
# Segment definitions
# ---------------------------------------------------------------------------

SEGMENTS = {
    "smallcap": {
        "name": "Small Cap",
        "alpaca_key": _SMALLCAP_KEY,
        "alpaca_secret": _SMALLCAP_SECRET,
        "db_path": "quantopsai_smallcap.db",
        "min_price": 1.0,
        "max_price": 20.0,
        "min_volume": 500_000,
        "max_position_pct": 0.10,
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.10,
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
