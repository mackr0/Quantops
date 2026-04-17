"""Small-cap and micro-cap stock screener using Yahoo Finance (yfinance)."""

import sys
from datetime import datetime

import pandas as pd
import yfinance as yf
import yf_lock

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
    return _dedup(SMALL_CAP_UNIVERSE)


def _dedup(symbols):
    """Deduplicate while preserving order."""
    seen = set()
    unique = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def to_yfinance_symbol(symbol):
    """Convert Alpaca symbol to yfinance format. E.g. 'BTC/USD' -> 'BTC-USD'."""
    return symbol.replace("/", "-")


def from_yfinance_symbol(symbol):
    """Convert yfinance symbol back to Alpaca format. E.g. 'BTC-USD' -> 'BTC/USD'."""
    return symbol.replace("-", "/")


def screen_by_price_range(min_price=1.0, max_price=20.0, min_volume=500_000,
                          limit=50, universe=None, api=None):
    """Screen small/micro-cap stocks by price range and minimum volume.

    Uses yfinance batch download for speed. The ``api`` parameter is
    ignored (kept for backward compatibility).

    Parameters
    ----------
    universe : list[str], optional
        Symbol list to screen.  Falls back to get_small_cap_universe()
        when not provided.
    """
    if universe is None:
        universe = get_small_cap_universe()

    print(f"Downloading 1-month data for {len(universe)} symbols (yfinance batch)...")

    # Batch download — very fast, single HTTP request per batch
    data = yf_lock.download(universe, period="1mo", progress=False, group_by="ticker",
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

    data = yf_lock.download(candidates, period="1mo", progress=False,
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

    data = yf_lock.download(candidates, period="1mo", progress=False,
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

    data = yf_lock.download(candidates, period="1mo", progress=False,
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


def run_full_screen(universe=None, min_price=None, max_price=None, min_volume=None,
                    api=None):
    """Run the complete small-cap screening pipeline.

    Parameters
    ----------
    universe : list[str], optional
        Symbol list to screen.  Falls back to get_small_cap_universe().
    min_price, max_price, min_volume : optional
        Override default screening parameters.
    api : ignored (kept for backward compatibility).
    """
    print("=" * 60)
    print("QUANTOPSAI SMALL-CAP / MICRO-CAP SCREENER")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Build kwargs for screen_by_price_range
    kwargs = {}
    if universe is not None:
        kwargs["universe"] = universe
    if min_price is not None:
        kwargs["min_price"] = min_price
    if max_price is not None:
        kwargs["max_price"] = max_price
    if min_volume is not None:
        kwargs["min_volume"] = min_volume

    # Step 1: Fast price/volume screen via yfinance batch download
    print("\n[1/4] Price & Volume Screen")
    candidates = screen_by_price_range(**kwargs)
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


def run_crypto_screen(universe=None):
    """Screen crypto assets using yfinance data.

    Crypto symbols are stored as 'BTC/USD' (Alpaca format) but fetched
    as 'BTC-USD' (yfinance format). Results use Alpaca format.
    """
    from segments import CRYPTO_UNIVERSE

    if universe is None:
        universe = CRYPTO_UNIVERSE

    print("=" * 60)
    print("QUANTOPSAI CRYPTO SCREENER")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Convert to yfinance format for download
    yf_symbols = [to_yfinance_symbol(s) for s in universe]

    print(f"\n[1/3] Downloading data for {len(yf_symbols)} crypto pairs...")
    data = yf_lock.download(yf_symbols, period="1mo", progress=False,
                       group_by="ticker", threads=True)

    # Screen all crypto — no price/volume filter (they're all candidates)
    candidates = []
    for yf_sym in yf_symbols:
        try:
            if len(yf_symbols) == 1:
                sym_df = data
            else:
                sym_df = data[yf_sym]

            sym_df = sym_df.dropna(subset=["Close"])
            if sym_df.empty or len(sym_df) < 2:
                continue

            price = float(sym_df["Close"].iloc[-1])
            volume = int(sym_df["Volume"].iloc[-1])
            prev_close = float(sym_df["Close"].iloc[-2])
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0

            alpaca_sym = from_yfinance_symbol(yf_sym)
            candidates.append({
                "symbol": alpaca_sym,
                "price": round(price, 6),
                "volume": volume,
                "price_change_pct": round(change_pct, 2),
                "reason": f"${price:,.2f} | chg: {change_pct:+.1f}%",
            })
        except Exception:
            pass

    print(f"  Found {len(candidates)} active crypto pairs")

    # Volume surges and momentum on crypto
    alpaca_symbols = [c["symbol"] for c in candidates]
    yf_syms = [to_yfinance_symbol(s) for s in alpaca_symbols]

    print(f"\n[2/3] Momentum Screen")
    momentum = []
    if len(candidates) >= 2:
        for yf_sym in yf_syms:
            try:
                sym_df = data[yf_sym].dropna(subset=["Close"])
                if len(sym_df) < 8:
                    continue
                price = float(sym_df["Close"].iloc[-1])
                price_5d = float(sym_df["Close"].iloc[-6]) if len(sym_df) >= 6 else None
                if price_5d and price_5d > 0:
                    gain_5d = ((price - price_5d) / price_5d) * 100
                    if gain_5d >= 3.0:
                        alpaca_sym = from_yfinance_symbol(yf_sym)
                        momentum.append({
                            "symbol": alpaca_sym,
                            "price": round(price, 6),
                            "gain_5d": round(gain_5d, 1),
                            "reason": f"5d: +{gain_5d:.1f}%",
                        })
            except Exception:
                pass
    momentum.sort(key=lambda x: x.get("gain_5d", 0), reverse=True)
    print(f"  Found {len(momentum)} momentum cryptos")

    print(f"\n[3/3] Volume Surge Detection")
    surges = []
    if len(candidates) >= 2:
        for yf_sym in yf_syms:
            try:
                sym_df = data[yf_sym].dropna(subset=["Close"])
                if len(sym_df) < 20:
                    continue
                avg_vol = float(sym_df["Volume"].iloc[-21:-1].mean())
                today_vol = float(sym_df["Volume"].iloc[-1])
                price = float(sym_df["Close"].iloc[-1])
                if avg_vol > 0:
                    ratio = today_vol / avg_vol
                    if ratio >= 1.5:
                        alpaca_sym = from_yfinance_symbol(yf_sym)
                        surges.append({
                            "symbol": alpaca_sym,
                            "price": round(price, 6),
                            "volume_ratio": round(ratio, 1),
                            "reason": f"Volume {ratio:.1f}x average",
                        })
            except Exception:
                pass
    surges.sort(key=lambda x: x.get("volume_ratio", 0), reverse=True)
    print(f"  Found {len(surges)} volume surges")

    print(f"\n{'='*60}")
    print(f"  Candidates: {len(candidates)}")
    print(f"  Momentum: {len(momentum)}")
    print(f"  Volume surges: {len(surges)}")
    print(f"{'='*60}")

    return {
        "candidates": candidates,
        "volume_surges": surges,
        "momentum": momentum,
        "breakouts": [],  # Not applicable for crypto the same way
        "summary": {
            "total_candidates": len(candidates),
            "volume_surges": len(surges),
            "momentum_stocks": len(momentum),
            "breakouts": 0,
        },
    }


# ---------------------------------------------------------------------------
# Dynamic universe discovery (scan beyond hardcoded lists)
# ---------------------------------------------------------------------------

import time as _time
import logging as _logging

_dynamic_cache = {}  # {market_type: (timestamp, [symbols])}
_DYNAMIC_TTL = 86400  # 24 hours — universe doesn't change much daily

# On-disk cache file. Persists the dynamic universe across process
# restarts so a redeploy during market hours doesn't force a 30-minute
# yfinance re-scan. Still subject to _DYNAMIC_TTL.
_DYNAMIC_CACHE_FILE = "dynamic_screener_cache.json"

# Max wall-clock seconds for the yfinance bulk download. Above this we
# fall back to whatever symbols we have (from Alpaca universe + fallback).
_DYNAMIC_YF_BUDGET_SEC = 180   # 3 minutes


def _load_disk_cache():
    """Load the on-disk cache into memory at module import."""
    global _dynamic_cache
    try:
        import json as _json
        with open(_DYNAMIC_CACHE_FILE) as f:
            raw = _json.load(f)
        # Format: {cache_key: [timestamp, [symbols]]}
        _dynamic_cache = {k: (float(v[0]), list(v[1])) for k, v in raw.items()}
    except Exception:
        pass


def _save_disk_cache():
    """Persist the current in-memory cache to disk. Best-effort."""
    try:
        import json as _json
        with open(_DYNAMIC_CACHE_FILE, "w") as f:
            _json.dump({k: [v[0], v[1]] for k, v in _dynamic_cache.items()}, f)
    except Exception:
        pass


# Warm the cache at module import so the first scan after a restart
# sees the last-known good result.
_load_disk_cache()

_dyn_logger = _logging.getLogger(__name__)


def screen_dynamic_universe(min_price=1.0, max_price=20.0, min_volume=500_000,
                             market_type="small", fallback_universe=None,
                             ctx=None, max_symbols=100):
    """Discover actively traded symbols beyond the hardcoded universe.

    Uses Alpaca's asset list to find ALL tradable symbols, then yfinance
    batch download to filter by price/volume. Cached for 24 hours.

    Falls back to the hardcoded universe if dynamic screening fails.

    Returns list of symbol strings.
    """
    cache_key = f"{market_type}_{min_price}_{max_price}_{min_volume}"
    cached = _dynamic_cache.get(cache_key)
    if cached and (_time.time() - cached[0]) < _DYNAMIC_TTL:
        return cached[1]

    try:
        # Step 1: Get all tradable assets from Alpaca
        from client import get_api
        api = get_api(ctx)
        assets = api.list_assets(status="active")

        # Filter to US exchanges, tradable, no OTC
        equity_symbols = []
        for a in assets:
            if (a.tradable and a.exchange in ("NYSE", "NASDAQ", "ARCA", "AMEX")
                    and not a.symbol.endswith(".W")  # No warrants
                    and "." not in a.symbol):  # No preferred shares
                equity_symbols.append(a.symbol)

        _dyn_logger.info(f"Dynamic screener: {len(equity_symbols)} tradable assets from Alpaca")

        if len(equity_symbols) < 100:
            raise ValueError(f"Too few assets ({len(equity_symbols)}), using fallback")

        # Step 2: Batch download 5-day data from yfinance in chunks
        import random
        # Sample to avoid downloading all 8000 at once
        # Take a random 500 to screen, plus the full fallback universe
        sample = random.sample(equity_symbols, min(500, len(equity_symbols)))
        if fallback_universe:
            # Always include the curated universe
            sample = list(set(sample + list(fallback_universe)))

        # Primary path: Alpaca snapshots. One API call returns the last
        # trade + minute bar + daily bar for up to 1000+ symbols at once,
        # and the Algo Trader Plus subscription has no per-minute cap
        # that we'd hit at this volume. Replaces the yfinance batch that
        # used to hang for 30+ minutes during market open.
        results = []
        alpaca_worked = False
        try:
            from market_data import _get_alpaca_data_client
            client = _get_alpaca_data_client()
            if client is None:
                raise RuntimeError("Alpaca client unavailable")

            # Alpaca's get_snapshots takes a list and returns {sym: Snapshot}
            # Chunk to 200 at a time to be conservative on payload size.
            snaps = {}
            for i in range(0, len(sample), 200):
                chunk = sample[i:i + 200]
                chunk_snaps = client.get_snapshots(chunk)
                snaps.update(chunk_snaps)

            for sym in sample:
                snap = snaps.get(sym)
                if snap is None:
                    continue
                # daily_bar is the current day's (or prior day's if pre-open)
                # OHLCV aggregate — exactly what we need for price + volume
                # filtering.
                daily = getattr(snap, "daily_bar", None)
                if daily is None:
                    continue
                try:
                    last_price = float(daily.c)
                    avg_volume = float(daily.v)
                except (TypeError, AttributeError):
                    continue

                if min_price <= last_price <= max_price and avg_volume >= min_volume:
                    results.append((sym, avg_volume))
            alpaca_worked = True
            _dyn_logger.info(
                "Dynamic screener: Alpaca snapshots returned "
                "%d filtered matches", len(results)
            )
        except Exception as exc:
            _dyn_logger.warning(
                "Alpaca screener path failed (%s), trying yfinance fallback",
                exc,
            )

        # Fallback path: yfinance bulk download with wall-clock budget.
        # Only runs if Alpaca failed.
        if not alpaca_worked:
            import threading
            yf_symbols = " ".join(sample)
            dl_result: dict = {"data": None, "error": None}

            def _do_download():
                try:
                    dl_result["data"] = yf_lock.download(
                        yf_symbols, period="5d", progress=False,
                        auto_adjust=True, threads=True,
                    )
                except Exception as exc:
                    dl_result["error"] = exc

            t = threading.Thread(target=_do_download, daemon=True)
            t.start()
            t.join(timeout=_DYNAMIC_YF_BUDGET_SEC)

            if t.is_alive():
                _dyn_logger.warning(
                    "Dynamic screener: yfinance fallback also exceeded %d-sec budget",
                    _DYNAMIC_YF_BUDGET_SEC,
                )
                raise TimeoutError(
                    f"yfinance fallback exceeded {_DYNAMIC_YF_BUDGET_SEC}s"
                )
            if dl_result["error"] is not None:
                raise dl_result["error"]
            data = dl_result["data"]
            if data is None or data.empty:
                raise ValueError("yfinance fallback returned empty")

            for sym in sample:
                try:
                    if len(sample) > 1 and isinstance(data.columns, pd.MultiIndex):
                        close_data = data["Close"][sym].dropna()
                        vol_data = data["Volume"][sym].dropna()
                    else:
                        close_data = data["Close"].dropna()
                        vol_data = data["Volume"].dropna()
                    if len(close_data) < 2:
                        continue
                    last_price = float(close_data.iloc[-1])
                    avg_volume = float(vol_data.mean())
                    if min_price <= last_price <= max_price and avg_volume >= min_volume:
                        results.append((sym, avg_volume))
                except Exception:
                    continue

        # Sort by volume (most active first), take top N
        results.sort(key=lambda x: x[1], reverse=True)
        symbols = [r[0] for r in results[:max_symbols]]

        _dyn_logger.info(f"Dynamic screener: {len(symbols)} symbols match "
                         f"${min_price}-${max_price}, vol>={min_volume:,}")

        _dynamic_cache[cache_key] = (_time.time(), symbols)
        _save_disk_cache()
        return symbols

    except Exception as exc:
        # Prefer stale cache over the hardcoded fallback — a day-old
        # universe is still closer to right than the curated list.
        stale = _dynamic_cache.get(cache_key)
        if stale and stale[1]:
            age_hrs = (_time.time() - stale[0]) / 3600
            _dyn_logger.warning(
                f"Dynamic universe failed ({exc}); using stale cache "
                f"({len(stale[1])} symbols, {age_hrs:.1f}h old)"
            )
            return stale[1]
        _dyn_logger.warning(f"Dynamic universe failed ({exc}), using fallback")
        return list(fallback_universe) if fallback_universe else []
