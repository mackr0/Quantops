"""Item 2a (continued) — historical scenario stress testing.

Replays the actual factor returns from documented crisis windows and
projects them onto the current portfolio's factor exposures to estimate
hypothetical P&L. Real fund risk teams use this for tail-risk awareness
the parametric VaR model can't capture.

Scenarios shipped:

  1987_blackmonday   — 1987-10-12 → 1987-10-31  (Oct '87 crash)
  2000_dotcom        — 2000-04-01 → 2000-06-30  (Nasdaq -40%)
  2008_lehman        — 2008-09-01 → 2008-10-31  (Lehman + TARP)
  2018_q4_selloff    — 2018-10-01 → 2018-12-24  (rate-fear selloff)
  2020_covid         — 2020-02-19 → 2020-03-23  (COVID crash)
  2022_rates         — 2022-01-01 → 2022-10-31  (Fed hiking cycle)
  2023_svb           — 2023-03-08 → 2023-03-15  (SVB / regional banks)

How it works:
  1. For each scenario, fetch the historical factor returns (Ken French
     for 1987 / dot-com, sector ETFs for newer ones, both when both
     are available).
  2. Project: scenario P&L_t = w' (B @ factor_returns_t) for each
     trading day in the window.
  3. Aggregate: sum daily, find worst day, find max drawdown over the
     window.
  4. Add idio noise estimate: idio σ × sqrt(window_days) (a rough
     approximation — real idio realizations are unknowable, but we
     don't want to under-report).

Limits:
  - Older scenarios (1987, dot-com) only have French factors, so
    sector-tilt P&L is approximated via what overlap exists.
  - Idio P&L is treated as a 1σ band, not a single number — we report
    a range.
  - Cross-asset risk (rates, FX, commodities) isn't in the factor set
    yet, so a 2022-style rate shock under-reports.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StressScenario:
    """Definition of one historical stress window."""
    name: str
    start_date: str            # ISO YYYY-MM-DD
    end_date: str
    description: str
    severity: str              # "moderate" | "severe" | "catastrophic"
    has_sector_etfs: bool      # whether SPDR sector ETFs existed in window


SCENARIOS: List[StressScenario] = [
    StressScenario(
        name="1987_blackmonday",
        start_date="1987-10-12",
        end_date="1987-10-31",
        description=(
            "Black Monday — Oct 19 1987 saw the largest one-day "
            "percentage drop in S&P 500 history (-20.5%)"
        ),
        severity="catastrophic",
        has_sector_etfs=False,
    ),
    StressScenario(
        name="2000_dotcom",
        start_date="2000-04-01",
        end_date="2000-06-30",
        description=(
            "Q2 2000 dot-com unwind — Nasdaq down ~40% from March "
            "highs as growth-tech multiples compressed"
        ),
        severity="severe",
        has_sector_etfs=True,    # XLK existed since 1998-12
    ),
    StressScenario(
        name="2008_lehman",
        start_date="2008-09-01",
        end_date="2008-10-31",
        description=(
            "GFC peak — Lehman bankruptcy 9/15, TARP rejection 9/29, "
            "VIX > 80, S&P -27% in 6 weeks"
        ),
        severity="catastrophic",
        has_sector_etfs=True,
    ),
    StressScenario(
        name="2018_q4_selloff",
        start_date="2018-10-01",
        end_date="2018-12-24",
        description=(
            "Q4 2018 — Powell rate-hike fears + trade war; S&P -19% "
            "peak-to-Christmas-Eve trough"
        ),
        severity="moderate",
        has_sector_etfs=True,
    ),
    StressScenario(
        name="2020_covid",
        start_date="2020-02-19",
        end_date="2020-03-23",
        description=(
            "COVID crash — S&P -34% in 33 days, fastest bear market "
            "in history"
        ),
        severity="severe",
        has_sector_etfs=True,
    ),
    StressScenario(
        name="2022_rates",
        start_date="2022-01-01",
        end_date="2022-10-31",
        description=(
            "2022 Fed hiking cycle — long-duration assets crushed; "
            "S&P -25% peak-to-trough; NDX -37%"
        ),
        severity="severe",
        has_sector_etfs=True,
    ),
    StressScenario(
        name="2023_svb",
        start_date="2023-03-08",
        end_date="2023-03-15",
        description=(
            "Silicon Valley Bank failure + regional bank contagion; "
            "KRE -28% in a week"
        ),
        severity="moderate",
        has_sector_etfs=True,
    ),
]


def _fetch_scenario_factor_returns(scenario: StressScenario):
    """Fetch the factor return matrix for the scenario window.

    Combines:
      - Sector + style ETF returns from Alpaca (when available in window)
      - Ken French daily 5F + Mom across the same window

    Returns DataFrame with columns matching the live factor model where
    possible. Empty columns when a factor didn't exist in the window.
    """
    import pandas as pd
    from market_data import get_bars_daterange
    from portfolio_risk_model import (
        SECTOR_ETFS, STYLE_ETFS, FRENCH_FACTORS, fetch_french_factors,
    )

    cols = {}

    # Sector + style ETFs (when they existed)
    if scenario.has_sector_etfs:
        for label, etf in {**SECTOR_ETFS, **STYLE_ETFS}.items():
            try:
                bars = get_bars_daterange(
                    etf, scenario.start_date, scenario.end_date,
                )
                if bars is None or bars.empty:
                    continue
                rets = bars["close"].pct_change().dropna()
                if not rets.empty:
                    rets.name = label
                    cols[label] = rets
            except Exception as exc:
                logger.debug(
                    "scenario %s: ETF %s fetch failed: %s",
                    scenario.name, etf, exc,
                )

    # Ken French — fetch the full history once, then slice
    french = fetch_french_factors(lookback_days=200000)  # max history
    if french is not None and not french.empty:
        french.index = french.index.normalize()
        start = pd.Timestamp(scenario.start_date)
        end = pd.Timestamp(scenario.end_date)
        sliced = french.loc[(french.index >= start) & (french.index <= end)]
        for col in FRENCH_FACTORS:
            if col in sliced.columns:
                cols[col] = sliced[col]

    if not cols:
        return pd.DataFrame()

    df = pd.DataFrame(cols)
    # Normalize all indices to date-only naive
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    return df.dropna(how="all")


def replay_scenario(
    scenario: StressScenario,
    weights: Dict[str, float],
    exposures: Dict[str, Dict[str, Any]],
    portfolio_value: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """Project the portfolio's exposures onto a historical factor-return
    window and return a P&L summary.

    Args:
        scenario: a StressScenario definition.
        weights:  current portfolio weights {symbol: weight}.
        exposures: {symbol: estimate_exposures()} from the live risk model.
        portfolio_value: dollar size of the book.

    Returns dict with:
        scenario:        scenario.name
        description:     human-readable
        n_days:          trading days in window
        total_pnl_pct / total_pnl_dollars
        worst_day_pct / worst_day_dollars / worst_day_date
        max_drawdown_pct / max_drawdown_dollars
        idio_band_pct:   ±1σ approx idio P&L over window
        factors_available: list of factors present in the historical
                           data (vs the live factor set)
        factors_missing:   factors in the live model but not in the
                           scenario window (so the user knows where the
                           reading underestimates)
    """
    import numpy as np
    import pandas as pd

    if not weights or not exposures:
        return None

    syms = [s for s in weights if s in exposures and exposures[s] is not None]
    if not syms:
        return None

    live_factor_names = list(next(iter(exposures.values()))["beta"].keys())
    factor_returns = _fetch_scenario_factor_returns(scenario)
    if factor_returns.empty:
        return None

    available = [f for f in live_factor_names if f in factor_returns.columns]
    missing = [f for f in live_factor_names if f not in factor_returns.columns]
    if not available:
        return None

    # Project portfolio factor exposure
    w = np.array([weights[s] for s in syms])
    B = np.array([[exposures[s]["beta"][f] for f in available]
                   for s in syms])
    portfolio_betas = w @ B   # shape (len(available),)

    # Daily portfolio P&L = portfolio_betas @ daily_factor_returns
    fr = factor_returns[available].dropna(how="any")
    if fr.empty:
        return None
    daily_pnl_pct = fr.values @ portfolio_betas    # length n_days
    daily_pnl_dollars = daily_pnl_pct * portfolio_value

    cumulative = np.cumprod(1 + daily_pnl_pct) - 1
    # Baseline peak is 0 (start of window) — otherwise a monotone-down
    # path under-reports drawdown because day-1 becomes the "peak".
    running_max = np.maximum.accumulate(
        np.concatenate(([0.0], cumulative))
    )[1:]
    drawdown = cumulative - running_max
    max_dd_pct = float(drawdown.min())
    total_pct = float(cumulative[-1])
    worst_idx = int(np.argmin(daily_pnl_pct))
    worst_pct = float(daily_pnl_pct[worst_idx])
    worst_date = fr.index[worst_idx].strftime("%Y-%m-%d")

    # Idio band: σ_idio,p × sqrt(n_days). Only if exposures carry idio_var.
    idio_var = np.array([exposures[s]["idio_var"] for s in syms])
    daily_idio_var = float(np.sum((w ** 2) * idio_var))
    n_days = len(fr)
    idio_band_pct = float(np.sqrt(daily_idio_var * n_days))

    return {
        "scenario": scenario.name,
        "description": scenario.description,
        "severity": scenario.severity,
        "start_date": scenario.start_date,
        "end_date": scenario.end_date,
        "n_days": n_days,
        "total_pnl_pct": total_pct,
        "total_pnl_dollars": total_pct * portfolio_value,
        "worst_day_pct": worst_pct,
        "worst_day_dollars": worst_pct * portfolio_value,
        "worst_day_date": worst_date,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_dollars": max_dd_pct * portfolio_value,
        "idio_band_pct": idio_band_pct,
        "idio_band_dollars": idio_band_pct * portfolio_value,
        "factors_available": available,
        "factors_missing": missing,
        "approximation_quality": (
            "high" if not missing else
            "medium" if len(missing) < len(available) else
            "low"
        ),
    }


def run_all_scenarios(
    weights: Dict[str, float],
    exposures: Dict[str, Dict[str, Any]],
    portfolio_value: float = 1.0,
) -> List[Dict[str, Any]]:
    """Run every defined scenario, return a list ordered by severity
    of the projected P&L (worst first)."""
    results = []
    for sc in SCENARIOS:
        try:
            r = replay_scenario(sc, weights, exposures, portfolio_value)
            if r is not None:
                results.append(r)
        except Exception as exc:
            logger.warning("scenario %s failed: %s", sc.name, exc)
    # Sort worst-total first
    results.sort(key=lambda r: r["total_pnl_pct"])
    return results


def render_scenarios_for_prompt(results: List[Dict[str, Any]]) -> str:
    """Compact rendering for the AI prompt + dashboard."""
    if not results:
        return ""
    lines = ["Stress scenarios (projected P&L on current book):"]
    for r in results:
        qual = ""
        if r["approximation_quality"] == "low":
            qual = " (approx — many factors missing)"
        elif r["approximation_quality"] == "medium":
            qual = " (approx)"
        lines.append(
            f"  • {r['scenario']:<22s} "
            f"total: {r['total_pnl_pct'] * 100:+6.2f}%, "
            f"worst day: {r['worst_day_pct'] * 100:+6.2f}% "
            f"({r['worst_day_date']}){qual}"
        )
    return "\n".join(lines)
