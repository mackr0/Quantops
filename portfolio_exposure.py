"""Portfolio exposure analysis — long/short by sector.

P2.1 of LONG_SHORT_PLAN.md. Today the dashboard knows total long
and short notional but not how it's distributed across sectors.
A profile that's "10% net long" might still be 50% long Tech and
40% short Tech (net 10%, gross 90%, sector-concentrated single
factor bet — exactly what real long/short funds try to AVOID).

This module computes:

  - aggregate net / gross / position count (existing behavior, moved here)
  - per-sector breakdown: {sector: {long_pct, short_pct, net_pct, gross_pct, n}}
  - largest sector concentration flag (warns when any sector > 30% gross)
  - directional balance (% long vs % short by sector)

The output is JSON-serializable and rendered on the Performance
Dashboard's Current Exposure section + passed to the AI prompt
so the AI can avoid stacking concentration that's already there.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# Threshold for flagging a sector as "concentrated" — % of total
# gross exposure. Real long/short funds typically target <20% per
# sector; we use 30% as a warn threshold (not a hard cap).
SECTOR_CONCENTRATION_WARN_PCT = 30.0


def compute_exposure(
    positions: List[Dict[str, Any]],
    equity: float,
    sector_lookup=None,
) -> Dict[str, Any]:
    """Build the full exposure breakdown from a list of open positions.

    Args:
      positions: list of dicts each with keys 'symbol', 'qty', 'market_value'.
                 Long positions have qty > 0, shorts have qty < 0.
      equity: total account equity (denominator for percentages).
      sector_lookup: optional callable(symbol) -> sector_name; defaults
                     to sector_classifier.get_sector. Pass a stub for tests.

    Returns dict with:
      net_pct, gross_pct, num_positions
      by_sector: {sector_name: {long_pct, short_pct, net_pct,
                                gross_pct, n_long, n_short}}
      concentration_flags: list of sectors exceeding the warn threshold

    All numeric outputs rounded to 1 decimal. Returns empty/zero
    fields when equity <= 0 or positions is empty (caller can render
    "no exposure" state).
    """
    if equity is None or equity <= 0 or not positions:
        return {
            "net_pct": 0.0,
            "gross_pct": 0.0,
            "num_positions": 0,
            "by_sector": {},
            "concentration_flags": [],
        }

    if sector_lookup is None:
        from sector_classifier import get_sector
        sector_lookup = get_sector

    long_val = 0.0
    short_val = 0.0
    by_sector: Dict[str, Dict[str, Any]] = {}
    n_positions = 0

    for p in positions:
        sym = p.get("symbol")
        qty = float(p.get("qty", 0) or 0)
        mv = float(p.get("market_value", 0) or 0)
        if not sym or qty == 0:
            continue
        n_positions += 1

        # market_value sign: positives for longs, negatives for shorts
        # in some Alpaca shapes. Normalize: abs() and use qty for direction.
        abs_mv = abs(mv)
        is_long = qty > 0

        if is_long:
            long_val += abs_mv
        else:
            short_val += abs_mv

        try:
            sector = sector_lookup(sym) or "Unknown"
        except Exception:
            sector = "Unknown"

        bucket = by_sector.setdefault(sector, {
            "long_val": 0.0, "short_val": 0.0,
            "n_long": 0, "n_short": 0,
        })
        if is_long:
            bucket["long_val"] += abs_mv
            bucket["n_long"] += 1
        else:
            bucket["short_val"] += abs_mv
            bucket["n_short"] += 1

    # Convert sector buckets to percentages
    sector_breakdown: Dict[str, Dict[str, Any]] = {}
    concentration_flags: List[str] = []
    for sector, bucket in by_sector.items():
        long_pct = round(bucket["long_val"] / equity * 100, 1)
        short_pct = round(bucket["short_val"] / equity * 100, 1)
        net_pct = round((bucket["long_val"] - bucket["short_val"]) / equity * 100, 1)
        gross_pct = round((bucket["long_val"] + bucket["short_val"]) / equity * 100, 1)
        sector_breakdown[sector] = {
            "long_pct": long_pct,
            "short_pct": short_pct,
            "net_pct": net_pct,
            "gross_pct": gross_pct,
            "n_long": bucket["n_long"],
            "n_short": bucket["n_short"],
        }
        if gross_pct >= SECTOR_CONCENTRATION_WARN_PCT:
            concentration_flags.append(sector)

    # P2.5 of LONG_SHORT_PLAN.md — bundle factor exposure (size +
    # direction balance) so the dashboard / AI prompt can show all
    # three slices (sector / size / direction) from one source.
    factors = compute_factor_exposure(positions, equity)

    return {
        "net_pct": round((long_val - short_val) / equity * 100, 1),
        "gross_pct": round((long_val + short_val) / equity * 100, 1),
        "num_positions": n_positions,
        "by_sector": sector_breakdown,
        "concentration_flags": concentration_flags,
        "factors": factors,
    }


def find_pair_opportunities(
    candidates: List[Dict[str, Any]],
    sector_lookup=None,
    max_pairs: int = 3,
) -> List[Dict[str, Any]]:
    """Identify same-sector long+short pair-trade candidates.

    P2.3 of LONG_SHORT_PLAN.md. The highest-Sharpe quant funds run
    pair trades — long the strong stock, short the weak stock within
    the same sector. The pair isolates the relative-strength signal:
    if Tech sells off broadly, both legs lose value but the spread
    widens (the weak stock falls more than the strong one). Pure
    market beta is hedged out.

    Args:
      candidates: list of candidate dicts from _rank_candidates,
        each with 'symbol' and 'signal' (BUY / SHORT / etc).
      sector_lookup: callable(symbol)->sector; defaults to
        sector_classifier.get_sector. Pass a stub for tests.
      max_pairs: cap on returned pairs (top N by combined score).

    Returns list of pair dicts, sorted by combined score desc:
      [
        {
          "sector": "Technology",
          "long":  {symbol, score, signal, reason},
          "short": {symbol, score, signal, reason},
          "combined_score": float,
        },
        ...
      ]
    Empty list if no same-sector pairs exist or candidates is empty.
    """
    if not candidates:
        return []
    if sector_lookup is None:
        from sector_classifier import get_sector
        sector_lookup = get_sector

    longs_by_sector: Dict[str, List[Dict[str, Any]]] = {}
    shorts_by_sector: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        sym = c.get("symbol")
        sig = (c.get("signal") or "").upper()
        if not sym or not sig:
            continue
        try:
            sector = sector_lookup(sym) or "Unknown"
        except Exception:
            sector = "Unknown"
        if sig in ("BUY", "STRONG_BUY"):
            longs_by_sector.setdefault(sector, []).append(c)
        elif sig in ("SHORT", "STRONG_SHORT", "SELL", "STRONG_SELL"):
            shorts_by_sector.setdefault(sector, []).append(c)

    pairs = []
    for sector, longs in longs_by_sector.items():
        shorts = shorts_by_sector.get(sector, [])
        if not shorts:
            continue
        # Pair the highest-scoring long with the highest-scoring short
        # in this sector. abs(score) for tiebreak symmetry.
        longs_sorted = sorted(longs, key=lambda x: abs(x.get("score", 0)), reverse=True)
        shorts_sorted = sorted(shorts, key=lambda x: abs(x.get("score", 0)), reverse=True)
        long_pick = longs_sorted[0]
        short_pick = shorts_sorted[0]
        if long_pick["symbol"] == short_pick["symbol"]:
            continue  # never pair a symbol with itself
        combined = abs(long_pick.get("score", 0)) + abs(short_pick.get("score", 0))
        pairs.append({
            "sector": sector,
            "long": {
                "symbol": long_pick["symbol"],
                "signal": long_pick.get("signal", "BUY"),
                "score": long_pick.get("score", 0),
                "reason": (long_pick.get("reason") or "")[:120],
            },
            "short": {
                "symbol": short_pick["symbol"],
                "signal": short_pick.get("signal", "SHORT"),
                "score": short_pick.get("score", 0),
                "reason": (short_pick.get("reason") or "")[:120],
            },
            "combined_score": combined,
        })

    pairs.sort(key=lambda p: p["combined_score"], reverse=True)
    return pairs[:max_pairs]


def compute_factor_exposure(
    positions: List[Dict[str, Any]],
    equity: float,
) -> Dict[str, Any]:
    """Compute simple factor-style breakdowns from position data we
    already have. Doesn't need fundamentals — uses price as a size
    proxy and direction as a structural factor.

    P2.5 of LONG_SHORT_PLAN.md (minimum viable). Real quant funds
    track size / value / momentum / beta factors with proper market
    data. We start with the two we can compute cheaply:

      - size_band: cheap (price < $20) / mid ($20-$100) / expensive (>$100).
        Stylized proxy — not a true small/mid/large cap classification,
        but correlates strongly with size and is free to compute.
      - direction_balance: long_pct of gross book vs short_pct.
        Surfaces "single-direction concentrated" warnings when one
        side carries >80% of the book.

    Returns dict:
      {
        "size_bands": {
            "cheap":     {"long_pct", "short_pct", "n_long", "n_short"},
            "mid":       {...},
            "expensive": {...},
        },
        "direction": {
            "long_share":  long_gross / total_gross,    # 0.0 - 1.0
            "short_share": short_gross / total_gross,
            "single_direction_concentrated": bool,
        },
      }
    """
    out = {
        "size_bands": {
            "cheap": {"long_pct": 0.0, "short_pct": 0.0,
                       "n_long": 0, "n_short": 0},
            "mid": {"long_pct": 0.0, "short_pct": 0.0,
                     "n_long": 0, "n_short": 0},
            "expensive": {"long_pct": 0.0, "short_pct": 0.0,
                           "n_long": 0, "n_short": 0},
        },
        "direction": {
            "long_share": 0.0,
            "short_share": 0.0,
            "single_direction_concentrated": False,
        },
    }
    if equity is None or equity <= 0 or not positions:
        return out

    long_gross = 0.0
    short_gross = 0.0

    for p in positions:
        qty = float(p.get("qty", 0) or 0)
        mv = float(p.get("market_value", 0) or 0)
        if qty == 0 or mv == 0:
            continue
        # Price = |market_value| / |qty|
        try:
            price = abs(mv) / abs(qty)
        except ZeroDivisionError:
            continue
        if price < 20:
            band = "cheap"
        elif price <= 100:
            band = "mid"
        else:
            band = "expensive"
        bucket = out["size_bands"][band]
        if qty > 0:
            bucket["long_pct"] += abs(mv) / equity * 100
            bucket["n_long"] += 1
            long_gross += abs(mv)
        else:
            bucket["short_pct"] += abs(mv) / equity * 100
            bucket["n_short"] += 1
            short_gross += abs(mv)

    # Round size band pcts
    for band in out["size_bands"].values():
        band["long_pct"] = round(band["long_pct"], 1)
        band["short_pct"] = round(band["short_pct"], 1)

    total_gross = long_gross + short_gross
    if total_gross > 0:
        out["direction"]["long_share"] = round(long_gross / total_gross, 3)
        out["direction"]["short_share"] = round(short_gross / total_gross, 3)
        # Single-direction concentrated when one side carries >80% of
        # the book. For a long/short profile this is a flag; for a
        # long-only profile (target_short_pct=0) it's expected.
        if (out["direction"]["long_share"] > 0.80
                or out["direction"]["short_share"] > 0.80):
            out["direction"]["single_direction_concentrated"] = True

    return out


def balance_gate(
    target_short_pct: float,
    current_exposure: Optional[Dict[str, Any]],
    hard_threshold: float = 0.25,
) -> str:
    """Decide whether new entries on either side should be blocked
    based on current vs target long/short balance.

    P2.4 of LONG_SHORT_PLAN.md. Avoids the "auto-trim positions"
    trap that real funds explicitly avoid (transaction costs,
    cutting winners short). Instead: when the book is materially
    off-target on one side, BLOCK new entries on that side and
    let the book rebalance through natural turnover (winners
    closing on TP, time stops covering shorts, etc.).

    Returns one of:
      "pass"          — balance is fine, accept either direction
      "block_longs"   — already too long; only allow SHORT entries
      "block_shorts"  — already too short; only allow BUY entries

    hard_threshold = 0.25 means "block when current_short_share
    deviates from target by more than 25 percentage points."
    Soft drift (target ±25%) is fine and handled by the prompt
    directive (P2.2).
    """
    if target_short_pct is None or target_short_pct <= 0:
        return "pass"  # long-only profile — gate is not relevant
    if not current_exposure:
        return "pass"
    gross = float(current_exposure.get("gross_pct") or 0)
    if gross <= 0:
        return "pass"
    by_sector = current_exposure.get("by_sector") or {}
    short_pct_sum = sum((b.get("short_pct") or 0) for b in by_sector.values())
    cur_short_frac = short_pct_sum / gross if gross > 0 else 0.0
    delta = target_short_pct - cur_short_frac
    if delta > hard_threshold:
        # Way undershorted — block new longs to force balance recovery
        return "block_longs"
    if delta < -hard_threshold:
        # Way overshorted — block new shorts
        return "block_shorts"
    return "pass"


def render_pairs_for_prompt(pairs: List[Dict[str, Any]]) -> str:
    """Format pair-trade opportunities as a compact AI prompt block.
    Returns empty string when no pairs found.
    """
    if not pairs:
        return ""
    lines = ["PAIR OPPORTUNITIES (same-sector long+short — isolates relative strength):"]
    for i, p in enumerate(pairs, 1):
        lines.append(
            f"  {i}. {p['sector']}: LONG {p['long']['symbol']} "
            f"(score {p['long']['score']}) / SHORT {p['short']['symbol']} "
            f"(score {p['short']['score']})"
        )
        lines.append(f"     Long thesis: {p['long']['reason']}")
        lines.append(f"     Short thesis: {p['short']['reason']}")
    lines.append(
        "  → A high-conviction pair is often better than two independent "
        "trades. Lower beta, isolates stock-picking edge from market drift."
    )
    return "\n".join(lines)


def render_for_prompt(exposure: Dict[str, Any]) -> str:
    """Format the exposure breakdown as a compact string for the AI prompt.

    Returns at most ~6 lines of text. Used by ai_analyst._build_batch_prompt
    to give the AI portfolio-aware context: "you're already 35% long Tech,
    don't stack another Tech long unless conviction is unusually high."
    """
    if not exposure or exposure.get("num_positions", 0) == 0:
        return "  No open positions."

    lines = [
        f"  Net: {exposure['net_pct']:+.1f}% | "
        f"Gross: {exposure['gross_pct']:.1f}% | "
        f"{exposure['num_positions']} positions"
    ]

    by_sector = exposure.get("by_sector") or {}
    if not by_sector:
        return "\n".join(lines)

    # Sort sectors by gross exposure desc; show top 5 + a tail count
    items = sorted(by_sector.items(),
                    key=lambda kv: kv[1]["gross_pct"], reverse=True)
    top = items[:5]
    rest = items[5:]

    lines.append("  By sector (top 5 by gross):")
    for sector, b in top:
        flag = "  ⚠ CONCENTRATED" if sector in exposure.get("concentration_flags", []) else ""
        if b["n_long"] and b["n_short"]:
            lines.append(
                f"    {sector}: long {b['long_pct']:.1f}% "
                f"({b['n_long']}) / short {b['short_pct']:.1f}% "
                f"({b['n_short']}) = net {b['net_pct']:+.1f}%{flag}"
            )
        elif b["n_long"]:
            lines.append(
                f"    {sector}: long {b['long_pct']:.1f}% "
                f"({b['n_long']} pos){flag}"
            )
        else:
            lines.append(
                f"    {sector}: short {b['short_pct']:.1f}% "
                f"({b['n_short']} pos){flag}"
            )
    if rest:
        rest_gross = sum(b["gross_pct"] for _, b in rest)
        lines.append(f"    + {len(rest)} other sectors ({rest_gross:.1f}% gross)")

    if exposure.get("concentration_flags"):
        lines.append(
            f"  CONCENTRATION WARNING: {', '.join(exposure['concentration_flags'])} "
            f">= {int(SECTOR_CONCENTRATION_WARN_PCT)}% of book — avoid stacking."
        )

    # P2.5 — surface size-band + direction-balance summary.
    factors = exposure.get("factors") or {}
    bands = factors.get("size_bands") or {}
    parts = []
    for band_name in ("cheap", "mid", "expensive"):
        b = bands.get(band_name) or {}
        gross = (b.get("long_pct") or 0) + (b.get("short_pct") or 0)
        if gross > 0:
            parts.append(f"{band_name} {gross:.1f}%")
    if parts:
        lines.append(f"  By price-band size proxy: {' | '.join(parts)}")
    direction = factors.get("direction") or {}
    if direction.get("single_direction_concentrated"):
        side = "long" if direction.get("long_share", 0) > 0.5 else "short"
        lines.append(
            f"  DIRECTIONAL CONCENTRATION: book is >80% {side} — "
            f"diversifying across direction would hedge market beta."
        )
    return "\n".join(lines)
