"""Bootstrap the regime classifier on 10+ years of yfinance history.

Run once to populate `regime_classifier_ml_v1.pkl`. After enough
production shadow-comparison data accumulates, the daily-retrain
task takes over with the system's own outcomes.

Usage:
    python scripts/bootstrap_regime_classifier.py [--years N]
        [--out path/to/model.pkl] [--start YYYY-MM-DD]

Default: pulls 2014-01-01 onward, writes
`/opt/quantopsai/regime_classifier_ml_v1.pkl`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from regime_classifier_ml import (
    RegimeClassifier, build_training_dataset,
)


def _fetch_yfinance(symbol: str, start: str, end: str):
    """Pull daily OHLCV from yfinance. Returns lists in ascending date
    order: (closes, highs, lows). Empty lists on failure."""
    try:
        import yfinance as yf
    except ImportError:
        print("ERROR: yfinance not installed", file=sys.stderr)
        sys.exit(1)
    df = yf.download(symbol, start=start, end=end, progress=False,
                      auto_adjust=False)
    if df.empty:
        return [], [], []
    # yfinance returns multi-index columns when downloading a single
    # symbol in some versions; flatten if needed
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    closes = [float(c) for c in df["Close"].tolist()]
    highs = [float(h) for h in df["High"].tolist()]
    lows = [float(l) for l in df["Low"].tolist()]
    return closes, highs, lows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=11,
                    help="Years of history to pull (default 11 = ~2014+)")
    ap.add_argument("--start", default=None,
                    help="Override start date (YYYY-MM-DD)")
    ap.add_argument("--out",
                    default="/opt/quantopsai/regime_classifier_ml_v1.pkl",
                    help="Output pickle path")
    args = ap.parse_args()

    end = datetime.utcnow().strftime("%Y-%m-%d")
    start = args.start or (
        datetime.utcnow() - timedelta(days=365 * args.years)
    ).strftime("%Y-%m-%d")

    print(f"Fetching SPY {start} → {end}...")
    spy_close, spy_high, spy_low = _fetch_yfinance("SPY", start, end)
    print(f"  got {len(spy_close)} bars")
    if len(spy_close) < 300:
        print("ERROR: need at least 300 SPY bars", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching ^VIX {start} → {end}...")
    vix_close, _, _ = _fetch_yfinance("^VIX", start, end)
    print(f"  got {len(vix_close)} bars")
    if len(vix_close) < 300:
        print("ERROR: need at least 300 VIX bars", file=sys.stderr)
        sys.exit(1)

    # Align lengths — VIX and SPY trading calendars are nearly
    # identical but occasional 1-2 day misalignment. Truncate to the
    # shorter series.
    n = min(len(spy_close), len(vix_close))
    spy_close = spy_close[-n:]
    spy_high = spy_high[-n:]
    spy_low = spy_low[-n:]
    vix_close = vix_close[-n:]

    print("Building training dataset...")
    X, y, feature_dicts = build_training_dataset(
        spy_close, spy_high, spy_low, vix_close)
    print(f"  {len(X)} labeled training examples")
    if len(X) < 500:
        print("ERROR: too few training examples", file=sys.stderr)
        sys.exit(1)

    feature_names = sorted(feature_dicts[0].keys())
    print(f"  feature names: {feature_names}")

    print("Training GradientBoostingClassifier...")
    clf = RegimeClassifier()
    metrics = clf.train(X, y, feature_names)
    print(json.dumps(metrics, indent=2, default=str))

    clf.save(args.out)
    print(f"Saved model → {args.out}")


if __name__ == "__main__":
    main()
