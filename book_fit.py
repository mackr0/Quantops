"""Per-candidate "book fit" — how correlated / concentrated a candidate is
with the profile's CURRENT holdings.

Surfaced in the AI prompt (advisory) so the AI proposes trades that DIVERSIFY
the book instead of piling onto the same factor cluster the risk specialists
(adversarial_reviewer / risk_assessor) would otherwise veto AFTER selection.
The dominant production veto reason is "book already concentrated in
correlated high-beta names, adding X increases correlation risk" — a signal
the AI never received because it only saw coarse 7-bucket sector exposure, not
a per-candidate return-correlation to the specific names already held.

Design contract:
  * ADVISORY ONLY — this annotates the prompt; it never blocks a trade.
  * FAIL-OPEN — returns None on any data gap (thin bars, unknown symbol,
    numpy missing); a missing signal must never break candidate building.
  * ALPACA-FIRST — reuses correlation._fetch_returns (Alpaca daily bars,
    cached) and sector_classifier (cached); no new data source.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def held_underlyings(positions) -> List[str]:
    """Distinct underlying symbols from a get_virtual_positions() list.

    Both stock rows and option legs expose ``symbol`` as the underlying
    (e.g. the T put spread's legs both report symbol='T'), so this dedupes
    to the set of underlyings the book is exposed to.
    """
    out: List[str] = []
    seen = set()
    for p in positions or []:
        try:
            sym = p.get("symbol") if hasattr(p, "get") else getattr(p, "symbol", None)
        except Exception:
            sym = None
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def sector_concentration_penalty(candidate_sector, held_sector_counts,
                                 per_name: float = 0.08, cap: float = 0.4) -> float:
    """A [0..cap] score haircut for a LONG candidate whose sector is already
    well-represented in the profile's OWN book.

    Used by _rank_candidates to push sector-diversifiers up the menu the AI
    sees, so it proposes trades that won't be concentration-vetoed. Pure
    function (no I/O) — the caller passes the candidate's sector and a
    {sector: count} Counter of the profile's OWN held names (both from the
    cached sector_classifier). per_name=0.08 → each held name in the same
    sector shaves 8% off the candidate's effective rank score, capped at 40%.
    Returns 0.0 when there's nothing to penalize (fail-open).
    """
    if not candidate_sector or not held_sector_counts:
        return 0.0
    try:
        n = held_sector_counts.get(candidate_sector, 0)
    except Exception:
        return 0.0
    return min(cap, per_name * n)


def compute_book_fit(
    symbol: str,
    held: List[str],
    held_returns: Optional[Dict] = None,
    high_corr: float = 0.7,
    elevated_corr: float = 0.5,
) -> Optional[Dict]:
    """Describe how `symbol` fits against the `held` underlyings.

    Args:
        symbol: candidate underlying.
        held: list of underlying symbols already in the book.
        held_returns: optional precomputed {sym: returns} for the held names
            (so the caller can fetch held bars ONCE per cycle instead of
            once per candidate). The candidate's own returns are fetched here.

    Returns a dict {max_corr, corr_with, same_sector, sector, summary} or
    None when there's nothing to say / data is insufficient. `summary` is the
    compact one-liner for the AI prompt.
    """
    held = [h for h in (held or []) if h and h != symbol]
    if not held:
        return None

    max_corr: Optional[float] = None
    corr_with: Optional[str] = None
    try:
        import numpy as np
        from correlation import _fetch_returns

        rets: Dict = dict(held_returns) if held_returns else {}
        # Always fetch the candidate; fetch held names too only if not given.
        need = [symbol] + ([] if held_returns else held)
        fetched = _fetch_returns(need, days=20) or {}
        rets.update(fetched)

        cand = rets.get(symbol)
        if cand is not None:
            for h in held:
                hr = rets.get(h)
                if hr is None:
                    continue
                n = min(len(cand), len(hr))
                if n < 5:
                    continue
                c = np.corrcoef(cand[:n], hr[:n])[0, 1]
                if not np.isfinite(c):
                    continue
                if max_corr is None or abs(c) > abs(max_corr):
                    max_corr = round(float(c), 2)
                    corr_with = h
    except Exception as exc:  # fail-open: never break candidate building
        logger.debug("book_fit correlation failed for %s: %s", symbol, exc)

    # Sector concentration (secondary signal; tolerate the classifier's
    # 'tech' default for unknowns by only reporting when >= 2 held names
    # share the candidate's sector).
    same_sector = 0
    sector: Optional[str] = None
    try:
        from sector_classifier import get_sector
        sector = get_sector(symbol)
        if sector:
            same_sector = sum(1 for h in held if get_sector(h) == sector)
    except Exception as exc:
        logger.debug("book_fit sector failed for %s: %s", symbol, exc)

    if max_corr is None and same_sector < 2:
        return None

    bits: List[str] = []
    if max_corr is not None:
        if abs(max_corr) >= high_corr:
            tag = "HIGH"
        elif abs(max_corr) >= elevated_corr:
            tag = "elevated"
        else:
            tag = "low"
        bits.append(f"max corr {max_corr:+.2f} w/ held {corr_with} ({tag})")
    if same_sector >= 2 and sector:
        bits.append(f"{same_sector} held in {sector}")

    summary = "; ".join(bits) if bits else None
    return {
        "max_corr": max_corr,
        "corr_with": corr_with,
        "same_sector": same_sector,
        "sector": sector,
        "summary": summary,
    }
