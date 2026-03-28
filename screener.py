"""Small-cap and micro-cap stock screener using Yahoo Finance (yfinance)."""

import sys
from datetime import datetime

import pandas as pd
import yfinance as yf

from market_data import get_bars


# ---------------------------------------------------------------------------
# Curated universe of ~300 liquid small / micro-cap symbols ($1-$30 range).
# Covers biotech, energy, tech, travel, fintech, EVs, mining, retail, etc.
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
    "PLTR", "F", "PCG", "T", "VZ", "WBD", "PARA", "GPRO", "VUZI",
    "LAZR", "MVIS", "LIDR", "OUST", "AEVA", "INVZ",
    "CIFR", "APPH", "HYLN", "PTRA", "GBS", "VG",
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


def get_small_cap_universe():
    """Return the curated list of small / micro-cap symbols to screen."""
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for s in SMALL_CAP_UNIVERSE:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def screen_by_price_range(min_price=1.0, max_price=20.0, min_volume=500_000,
                          limit=50, api=None):
    """Screen small/micro-cap stocks by price range and minimum volume.

    Uses yfinance batch download for speed. The ``api`` parameter is
    ignored (kept for backward compatibility).
    """
    universe = get_small_cap_universe()
    print(f"Downloading 1-month data for {len(universe)} symbols (yfinance batch)...")

    # Batch download — very fast, single HTTP request per batch
    data = yf.download(universe, period="1mo", progress=False, group_by="ticker",
                       threads=True)

    results = []
    for sym in universe:
        try:
            if len(universe) == 1:
                sym_df = data
            else:
                sym_df = data[sym]

            sym_df = sym_df.dropna(subset=["Close"])
            if sym_df.empty or len(sym_df) < 2:
                continue

            price = float(sym_df["Close"].iloc[-1])
            volume = int(sym_df["Volume"].iloc[-1])
            prev_close = float(sym_df["Close"].iloc[-2])
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0

            if min_price <= price <= max_price and volume >= min_volume:
                results.append({
                    "symbol": sym,
                    "price": round(price, 2),
                    "volume": volume,
                    "price_change_pct": round(change_pct, 2),
                    "reason": f"${price:.2f} | vol: {volume:,} | chg: {change_pct:+.1f}%",
                })
        except Exception:
            pass

    print(f"  Found {len(results)} stocks in ${min_price}-${max_price} with {min_volume:,}+ volume")

    results.sort(key=lambda x: x["volume"], reverse=True)
    return results[:limit]


def find_volume_surges(candidates, volume_multiplier=2.0, api=None):
    """Find stocks where today's volume surges above the 20-day average.

    ``candidates`` is a list of ticker symbol strings.
    """
    print(f"  Checking {len(candidates)} stocks for volume surges ({volume_multiplier}x+ avg)...")

    if not candidates:
        print("\n  Found 0 volume surges")
        return []

    data = yf.download(candidates, period="1mo", progress=False,
                       group_by="ticker", threads=True)

    surges = []
    for sym in candidates:
        try:
            if len(candidates) == 1:
                sym_df = data
            else:
                sym_df = data[sym]

            sym_df = sym_df.dropna(subset=["Close"])
            if len(sym_df) < 20:
                continue

            avg_vol = float(sym_df["Volume"].iloc[-21:-1].mean())
            today_vol = float(sym_df["Volume"].iloc[-1])
            price = float(sym_df["Close"].iloc[-1])

            if avg_vol > 0:
                ratio = today_vol / avg_vol
                if ratio >= volume_multiplier:
                    surges.append({
                        "symbol": sym,
                        "price": round(price, 2),
                        "volume_ratio": round(ratio, 1),
                        "today_vol": int(today_vol),
                        "avg_vol": int(avg_vol),
                        "reason": f"Volume {ratio:.1f}x average ({int(today_vol):,} vs {int(avg_vol):,})",
                    })
        except Exception:
            pass

    print(f"\n  Found {len(surges)} volume surges")
    surges.sort(key=lambda x: x["volume_ratio"], reverse=True)
    return surges


def find_momentum_stocks(candidates, min_gain_5d=3.0, min_gain_20d=5.0, api=None):
    """Find stocks with strong recent price momentum."""
    print(f"  Checking {len(candidates)} stocks for momentum ({min_gain_5d}%+ 5d, {min_gain_20d}%+ 20d)...")

    if not candidates:
        print("\n  Found 0 momentum stocks")
        return []

    data = yf.download(candidates, period="1mo", progress=False,
                       group_by="ticker", threads=True)

    momentum = []
    for sym in candidates:
        try:
            if len(candidates) == 1:
                sym_df = data
            else:
                sym_df = data[sym]

            sym_df = sym_df.dropna(subset=["Close"])
            if len(sym_df) < 21:
                continue

            price = float(sym_df["Close"].iloc[-1])
            price_5d = float(sym_df["Close"].iloc[-6])
            price_20d = float(sym_df["Close"].iloc[-21])

            gain_5d = ((price - price_5d) / price_5d) * 100
            gain_20d = ((price - price_20d) / price_20d) * 100

            if gain_5d >= min_gain_5d and gain_20d >= min_gain_20d:
                momentum.append({
                    "symbol": sym,
                    "price": round(price, 2),
                    "gain_5d": round(gain_5d, 1),
                    "gain_20d": round(gain_20d, 1),
                    "reason": f"5d: +{gain_5d:.1f}% | 20d: +{gain_20d:.1f}%",
                })
        except Exception:
            pass

    print(f"\n  Found {len(momentum)} momentum stocks")
    momentum.sort(key=lambda x: x["gain_20d"], reverse=True)
    return momentum


def find_breakouts(candidates, api=None):
    """Find stocks breaking above their 20-day high on above-average volume."""
    print(f"  Checking {len(candidates)} stocks for 20-day high breakouts...")

    if not candidates:
        print("\n  Found 0 breakout candidates")
        return []

    data = yf.download(candidates, period="1mo", progress=False,
                       group_by="ticker", threads=True)

    breakouts = []
    for sym in candidates:
        try:
            if len(candidates) == 1:
                sym_df = data
            else:
                sym_df = data[sym]

            sym_df = sym_df.dropna(subset=["Close"])
            if len(sym_df) < 21:
                continue

            price = float(sym_df["Close"].iloc[-1])
            high_20d = float(sym_df["High"].iloc[-21:-1].max())
            avg_vol = float(sym_df["Volume"].iloc[-21:-1].mean())
            today_vol = float(sym_df["Volume"].iloc[-1])

            vol_ratio = (today_vol / avg_vol) if avg_vol > 0 else 0

            if price > high_20d and vol_ratio > 1.0:
                breakout_pct = ((price - high_20d) / high_20d) * 100
                breakouts.append({
                    "symbol": sym,
                    "price": round(price, 2),
                    "prev_high": round(high_20d, 2),
                    "breakout_pct": round(breakout_pct, 1),
                    "volume_ratio": round(vol_ratio, 1),
                    "reason": f"Broke ${high_20d:.2f} by +{breakout_pct:.1f}% on {vol_ratio:.1f}x volume",
                })
        except Exception:
            pass

    print(f"\n  Found {len(breakouts)} breakout candidates")
    breakouts.sort(key=lambda x: x["breakout_pct"], reverse=True)
    return breakouts


def run_full_screen(api=None):
    """Run the complete small-cap screening pipeline.

    The ``api`` parameter is ignored (kept for backward compatibility).
    """
    print("=" * 60)
    print("QUANTOPSAI SMALL-CAP / MICRO-CAP SCREENER")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Step 1: Fast price/volume screen via yfinance batch download
    print("\n[1/4] Price & Volume Screen")
    candidates = screen_by_price_range()
    symbols = [c["symbol"] for c in candidates]

    # Steps 2-4: Detailed analysis on candidates only
    print(f"\n[2/4] Volume Surge Detection")
    volume_surges = find_volume_surges(symbols)

    print(f"\n[3/4] Momentum Screen")
    momentum = find_momentum_stocks(symbols)

    print(f"\n[4/4] Breakout Detection")
    breakouts = find_breakouts(symbols)

    print(f"\n{'='*60}")
    print(f"  Candidates: {len(candidates)}")
    print(f"  Volume surges: {len(volume_surges)}")
    print(f"  Momentum: {len(momentum)}")
    print(f"  Breakouts: {len(breakouts)}")
    print(f"{'='*60}")

    return {
        "candidates": candidates,
        "volume_surges": volume_surges,
        "momentum": momentum,
        "breakouts": breakouts,
        "summary": {
            "total_candidates": len(candidates),
            "volume_surges": len(volume_surges),
            "momentum_stocks": len(momentum),
            "breakouts": len(breakouts),
        },
    }
