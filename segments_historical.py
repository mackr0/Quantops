"""Historical universe lists, frozen for backtest fidelity.

Wave 4 / Issue #10 of `METHODOLOGY_FIX_PLAN.md` — survivorship-bias fix.

WHY THIS FILE EXISTS
====================
Backtests previously read `segments.SEGMENTS[market]["universe"]`, the
same list live trading uses. Because that list is curated to be
tradeable today, every backtest silently excluded names that had been
tradeable in the past but later delisted, were renamed, taken
private, or merged out of existence (e.g. SQ → XYZ, PARA → PSKY,
CFLT taken private, X acquired). Those excluded symbols
disproportionately had bad outcomes. Backtest results were therefore
biased UP — survivorship bias.

THE FIX HAS TWO PARTS
=====================
1. **Frozen baseline (this file).** Verbatim snapshot of segments.py's
   four equity universes as of 2026-04-27. Includes every name the
   system has tracked, dead or alive. Backtester reads from here, not
   from segments.py. CRYPTO_UNIVERSE stays only in segments.py — the
   set is small, stable, and crypto symbols don't get delisted in the
   same way equities do.

2. **Auto-augmentation (`historical_universe_augment.py` + a daily
   scheduler task).** Every day, diff yesterday's Alpaca-active asset
   list against today's. Symbols that disappeared get appended to the
   `historical_universe_additions` table. Backtests over windows that
   include those `last_seen_active` dates pull them into the universe.

The two parts compose: the frozen baseline carries the historical
universe as of today; auto-augmentation captures every future death
from this point forward. After ~6-12 months of accumulation, the
baseline can be re-frozen with the additions merged in if desired,
but the augmentation table is the source of truth either way.

LIVE TRADING IS NOT TOUCHED
===========================
`segments.py` continues to be the source of truth for live trading.
The Alpaca-active filter that protects live paths (CHANGELOG
2026-04-23 / 2026-04-24) still runs and still keeps dead tickers out
of live calls. This file is consumed only by backtest paths.
"""

# ---------------------------------------------------------------------------
# Frozen verbatim copy of segments.MICRO_CAP_UNIVERSE (2026-04-27)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Frozen verbatim copy of segments.SMALL_CAP_UNIVERSE (2026-04-27)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Frozen verbatim copy of segments.MID_CAP_UNIVERSE (2026-04-27)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Frozen verbatim copy of segments.LARGE_CAP_UNIVERSE (2026-04-27)
# ---------------------------------------------------------------------------

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

# Map segment name → frozen list. Backtester reads through this map.
# CRYPTO is intentionally absent — see file header.
HISTORICAL_UNIVERSES = {
    "micro": MICRO_CAP_UNIVERSE,
    "small": SMALL_CAP_UNIVERSE,
    "midcap": MID_CAP_UNIVERSE,
    "largecap": LARGE_CAP_UNIVERSE,
}

# Date this file was frozen. Used for surfacing in backtest output as
# "Universe: 312 frozen + N captured deaths since 2026-04-27".
FROZEN_AT = "2026-04-27"


def get_historical_universe(segment_name):
    """Return the frozen historical universe for one segment.

    Returns an empty list for unknown segments (e.g. "crypto") so
    callers can fall back to the live universe without special-casing.
    """
    return list(HISTORICAL_UNIVERSES.get(segment_name, []))
