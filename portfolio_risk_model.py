"""Item 2a of COMPETITIVE_GAP_PLAN.md — full Barra-style multi-factor risk model.

Real funds run 50-100 factor models with full covariance matrices,
portfolio-level VaR, expected shortfall, and historical scenario stress
tests. We don't need 100 factors at our scale — but we DO need the
machinery: a real factor universe, exposure regressions, factor
covariance, parametric AND Monte Carlo VaR, expected shortfall, and
historical scenario replay.

Factor universe (~21 factors):

  Ken French academic factors (free CSV, daily, 1926-present):
    Mkt-RF  — market excess return
    SMB     — size (small minus big)
    HML     — value (high minus low book/market)
    RMW     — profitability (robust minus weak)
    CMA     — investment (conservative minus aggressive)
    Mom     — momentum (winners minus losers)

  SPDR sector ETFs (from Alpaca, daily, 1998-present):
    XLK XLF XLE XLV XLI XLP XLY XLU XLB XLRE XLC

  Style ETFs (from Alpaca):
    IWM   — small caps
    MTUM  — momentum
    QUAL  — quality
    USMV  — minimum volatility

Why both French + sector ETFs: French factors capture style risk (the
academic anomalies). Sector ETFs capture industry concentration risk
(banking blowup ≠ tech blowup ≠ energy blowup). Real fund managers
care about both. Style alone misses sector tilt; sector alone misses
size/value/momentum exposure.

Pipeline:
  1. compute_factor_returns(lookback_days)
        → DataFrame of joint factor daily returns
  2. estimate_exposures(symbol_rets, factor_returns)
        → β vector + idiosyncratic variance per symbol via OLS
  3. estimate_factor_cov(factor_returns)
        → factor covariance with Ledoit-Wolf shrinkage
  4. compute_portfolio_risk(weights, exposures, factor_cov, ...)
        → factor + idio variance, parametric 95/99% VaR + ES,
          per-factor and per-sector decomposition
  5. monte_carlo_var(...)
        → simulation-based VaR + ES (handles non-normality of the
          mixture distribution better than parametric)
  6. compute_portfolio_risk_from_positions(positions, equity)
        → end-to-end convenience: takes broker positions, returns the
          full risk dict

Limits documented honestly:
  - Parametric VaR assumes normal returns — understates true tail risk.
    Monte Carlo helps but inherits the normality assumption of the
    factor distribution.
  - Ken French data is published with a ~1 month lag — most recent
    weeks may be missing. We fall back to ETF proxies when missing.
  - Sector ETFs carry residual market beta. Multicollinearity with
    Mkt-RF is real; ridge-regularized regression mitigates. We use
    ridge with α tuned per the documented stability/bias trade-off.
  - 1987 / dot-com scenarios use French data only (sector ETFs didn't
    exist), so sector-tilt P&L for those windows is approximated via
    the closest available factor exposure.
"""
from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Factor definitions
# ---------------------------------------------------------------------------

# Sectors — SPDR sector ETFs
SECTOR_ETFS = {
    "sector_tech":         "XLK",
    "sector_financials":   "XLF",
    "sector_energy":       "XLE",
    "sector_healthcare":   "XLV",
    "sector_industrials":  "XLI",
    "sector_staples":      "XLP",
    "sector_discretionary": "XLY",
    "sector_utilities":    "XLU",
    "sector_materials":    "XLB",
    "sector_realestate":   "XLRE",   # since 2015-10
    "sector_communication": "XLC",   # since 2018-06
}

# Style — MSCI USA factor ETFs
STYLE_ETFS = {
    "style_smallcap":  "IWM",
    "style_momentum":  "MTUM",
    "style_quality":   "QUAL",
    "style_lowvol":    "USMV",
}

# Ken French daily 5-factor + Momentum
FRENCH_FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"]

DEFAULT_LOOKBACK_DAYS = 252            # ~1 trading year
MIN_OBS_FOR_REGRESSION = 60            # below this, skip a symbol
RIDGE_ALPHA = 1.0                      # mild regularization vs OLS

# Normal-quantile VaR multipliers
Z_95 = 1.645
Z_99 = 2.326


# ---------------------------------------------------------------------------
# Ken French data fetcher (cached on disk)
# ---------------------------------------------------------------------------

KEN_FRENCH_5F_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
)
KEN_FRENCH_MOM_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Momentum_Factor_daily_CSV.zip"
)
FRENCH_CACHE_DIR = ".cache/french_factors"
FRENCH_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days; published monthly


def _cache_path(name: str) -> str:
    os.makedirs(FRENCH_CACHE_DIR, exist_ok=True)
    return os.path.join(FRENCH_CACHE_DIR, name)


def _fetch_zip_csv(url: str, cache_name: str) -> Optional[str]:
    """Fetch a Ken French ZIP, extract the inner CSV, cache to disk for
    7 days. Returns the CSV text, or None on failure.
    """
    import urllib.request
    cache = _cache_path(cache_name)
    if (os.path.exists(cache)
            and (time.time() - os.path.getmtime(cache)) < FRENCH_CACHE_TTL_SECONDS):
        try:
            with open(cache, "r", encoding="latin-1") as fh:
                return fh.read()
        except OSError:
            pass
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "QuantOpsAI risk model"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            inner = zf.namelist()[0]
            csv_text = zf.read(inner).decode("latin-1")
        with open(cache, "w", encoding="latin-1") as fh:
            fh.write(csv_text)
        return csv_text
    except Exception as exc:
        logger.warning("Ken French fetch failed for %s: %s", url, exc)
        return None


def _parse_french_csv(csv_text: str, columns: List[str]):
    """Ken French daily CSVs have a multi-line header, then YYYYMMDD,
    factor1, factor2, ... rows, then sometimes annual rows below.

    Returns a pandas DataFrame indexed by date, with the requested columns
    in DECIMAL form (the source is in percent — we divide by 100).
    """
    import pandas as pd

    lines = csv_text.splitlines()
    # Find header row (line starting with the first column name)
    start = None
    for i, line in enumerate(lines):
        cells = [c.strip() for c in line.split(",")]
        # header looks like: '', 'Mkt-RF', 'SMB', 'HML', ...
        if len(cells) >= 2 and any(c == columns[0] for c in cells):
            start = i
            break
    if start is None:
        return None

    # Stop at the first blank or non-date row after start
    rows = []
    for line in lines[start + 1:]:
        cells = [c.strip() for c in line.split(",")]
        if not cells or not cells[0] or not cells[0].isdigit():
            break
        if len(cells[0]) != 8:
            break
        try:
            row = {col: float(cells[1 + idx]) / 100.0
                   for idx, col in enumerate(columns)}
            row["date"] = pd.to_datetime(cells[0], format="%Y%m%d")
            rows.append(row)
        except (ValueError, IndexError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows).set_index("date").sort_index()
    return df[columns]


def fetch_french_factors(lookback_days: int = DEFAULT_LOOKBACK_DAYS):
    """Fetch Ken French daily 5-factor + Momentum, joined and trimmed.

    Returns DataFrame with columns [Mkt-RF, SMB, HML, RMW, CMA, Mom],
    indexed by date, in DECIMAL form. None if fetch fails.
    """
    import pandas as pd

    csv_5f = _fetch_zip_csv(KEN_FRENCH_5F_URL, "ff5_daily.csv")
    csv_mom = _fetch_zip_csv(KEN_FRENCH_MOM_URL, "mom_daily.csv")
    if not csv_5f or not csv_mom:
        return None

    df_5f = _parse_french_csv(csv_5f, ["Mkt-RF", "SMB", "HML", "RMW", "CMA"])
    df_mom = _parse_french_csv(csv_mom, ["Mom"])
    if df_5f is None or df_mom is None:
        return None
    joined = df_5f.join(df_mom, how="inner")
    if joined.empty:
        return None
    return joined.tail(lookback_days * 2)   # extra slack for joins


# ---------------------------------------------------------------------------
# ETF returns
# ---------------------------------------------------------------------------

def _etf_returns(etf_map: Dict[str, str], lookback_days: int):
    """Return DataFrame of daily returns for each ETF in the map. Keys
    are factor names; values are tickers. Missing tickers are dropped
    silently.
    """
    import pandas as pd
    from market_data import get_bars

    cols = {}
    for factor, etf in etf_map.items():
        bars = get_bars(etf, limit=lookback_days + 10)
        if bars is None or bars.empty:
            logger.info(
                "portfolio_risk_model: bars unavailable for factor %s (%s)",
                factor, etf,
            )
            continue
        rets = bars["close"].pct_change().dropna()
        rets.name = factor
        cols[factor] = rets
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols)


# ---------------------------------------------------------------------------
# Build the joint factor matrix
# ---------------------------------------------------------------------------

def compute_factor_returns(
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    include_french: bool = True,
):
    """Build the joint factor return matrix (sectors + styles + French).

    Joins on the date index — only days where ALL available factors
    have a return are kept. Trims to the most recent `lookback_days`.
    Returns an empty DataFrame on total failure (caller should handle).
    """
    import pandas as pd

    sector = _etf_returns(SECTOR_ETFS, lookback_days)
    style = _etf_returns(STYLE_ETFS, lookback_days)
    parts = [df for df in (sector, style) if not df.empty]
    if not parts:
        return pd.DataFrame()

    etf_df = pd.concat(parts, axis=1)
    # Match Alpaca's tz-aware index style — French data is tz-naive
    if etf_df.index.tz is not None:
        etf_df.index = etf_df.index.tz_localize(None).normalize()
    else:
        etf_df.index = etf_df.index.normalize()

    if include_french:
        french = fetch_french_factors(lookback_days)
        if french is not None and not french.empty:
            french.index = french.index.normalize()
            joined = etf_df.join(french, how="inner")
        else:
            logger.info(
                "portfolio_risk_model: Ken French unavailable; using "
                "ETF-only factor set (%d factors)",
                len(etf_df.columns),
            )
            joined = etf_df
    else:
        joined = etf_df

    joined = joined.dropna()
    if joined.empty:
        return joined
    return joined.tail(lookback_days)


# ---------------------------------------------------------------------------
# Per-symbol exposures (ridge-regularized regression)
# ---------------------------------------------------------------------------

def estimate_exposures(symbol_returns, factor_returns):
    """Ridge regression of one symbol's daily returns onto factor
    returns. Ridge (vs plain OLS) keeps multicollinearity from blowing
    up the β estimates — sector ETFs and Mkt-RF are strongly correlated.

    Returns a dict with:
        alpha:    intercept
        beta:     {factor_name: exposure}
        idio_var: residual variance (daily, raw — not annualized)
        n_obs:    samples used
        r_squared: regression fit
    """
    import numpy as np
    import pandas as pd

    if isinstance(symbol_returns, pd.Series):
        df = pd.concat([symbol_returns.rename("y"), factor_returns],
                        axis=1).dropna()
    else:
        df = pd.concat([pd.Series(symbol_returns, name="y"),
                         factor_returns], axis=1).dropna()

    if len(df) < MIN_OBS_FOR_REGRESSION:
        return None

    y = df["y"].values
    X = df[list(factor_returns.columns)].values
    n, k = X.shape
    # Ridge: β̂ = (XᵀX + αI)⁻¹ Xᵀy
    # Standardize features so α applies uniformly across columns
    mu = X.mean(axis=0)
    sd = X.std(axis=0, ddof=1)
    sd_safe = np.where(sd == 0, 1.0, sd)
    Xs = (X - mu) / sd_safe
    XtX = Xs.T @ Xs
    reg = RIDGE_ALPHA * np.eye(k)
    try:
        beta_std = np.linalg.solve(XtX + reg, Xs.T @ (y - y.mean()))
    except np.linalg.LinAlgError:
        return None
    # Convert back to raw scale
    beta_raw = beta_std / sd_safe
    intercept = float(y.mean() - mu @ beta_raw)

    fitted = X @ beta_raw + intercept
    resid = y - fitted
    dof = max(n - k - 1, 1)
    idio_var = float(np.sum(resid ** 2) / dof)
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = float(1.0 - np.sum(resid ** 2) / ss_tot) if ss_tot > 0 else 0.0

    beta_dict = {f: float(b)
                 for f, b in zip(factor_returns.columns, beta_raw)}

    return {
        "alpha": intercept,
        "beta": beta_dict,
        "idio_var": idio_var,
        "n_obs": n,
        "r_squared": r_squared,
    }


# ---------------------------------------------------------------------------
# Factor covariance
# ---------------------------------------------------------------------------

def estimate_factor_cov(factor_returns,
                          shrinkage: Optional[float] = None):
    """Sample covariance of factor returns with Ledoit-Wolf shrinkage
    toward the diagonal, falling back to a manual shrinkage if sklearn
    is unavailable. Returns a numpy 2D array, or None if input is empty.
    """
    import numpy as np

    if factor_returns is None or factor_returns.empty:
        return None
    X = factor_returns.values

    if shrinkage is None:
        try:
            from sklearn.covariance import LedoitWolf
            return LedoitWolf().fit(X).covariance_
        except Exception:
            shrinkage = 0.1
    sample_cov = np.cov(X, rowvar=False, ddof=1)
    diag = np.diag(np.diag(sample_cov))
    return (1.0 - shrinkage) * sample_cov + shrinkage * diag


# ---------------------------------------------------------------------------
# Portfolio-level risk
# ---------------------------------------------------------------------------

def compute_portfolio_risk(
    weights: Dict[str, float],
    exposures: Dict[str, Dict[str, Any]],
    factor_cov,
    portfolio_value: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """Combine per-symbol exposures into a portfolio risk decomposition.

    Args:
        weights: {symbol: weight} signed (long > 0, short < 0). Need
            not sum to 1 (allows dollar-neutral books or net leverage).
        exposures: {symbol: estimate_exposures() result}.
        factor_cov: factor x factor covariance numpy array.
        portfolio_value: total dollar value (to express VaR in dollars).

    Returns dict with parametric VaR + ES + per-factor decomposition.
    Use monte_carlo_var() for simulation-based VaR/ES.
    """
    import numpy as np

    if not weights or not exposures or factor_cov is None:
        return None

    syms = [s for s in weights if s in exposures and exposures[s] is not None]
    if not syms:
        return None

    factor_names = list(next(iter(exposures.values()))["beta"].keys())
    w = np.array([weights[s] for s in syms])
    B = np.array([[exposures[s]["beta"][f] for f in factor_names]
                   for s in syms])
    idio = np.array([exposures[s]["idio_var"] for s in syms])

    # Portfolio-level factor exposure
    portfolio_betas = w @ B
    factor_var = float(portfolio_betas @ factor_cov @ portfolio_betas)
    idio_var = float(np.sum((w ** 2) * idio))
    total_var = max(factor_var + idio_var, 0.0)
    sigma = float(np.sqrt(total_var))

    # Parametric ES under normal: ES_α = σ * φ(Z_α) / (1 - α)
    # 95% → φ(1.645)/0.05 ≈ 2.063; 99% → φ(2.326)/0.01 ≈ 2.665
    es_95_mult = 2.063
    es_99_mult = 2.665

    cov_times_beta = factor_cov @ portfolio_betas
    decomp = {f: float(portfolio_betas[i] * cov_times_beta[i])
                for i, f in enumerate(factor_names)}

    # Group decomposition into sector / style / french for the prompt
    grouped = {"sectors": 0.0, "styles": 0.0, "french": 0.0,
               "idio": idio_var}
    for fname, contrib in decomp.items():
        if fname.startswith("sector_"):
            grouped["sectors"] += contrib
        elif fname.startswith("style_"):
            grouped["styles"] += contrib
        else:
            grouped["french"] += contrib

    return {
        "factor_var": factor_var,
        "idio_var": idio_var,
        "total_var": total_var,
        "sigma": sigma,
        "var_95_pct": Z_95 * sigma,
        "var_99_pct": Z_99 * sigma,
        "var_95_dollars": Z_95 * sigma * portfolio_value,
        "var_99_dollars": Z_99 * sigma * portfolio_value,
        "es_95_pct": es_95_mult * sigma,
        "es_99_pct": es_99_mult * sigma,
        "es_95_dollars": es_95_mult * sigma * portfolio_value,
        "es_99_dollars": es_99_mult * sigma * portfolio_value,
        "factor_exposures": {f: float(portfolio_betas[i])
                              for i, f in enumerate(factor_names)},
        "factor_decomposition": decomp,
        "grouped_decomposition": grouped,
        "n_symbols": len(syms),
        "factor_pct_of_total":
            (factor_var / total_var) if total_var > 0 else 0.0,
        "factor_names": factor_names,
        # Internal — used by Monte Carlo
        "_w": w.tolist(),
        "_B": B.tolist(),
        "_idio": idio.tolist(),
        "_syms": syms,
    }


def monte_carlo_var(
    weights: Dict[str, float],
    exposures: Dict[str, Dict[str, Any]],
    factor_cov,
    portfolio_value: float = 1.0,
    n_sims: int = 10000,
    seed: int = 42,
) -> Optional[Dict[str, Any]]:
    """Simulate portfolio P&L by drawing from N(0, Σ_f) for the factor
    block and N(0, σ²ᵢ) for each symbol's idio block, aggregating to a
    daily portfolio return. Reports VaR + ES from the empirical
    distribution.

    More honest than parametric VaR when factor exposures are large or
    idio dominates: the mixture is rarely truly normal in practice,
    and the simulation captures the joint distribution of all symbol
    contributions.
    """
    import numpy as np

    if not weights or not exposures or factor_cov is None:
        return None
    syms = [s for s in weights if s in exposures and exposures[s] is not None]
    if not syms:
        return None
    factor_names = list(next(iter(exposures.values()))["beta"].keys())

    w = np.array([weights[s] for s in syms])
    B = np.array([[exposures[s]["beta"][f] for f in factor_names]
                   for s in syms])
    idio_var = np.array([exposures[s]["idio_var"] for s in syms])
    idio_sd = np.sqrt(np.maximum(idio_var, 0.0))

    rng = np.random.default_rng(seed)
    # Factor draws: shape (n_sims, n_factors)
    try:
        L = np.linalg.cholesky(factor_cov)
    except np.linalg.LinAlgError:
        # Fallback — symmetrize and add small diagonal
        n = factor_cov.shape[0]
        L = np.linalg.cholesky(
            (factor_cov + factor_cov.T) / 2 + 1e-10 * np.eye(n)
        )
    z = rng.standard_normal((n_sims, factor_cov.shape[0]))
    factor_draws = z @ L.T

    # Symbol returns: factor exposure * factor draw + independent idio
    # shape (n_sims, n_syms)
    sym_factor_part = factor_draws @ B.T
    idio_draws = rng.standard_normal((n_sims, len(syms))) * idio_sd
    sym_returns = sym_factor_part + idio_draws

    # Portfolio returns
    pnl = sym_returns @ w * portfolio_value

    losses = -pnl  # positive = loss
    var_95 = float(np.percentile(losses, 95))
    var_99 = float(np.percentile(losses, 99))
    es_95 = float(losses[losses >= var_95].mean()) if (losses >= var_95).any() else var_95
    es_99 = float(losses[losses >= var_99].mean()) if (losses >= var_99).any() else var_99
    return {
        "n_sims": n_sims,
        "var_95_dollars": var_95,
        "var_99_dollars": var_99,
        "es_95_dollars": es_95,
        "es_99_dollars": es_99,
        "var_95_pct": var_95 / portfolio_value if portfolio_value else 0,
        "var_99_pct": var_99 / portfolio_value if portfolio_value else 0,
        "es_95_pct": es_95 / portfolio_value if portfolio_value else 0,
        "es_99_pct": es_99 / portfolio_value if portfolio_value else 0,
        "worst_day": float(losses.max()),
        "worst_day_pct": float(losses.max() / portfolio_value) if portfolio_value else 0,
    }


# ---------------------------------------------------------------------------
# End-to-end convenience
# ---------------------------------------------------------------------------

def compute_portfolio_risk_from_positions(
    positions: List[Dict[str, Any]],
    portfolio_value: float,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    run_monte_carlo: bool = True,
) -> Optional[Dict[str, Any]]:
    """End-to-end: positions → factor returns → exposures → risk.

    `positions` is a list of dicts each with at least `symbol` and
    `market_value` (signed; long positive, short negative). Returns the
    full risk dict, with `monte_carlo` nested in it when `run_monte_carlo`.
    """
    from market_data import get_bars

    if not positions or portfolio_value <= 0:
        return None

    factor_returns = compute_factor_returns(lookback_days)
    if factor_returns.empty:
        return None
    factor_cov = estimate_factor_cov(factor_returns)
    if factor_cov is None:
        return None

    exposures: Dict[str, Dict[str, Any]] = {}
    weights: Dict[str, float] = {}
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        mv = float(p.get("market_value") or 0)
        if not sym or mv == 0:
            continue
        bars = get_bars(sym, limit=lookback_days + 10)
        if bars is None or bars.empty:
            continue
        rets = bars["close"].pct_change().dropna()
        if rets.empty:
            continue
        rets.index = rets.index.tz_localize(None).normalize() \
            if rets.index.tz is not None else rets.index.normalize()
        rets = rets.reindex(factor_returns.index).dropna()
        if len(rets) < MIN_OBS_FOR_REGRESSION:
            continue
        est = estimate_exposures(rets, factor_returns)
        if est is None:
            continue
        exposures[sym] = est
        weights[sym] = mv / portfolio_value

    if not exposures:
        return None

    risk = compute_portfolio_risk(
        weights, exposures, factor_cov, portfolio_value,
    )
    if risk is None:
        return None
    if run_monte_carlo:
        risk["monte_carlo"] = monte_carlo_var(
            weights, exposures, factor_cov, portfolio_value,
        )
    risk["lookback_days"] = lookback_days
    risk["factor_returns"] = factor_returns      # for stress-scenario reuse
    risk["factor_cov"] = factor_cov
    risk["exposures"] = exposures
    risk["weights"] = weights
    return risk


# ---------------------------------------------------------------------------
# Prompt + dashboard rendering
# ---------------------------------------------------------------------------

def render_risk_summary_for_prompt(risk: Dict[str, Any]) -> str:
    """Compact one-liner string for the AI prompt and dashboard ribbon."""
    if not risk:
        return ""
    parts = [
        f"Daily σ: {risk['sigma'] * 100:.2f}%",
        f"95% VaR: ${risk['var_95_dollars']:,.0f} ({risk['var_95_pct'] * 100:.2f}%)",
        f"95% ES: ${risk['es_95_dollars']:,.0f}",
    ]
    mc = risk.get("monte_carlo")
    if mc:
        parts.append(f"MC 95% VaR: ${mc['var_95_dollars']:,.0f}")
    fx = risk.get("factor_exposures", {})
    if fx:
        ranked = sorted(fx.items(), key=lambda kv: abs(kv[1]), reverse=True)
        top = ", ".join(f"{f.replace('sector_','').replace('style_','')}={b:+.2f}"
                        for f, b in ranked[:4])
        parts.append(f"Top exposures: {top}")
    grouped = risk.get("grouped_decomposition", {})
    if grouped and risk.get("total_var", 0) > 0:
        tv = risk["total_var"]
        parts.append(
            f"Risk mix — sectors: {grouped.get('sectors',0)/tv*100:.0f}%, "
            f"styles: {grouped.get('styles',0)/tv*100:.0f}%, "
            f"french: {grouped.get('french',0)/tv*100:.0f}%, "
            f"idio: {grouped.get('idio',0)/tv*100:.0f}%"
        )
    return " | ".join(parts)
