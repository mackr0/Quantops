"""Institutional-grade performance metrics calculator.

Centralised module that computes every metric needed by the 5-tab
Performance Dashboard.  All calculations handle empty / insufficient
data gracefully (returning zeroes or empty lists).
"""

import math
import sqlite3
import os
import time
import logging
from datetime import datetime, timedelta, date
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional numpy — fall back to pure-Python when unavailable
# ---------------------------------------------------------------------------
try:
    import numpy as _np

    def _mean(xs):
        if not xs:
            return 0.0
        return float(_np.mean(xs))

    def _std(xs, ddof=1):
        if len(xs) < 2:
            return 0.0
        return float(_np.std(xs, ddof=ddof))

    def _percentile(xs, pct):
        if not xs:
            return 0.0
        return float(_np.percentile(xs, pct))

except ImportError:
    _np = None

    def _mean(xs):
        if not xs:
            return 0.0
        return sum(xs) / len(xs)

    def _std(xs, ddof=1):
        if len(xs) < 2:
            return 0.0
        m = _mean(xs)
        var = sum((x - m) ** 2 for x in xs) / (len(xs) - ddof)
        return math.sqrt(max(var, 0))

    def _percentile(xs, pct):
        if not xs:
            return 0.0
        s = sorted(xs)
        k = (pct / 100) * (len(s) - 1)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return s[int(k)]
        return s[f] * (c - k) + s[c] * (k - f)


# ---------------------------------------------------------------------------
# Module-level cache for market benchmark data (SPY / QQQ / BTC)
# ---------------------------------------------------------------------------
_benchmark_cache: Dict[str, Any] = {}
_benchmark_cache_time: float = 0.0
_BENCHMARK_CACHE_TTL = 1800  # 30 minutes


def _fetch_benchmark_returns(ticker: str, start_date: str, end_date: str) -> Dict[str, float]:
    """Fetch daily returns for a benchmark ticker from yfinance.

    Returns {date_str: daily_return_pct, ...}.  Cached at module level for
    30 minutes.
    """
    global _benchmark_cache, _benchmark_cache_time

    cache_key = f"{ticker}|{start_date}|{end_date}"
    now = time.time()

    if now - _benchmark_cache_time < _BENCHMARK_CACHE_TTL and cache_key in _benchmark_cache:
        return _benchmark_cache[cache_key]

    try:
        import yf_lock
        df = yf_lock.download(ticker, start=start_date, end=end_date, progress=False, auto_adjust=True)
        if df is None or df.empty:
            return {}
        # Handle multi-level columns from yfinance
        close_col = df["Close"]
        if hasattr(close_col, "columns"):
            close_col = close_col.iloc[:, 0]
        returns = close_col.pct_change().dropna()
        result = {}
        for idx, val in returns.items():
            date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            result[date_str] = float(val)
        _benchmark_cache[cache_key] = result
        _benchmark_cache_time = now
        return result
    except Exception as exc:
        logger.debug("Could not fetch %s from yfinance: %s", ticker, exc)
        return {}


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _gather_trades(db_paths) -> List[Dict]:
    """Collect all closed trades (with pnl) from the provided DBs."""
    all_trades = []
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, symbol, side, qty, price, pnl, strategy, "
                "decision_price, fill_price, slippage_pct, status "
                "FROM trades WHERE pnl IS NOT NULL "
                "ORDER BY timestamp ASC"
            ).fetchall()
            for r in rows:
                all_trades.append(dict(r))
            conn.close()
        except Exception:
            pass
    all_trades.sort(key=lambda t: t.get("timestamp") or "")
    return all_trades


def _count_open_trades(db_paths) -> int:
    """Count trades that opened positions but haven't been closed yet.

    Used to display the full activity picture alongside the closed-trade
    metrics — a shortlist of 5 trades (3 open + 2 closed) previously
    showed as 'Total Trades: 2' which misleads users into thinking the
    system hasn't traded.
    """
    total = 0
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT COUNT(*) FROM trades "
                "WHERE pnl IS NULL AND side IN ('buy', 'short')"
            ).fetchone()
            if rows:
                total += int(rows[0] or 0)
            conn.close()
        except Exception:
            pass
    return total


def _gather_snapshots(db_paths, initial_capital_per_profile: float = 10000) -> List[Dict]:
    """Collect daily snapshots from all DBs and aggregate across profiles.

    For each date, sums equity across all profiles. If a profile is missing a
    snapshot for a given date, its most recent known equity (or initial capital)
    is carried forward. This prevents artificial return spikes when profiles
    have different snapshot dates.
    """
    per_db_snaps: Dict[str, List[Dict]] = {}
    all_dates = set()
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT date, equity, cash, portfolio_value, num_positions, daily_pnl "
                "FROM daily_snapshots WHERE equity IS NOT NULL ORDER BY date ASC"
            ).fetchall()
            snaps = [dict(r) for r in rows]
            per_db_snaps[db_path] = snaps
            for s in snaps:
                if s.get("date"):
                    all_dates.add(s["date"])
            conn.close()
        except Exception:
            per_db_snaps[db_path] = []

    if not all_dates:
        return []

    sorted_dates = sorted(all_dates)
    # Build per-DB equity by date (with forward-fill)
    per_db_equity: Dict[str, Dict[str, float]] = {}
    for db_path, snaps in per_db_snaps.items():
        by_date = {s["date"]: s for s in snaps if s.get("date")}
        per_db_equity[db_path] = {}
        last_eq = initial_capital_per_profile
        last_pnl = 0
        for d in sorted_dates:
            if d in by_date:
                last_eq = by_date[d].get("equity") or last_eq
                last_pnl = by_date[d].get("daily_pnl") or 0
            else:
                last_pnl = 0  # no new pnl on days with no snapshot
            per_db_equity[db_path][d] = {"equity": last_eq, "daily_pnl": last_pnl}

    # Aggregate across DBs per date
    result = []
    for d in sorted_dates:
        total_equity = sum(per_db_equity[db][d]["equity"] for db in per_db_snaps)
        total_pnl = sum(per_db_equity[db][d]["daily_pnl"] for db in per_db_snaps)
        result.append({"date": d, "equity": total_equity, "daily_pnl": total_pnl})
    return result


# ---------------------------------------------------------------------------
# SVG chart helpers
# ---------------------------------------------------------------------------

def render_equity_curve_svg(equity_data: List[Dict], width: int = 700, height: int = 200) -> str:
    """Generate an inline SVG line chart from equity curve data.

    equity_data: list of {date, equity}.
    Returns SVG markup string.
    """
    if not equity_data or len(equity_data) < 2:
        return '<svg viewBox="0 0 700 200" style="width:100%;max-width:700px;"><text x="350" y="100" text-anchor="middle" fill="#888" font-size="14">Not enough data for equity curve</text></svg>'

    equities = [e.get("equity", 0) or 0 for e in equity_data]
    dates = [e.get("date", "") for e in equity_data]

    min_eq = min(equities)
    max_eq = max(equities)
    eq_range = max_eq - min_eq if max_eq != min_eq else 1

    pad_top, pad_bottom, pad_left, pad_right = 20, 30, 60, 20
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom

    def x_pos(i):
        return pad_left + (i / max(len(equities) - 1, 1)) * chart_w

    def y_pos(val):
        return pad_top + chart_h - ((val - min_eq) / eq_range) * chart_h

    # Build polyline
    points = " ".join(f"{x_pos(i):.1f},{y_pos(v):.1f}" for i, v in enumerate(equities))

    # Fill area under curve
    fill_points = points + f" {x_pos(len(equities)-1):.1f},{pad_top + chart_h:.1f} {pad_left:.1f},{pad_top + chart_h:.1f}"

    # Colour: green if ended higher than started, red otherwise
    color = "#00c853" if equities[-1] >= equities[0] else "#ff1744"
    fill_color = "#00c85320" if equities[-1] >= equities[0] else "#ff174420"

    # Axis labels
    labels_svg = ""
    # Y-axis: min, mid, max
    for val, label in [(max_eq, f"${max_eq:,.0f}"), ((min_eq + max_eq) / 2, f"${(min_eq+max_eq)/2:,.0f}"), (min_eq, f"${min_eq:,.0f}")]:
        y = y_pos(val)
        labels_svg += f'<text x="{pad_left - 5}" y="{y + 4}" text-anchor="end" fill="#888" font-size="10">{label}</text>'
        labels_svg += f'<line x1="{pad_left}" y1="{y}" x2="{pad_left + chart_w}" y2="{y}" stroke="#e0e0e0" stroke-width="0.5"/>'

    # X-axis: first and last date
    if dates:
        labels_svg += f'<text x="{pad_left}" y="{pad_top + chart_h + 18}" text-anchor="start" fill="#888" font-size="10">{dates[0]}</text>'
        labels_svg += f'<text x="{pad_left + chart_w}" y="{pad_top + chart_h + 18}" text-anchor="end" fill="#888" font-size="10">{dates[-1]}</text>'

    return f'''<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px;">
    {labels_svg}
    <polygon points="{fill_points}" fill="{fill_color}"/>
    <polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>
</svg>'''


def render_drawdown_svg(drawdown_data: List[Dict], width: int = 700, height: int = 150) -> str:
    """Generate an inline SVG area chart for drawdown (filled below zero).

    drawdown_data: list of {date, drawdown_pct} where drawdown_pct <= 0.
    """
    if not drawdown_data or len(drawdown_data) < 2:
        return '<svg viewBox="0 0 700 150" style="width:100%;max-width:700px;"><text x="350" y="75" text-anchor="middle" fill="#888" font-size="14">Not enough data for drawdown chart</text></svg>'

    vals = [d.get("drawdown_pct", 0) for d in drawdown_data]
    dates = [d.get("date", "") for d in drawdown_data]
    min_val = min(vals) if vals else 0
    max_val = 0  # drawdown is always <= 0

    val_range = abs(min_val) if min_val != 0 else 1

    pad_top, pad_bottom, pad_left, pad_right = 15, 25, 55, 20
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom

    def x_pos(i):
        return pad_left + (i / max(len(vals) - 1, 1)) * chart_w

    def y_pos(val):
        # 0 is at top, min_val at bottom
        return pad_top + (abs(val) / val_range) * chart_h

    points = " ".join(f"{x_pos(i):.1f},{y_pos(v):.1f}" for i, v in enumerate(vals))
    fill_points = f"{pad_left:.1f},{pad_top:.1f} " + points + f" {x_pos(len(vals)-1):.1f},{pad_top:.1f}"

    # Labels
    labels_svg = ""
    labels_svg += f'<text x="{pad_left - 5}" y="{pad_top + 4}" text-anchor="end" fill="#888" font-size="10">0%</text>'
    labels_svg += f'<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left + chart_w}" y2="{pad_top}" stroke="#e0e0e0" stroke-width="0.5"/>'
    labels_svg += f'<text x="{pad_left - 5}" y="{pad_top + chart_h + 4}" text-anchor="end" fill="#888" font-size="10">{min_val:.1f}%</text>'

    if dates:
        labels_svg += f'<text x="{pad_left}" y="{pad_top + chart_h + 18}" text-anchor="start" fill="#888" font-size="10">{dates[0]}</text>'
        labels_svg += f'<text x="{pad_left + chart_w}" y="{pad_top + chart_h + 18}" text-anchor="end" fill="#888" font-size="10">{dates[-1]}</text>'

    return f'''<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px;">
    {labels_svg}
    <polygon points="{fill_points}" fill="#ff174430"/>
    <polyline points="{points}" fill="none" stroke="#ff1744" stroke-width="1.5"/>
</svg>'''


def render_bar_chart_svg(data: List[Dict], value_key: str = "value", label_key: str = "label",
                         width: int = 700, height: int = 150,
                         color_positive: str = "#00c853", color_negative: str = "#ff1744") -> str:
    """Generate an inline SVG bar chart.

    data: list of dicts with value_key and label_key fields.
    """
    if not data:
        return '<svg viewBox="0 0 700 150" style="width:100%;max-width:700px;"><text x="350" y="75" text-anchor="middle" fill="#888" font-size="14">No data</text></svg>'

    values = [d.get(value_key, 0) for d in data]
    labels = [d.get(label_key, "") for d in data]
    max_abs = max(abs(v) for v in values) if values else 1
    if max_abs == 0:
        max_abs = 1

    pad_top, pad_bottom, pad_left, pad_right = 10, 25, 10, 10
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom

    # Zero line in the middle
    zero_y = pad_top + chart_h / 2
    bar_width = max(1, (chart_w / len(values)) * 0.8)
    gap = (chart_w / len(values)) * 0.2

    bars_svg = ""
    for i, v in enumerate(values):
        x = pad_left + i * (bar_width + gap)
        bar_h = (abs(v) / max_abs) * (chart_h / 2)
        color = color_positive if v >= 0 else color_negative

        if v >= 0:
            y = zero_y - bar_h
        else:
            y = zero_y

        bars_svg += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{max(bar_h, 1):.1f}" fill="{color}" rx="1"/>'

    # Zero line
    bars_svg += f'<line x1="{pad_left}" y1="{zero_y}" x2="{pad_left + chart_w}" y2="{zero_y}" stroke="#666" stroke-width="0.5"/>'

    # X-axis labels (first, middle, last — deduped so single-bar charts
    # don't render the same label 3 times)
    label_svg = ""
    if labels:
        label_idxs = sorted(set(
            i for i in (0, len(labels) // 2, len(labels) - 1)
            if 0 <= i < len(labels)
        ))
        for idx in label_idxs:
            x = pad_left + idx * (bar_width + gap) + bar_width / 2
            label_svg += f'<text x="{x:.1f}" y="{pad_top + chart_h + 15}" text-anchor="middle" fill="#888" font-size="9">{labels[idx]}</text>'

    return f'''<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px;">
    {bars_svg}
    {label_svg}
</svg>'''


def render_rolling_sharpe_svg(rolling_data: List[Dict], width: int = 700, height: int = 150) -> str:
    """Generate an inline SVG line chart for rolling Sharpe ratio."""
    if not rolling_data or len(rolling_data) < 2:
        return '<svg viewBox="0 0 700 150" style="width:100%;max-width:700px;"><text x="350" y="75" text-anchor="middle" fill="#888" font-size="14">Need more data for rolling Sharpe</text></svg>'

    vals = [d.get("sharpe", 0) for d in rolling_data]
    dates = [d.get("date", "") for d in rolling_data]

    min_v = min(vals)
    max_v = max(vals)
    v_range = max_v - min_v if max_v != min_v else 1

    pad_top, pad_bottom, pad_left, pad_right = 15, 25, 45, 20
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom

    def x_pos(i):
        return pad_left + (i / max(len(vals) - 1, 1)) * chart_w

    def y_pos(val):
        return pad_top + chart_h - ((val - min_v) / v_range) * chart_h

    points = " ".join(f"{x_pos(i):.1f},{y_pos(v):.1f}" for i, v in enumerate(vals))

    # Reference lines at 0, 1, 2
    ref_lines = ""
    for ref_val in [0, 1.0, 2.0]:
        if min_v <= ref_val <= max_v:
            y = y_pos(ref_val)
            color = "#00c853" if ref_val >= 2 else "#ff9800" if ref_val >= 1 else "#ff1744"
            ref_lines += f'<line x1="{pad_left}" y1="{y:.1f}" x2="{pad_left + chart_w}" y2="{y:.1f}" stroke="{color}" stroke-width="0.5" stroke-dasharray="4,4"/>'
            ref_lines += f'<text x="{pad_left - 5}" y="{y + 4:.1f}" text-anchor="end" fill="{color}" font-size="10">{ref_val:.1f}</text>'

    labels_svg = ""
    if dates:
        labels_svg += f'<text x="{pad_left}" y="{pad_top + chart_h + 18}" text-anchor="start" fill="#888" font-size="10">{dates[0]}</text>'
        labels_svg += f'<text x="{pad_left + chart_w}" y="{pad_top + chart_h + 18}" text-anchor="end" fill="#888" font-size="10">{dates[-1]}</text>'

    return f'''<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px;">
    {ref_lines}
    {labels_svg}
    <polyline points="{points}" fill="none" stroke="#2196f3" stroke-width="2"/>
</svg>'''


# ---------------------------------------------------------------------------
# Master metrics calculator
# ---------------------------------------------------------------------------

def calculate_all_metrics(db_paths, initial_capital: float = 10000) -> Dict[str, Any]:
    """Calculate every institutional metric from trade and snapshot data.

    Returns a comprehensive dict used by all 5 dashboard pages.
    Handles empty data gracefully -- returns zeroes / empty lists.
    """
    trades = _gather_trades(db_paths)
    # Pass per-profile initial capital so snapshots forward-fill correctly
    snapshots = _gather_snapshots(db_paths, initial_capital_per_profile=initial_capital)
    # Combined baseline = per-profile capital × number of profiles
    num_profiles = max(len(list(db_paths)), 1)
    combined_initial_capital = initial_capital * num_profiles

    result: Dict[str, Any] = {}

    # -----------------------------------------------------------------------
    # Basic counts
    # -----------------------------------------------------------------------
    # "closed_trades" = trades with realized PnL — the metric-relevant set.
    # "open_trades"   = positions currently held (BUY/SHORT without a matching
    # close yet). Both are shown on the dashboard so users see the full
    # activity count, but win-rate / profit-factor / Sharpe etc. still use
    # closed trades only (that's what has a realized PnL to measure).
    result["total_trades"] = len(trades)           # closed trades (backwards compat)
    result["closed_trades"] = len(trades)
    result["open_trades"] = _count_open_trades(db_paths)
    result["all_trades"] = len(trades) + result["open_trades"]
    result["has_trades"] = result["all_trades"] > 0
    result["has_snapshots"] = len(snapshots) > 0

    # -----------------------------------------------------------------------
    # Equity curve
    # -----------------------------------------------------------------------
    result["equity_curve"] = [{"date": s.get("date", ""), "equity": s.get("equity", 0)} for s in snapshots]

    # -----------------------------------------------------------------------
    # Performance metrics
    # -----------------------------------------------------------------------
    # Use combined capital as baseline so total return reflects actual P&L
    # against deposited capital (not just first snapshot value which can be
    # skewed when profiles have different first-snapshot dates)
    if snapshots:
        first_eq = combined_initial_capital
        last_eq = snapshots[-1].get("equity", combined_initial_capital) or combined_initial_capital
    else:
        first_eq = combined_initial_capital
        last_eq = combined_initial_capital

    total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
    result["total_pnl"] = round(total_pnl, 2)

    total_return = (last_eq - first_eq) / first_eq if first_eq > 0 else 0
    result["total_return_pct"] = round(total_return * 100, 2)

    # Days active
    if trades:
        try:
            first_ts = trades[0].get("timestamp", "")[:10]
            last_ts = trades[-1].get("timestamp", "")[:10]
            d1 = datetime.strptime(first_ts, "%Y-%m-%d")
            d2 = datetime.strptime(last_ts, "%Y-%m-%d")
            days_active = max((d2 - d1).days, 1)
        except Exception:
            days_active = 1
    else:
        days_active = 0
    result["days_active"] = days_active

    # Annualised return
    if days_active > 0 and total_return > -1:
        result["annualized_return_pct"] = round(
            ((1 + total_return) ** (365 / days_active) - 1) * 100, 2
        )
    else:
        result["annualized_return_pct"] = 0.0

    # Gross / Net return
    gross_pnl = 0.0
    slippage_impact = 0.0
    for t in trades:
        pnl = t.get("pnl", 0) or 0
        gross_pnl += pnl
        slip = t.get("slippage_pct", 0) or 0
        price = t.get("price", 0) or 0
        qty = t.get("qty", 0) or 0
        if slip and price and qty:
            slippage_impact += abs(slip / 100 * price * qty)

    result["gross_pnl"] = round(gross_pnl, 2)
    result["net_pnl"] = round(gross_pnl, 2)  # pnl already includes slippage from fills
    result["gross_return_pct"] = round((gross_pnl + slippage_impact) / first_eq * 100, 2) if first_eq > 0 else 0.0
    result["net_return_pct"] = round(gross_pnl / first_eq * 100, 2) if first_eq > 0 else 0.0

    # -----------------------------------------------------------------------
    # Daily returns (from snapshots or trades)
    # -----------------------------------------------------------------------
    daily_returns = []
    if len(snapshots) >= 2:
        for i in range(1, len(snapshots)):
            prev_eq = snapshots[i - 1].get("equity", 0) or 0
            cur_eq = snapshots[i].get("equity", 0) or 0
            if prev_eq > 0:
                daily_returns.append((cur_eq - prev_eq) / prev_eq)
    elif trades:
        # Fall back to daily P&L from trades
        daily_pnl_map = defaultdict(float)
        for t in trades:
            day = (t.get("timestamp") or "")[:10]
            daily_pnl_map[day] += t.get("pnl", 0) or 0
        for day in sorted(daily_pnl_map.keys()):
            daily_returns.append(daily_pnl_map[day] / first_eq if first_eq > 0 else 0)

    result["num_daily_returns"] = len(daily_returns)

    # -----------------------------------------------------------------------
    # Risk metrics — need at least 2 daily returns to compute any std.
    # Anything less is "insufficient data", not "zero volatility / sharpe".
    # Templates check the `_computable` flags to show N/A instead of 0.
    MIN_RETURNS_FOR_SHARPE = 2
    if len(daily_returns) >= MIN_RETURNS_FOR_SHARPE:
        avg_ret = _mean(daily_returns)
        std_ret = _std(daily_returns)
        neg_returns = [r for r in daily_returns if r < 0]
        std_neg = _std(neg_returns) if len(neg_returns) >= 2 else 0

        if std_ret > 0:
            result["sharpe_ratio"] = round(avg_ret / std_ret * math.sqrt(252), 2)
            result["sharpe_ratio_computable"] = True
            result["annualized_volatility"] = round(std_ret * math.sqrt(252) * 100, 2)
            result["annualized_volatility_computable"] = True
        else:
            # All daily returns identical → no volatility to measure
            result["sharpe_ratio"] = 0.0
            result["sharpe_ratio_computable"] = False
            result["annualized_volatility"] = 0.0
            result["annualized_volatility_computable"] = False

        if std_neg > 0:
            result["sortino_ratio"] = round(avg_ret / std_neg * math.sqrt(252), 2)
            result["sortino_ratio_computable"] = True
        else:
            result["sortino_ratio"] = 0.0
            result["sortino_ratio_computable"] = False
    else:
        result["sharpe_ratio"] = 0.0
        result["sharpe_ratio_computable"] = False
        result["sortino_ratio"] = 0.0
        result["sortino_ratio_computable"] = False
        result["annualized_volatility"] = 0.0
        result["annualized_volatility_computable"] = False

    # Max drawdown from equity curve
    max_dd_pct = 0.0
    max_dd_duration = 0
    dd_peak_date = ""
    dd_trough_date = ""
    rolling_drawdown = []

    if snapshots:
        peak = snapshots[0].get("equity", 0)
        peak_date = snapshots[0].get("date", "")
        peak_idx = 0
        max_dd = 0.0
        current_dd_start = 0

        for i, s in enumerate(snapshots):
            eq = s.get("equity", 0) or 0
            dt = s.get("date", "")
            if eq > peak:
                peak = eq
                peak_date = dt
                peak_idx = i
            dd = (peak - eq) / peak if peak > 0 else 0
            rolling_drawdown.append({"date": dt, "drawdown_pct": round(-dd * 100, 2)})
            if dd > max_dd:
                max_dd = dd
                dd_peak_date = peak_date
                dd_trough_date = dt

        max_dd_pct = round(max_dd * 100, 2)

        # Duration: count days from peak to recovery (or end)
        if dd_peak_date and dd_trough_date:
            try:
                pk = datetime.strptime(dd_peak_date, "%Y-%m-%d")
                tr = datetime.strptime(dd_trough_date, "%Y-%m-%d")
                # Check if we recovered
                recovered = False
                for s in snapshots:
                    try:
                        sd = datetime.strptime(s.get("date", ""), "%Y-%m-%d")
                    except Exception:
                        continue
                    if sd > tr and (s.get("equity", 0) or 0) >= peak:
                        max_dd_duration = (sd - pk).days
                        recovered = True
                        break
                if not recovered:
                    last_d = datetime.strptime(snapshots[-1].get("date", dd_trough_date), "%Y-%m-%d")
                    max_dd_duration = (last_d - pk).days
            except Exception:
                pass

    result["max_drawdown_pct"] = max_dd_pct
    result["max_drawdown_duration_days"] = max_dd_duration
    result["max_drawdown_peak_date"] = dd_peak_date
    result["max_drawdown_trough_date"] = dd_trough_date
    result["rolling_drawdown"] = rolling_drawdown

    # Calmar ratio — only meaningful with real drawdown and real history.
    # With 1 day of data and a 0.07% DD, -21% / 0.07 = -310 which is
    # nonsense. Guard against that by requiring both a non-trivial DD
    # and enough trading history.
    ann_ret = result["annualized_return_pct"]
    CALMAR_MIN_DD_PCT = 1.0      # DD must be >= 1% for Calmar to be meaningful
    CALMAR_MIN_DAYS = 30          # need at least a month of activity
    if (max_dd_pct >= CALMAR_MIN_DD_PCT
            and days_active >= CALMAR_MIN_DAYS):
        result["calmar_ratio"] = round(ann_ret / max_dd_pct, 2)
        result["calmar_ratio_computable"] = True
    else:
        result["calmar_ratio"] = 0.0
        result["calmar_ratio_computable"] = False

    # VaR / CVaR from trade returns — undefined with zero closed trades.
    # We also want a minimum sample (5+) before reporting VaR honestly;
    # one trade's return isn't a distribution.
    trade_return_pcts = []
    for t in trades:
        price = t.get("price", 0) or 0
        qty = t.get("qty", 0) or 0
        pnl = t.get("pnl", 0) or 0
        if price > 0 and qty > 0:
            cost = price * qty
            trade_return_pcts.append(pnl / cost * 100)

    MIN_TRADES_FOR_VAR = 5
    if len(trade_return_pcts) >= MIN_TRADES_FOR_VAR:
        result["var_95"] = round(_percentile(trade_return_pcts, 5), 2)
        result["var_95_computable"] = True
        var_threshold = result["var_95"]
        tail = [r for r in trade_return_pcts if r <= var_threshold]
        if tail:
            result["cvar_95"] = round(_mean(tail), 2)
            result["cvar_95_computable"] = True
        else:
            # Degenerate: no trades at or below the 5th percentile (rare)
            result["cvar_95"] = result["var_95"]
            result["cvar_95_computable"] = True
    else:
        result["var_95"] = 0.0
        result["var_95_computable"] = False
        result["cvar_95"] = 0.0
        result["cvar_95_computable"] = False

    # -----------------------------------------------------------------------
    # Rolling metrics
    # -----------------------------------------------------------------------
    # Rolling 3-month returns (63 trading days, calculated monthly)
    rolling_3m_returns = []
    rolling_6m_sharpe = []

    if len(daily_returns) >= 63 and snapshots:
        # Build date-aligned return series
        dated_returns = []
        if len(snapshots) >= 2:
            for i in range(1, len(snapshots)):
                prev_eq = snapshots[i - 1].get("equity", 0) or 0
                cur_eq = snapshots[i].get("equity", 0) or 0
                dt = snapshots[i].get("date", "")
                ret = (cur_eq - prev_eq) / prev_eq if prev_eq > 0 else 0
                dated_returns.append({"date": dt, "return": ret})

        # 3-month rolling return
        for i in range(63, len(dated_returns), 21):  # step by ~1 month
            window = dated_returns[i - 63:i]
            cum = 1.0
            for w in window:
                cum *= (1 + w["return"])
            rolling_3m_returns.append({
                "date": window[-1]["date"],
                "return_pct": round((cum - 1) * 100, 2),
            })

        # 6-month rolling Sharpe
        if len(dated_returns) >= 126:
            for i in range(126, len(dated_returns), 21):
                window = dated_returns[i - 126:i]
                rets = [w["return"] for w in window]
                m = _mean(rets)
                s = _std(rets)
                sharpe = (m / s * math.sqrt(252)) if s > 0 else 0
                rolling_6m_sharpe.append({
                    "date": window[-1]["date"],
                    "sharpe": round(sharpe, 2),
                })

    result["rolling_3m_returns"] = rolling_3m_returns
    result["rolling_6m_sharpe"] = rolling_6m_sharpe
    result["needs_more_days_rolling"] = max(0, 63 - len(daily_returns))

    # -----------------------------------------------------------------------
    # Trade analytics
    # -----------------------------------------------------------------------
    winning_trades = [t for t in trades if (t.get("pnl") or 0) > 0]
    losing_trades = [t for t in trades if (t.get("pnl") or 0) < 0]

    result["winning_trades"] = len(winning_trades)
    result["losing_trades"] = len(losing_trades)
    # Win rate — undefined without any closed trades
    if trades:
        result["win_rate"] = round(len(winning_trades) / len(trades) * 100, 1)
        result["win_rate_computable"] = True
    else:
        result["win_rate"] = 0.0
        result["win_rate_computable"] = False

    win_pnls = [t.get("pnl", 0) for t in winning_trades]
    loss_pnls = [t.get("pnl", 0) for t in losing_trades]

    total_gains = sum(win_pnls)
    total_losses_abs = abs(sum(loss_pnls))

    # Profit factor is undefined when there are no losses (all wins → +inf
    # is meaningless) or no trades. Only reportable when both sides exist.
    if winning_trades and losing_trades and total_losses_abs > 0:
        result["profit_factor"] = round(total_gains / total_losses_abs, 2)
        result["profit_factor_computable"] = True
    else:
        result["profit_factor"] = 0.0
        result["profit_factor_computable"] = False

    avg_win = _mean(win_pnls) if win_pnls else 0
    avg_loss = _mean(loss_pnls) if loss_pnls else 0  # negative number

    result["avg_win"] = round(avg_win, 2)
    result["avg_loss"] = round(avg_loss, 2)

    win_rate_frac = len(winning_trades) / len(trades) if trades else 0
    loss_rate_frac = 1 - win_rate_frac
    result["expectancy"] = round(
        (win_rate_frac * avg_win) + (loss_rate_frac * abs(avg_loss)) * (-1 if avg_loss < 0 else 1), 2
    ) if trades else 0.0
    # Correct formula: (win_rate * avg_win) - (loss_rate * avg_loss_abs)
    if trades:
        result["expectancy"] = round(
            (win_rate_frac * avg_win) - (loss_rate_frac * abs(avg_loss)), 2
        )

    # Avg win/loss %
    win_return_pcts = []
    loss_return_pcts = []
    for t in trades:
        price = t.get("price", 0) or 0
        qty = t.get("qty", 0) or 0
        pnl = t.get("pnl", 0) or 0
        if price > 0 and qty > 0:
            cost = price * qty
            ret_pct = pnl / cost * 100
            if pnl > 0:
                win_return_pcts.append(ret_pct)
            elif pnl < 0:
                loss_return_pcts.append(ret_pct)

    result["avg_win_pct"] = round(_mean(win_return_pcts), 2) if win_return_pcts else 0.0
    result["avg_loss_pct"] = round(_mean(loss_return_pcts), 2) if loss_return_pcts else 0.0
    # Win/Loss ratio is undefined when there are no wins OR no losses —
    # showing 0.00 in either case misleads users into thinking they have
    # a 0× edge rather than "no data to compute the ratio yet". Flag the
    # undefined case so the template can show N/A.
    if winning_trades and losing_trades and avg_loss != 0:
        result["win_loss_ratio"] = round(avg_win / abs(avg_loss), 2)
        result["win_loss_ratio_computable"] = True
    else:
        result["win_loss_ratio"] = 0.0
        result["win_loss_ratio_computable"] = False

    # Largest win / loss
    if winning_trades:
        best = max(winning_trades, key=lambda t: t.get("pnl", 0))
        result["largest_win"] = round(best.get("pnl", 0), 2)
        result["largest_win_symbol"] = best.get("symbol", "")
    else:
        result["largest_win"] = 0.0
        result["largest_win_symbol"] = ""

    if losing_trades:
        worst = min(losing_trades, key=lambda t: t.get("pnl", 0))
        result["largest_loss"] = round(worst.get("pnl", 0), 2)
        result["largest_loss_symbol"] = worst.get("symbol", "")
    else:
        result["largest_loss"] = 0.0
        result["largest_loss_symbol"] = ""

    # Avg hold days (match buys to sells — must use ALL trades, not the
    # pnl-filtered list above, because buys never have pnl set until
    # closed by a later sell).
    hold_days_list = []
    all_rows: List[Dict] = []
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, symbol, side FROM trades ORDER BY timestamp ASC"
            ).fetchall()
            all_rows.extend(dict(r) for r in rows)
            conn.close()
        except Exception:
            pass
    open_positions: Dict[str, str] = {}  # symbol -> timestamp of buy
    for t in all_rows:
        sym = t.get("symbol", "")
        side = (t.get("side") or "").lower()
        ts = t.get("timestamp", "")[:10]
        if side == "buy":
            open_positions[sym] = ts
        elif side == "sell" and sym in open_positions:
            try:
                d1 = datetime.strptime(open_positions[sym], "%Y-%m-%d")
                d2 = datetime.strptime(ts, "%Y-%m-%d")
                hold_days_list.append(max((d2 - d1).days, 0))
            except Exception:
                pass
            del open_positions[sym]
    result["avg_hold_days"] = round(_mean(hold_days_list), 1) if hold_days_list else 0.0

    # Trades per month
    months_active = max(days_active / 30.44, 1) if days_active > 0 else 1
    result["trades_per_month"] = round(len(trades) / months_active, 1) if trades else 0.0

    # -----------------------------------------------------------------------
    # Monthly returns
    # -----------------------------------------------------------------------
    monthly = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        ts = t.get("timestamp", "")
        if len(ts) >= 7:
            mk = ts[:7]
        else:
            continue
        monthly[mk]["trades"] += 1
        monthly[mk]["pnl"] += t.get("pnl", 0) or 0
        if (t.get("pnl", 0) or 0) > 0:
            monthly[mk]["wins"] += 1
        elif (t.get("pnl", 0) or 0) < 0:
            monthly[mk]["losses"] += 1

    # Equity at start of each month from snapshots
    snap_by_month = {}
    for s in snapshots:
        mk = (s.get("date") or "")[:7]
        if mk not in snap_by_month:
            snap_by_month[mk] = s.get("equity", 0) or 0

    monthly_list = []
    for mk in sorted(monthly.keys(), reverse=True):
        m = monthly[mk]
        try:
            label = datetime.strptime(mk, "%Y-%m").strftime("%b %Y")
        except Exception:
            label = mk
        eq_start = snap_by_month.get(mk, 0)
        return_pct = round(m["pnl"] / eq_start * 100, 1) if eq_start > 0 else 0.0
        monthly_list.append({
            "month": label,
            "month_key": mk,
            "trades": m["trades"],
            "wins": m["wins"],
            "losses": m["losses"],
            "pnl": round(m["pnl"], 2),
            "return_pct": return_pct,
        })
    result["monthly_returns"] = monthly_list

    # Monthly win rate — undefined when there's not a full month of activity
    profitable_months = sum(1 for m in monthly_list if m["pnl"] > 0)
    total_months = len(monthly_list)
    if total_months > 0:
        result["monthly_win_rate"] = round(profitable_months / total_months * 100, 1)
        result["monthly_win_rate_computable"] = True
    else:
        result["monthly_win_rate"] = 0.0
        result["monthly_win_rate_computable"] = False

    # Best / worst month
    if monthly_list:
        best_m = max(monthly_list, key=lambda m: m["pnl"])
        worst_m = min(monthly_list, key=lambda m: m["pnl"])
        result["best_month"] = {"month_label": best_m["month"], "pnl": best_m["pnl"], "return_pct": best_m["return_pct"]}
        result["worst_month"] = {"month_label": worst_m["month"], "pnl": worst_m["pnl"], "return_pct": worst_m["return_pct"]}
    else:
        result["best_month"] = {"month_label": "N/A", "pnl": 0, "return_pct": 0}
        result["worst_month"] = {"month_label": "N/A", "pnl": 0, "return_pct": 0}

    # -----------------------------------------------------------------------
    # Streaks
    # -----------------------------------------------------------------------
    max_consec_wins = 0
    max_consec_losses = 0
    cur_streak_len = 0
    cur_streak_type = "none"
    winning_streaks = []
    losing_streaks = []

    for t in trades:
        pnl = t.get("pnl", 0) or 0
        if pnl > 0:
            if cur_streak_type == "winning":
                cur_streak_len += 1
            else:
                if cur_streak_type == "losing" and cur_streak_len > 0:
                    losing_streaks.append(cur_streak_len)
                cur_streak_type = "winning"
                cur_streak_len = 1
        elif pnl < 0:
            if cur_streak_type == "losing":
                cur_streak_len += 1
            else:
                if cur_streak_type == "winning" and cur_streak_len > 0:
                    winning_streaks.append(cur_streak_len)
                cur_streak_type = "losing"
                cur_streak_len = 1

    # Final streak
    if cur_streak_type == "winning" and cur_streak_len > 0:
        winning_streaks.append(cur_streak_len)
    elif cur_streak_type == "losing" and cur_streak_len > 0:
        losing_streaks.append(cur_streak_len)

    result["max_consecutive_wins"] = max(winning_streaks) if winning_streaks else 0
    result["max_consecutive_losses"] = max(losing_streaks) if losing_streaks else 0
    result["current_streak"] = {
        "count": cur_streak_len,
        "type": cur_streak_type,
        # Computable = has any closed trades; "0 none" is confusing display
        "computable": bool(trades),
    }

    # -----------------------------------------------------------------------
    # Worst periods
    # -----------------------------------------------------------------------
    daily_pnl_map = defaultdict(float)
    for t in trades:
        day = (t.get("timestamp") or "")[:10]
        daily_pnl_map[day] += t.get("pnl", 0) or 0

    sorted_days = sorted(daily_pnl_map.keys())

    def _worst_period(n_days: int) -> Dict:
        """Find worst N-day window by PnL."""
        if len(sorted_days) < 2:
            return {"period": "N/A", "pnl": 0, "return_pct": 0}
        worst_pnl = 0
        worst_start = ""
        worst_end = ""
        for i in range(len(sorted_days)):
            try:
                start_d = datetime.strptime(sorted_days[i], "%Y-%m-%d")
            except Exception:
                continue
            window_pnl = 0
            end_d_str = sorted_days[i]
            for j in range(i, len(sorted_days)):
                try:
                    cur_d = datetime.strptime(sorted_days[j], "%Y-%m-%d")
                except Exception:
                    continue
                if (cur_d - start_d).days > n_days:
                    break
                window_pnl += daily_pnl_map[sorted_days[j]]
                end_d_str = sorted_days[j]
            if window_pnl < worst_pnl:
                worst_pnl = window_pnl
                worst_start = sorted_days[i]
                worst_end = end_d_str
        return {
            "period": f"{worst_start} to {worst_end}" if worst_start else "N/A",
            "pnl": round(worst_pnl, 2),
            "return_pct": round(worst_pnl / first_eq * 100, 2) if first_eq > 0 else 0,
        }

    result["worst_week"] = _worst_period(7)
    result["worst_month_period"] = _worst_period(30)
    result["worst_quarter"] = _worst_period(90)

    # -----------------------------------------------------------------------
    # Market relationship (SPY, QQQ, BTC) — computable flags distinguish
    # "insufficient data" from "actually uncorrelated / zero alpha"
    # -----------------------------------------------------------------------
    result["beta_spy"] = 0.0
    result["beta_spy_computable"] = False
    result["alpha"] = 0.0
    result["alpha_computable"] = False
    result["correlation_spy"] = 0.0
    result["correlation_spy_computable"] = False
    result["correlation_qqq"] = 0.0
    result["correlation_qqq_computable"] = False
    result["correlation_btc"] = 0.0
    result["correlation_btc_computable"] = False

    if len(daily_returns) >= 20 and snapshots:
        # Build portfolio daily returns keyed by date
        portfolio_by_date: Dict[str, float] = {}
        if len(snapshots) >= 2:
            for i in range(1, len(snapshots)):
                prev_eq = snapshots[i - 1].get("equity", 0) or 0
                cur_eq = snapshots[i].get("equity", 0) or 0
                dt = snapshots[i].get("date", "")
                if prev_eq > 0 and dt:
                    portfolio_by_date[dt] = (cur_eq - prev_eq) / prev_eq

        start_date = snapshots[0].get("date", "")
        end_date = snapshots[-1].get("date", "")

        if start_date and end_date:
            # Add buffer day
            try:
                sd = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=5)
                ed = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                start_str = sd.strftime("%Y-%m-%d")
                end_str = ed.strftime("%Y-%m-%d")
            except Exception:
                start_str = start_date
                end_str = end_date

            for ticker, key in [("SPY", "spy"), ("QQQ", "qqq"), ("BTC-USD", "btc")]:
                bench = _fetch_benchmark_returns(ticker, start_str, end_str)
                if not bench:
                    continue

                # Align dates
                aligned_port = []
                aligned_bench = []
                for dt in sorted(portfolio_by_date.keys()):
                    if dt in bench:
                        aligned_port.append(portfolio_by_date[dt])
                        aligned_bench.append(bench[dt])

                if len(aligned_port) < 10:
                    continue

                # Correlation
                m_p = _mean(aligned_port)
                m_b = _mean(aligned_bench)
                s_p = _std(aligned_port)
                s_b = _std(aligned_bench)

                if s_p > 0 and s_b > 0:
                    cov = _mean([(p - m_p) * (b - m_b) for p, b in zip(aligned_port, aligned_bench)])
                    corr = cov / (s_p * s_b)
                    result[f"correlation_{key}"] = round(corr, 3)
                    result[f"correlation_{key}_computable"] = True

                    if key == "spy":
                        var_bench = s_b ** 2
                        if var_bench > 0:
                            beta = cov / var_bench
                            result["beta_spy"] = round(beta, 3)
                            result["beta_spy_computable"] = True
                            # Alpha: annualised
                            ann_port = result["annualized_return_pct"]
                            ann_bench = _mean(aligned_bench) * 252 * 100
                            result["alpha"] = round(ann_port - (beta * ann_bench), 2)
                            result["alpha_computable"] = True

    # -----------------------------------------------------------------------
    # Scalability metrics
    # -----------------------------------------------------------------------
    position_sizes = []
    slippage_pcts = []
    slippage_costs = []
    gross_profit = total_gains

    for t in trades:
        price = t.get("price", 0) or 0
        qty = t.get("qty", 0) or 0
        if price > 0 and qty > 0:
            position_sizes.append(price * qty)

        slip = t.get("slippage_pct", None)
        if slip is not None:
            slippage_pcts.append(slip)
            dp = t.get("decision_price", 0) or 0
            fp = t.get("fill_price", 0) or 0
            if dp > 0 and fp > 0 and qty > 0:
                slippage_costs.append(abs(fp - dp) * qty)

    result["avg_position_size"] = round(_mean(position_sizes), 2) if position_sizes else 0.0
    result["slippage_avg_pct"] = round(_mean(slippage_pcts), 4) if slippage_pcts else 0.0
    result["slippage_total_cost"] = round(sum(slippage_costs), 2) if slippage_costs else 0.0
    # Slippage vs gross — undefined when gross profit ≤ 0 (can't express
    # slippage as a fraction of profit that doesn't exist)
    if gross_profit > 0 and slippage_costs:
        result["slippage_vs_gross"] = round(
            sum(slippage_costs) / gross_profit * 100, 2
        )
        result["slippage_vs_gross_computable"] = True
    else:
        result["slippage_vs_gross"] = 0.0
        result["slippage_vs_gross_computable"] = False
    result["trades_with_slippage"] = len(slippage_pcts)

    # Trade PnL distribution (for histogram)
    pnl_distribution = []
    if trade_return_pcts:
        # Bucket into 2% intervals
        buckets = defaultdict(int)
        for r in trade_return_pcts:
            bucket = int(r // 2) * 2
            buckets[bucket] += 1
        for b in sorted(buckets.keys()):
            pnl_distribution.append({
                "label": f"{b}%",
                "value": buckets[b],
                "bucket_start": b,
            })
    result["pnl_distribution"] = pnl_distribution

    # -----------------------------------------------------------------------
    # SVG charts (pre-rendered)
    # -----------------------------------------------------------------------
    result["equity_curve_svg"] = render_equity_curve_svg(result["equity_curve"])
    result["drawdown_svg"] = render_drawdown_svg(result["rolling_drawdown"])
    result["rolling_sharpe_svg"] = render_rolling_sharpe_svg(result["rolling_6m_sharpe"])

    # Monthly returns bar chart
    monthly_bar_data = []
    for m in reversed(result["monthly_returns"]):  # chronological
        monthly_bar_data.append({"label": m["month"], "value": m["return_pct"]})
    result["monthly_returns_svg"] = render_bar_chart_svg(monthly_bar_data, value_key="value", label_key="label")

    # PnL distribution bar chart
    if pnl_distribution:
        result["pnl_distribution_svg"] = render_bar_chart_svg(
            pnl_distribution, value_key="value", label_key="label",
            color_positive="#2196f3", color_negative="#2196f3"
        )
    else:
        result["pnl_distribution_svg"] = ""

    return result
