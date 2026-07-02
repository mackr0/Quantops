"""Unified risk-adjusted opportunity ledger (selection-engine P2b, 2026-07-01).

Presents the STOCK expression and each OPTION-spread expression of every
candidate as INDEPENDENT opportunities, scored on ONE axis (RAR, from
`risk_adjusted`) and ranked together in a single ledger — so the AI picks the
genuinely-best risk/reward trade instead of defaulting to options because they
arrive pre-packaged with strikes and a defined max-loss.

This replaces the two asymmetric prompt blocks (STOCK ACTION RECOMMENDATIONS +
MULTI-LEG OPTIONS STRATEGIES), each an equally-long list that structurally
implied "here are N of each, pick from both". A healthy mix is the OUTPUT of
ranking, never a bias. See docs/SELECTION_ENGINE_DESIGN.md.

Both expressions of a candidate are sized to the SAME capital-at-risk envelope
`REF$ = size_pct·equity` (the stock rec's conviction-scaled size), so a stock
and a spread on the same name are "the same bet" expressed two ways.

Everything here is fail-safe: any per-candidate failure drops that expression
and keeps the rest; a total failure returns an empty block and the caller's
prompt continues (the AI still has the raw candidate indicators).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import risk_adjusted as _ra

logger = logging.getLogger(__name__)

# How many ledger rows to show the AI. Interleaved stock + option, RAR-ranked;
# well above the old per-block cap of 8 so nothing high-RAR is hidden, but
# bounded so the prompt doesn't bloat.
_LEDGER_ROWS = 14


def _sector_of(symbol: Any) -> str:
    """This symbol's internal sector key (cached ~7d), or "" on any failure —
    fail-open so a sector lookup never blocks the ledger. Must match the sector
    recorded at option-proposal time (same `get_sector`) so the veto discount
    keys line up."""
    try:
        from sector_classifier import get_sector
        return str(get_sector(str(symbol)) or "")
    except Exception as exc:
        logger.debug("sector lookup failed for %s: %s", symbol, exc)
        return ""


def _conviction_p_win(score: Any) -> float:
    """Cold-start P_win prior from ensemble conviction (design §Scoring):
    clip(0.50 + 0.06·|score|, 0.50, 0.68). |score|≥3 → 0.68 ceiling."""
    try:
        return _ra._clip01(min(0.68, max(0.50, 0.50 + 0.06 * abs(float(score)))))
    except Exception:
        return 0.5


def p_win_from_reputation(score: Any, signal: Any,
                          rep: Optional[Dict[str, Any]]) -> float:
    """Design §Scoring P_win: this profile's OWN realized same-signal win-rate
    (cold-start) partial-blended toward the conviction prior when a meaningful
    sample exists, else the conviction prior alone.

    `rep` is a `symbol_reputation` entry — {win_rate (0-100), total,
    by_signal:{SIG:{win_rate,total}}} — or None. Same-signal bucket preferred;
    falls back to the symbol aggregate when that bucket is thin. Own-book only
    (reputation is built from this profile's own resolved predictions)."""
    prior = _conviction_p_win(score)
    if not isinstance(rep, dict):
        return prior
    sig = (str(signal) or "").upper()
    # Reputation `by_signal` stores SELL and SHORT as DISTINCT buckets (they're
    # separate entries in the per-signal breakdown), so prefer the literal
    # bucket, then the directional fold, then the symbol aggregate. STRONG_*
    # actions are re-labeled to BUY/SHORT before storage, so they fold directly.
    if sig == "SELL":
        keys = ["SELL", "SHORT"]
    elif sig in ("SHORT", "STRONG_SELL", "STRONG_SHORT"):
        keys = ["SHORT"]
    elif sig in ("BUY", "STRONG_BUY"):
        keys = ["BUY"]
    else:
        keys = []
    try:
        by_sig = rep.get("by_signal") or {}
        # ordered: same-signal bucket(s) first, then the symbol aggregate
        for b in [by_sig.get(k) for k in keys] + [rep]:
            if not isinstance(b, dict):
                continue
            total = int(b.get("total") or 0)
            wr = b.get("win_rate")
            if wr is not None and total >= 10:
                w = min(1.0, total / 30.0)      # partial blend (decision #3)
                return _ra._clip01(w * (float(wr) / 100.0) + (1 - w) * prior)
    except Exception as exc:
        logger.debug("p_win_from_reputation failed: %s", exc)
    return prior


def _p_win(candidate: Dict[str, Any]) -> float:
    """P_win for a candidate. Prefers a realized win-probability stamped by
    `trade_pipeline._build_candidates_data` (reputation-blended toward the
    conviction prior); falls back to the conviction prior computed from the
    ensemble score. Always in [0,1]."""
    pw = candidate.get("p_win")
    if pw is not None:
        try:
            return _ra._clip01(float(pw))
        except Exception as _pw_exc:
            # malformed stamped p_win → fall back to the conviction prior;
            # surface for follow-up (never silent).
            logger.debug("candidate p_win unparseable (%r): %s", pw, _pw_exc)
    return _conviction_p_win(candidate.get("score", 0))


def _dte_days(expiry: Any) -> int:
    """Days-to-expiry from an ISO 'YYYY-MM-DD' string. Falls back to 30 on any
    parse failure (a neutral mid-DTE, never a crash)."""
    try:
        from datetime import date
        y, m, d = (int(x) for x in str(expiry).split("-"))
        return max(0, (date(y, m, d) - date.today()).days)
    except Exception:
        return 30


def _option_pwin(rec: Dict[str, Any], spot: Any, atr: Any) -> float:
    """Conservative probability-of-profit for a priced VERTICAL, used as the
    option's P_win so a spread is scored on whether it actually pays — NOT on
    the underlying's directional conviction (a bullish score says nothing about
    whether a bull-put's short strike stays OTM).

    POP inputs are derived OFFLINE from ATR (already on the candidate) so the
    hot path stays network-free: IV ≈ (atr/spot)·√252, 1σ move over the hold
    ≈ (atr/spot)·√dte. `risk_adjusted.option_pop` then takes the conservative
    MIN of the short-strike-delta rule and the breakeven-vs-implied-move rule.
    Non-vertical or unrecognizable → 0.5 (no-information neutral), matching the
    conservative width fallback P1 used when a spread couldn't be priced."""
    try:
        import math
        spot = float(spot or 0)
        atr = float(atr or 0)
        if spot <= 0:
            return 0.5
        strat = str(rec.get("strategy", ""))
        strikes = rec.get("strikes") or {}
        # Only the four verticals have a well-defined short strike + right.
        if strat not in ("bull_put_spread", "bear_call_spread",
                         "bull_call_spread", "bear_put_spread"):
            return 0.5
        short_k = strikes.get("short")
        if short_k is None:
            return 0.5
        dte = _dte_days(rec.get("expiry"))
        iv = (atr / spot) * math.sqrt(252) if atr > 0 else 0.0
        implied_move_pct = ((atr / spot) * math.sqrt(max(1, dte)) * 100
                            if atr > 0 else None)
        right = "P" if "put" in strat else "C"
        return _ra.option_pop(
            spot, float(short_k), dte, iv, right,
            is_credit=bool(rec.get("is_credit")),
            breakeven=rec.get("breakeven"),
            implied_move_pct=implied_move_pct)
    except Exception as exc:
        logger.debug("option P_win derivation failed: %s", exc)
        return 0.5


def _fmt_strikes(strategy: str, strikes: Optional[Dict[str, Any]]) -> str:
    """Compact human-readable strike string for the ledger row, tolerant of
    every strategy's strike-dict shape (short/long, put/call, the 4-leg
    iron_condor/butterfly, the strangle put/call). Empty when unknown."""
    if not isinstance(strikes, dict) or not strikes:
        return ""
    def _n(v):
        try:
            f = float(v)
            return f"{f:g}"
        except Exception:
            return str(v)
    # Preserve a sensible leg order per common key set.
    order = ["short", "long", "put", "call",
             "put_long", "put_short", "call_short", "call_long"]
    keys = [k for k in order if k in strikes] + \
           [k for k in strikes if k not in order]
    return " / ".join(f"{k} {_n(strikes[k])}" for k in keys)


def build_opportunities(
    candidates: List[Dict[str, Any]],
    ctx: Any,
    equity: float,
    iv_rank_lookup=None,
    regime: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Score every candidate's stock + option expressions into a flat list of
    opportunity dicts, ranked by RAR desc (tie-break EV$). `enable_options`
    False → stock-only (option stream skipped entirely, e.g. p201).

    Reuses the existing advisors so all their suppressors are preserved:
      • stock: `evaluate_candidate_for_stock_action` (conviction sizing,
        ATR-clamped stop/TP);
      • option: `evaluate_candidate_for_multileg` (own-book held-underlying
        skip, IV dead-zone, budget-exhausted gate) + P1 pricing.
    """
    from stock_strategy_advisor import evaluate_candidate_for_stock_action

    equity = max(0.0, float(equity or 0))
    options_enabled = bool(getattr(ctx, "enable_options", True))
    default_size_frac = float(getattr(ctx, "max_position_pct", 0.08) or 0.08)

    # Per-(strategy x sector) veto-rate discount from THIS profile's own option
    # history (P3): down-rank spreads the profile's specialists keep vetoing so
    # the AI stops wasting picks on doomed proposals. Loaded ONCE per build;
    # own-book, fail-open to {} (no data / any error → no discount).
    _veto_discounts = {}
    if options_enabled:
        try:
            from veto_feedback import load_veto_discounts
            _veto_discounts = load_veto_discounts(getattr(ctx, "db_path", None))
        except Exception as _vd_exc:
            logger.debug("veto discounts unavailable (no discount): %s", _vd_exc)

    # --- option recs (own-book/budget/IV/priced), one batch, gated ---------
    option_recs_by_sym: Dict[str, List[Dict[str, Any]]] = {}
    if options_enabled:
        try:
            from options_strategy_advisor import (
                evaluate_candidate_for_multileg,
                _options_budget_exhausted,
                _own_book_held_underlyings,
            )
            if not _options_budget_exhausted(ctx):
                held = _own_book_held_underlyings(ctx)
                for c in candidates:
                    sym = c.get("symbol")
                    if not sym:
                        continue
                    iv = None
                    if iv_rank_lookup is not None:
                        try:
                            iv = iv_rank_lookup(sym)
                        except Exception as _iv_exc:
                            logger.debug("ledger iv lookup(%s) failed: %s",
                                         sym, _iv_exc)
                            iv = None
                    try:
                        recs = evaluate_candidate_for_multileg(
                            c, iv_rank_pct=iv, regime=regime, ctx=ctx, held=held)
                        if recs:
                            option_recs_by_sym[sym] = recs
                    except Exception as _mr_exc:
                        logger.debug("ledger option recs(%s) failed: %s",
                                     sym, _mr_exc)
        except Exception as _opt_exc:
            # Whole option stream unavailable → stock-only ledger (never crash).
            logger.debug("ledger option stream unavailable (stock-only): %s",
                         _opt_exc)

    opps: List[Dict[str, Any]] = []
    for c in candidates:
        sym = c.get("symbol")
        if not sym:
            continue
        p_win = _p_win(c)
        # capital-at-risk envelope = the stock rec's conviction-scaled size.
        ref_dollars = default_size_frac * equity

        # stock expression
        try:
            for rec in (evaluate_candidate_for_stock_action(c, ctx=ctx) or []):
                o = _ra.score_stock_opportunity(rec, equity, p_win)
                if o:
                    o["symbol"] = sym
                    opps.append(o)
                    # size the option to the SAME envelope this stock used
                    sz = rec.get("size_pct")
                    if sz:
                        ref_dollars = (float(sz) / 100.0) * equity
        except Exception as _sk_exc:
            logger.debug("ledger stock expr(%s) failed: %s", sym, _sk_exc)

        # option expressions (already priced by P1). Scored with the option's
        # own POP (not the stock's directional p_win) — a spread must earn its
        # RAR on the probability its short strike / breakeven actually holds.
        if ref_dollars <= 0:
            ref_dollars = default_size_frac * equity
        for rec in option_recs_by_sym.get(sym, []):
            try:
                pop = _option_pwin(rec, c.get("price"), c.get("atr"))
                o = _ra.score_option_opportunity(rec, equity, pop,
                                                 ref_dollars=ref_dollars)
                if o:                          # None → unsizeable, refuse
                    o["symbol"] = sym
                    # P3 veto-rate discount: down-rank this spread by the
                    # profile's own historical P(veto) for (strategy, sector)
                    # so specialist-doomed proposals don't crowd the ledger.
                    if _veto_discounts:
                        from veto_feedback import discount_for, apply_veto_discount
                        d = discount_for(_veto_discounts, rec.get("strategy"),
                                         _sector_of(sym))
                        if d > 0:
                            o["veto_discount"] = d
                            o["rar"] = apply_veto_discount(o.get("rar", 0.0), d)
                    opps.append(o)
            except Exception as _ok_exc:
                logger.debug("ledger option expr(%s) failed: %s", sym, _ok_exc)

    opps.sort(key=lambda o: (o.get("rar", -1.0), o.get("ev_dollars", 0.0)),
              reverse=True)
    return opps


def render_opportunity_ledger(
    candidates: List[Dict[str, Any]],
    ctx: Any,
    equity: float,
    iv_rank_lookup=None,
    regime: Optional[str] = None,
) -> Tuple[str, bool]:
    """Build + render the single ranked ledger block. Returns (block,
    has_option_rows). has_option_rows drives whether the MULTILEG_OPEN action +
    example are offered to the AI."""
    if not candidates:
        return "", False
    opps = build_opportunities(candidates, ctx, equity,
                               iv_rank_lookup=iv_rank_lookup, regime=regime)
    return render_ledger_block(opps)


def render_ledger_block(opps: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """Render the ranked ledger block from PRE-BUILT opportunities, so a caller
    that also needs the opps (e.g. override tagging) builds them once. Returns
    (block, has_option_rows); empty block when there are no opps."""
    if not opps:
        return "", False

    has_option = any(o.get("expression") == "option" for o in opps)
    lines = [
        "",
        "RISK-ADJUSTED OPPORTUNITY LEDGER — the stock and option expressions "
        "of the flagged candidates, scored on ONE axis and ranked together:",
        "  RAR = expected profit per $ at risk (higher is better; a negative "
        "RAR means the edge doesn't cover the risk — skip it). This is the "
        "ONLY ranking that matters. A stock routinely outranks an option and "
        "vice-versa — there is NO preference for either expression. Propose "
        "the best risk-adjusted setups; a healthy stock/option mix is the "
        "natural OUTPUT of picking the best, never a target to hit.",
        "   #   RAR    P(win)  risk$      reward$    trade",
    ]
    for i, o in enumerate(opps[:_LEDGER_ROWS], start=1):
        rar = o.get("rar", 0.0)
        pw = o.get("p_win", 0.0)
        risk = o.get("risk_dollars", 0.0)
        reward = o.get("reward_dollars", 0.0)
        if o.get("expression") == "stock":
            desc = (f"{o.get('action')} {o.get('symbol')} — size "
                    f"{o.get('size_pct')}%, stop -{o.get('stop_loss_pct')}%, "
                    f"TP +{o.get('take_profit_pct')}%")
        else:
            strikes = _fmt_strikes(o.get("strategy", ""), o.get("strikes"))
            strikes_s = f" ({strikes})" if strikes else ""
            exp = o.get("expiry")
            exp_s = f" exp {exp}" if exp else ""
            priced_s = "" if o.get("priced") else " [est max-loss]"
            desc = (f"{o.get('symbol')} {o.get('strategy')}{exp_s}{strikes_s} "
                    f"{o.get('qty')}x{priced_s}")
        lines.append(
            f"  {i:>2}  {rar:+.2f}   {pw*100:>3.0f}%   "
            f"${risk:>8,.0f}  ${reward:>8,.0f}   {desc}"
        )
    hidden = len(opps) - _LEDGER_ROWS
    if hidden > 0:
        lines.append(f"  ... and {hidden} more lower-RAR expressions not shown")
    return "\n".join(lines), has_option


# --- AI-override logging (operator decision #4) -----------------------------
# The ledger's RAR ranking is the DEFAULT; the AI is the final chooser
# (default-with-reason). An "override" is when the AI picks a LOWER-RAR
# expression of a symbol than the ledger's best for that symbol (e.g. takes the
# option when the stock scored higher, or vice-versa). We tag every chosen
# trade with the ledger RARs so a later scorecard can measure whether the AI's
# overrides actually BEAT the number — per decision #4, "log overrides to
# measure if they beat the number". Pure/own-book; never affects selection.

def _expr_of_action(action: Any) -> Optional[str]:
    """The ledger EXPRESSION an executable action maps to. Only unambiguous
    ENTRY actions map; bare SELL/STRONG_SELL are DELIBERATELY unmapped — SELL is
    ambiguous (an EXIT of a held long vs a directional short), and the ledger
    only scores entries, so tagging a SELL would mis-attribute an exit's outcome
    to an entry-expression choice and corrupt the scorecard."""
    a = (str(action) or "").upper()
    if a in ("MULTILEG_OPEN", "OPTIONS"):
        return "option"
    if a in ("BUY", "STRONG_BUY", "SHORT", "STRONG_SHORT"):
        return "stock"
    return None


def _direction_of(action: Any, strategy: Any = None) -> Optional[str]:
    """Directional thesis of an action: long / short / neutral, or None when
    ambiguous (SELL/exit/unknown). Options derive direction from the spread
    strategy (bull_* → long, bear_* → short, condor/strangle → neutral)."""
    a = (str(action) or "").upper()
    if a in ("BUY", "STRONG_BUY"):
        return "long"
    if a in ("SHORT", "STRONG_SHORT"):
        return "short"
    if a in ("MULTILEG_OPEN", "OPTIONS"):
        s = (str(strategy) or "").lower()
        if s.startswith("bull"):
            return "long"
        if s.startswith("bear"):
            return "short"
        return "neutral"
    return None


def build_rar_index(opps: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{symbol: {"stock", "option" (max), "option_by_strat", "best",
    "best_expr", "direction"}}. A screener candidate carries ONE directional
    thesis, so all its opps share a direction (captured for the tag-time
    direction guard); `option_by_strat` keeps each spread's own RAR so an option
    pick is scored against the exact spread it chose, not the best spread."""
    idx: Dict[str, Dict[str, Any]] = {}
    for o in opps or []:
        sym = o.get("symbol")
        expr = o.get("expression")
        rar = o.get("rar")
        if not sym or expr not in ("stock", "option") or rar is None:
            continue
        e = idx.setdefault(sym, {"stock": None, "option": None,
                                 "option_by_strat": {}, "direction": None})
        cur = e.get(expr)
        e[expr] = rar if cur is None else max(cur, rar)
        if expr == "option" and o.get("strategy"):
            e["option_by_strat"][str(o["strategy"])] = rar
        d = _direction_of(o.get("action"), o.get("strategy"))
        if d and d != "neutral" and e["direction"] is None:
            e["direction"] = d
    for e in idx.values():
        best, best_expr = None, None
        for expr in ("stock", "option"):
            v = e.get(expr)
            if v is not None and (best is None or v > best):
                best, best_expr = v, expr
        e["best"], e["best_expr"] = best, best_expr
    return idx


def tag_overrides(trades: List[Dict[str, Any]],
                  opps: List[Dict[str, Any]]) -> int:
    """Stamp each chosen trade with ledger-RAR override metadata (decision #4):
    `_ledger_rar` (RAR of the exact expression/spread the AI chose),
    `_ledger_best_rar` (best RAR the ledger offered for that name+direction),
    `_ledger_best_expr`, and `_ledger_is_override` (chose a lower-RAR expression
    than the ledger's best). Returns the override count. Fail-safe: skips trades
    it can't cleanly map — off-ledger names, exits (bare SELL), and DIRECTION
    MISMATCHES (an AI BUY on a name the ledger only scored SHORT is a different
    trade, not an override). Never mutates a trade's executable fields."""
    idx = build_rar_index(opps)
    n_over = 0
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        e = idx.get(t.get("symbol"))
        strat = t.get("strategy_name") or t.get("strategy")
        expr = _expr_of_action(t.get("action"))
        td = _direction_of(t.get("action"), strat)
        if not e or expr is None or td is None:
            continue
        # direction guard: don't score a trade against an opp of the opposite
        # thesis (the ledger never offered THIS trade).
        if e.get("direction") and td != "neutral" and td != e["direction"]:
            continue
        # the exact spread's RAR when known, else the expression's best.
        if expr == "option" and strat and str(strat) in e["option_by_strat"]:
            chosen = e["option_by_strat"][str(strat)]
        else:
            chosen = e.get(expr)
        best = e.get("best")
        if chosen is None or best is None:
            continue
        is_override = (e.get("best_expr") != expr) and (best - chosen > 1e-9)
        t["_ledger_rar"] = round(float(chosen), 4)
        t["_ledger_best_rar"] = round(float(best), 4)
        t["_ledger_best_expr"] = e.get("best_expr")
        t["_ledger_is_override"] = bool(is_override)
        if is_override:
            n_over += 1
    return n_over


def override_scorecard(db_path) -> Dict[str, Any]:
    """Measure whether the AI's ledger-overrides BEAT the number (decision #4):
    over this profile's RESOLVED predictions that carried override metadata,
    compare the realized win-rate + avg return of OVERRIDE picks vs LEDGER-ALIGNED
    picks. Own-book (reads this profile's ai_predictions.features_json only —
    the override tag is metadata ON a real prediction, never shadow data).
    Returns counts + win-rates; {} on any error (fail-open)."""
    import json as _json
    out = {"override": {"n": 0, "wins": 0, "ret_sum": 0.0},
           "aligned": {"n": 0, "wins": 0, "ret_sum": 0.0}}
    try:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT features_json, actual_outcome, actual_return_pct "
                "FROM ai_predictions WHERE status = 'resolved' "
                "AND features_json IS NOT NULL"
            ).fetchall()
        for r in rows:
            try:
                feats = _json.loads(r["features_json"] or "{}")
            except Exception as _fj_exc:
                # a malformed features_json row can't be scored — skip it, but
                # surface (never silent) so systematic corruption is visible.
                logger.debug("override_scorecard: bad features_json: %s", _fj_exc)
                continue
            if "_ledger_is_override" not in feats:
                continue
            bucket = out["override"] if feats.get("_ledger_is_override") \
                else out["aligned"]
            bucket["n"] += 1
            if r["actual_outcome"] == "win":
                bucket["wins"] += 1
            if r["actual_return_pct"] is not None:
                bucket["ret_sum"] += float(r["actual_return_pct"])
        for k in ("override", "aligned"):
            b = out[k]
            b["win_rate"] = round(100.0 * b["wins"] / b["n"], 1) if b["n"] else None
            b["avg_return_pct"] = round(b["ret_sum"] / b["n"], 3) if b["n"] else None
        return out
    except Exception as exc:
        logger.debug("override_scorecard unavailable (fail-open): %s", exc)
        return {}
