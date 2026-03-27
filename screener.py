"""Small-cap and micro-cap stock screener using Alpaca API."""

import sys
from datetime import datetime

from client import get_api
from market_data import get_bars


def get_tradable_symbols(api=None):
    """Get all tradable US equity symbols from major exchanges."""
    api = api or get_api()
    assets = api.list_assets(status="active")
    return [
        a.symbol for a in assets
        if a.tradable
        and a.exchange in ("NYSE", "NASDAQ", "AMEX")
        and "." not in a.symbol
        and "/" not in a.symbol
        and "-" not in a.symbol
        and len(a.symbol) <= 5
    ]


def screen_by_price_range(min_price=1.0, max_price=20.0, min_volume=500000,
                          limit=50, api=None):
    """Screen for small/micro-cap stocks using batch snapshots for speed.

    Uses Alpaca's multi-snapshot API to quickly filter thousands of symbols
    by price and volume without fetching individual bar histories.
    """
    api = api or get_api()

    print("Fetching tradable assets...")
    all_symbols = get_tradable_symbols(api=api)
    print(f"Found {len(all_symbols)} tradable symbols. Fetching snapshots...")

    results = []
    batch_size = 1000
    for i in range(0, len(all_symbols), batch_size):
        batch = all_symbols[i:i + batch_size]
        try:
            snapshots = api.get_snapshots(batch)
            for sym, snap in snapshots.items():
                try:
                    price = float(snap.latest_trade.p)
                    volume = int(snap.daily_bar.v)
                    prev_close = float(snap.prev_daily_bar.c) if hasattr(snap, 'prev_daily_bar') and snap.prev_daily_bar else price
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
        except Exception as e:
            print(f"  Batch error: {e}")

        sys.stdout.write(f"\r  Scanned {min(i + batch_size, len(all_symbols)):,}/{len(all_symbols):,} symbols...")
        sys.stdout.flush()

    print(f"\n  Found {len(results)} stocks in ${min_price}-${max_price} with {min_volume:,}+ volume")

    results.sort(key=lambda x: x["volume"], reverse=True)
    return results[:limit]


def find_volume_surges(candidates, volume_multiplier=2.0, api=None):
    """Find stocks where today's volume surges above the 20-day average."""
    api = api or get_api()
    print(f"  Checking {len(candidates)} stocks for volume surges ({volume_multiplier}x+ avg)...")

    surges = []
    for i, sym in enumerate(candidates):
        try:
            bars = get_bars(sym, limit=30, api=api)
            if len(bars) < 20:
                continue

            avg_vol = float(bars["volume"].iloc[-21:-1].mean())
            today_vol = float(bars["volume"].iloc[-1])
            price = float(bars["close"].iloc[-1])

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

        if (i + 1) % 5 == 0:
            sys.stdout.write(".")
            sys.stdout.flush()

    print(f"\n  Found {len(surges)} volume surges")
    surges.sort(key=lambda x: x["volume_ratio"], reverse=True)
    return surges


def find_momentum_stocks(candidates, min_gain_5d=5.0, min_gain_20d=10.0, api=None):
    """Find stocks with strong recent price momentum."""
    api = api or get_api()
    print(f"  Checking {len(candidates)} stocks for momentum ({min_gain_5d}%+ 5d, {min_gain_20d}%+ 20d)...")

    momentum = []
    for i, sym in enumerate(candidates):
        try:
            bars = get_bars(sym, limit=30, api=api)
            if len(bars) < 21:
                continue

            price = float(bars["close"].iloc[-1])
            price_5d = float(bars["close"].iloc[-6])
            price_20d = float(bars["close"].iloc[-21])

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

        if (i + 1) % 5 == 0:
            sys.stdout.write(".")
            sys.stdout.flush()

    print(f"\n  Found {len(momentum)} momentum stocks")
    momentum.sort(key=lambda x: x["gain_20d"], reverse=True)
    return momentum


def find_breakouts(candidates, api=None):
    """Find stocks breaking above their 20-day high on above-average volume."""
    api = api or get_api()
    print(f"  Checking {len(candidates)} stocks for 20-day high breakouts...")

    breakouts = []
    for i, sym in enumerate(candidates):
        try:
            bars = get_bars(sym, limit=30, api=api)
            if len(bars) < 21:
                continue

            price = float(bars["close"].iloc[-1])
            high_20d = float(bars["high"].iloc[-21:-1].max())
            avg_vol = float(bars["volume"].iloc[-21:-1].mean())
            today_vol = float(bars["volume"].iloc[-1])

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

        if (i + 1) % 5 == 0:
            sys.stdout.write(".")
            sys.stdout.flush()

    print(f"\n  Found {len(breakouts)} breakout candidates")
    breakouts.sort(key=lambda x: x["breakout_pct"], reverse=True)
    return breakouts


def run_full_screen(api=None):
    """Run the complete small-cap screening pipeline."""
    api = api or get_api()

    print("=" * 60)
    print("QUANTOPS SMALL-CAP / MICRO-CAP SCREENER")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Step 1: Fast price/volume screen via snapshots
    print("\n[1/4] Price & Volume Screen")
    candidates = screen_by_price_range(api=api)
    symbols = [c["symbol"] for c in candidates]

    # Steps 2-4: Detailed analysis on candidates only
    print(f"\n[2/4] Volume Surge Detection")
    volume_surges = find_volume_surges(symbols, api=api)

    print(f"\n[3/4] Momentum Screen")
    momentum = find_momentum_stocks(symbols, api=api)

    print(f"\n[4/4] Breakout Detection")
    breakouts = find_breakouts(symbols, api=api)

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
