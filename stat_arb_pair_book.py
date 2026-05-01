"""Statistical arbitrage pair book — Item 1b of COMPETITIVE_GAP_PLAN.md.

Real long/short funds (Citadel-class) trade hundreds-to-thousands of
cointegrated pairs simultaneously. We had a one-shot pair-trade
primitive (P2.3 of LONG_SHORT_PLAN) that surfaces 1-3 candidate pairs
to the AI per cycle. This module is the foundation for replacing that
with a proper pair book of 50-200 active cointegrated pairs.

What this module provides (foundation; wiring is multi-session):

  1. `engle_granger` — pairwise cointegration test on two price series.
     Returns p-value, hedge ratio, half-life of mean reversion.
  2. `compute_spread_zscore` — current standardized spread for a known
     pair given its hedge ratio.
  3. `find_cointegrated_pairs` — universe scan. Pairwise EG over the
     supplied symbols + price-history fetcher; ranks survivors.
  4. `Pair` dataclass — frozen description of a cointegrated pair.

Out of scope for this commit (separate sessions):
  - Persistent pair book table in journal
  - Daily rebalance task that re-tests cointegration of active pairs
  - Trade entry/exit signal generator
  - Wiring into trade_pipeline (proposing pair-trade actions to the AI)

Math reference: Engle-Granger two-step.
  Step 1: OLS regress price_a on price_b → hedge_ratio (β) + residuals
  Step 2: ADF test on residuals → p-value (cointegrated if p < 0.05).
  Half-life of mean reversion comes from AR(1) on residual differences:
    Δresid_t = γ·resid_{t-1} + ε  →  half-life = -ln(2) / ln(1+γ).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# Default thresholds — tunable; conservative starting points.
COINT_PVALUE_THRESHOLD = 0.05    # require p < this to call a pair cointegrated
MIN_CORRELATION = 0.6             # filter before EG to cut noise
MIN_HALF_LIFE_DAYS = 1.0          # < 1 day is noise, not mean reversion
MAX_HALF_LIFE_DAYS = 30.0         # > 30 days is too slow to trade


@dataclass(frozen=True)
class Pair:
    """One cointegrated pair. Hedge ratio is "shares of B per share of A"
    so that spread = price_a − hedge_ratio × price_b is stationary."""
    symbol_a: str
    symbol_b: str
    hedge_ratio: float
    p_value: float
    half_life_days: float
    correlation: float

    @property
    def label(self) -> str:
        return f"{self.symbol_a}/{self.symbol_b}"


def _ols_hedge_ratio(price_a: np.ndarray, price_b: np.ndarray) -> float:
    """Plain-numpy OLS slope (no intercept). Slope = β in price_a = β·price_b + ε.

    We use the no-intercept form because the constant gets absorbed into
    the spread mean during z-scoring; including it adds noise without
    changing the cointegration test outcome materially for short-window
    equity pair trading.
    """
    if len(price_a) != len(price_b) or len(price_a) < 2:
        raise ValueError("Series must be same length, ≥ 2 observations")
    pa = np.asarray(price_a, dtype=float)
    pb = np.asarray(price_b, dtype=float)
    denom = float(np.dot(pb, pb))
    if denom <= 0:
        raise ValueError("Degenerate B series (sum of squares is zero)")
    return float(np.dot(pa, pb) / denom)


def _half_life(spread: np.ndarray) -> float:
    """Half-life of mean reversion on the spread series.

    Estimate γ from AR(1) on differences:
      Δspread_t = γ·spread_{t-1} + ε
    Then half-life = -ln(2) / ln(1+γ).

    Returns inf when γ ≥ 0 (series isn't mean-reverting in this window).
    """
    s = np.asarray(spread, dtype=float)
    if len(s) < 3:
        return float("inf")
    lagged = s[:-1]
    diff = s[1:] - s[:-1]
    denom = float(np.dot(lagged, lagged))
    if denom <= 0:
        return float("inf")
    gamma = float(np.dot(lagged, diff) / denom)
    if gamma >= 0 or gamma <= -1:
        # Non-stationary or unstable AR(1)
        return float("inf")
    try:
        return float(-math.log(2) / math.log(1 + gamma))
    except (ValueError, ZeroDivisionError):
        return float("inf")


def engle_granger(price_a: Sequence[float],
                    price_b: Sequence[float]) -> Dict[str, float]:
    """Engle-Granger cointegration test on two price series.

    Returns:
      {
        "p_value": float,          # ADF p-value on residuals
        "hedge_ratio": float,      # OLS β (no-intercept)
        "half_life_days": float,   # mean-reversion speed
        "correlation": float,      # pearson on the raw series
        "n_obs": int,              # number of observations used
      }

    Cointegrated if p_value < COINT_PVALUE_THRESHOLD AND half_life is
    bounded (MIN_HALF_LIFE_DAYS ≤ hl ≤ MAX_HALF_LIFE_DAYS).

    Insufficient data or degenerate series → p_value=1.0 (rejected).
    """
    pa = np.asarray(price_a, dtype=float)
    pb = np.asarray(price_b, dtype=float)
    n = len(pa)

    base = {
        "p_value": 1.0, "hedge_ratio": 0.0,
        "half_life_days": float("inf"),
        "correlation": 0.0, "n_obs": n,
    }

    if n < 30 or len(pb) != n:
        # ADF needs ~30+ obs to be meaningful
        return base
    if not (np.all(np.isfinite(pa)) and np.all(np.isfinite(pb))):
        return base

    try:
        corr_matrix = np.corrcoef(pa, pb)
        correlation = float(corr_matrix[0, 1]) if corr_matrix.size >= 4 else 0.0
    except Exception:
        correlation = 0.0
    base["correlation"] = correlation

    try:
        beta = _ols_hedge_ratio(pa, pb)
    except ValueError:
        return base

    spread = pa - beta * pb
    if not np.all(np.isfinite(spread)) or np.std(spread) <= 1e-9:
        # Degenerate spread → not a real pair
        return base

    # ADF test from statsmodels — returns (test_statistic, p_value, ...)
    try:
        from statsmodels.tsa.stattools import adfuller
        adf_result = adfuller(spread, autolag="AIC")
        p_value = float(adf_result[1])
    except Exception as exc:
        logger.warning("ADF failed for pair (n=%d): %s", n, exc)
        return base

    return {
        "p_value": p_value,
        "hedge_ratio": float(beta),
        "half_life_days": _half_life(spread),
        "correlation": correlation,
        "n_obs": n,
    }


def is_pair_tradeable(eg_result: Dict[str, float],
                        pvalue_threshold: float = COINT_PVALUE_THRESHOLD,
                        min_correlation: float = MIN_CORRELATION) -> bool:
    """Apply the standard filters: cointegrated, correlated, mean-reverting
    on a tradeable timescale."""
    if eg_result["p_value"] >= pvalue_threshold:
        return False
    if abs(eg_result["correlation"]) < min_correlation:
        return False
    hl = eg_result["half_life_days"]
    if not (MIN_HALF_LIFE_DAYS <= hl <= MAX_HALF_LIFE_DAYS):
        return False
    return True


def compute_spread_zscore(price_a: Sequence[float],
                            price_b: Sequence[float],
                            hedge_ratio: float,
                            lookback: int = 60) -> Optional[float]:
    """Current z-score of the spread for a known pair.

    Uses the trailing `lookback` bars to compute the spread mean+std,
    then standardizes the most recent spread observation against them.

    Returns None when there isn't enough history.

    Trading interpretation:
      z > +2  → spread unusually wide. SHORT A / LONG B (hedge_ratio shares).
      z < -2  → spread unusually tight. LONG A / SHORT B.
      |z| < 0.5 → exit (mean-reverted).
      |z| > 3 → regime break — exit and re-test cointegration.
    """
    pa = np.asarray(price_a, dtype=float)
    pb = np.asarray(price_b, dtype=float)
    if len(pa) != len(pb) or len(pa) < lookback + 1:
        return None
    spread = pa[-lookback:] - hedge_ratio * pb[-lookback:]
    mean = float(np.mean(spread))
    std = float(np.std(spread))
    if std <= 1e-9:
        return None
    current = float(spread[-1])
    return (current - mean) / std


def find_cointegrated_pairs(
    symbols: List[str],
    price_history: Callable[[str], Optional[Sequence[float]]],
    pvalue_threshold: float = COINT_PVALUE_THRESHOLD,
    min_correlation: float = MIN_CORRELATION,
    max_pairs: int = 50,
) -> List[Pair]:
    """Pairwise scan for cointegrated pairs over a symbol universe.

    Args:
        symbols: list of tickers to scan.
        price_history: callable(symbol) → close-price series (or None
            if unavailable). Caller decides the window (recommend
            60-180 trading days).
        pvalue_threshold / min_correlation: filter knobs.
        max_pairs: cap returned pairs (sorted by p_value × |1 - corr|).

    Returns a list of Pair, sorted by quality (best first).

    Cost: pre-fetches all price series once (caller's price_history is
    expected to cache); then runs N·(N-1)/2 EG tests. For 100 symbols
    that's ~5000 tests. Each ADF on 90 obs is ~5ms → ~25s total. Run
    daily, not per-cycle.
    """
    series_by_sym: Dict[str, np.ndarray] = {}
    for sym in symbols:
        try:
            ph = price_history(sym)
        except Exception as exc:
            logger.debug("price_history(%s) raised: %s", sym, exc)
            continue
        if ph is None:
            continue
        arr = np.asarray(ph, dtype=float)
        if len(arr) >= 30 and np.all(np.isfinite(arr)):
            series_by_sym[sym] = arr

    valid_syms = sorted(series_by_sym.keys())
    pairs: List[Tuple[float, Pair]] = []

    for i, sym_a in enumerate(valid_syms):
        for sym_b in valid_syms[i + 1:]:
            pa = series_by_sym[sym_a]
            pb = series_by_sym[sym_b]
            n = min(len(pa), len(pb))
            if n < 30:
                continue
            try:
                result = engle_granger(pa[-n:], pb[-n:])
            except Exception as exc:
                logger.debug("EG(%s,%s) raised: %s", sym_a, sym_b, exc)
                continue

            if not is_pair_tradeable(result, pvalue_threshold,
                                       min_correlation):
                continue

            pair = Pair(
                symbol_a=sym_a, symbol_b=sym_b,
                hedge_ratio=result["hedge_ratio"],
                p_value=result["p_value"],
                half_life_days=result["half_life_days"],
                correlation=result["correlation"],
            )
            # Quality score: lower p × shorter half-life → higher quality.
            quality = result["p_value"] * result["half_life_days"]
            pairs.append((quality, pair))

    pairs.sort(key=lambda x: x[0])
    return [p for _, p in pairs[:max_pairs]]


# ---------------------------------------------------------------------------
# Persistence — store / retrieve the active pair book in the journal DB
# ---------------------------------------------------------------------------

def _canonical_order(sym_a: str, sym_b: str) -> Tuple[str, str]:
    """Return (a, b) sorted so each unordered pair maps to exactly one row.
    The DB has UNIQUE(symbol_a, symbol_b); we enforce a < b alphabetically
    so there's no ambiguity."""
    a, b = sym_a.upper(), sym_b.upper()
    return (a, b) if a < b else (b, a)


def upsert_pair(db_path: str, pair: Pair) -> int:
    """Insert or refresh a pair in the book. Returns the row id.

    Refreshes hedge_ratio / p_value / half_life / correlation /
    retested_at when the (a, b) row already exists. The pair's hedge
    ratio is converted to canonical-order space if a/b were swapped.
    """
    from journal import _get_conn
    a, b = _canonical_order(pair.symbol_a, pair.symbol_b)
    # Hedge ratio convention: A = β·B + spread. If we swapped, the
    # equivalent B = (1/β)·A + spread' — invert the ratio.
    hedge_ratio = pair.hedge_ratio
    if (pair.symbol_a.upper(), pair.symbol_b.upper()) != (a, b):
        hedge_ratio = 1.0 / hedge_ratio if abs(hedge_ratio) > 1e-9 else 0.0

    conn = _get_conn(db_path)
    cur = conn.execute(
        "SELECT id FROM stat_arb_pairs WHERE symbol_a=? AND symbol_b=?",
        (a, b),
    )
    row = cur.fetchone()
    if row:
        conn.execute(
            """UPDATE stat_arb_pairs
               SET hedge_ratio=?, p_value=?, half_life_days=?,
                   correlation=?, retested_at=datetime('now'),
                   status=CASE WHEN status='retired' THEN 'active' ELSE status END,
                   retired_at=NULL, retirement_reason=NULL
               WHERE id=?""",
            (hedge_ratio, pair.p_value, pair.half_life_days,
             pair.correlation, row["id"]),
        )
        pair_id = row["id"]
    else:
        cur = conn.execute(
            """INSERT INTO stat_arb_pairs
               (symbol_a, symbol_b, hedge_ratio, p_value,
                half_life_days, correlation, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (a, b, hedge_ratio, pair.p_value, pair.half_life_days,
             pair.correlation),
        )
        pair_id = cur.lastrowid
    conn.commit()
    conn.close()
    return pair_id


def get_active_pairs(db_path: str) -> List[Pair]:
    """Return all active pairs from the book."""
    from journal import _get_conn
    conn = _get_conn(db_path)
    rows = conn.execute(
        """SELECT symbol_a, symbol_b, hedge_ratio, p_value,
                  half_life_days, correlation
           FROM stat_arb_pairs WHERE status='active'
           ORDER BY p_value ASC""",
    ).fetchall()
    conn.close()
    return [
        Pair(symbol_a=r["symbol_a"], symbol_b=r["symbol_b"],
             hedge_ratio=float(r["hedge_ratio"]),
             p_value=float(r["p_value"]),
             half_life_days=float(r["half_life_days"]),
             correlation=float(r["correlation"]))
        for r in rows
    ]


def retire_pair(db_path: str, sym_a: str, sym_b: str,
                  reason: str) -> bool:
    """Mark a pair retired with a reason. Returns True if a row updated."""
    from journal import _get_conn
    a, b = _canonical_order(sym_a, sym_b)
    conn = _get_conn(db_path)
    cur = conn.execute(
        """UPDATE stat_arb_pairs
           SET status='retired', retired_at=datetime('now'),
               retirement_reason=?
           WHERE symbol_a=? AND symbol_b=? AND status='active'""",
        (reason, a, b),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


# ---------------------------------------------------------------------------
# Trade signal generator — given current prices, what action does the
# pair recommend?
# ---------------------------------------------------------------------------

# Z-score thresholds for entry / exit. Entry at ±2σ is the standard
# stat-arb convention; tighter (e.g. ±1.5σ) trades more often but with
# weaker mean-reversion signal. Exit at 0σ captures full mean
# reversion. ±3σ is the regime-break threshold — past this, we suspect
# the cointegration relationship has broken and we exit defensively.
ZSCORE_ENTRY = 2.0
ZSCORE_EXIT = 0.5
ZSCORE_REGIME_BREAK = 3.0


def pair_signal(pair: Pair,
                 price_a: Sequence[float],
                 price_b: Sequence[float],
                 lookback: int = 60,
                 currently_open: bool = False,
                 entry_direction: Optional[str] = None) -> Dict[str, Any]:
    """Generate a trade signal for one pair given current price history.

    Args:
        pair: the cointegrated pair (with hedge_ratio).
        price_a, price_b: trailing close-price series. Need ≥ lookback+1.
        lookback: bars used for spread mean+std.
        currently_open: True if we already hold this pair.
        entry_direction: when currently_open, what side we're holding.
            "long_a_short_b" (entered when z was very negative) or
            "short_a_long_b" (entered when z was very positive).

    Returns: {
      "action": "ENTER_LONG_A_SHORT_B" | "ENTER_SHORT_A_LONG_B" |
                "EXIT" | "REGIME_BREAK_EXIT" | "HOLD",
      "z_score": float,
      "reason": str,
    }

    Logic (when not currently open):
      z >= +entry         → ENTER_SHORT_A_LONG_B (spread will fall)
      z <= -entry         → ENTER_LONG_A_SHORT_B (spread will rise)
      otherwise           → HOLD

    Logic (when currently open):
      |z| >= regime_break → REGIME_BREAK_EXIT (cointegration may have broken)
      |z| <= exit         → EXIT (mean reverted; take profit)
      otherwise           → HOLD (still in the trade)
    """
    z = compute_spread_zscore(price_a, price_b,
                                pair.hedge_ratio, lookback=lookback)
    if z is None:
        return {
            "action": "HOLD",
            "z_score": None,
            "reason": "Insufficient history for spread z-score",
        }

    if currently_open:
        if abs(z) >= ZSCORE_REGIME_BREAK:
            return {
                "action": "REGIME_BREAK_EXIT",
                "z_score": z,
                "reason": (
                    f"Spread |z|={abs(z):.2f} > {ZSCORE_REGIME_BREAK} — "
                    "regime break, exit defensively"
                ),
            }
        if abs(z) <= ZSCORE_EXIT:
            return {
                "action": "EXIT",
                "z_score": z,
                "reason": f"Spread mean-reverted to z={z:.2f}",
            }
        return {
            "action": "HOLD",
            "z_score": z,
            "reason": f"Trade still in window (z={z:.2f})",
        }

    # Not currently open — look for an entry
    if z >= ZSCORE_ENTRY:
        return {
            "action": "ENTER_SHORT_A_LONG_B",
            "z_score": z,
            "reason": (
                f"Spread wide (z={z:.2f}); short {pair.symbol_a}, "
                f"long {pair.symbol_b} (ratio {pair.hedge_ratio:.3f})"
            ),
        }
    if z <= -ZSCORE_ENTRY:
        return {
            "action": "ENTER_LONG_A_SHORT_B",
            "z_score": z,
            "reason": (
                f"Spread tight (z={z:.2f}); long {pair.symbol_a}, "
                f"short {pair.symbol_b} (ratio {pair.hedge_ratio:.3f})"
            ),
        }
    return {
        "action": "HOLD",
        "z_score": z,
        "reason": f"No edge (z={z:.2f}, |z|<{ZSCORE_ENTRY})",
    }


# ---------------------------------------------------------------------------
# Daily rebalance — retest cointegration of active pairs, eject breakers
# ---------------------------------------------------------------------------

# When a pair's p_value drifts above this in the daily retest, we
# retire it. Note this is LOOSER than COINT_PVALUE_THRESHOLD (0.05) —
# we don't want to eject on borderline noise; we wait for clear
# evidence the relationship has broken.
RETIRE_PVALUE_THRESHOLD = 0.10


def retest_active_pairs(db_path: str,
                          price_history: Callable[[str], Optional[Sequence[float]]]
                          ) -> Dict[str, Any]:
    """Daily rebalance: retest each active pair's cointegration.

    For each active pair, fetch fresh price history and re-run the
    Engle-Granger test. Three outcomes:
      - p stays low and tradeability filter passes → upsert (refresh)
      - p > RETIRE_PVALUE_THRESHOLD → retire pair (cointegration broke)
      - tradeability filter fails for other reasons (e.g., half-life
        moved out of [1, 30] days) → retire pair

    Args:
        db_path: profile journal DB.
        price_history: callable(symbol) → close-price series.

    Returns: {"retested": int, "refreshed": int, "retired": int,
              "errors": int, "details": [...]}
    """
    summary = {"retested": 0, "refreshed": 0, "retired": 0,
               "errors": 0, "details": []}

    active = get_active_pairs(db_path)
    summary["retested"] = len(active)

    if not active:
        return summary

    for pair in active:
        try:
            ph_a = price_history(pair.symbol_a)
            ph_b = price_history(pair.symbol_b)
        except Exception as exc:
            logger.debug("price_history failed for %s: %s",
                         pair.label, exc)
            summary["errors"] += 1
            continue
        if ph_a is None or ph_b is None:
            # Can't evaluate; leave as-is, count as error
            summary["errors"] += 1
            continue

        try:
            arr_a = np.asarray(ph_a, dtype=float)
            arr_b = np.asarray(ph_b, dtype=float)
            n = min(len(arr_a), len(arr_b))
            if n < 30:
                summary["errors"] += 1
                continue
            result = engle_granger(arr_a[-n:], arr_b[-n:])
        except Exception as exc:
            logger.warning("EG retest failed for %s: %s", pair.label, exc)
            summary["errors"] += 1
            continue

        # Decide
        broke_pvalue = result["p_value"] >= RETIRE_PVALUE_THRESHOLD
        broke_filter = not is_pair_tradeable(result)
        if broke_pvalue or broke_filter:
            reason = (
                f"p={result['p_value']:.3f}, "
                f"hl={result['half_life_days']:.1f}d, "
                f"corr={result['correlation']:.2f}"
            )
            retire_pair(db_path, pair.symbol_a, pair.symbol_b, reason)
            summary["retired"] += 1
            summary["details"].append({
                "pair": pair.label, "outcome": "retired",
                "p_value": result["p_value"],
                "half_life_days": result["half_life_days"],
            })
        else:
            refreshed = Pair(
                symbol_a=pair.symbol_a, symbol_b=pair.symbol_b,
                hedge_ratio=result["hedge_ratio"],
                p_value=result["p_value"],
                half_life_days=result["half_life_days"],
                correlation=result["correlation"],
            )
            upsert_pair(db_path, refreshed)
            summary["refreshed"] += 1

    return summary


# ---------------------------------------------------------------------------
# Universe scan — discover new cointegrated pairs
# ---------------------------------------------------------------------------

def scan_and_persist_pairs(db_path: str,
                              symbols: List[str],
                              price_history: Callable[[str], Optional[Sequence[float]]],
                              max_pairs: int = 50) -> Dict[str, Any]:
    """Run a fresh universe scan and persist any new tradeable pairs.

    Cost is O(N²) — for 100 symbols ~5000 EG tests, ~25s wall time.
    Designed to run on a long cadence (weekly is reasonable). Daily
    retesting of already-discovered pairs is the cheaper, more frequent
    cousin (`retest_active_pairs`).

    Args:
        db_path: profile journal DB.
        symbols: candidate ticker universe to scan.
        price_history: callable(symbol) → close-price series.
        max_pairs: cap on top-N pairs to keep from this scan.

    Returns: {
      "scanned_symbols": int,    # candidates supplied
      "found": int,              # tradeable pairs discovered
      "persisted": int,          # rows upserted (subset of found)
      "details": [pair labels],
    }
    """
    summary = {"scanned_symbols": len(symbols), "found": 0,
               "persisted": 0, "details": []}

    pairs = find_cointegrated_pairs(
        symbols, price_history, max_pairs=max_pairs,
    )
    summary["found"] = len(pairs)

    for pair in pairs:
        try:
            upsert_pair(db_path, pair)
            summary["persisted"] += 1
            summary["details"].append(pair.label)
        except Exception as exc:
            logger.warning("upsert_pair failed for %s: %s",
                           pair.label, exc)
    return summary


# ---------------------------------------------------------------------------
# AI prompt rendering — surface the active pair book to the batch prompt
# ---------------------------------------------------------------------------

def render_pair_book_for_prompt(
    db_path: str,
    price_history: Callable[[str], Optional[Sequence[float]]],
    open_pair_legs: Optional[Dict[str, str]] = None,
    max_lines: int = 10,
) -> str:
    """Render the active stat-arb pair book as a prompt section.

    For each active pair, computes the current z-score and emits one
    line summarizing the actionable state. Surfaces only pairs where
    |z| >= ZSCORE_ENTRY (entry signal) OR currently_open (so the AI
    can see when to exit), plus a trailing few quietest pairs for
    context.

    Args:
        db_path: profile journal DB.
        price_history: callable(symbol) → close-price series.
        open_pair_legs: optional map of {symbol: "long"|"short"} so we
            can flag pairs we're already in. The caller (ai_analyst)
            builds this from the current positions list.
        max_lines: cap on actionable lines rendered.

    Returns prompt section (multi-line string), or "" when the book is
    empty / no actionable signals are visible.
    """
    open_pair_legs = open_pair_legs or {}
    try:
        active = get_active_pairs(db_path)
    except Exception:
        return ""
    if not active:
        return ""

    actionable_lines: List[str] = []
    informational_lines: List[str] = []

    for pair in active:
        try:
            ph_a = price_history(pair.symbol_a)
            ph_b = price_history(pair.symbol_b)
        except Exception:
            continue
        if ph_a is None or ph_b is None:
            continue

        # Detect "currently in this pair" — both legs held in opposite
        # directions count as an open pair trade.
        side_a = open_pair_legs.get(pair.symbol_a)
        side_b = open_pair_legs.get(pair.symbol_b)
        currently_open = (
            (side_a == "long" and side_b == "short")
            or (side_a == "short" and side_b == "long")
        )

        sig = pair_signal(pair, ph_a, ph_b,
                            currently_open=currently_open)
        if sig["z_score"] is None:
            continue

        line = (
            f"  - {pair.label}  z={sig['z_score']:+.2f}  "
            f"hedge={pair.hedge_ratio:.3f}  "
            f"hl={pair.half_life_days:.1f}d → "
            f"{sig['action']}"
        )
        if sig["action"] in ("ENTER_SHORT_A_LONG_B",
                                "ENTER_LONG_A_SHORT_B",
                                "EXIT", "REGIME_BREAK_EXIT"):
            actionable_lines.append(line)
        else:
            informational_lines.append(line)

    if not actionable_lines and not informational_lines:
        return ""

    out = ["STAT-ARB PAIR BOOK (cointegrated pairs — z-score trading):"]
    if actionable_lines:
        out.append("Actionable now:")
        out.extend(actionable_lines[:max_lines])
    if informational_lines and not actionable_lines:
        # Only show quiet pairs when there's nothing actionable —
        # otherwise the prompt gets noisy.
        out.append("Currently quiet (no entry signal):")
        out.extend(informational_lines[:3])
    out.append(
        "  → Entry at |z|>=2 (mean-reversion bet); exit at |z|<=0.5 "
        "or |z|>=3 (regime break)."
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# PAIR_TRADE execution — two-leg atomic order with dollar-neutral sizing
# ---------------------------------------------------------------------------

# Per-leg dollar size cap as fraction of equity. Both legs together
# consume 2× this (e.g., 0.05 = 5% per leg = 10% gross exposure on
# the pair). Conservative starting point.
PAIR_LEG_MAX_PCT_OF_EQUITY = 0.05


def _lookup_active_pair(db_path: str, symbol_a: str,
                            symbol_b: str) -> Optional[Pair]:
    """Find an active pair by either ordering of the symbols. Returns
    None if no active row matches."""
    a, b = _canonical_order(symbol_a, symbol_b)
    from journal import _get_conn
    conn = _get_conn(db_path)
    row = conn.execute(
        """SELECT symbol_a, symbol_b, hedge_ratio, p_value,
                  half_life_days, correlation
           FROM stat_arb_pairs
           WHERE symbol_a=? AND symbol_b=? AND status='active'""",
        (a, b),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return Pair(
        symbol_a=row["symbol_a"], symbol_b=row["symbol_b"],
        hedge_ratio=float(row["hedge_ratio"]),
        p_value=float(row["p_value"]),
        half_life_days=float(row["half_life_days"]),
        correlation=float(row["correlation"]),
    )


def execute_pair_trade(api, proposal: Dict[str, Any], ctx,
                          log: bool = True) -> Dict[str, Any]:
    """Execute an AI-proposed PAIR_TRADE end-to-end.

    `proposal` shape (validated upstream in ai_analyst._validate_ai_trades):
      {
        "action": "PAIR_TRADE",
        "pair_action": "ENTER_LONG_A_SHORT_B" | "ENTER_SHORT_A_LONG_B"
                         | "EXIT",
        "symbol_a": "AAPL",
        "symbol_b": "MSFT",
        "dollars_per_leg": 5000,      # required for ENTER
        "reasoning": "...",
        "confidence": 70,
      }

    Returns a result dict shaped like execute_trade's output:
      {action, symbol, qty, order_ids, reason}
    Action will be "PAIR_OPEN" / "PAIR_CLOSE" / "SKIP" / "ERROR".

    Sizing: dollar-neutral. Each leg gets `dollars_per_leg`; shares =
    floor(dollars_per_leg / current_price). Hedge ratio influences
    *cointegration* but not sizing — dollar-neutral keeps risk
    symmetric, which is the standard pro convention.

    Atomicity: best-effort. If leg 2 fails after leg 1 submitted, leg 1
    is left as-is with a warning logged. Caller must reconcile via
    the cross-account audit. We don't auto-cancel because Alpaca
    cancellation isn't synchronous and can race.
    """
    pair_action = (proposal.get("pair_action") or "").upper()
    sym_a_raw = (proposal.get("symbol_a") or "").upper()
    sym_b_raw = (proposal.get("symbol_b") or "").upper()
    dollars_per_leg = float(proposal.get("dollars_per_leg") or 0)
    reasoning = (proposal.get("reasoning") or "")[:500]

    result: Dict[str, Any] = {
        "action": "SKIP", "symbol": f"{sym_a_raw}/{sym_b_raw}",
        "reason": "",
    }

    # Validate
    if pair_action not in ("ENTER_LONG_A_SHORT_B",
                              "ENTER_SHORT_A_LONG_B", "EXIT"):
        result["reason"] = f"Unsupported pair_action: {pair_action!r}"
        return result
    if not sym_a_raw or not sym_b_raw:
        result["reason"] = "Missing symbol_a / symbol_b"
        return result

    # Look up the active pair — confirms it's in our book (the AI can't
    # spontaneously invent pairs we haven't validated)
    db_path = getattr(ctx, "db_path", None) if ctx else None
    if not db_path:
        result["reason"] = "No db_path on ctx — cannot look up pair book"
        return result
    pair = _lookup_active_pair(db_path, sym_a_raw, sym_b_raw)
    if pair is None:
        result["reason"] = (
            f"{sym_a_raw}/{sym_b_raw} not in active pair book — "
            "AI proposed an unverified pair"
        )
        return result

    # Use the pair's stored canonical symbols (the book may have them
    # in (b, a) order if the AI named them swapped)
    sym_a = pair.symbol_a
    sym_b = pair.symbol_b

    # ENTER paths need positive sizing
    if pair_action.startswith("ENTER"):
        if dollars_per_leg <= 0:
            result["reason"] = "dollars_per_leg must be > 0 for ENTER"
            return result

        # Cap leg size at PAIR_LEG_MAX_PCT_OF_EQUITY of equity
        try:
            from client import get_account_info
            account = get_account_info(ctx=ctx) or {}
            equity = float(account.get("equity") or 0)
        except Exception as exc:
            result["reason"] = f"Could not read equity: {exc}"
            return result

        max_dollars = equity * PAIR_LEG_MAX_PCT_OF_EQUITY
        if dollars_per_leg > max_dollars:
            logger.info(
                "Capping pair leg size from $%.0f to $%.0f (%.0f%% equity)",
                dollars_per_leg, max_dollars,
                PAIR_LEG_MAX_PCT_OF_EQUITY * 100,
            )
            dollars_per_leg = max_dollars

        # Decide leg sides
        if pair_action == "ENTER_LONG_A_SHORT_B":
            side_a, side_b = "buy", "sell"
        else:
            side_a, side_b = "sell", "buy"

        # Quote both legs to compute shares
        try:
            from market_data import get_bars
            bars_a = get_bars(sym_a, limit=5)
            bars_b = get_bars(sym_b, limit=5)
        except Exception as exc:
            result["reason"] = f"Could not fetch bars: {exc}"
            return result
        try:
            price_a = float(bars_a["close"].iloc[-1])
            price_b = float(bars_b["close"].iloc[-1])
        except Exception as exc:
            result["reason"] = f"Could not read prices: {exc}"
            return result
        if price_a <= 0 or price_b <= 0:
            result["reason"] = "Non-positive price on one leg"
            return result

        qty_a = int(dollars_per_leg / price_a)
        qty_b = int(dollars_per_leg / price_b)
        if qty_a <= 0 or qty_b <= 0:
            result["reason"] = (
                f"Sizing produced zero shares (dollars_per_leg=${dollars_per_leg:.0f}, "
                f"prices a={price_a:.2f}, b={price_b:.2f})"
            )
            return result

        # Submit leg A
        try:
            order_a = api.submit_order(
                symbol=sym_a, qty=qty_a, side=side_a,
                type="market", time_in_force="day",
            )
            order_id_a = getattr(order_a, "id", None)
        except Exception as exc:
            result["action"] = "ERROR"
            result["reason"] = f"Leg A submit failed: {exc}"
            return result

        # Submit leg B — if this fails, log loud so operator knows
        # they have a half-pair open
        try:
            order_b = api.submit_order(
                symbol=sym_b, qty=qty_b, side=side_b,
                type="market", time_in_force="day",
            )
            order_id_b = getattr(order_b, "id", None)
        except Exception as exc:
            logger.error(
                "Pair leg A submitted (%s) but leg B failed: %s. "
                "Half-pair open — operator must reconcile.",
                order_id_a, exc,
            )
            result["action"] = "ERROR"
            result["reason"] = (
                f"Leg A submitted ({order_id_a}) but leg B failed: {exc}"
            )
            result["order_id_a"] = order_id_a
            return result

        # Log both legs
        if log:
            try:
                from journal import log_trade
                pair_tag = pair.label
                log_trade(
                    symbol=sym_a, side=side_a, qty=qty_a,
                    price=price_a, order_id=order_id_a,
                    signal_type="PAIR_TRADE", strategy="stat_arb_pair",
                    reason=f"Pair {pair_tag} leg A: {reasoning}",
                    ai_reasoning=reasoning,
                    ai_confidence=int(proposal.get("confidence", 0) or 0),
                    decision_price=price_a,
                    db_path=db_path,
                )
                log_trade(
                    symbol=sym_b, side=side_b, qty=qty_b,
                    price=price_b, order_id=order_id_b,
                    signal_type="PAIR_TRADE", strategy="stat_arb_pair",
                    reason=f"Pair {pair_tag} leg B: {reasoning}",
                    ai_reasoning=reasoning,
                    ai_confidence=int(proposal.get("confidence", 0) or 0),
                    decision_price=price_b,
                    db_path=db_path,
                )
            except Exception as exc:
                logger.warning(
                    "Pair %s submitted (%s, %s) but log_trade failed: %s",
                    pair.label, order_id_a, order_id_b, exc,
                )

        result.update({
            "action": "PAIR_OPEN",
            "qty_a": qty_a, "qty_b": qty_b,
            "order_id_a": order_id_a, "order_id_b": order_id_b,
            "pair": pair.label,
            "reason": (f"Opened {pair.label}: {pair_action} "
                       f"({qty_a}@${price_a:.2f}, {qty_b}@${price_b:.2f})"),
        })
        return result

    # EXIT path — close whatever we hold on both legs
    try:
        from client import get_positions
        positions = get_positions(ctx=ctx) or []
    except Exception as exc:
        result["reason"] = f"Could not read positions: {exc}"
        return result

    pos_by_sym = {p.get("symbol", "").upper(): p for p in positions}
    pos_a = pos_by_sym.get(sym_a)
    pos_b = pos_by_sym.get(sym_b)

    if not pos_a and not pos_b:
        result["action"] = "SKIP"
        result["reason"] = f"No legs of {pair.label} currently held"
        return result

    closed = []
    for sym, pos in (("A", pos_a), ("B", pos_b)):
        actual_sym = sym_a if sym == "A" else sym_b
        if not pos:
            continue
        qty = abs(int(float(pos.get("qty") or 0)))
        if qty == 0:
            continue
        # Closing side is opposite of position side
        is_short = float(pos.get("qty") or 0) < 0
        close_side = "buy" if is_short else "sell"
        try:
            order = api.submit_order(
                symbol=actual_sym, qty=qty, side=close_side,
                type="market", time_in_force="day",
            )
            closed.append({
                "leg": sym, "symbol": actual_sym,
                "qty": qty, "order_id": getattr(order, "id", None),
            })
        except Exception as exc:
            logger.warning("EXIT leg %s (%s) failed: %s",
                            sym, actual_sym, exc)
            closed.append({
                "leg": sym, "symbol": actual_sym,
                "error": str(exc),
            })

    result.update({
        "action": "PAIR_CLOSE",
        "pair": pair.label,
        "closed": closed,
        "reason": f"Closed {pair.label}: {len(closed)} legs",
    })
    return result
